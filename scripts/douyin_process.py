#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音视频一键处理脚本（数据契约模式）
输入: 抖音链接（video/ 或 v.douyin 短链）
流程: web API 元数据 → 音频下载 → FunASR ASR → SRT → 弹幕CDP → 高能分析 → 评论CDP → 数据契约
输出: <workdir>/<aweme_id>/ + <aweme_id>_result.json（供 insert_notion.py 入库）

用法:
  python douyin_process.py "https://www.douyin.com/video/xxx" [workdir]
  # 弹幕/评论需浏览器 CDP：设置环境变量 DY_WS_URL 指向抖音 tab
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import datetime

import config
WORKDIR = config.WORKDIR
BASE = os.path.dirname(config.WORKDIR)  # 产物目录上级（含 douyin_cookies.json）
PY = config.PYTHON_BIN
GEN_SRT = config.GEN_SRT_PATH
FUSION = config.FUSION_PATH
FFMPEG = config.FFMPEG_BIN
CDP_SCRIPT = config.CDP_EVAL_JS

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'


def load_cookie_header():
    """从 douyin_cookies.json 读取完整 Cookie 头（路径可用 DOUYIN_COOKIES 覆盖）"""
    path = config.DOUYIN_COOKIES
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('cookieHeader', '')
    except Exception:
        return ''


def http_get(url, referer='https://www.douyin.com/', binary=False, timeout=60):
    """抖音 API GET，用 curl（urllib 偶发卡死/TLS 指纹被拒）"""
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), 'dy_http_tmp')
    cmd = ['curl.exe', '-s', '-L', '--max-time', str(timeout), '-o', tmp,
           url, '-H', f'User-Agent: {UA}', '-H', f'Referer: {referer}']
    cookie = load_cookie_header()
    if cookie:
        cmd += ['-H', f'Cookie: {cookie}']
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    if not os.path.exists(tmp):
        raise RuntimeError(f'curl 下载失败: {url[:60]}')
    data = open(tmp, 'rb').read()
    os.remove(tmp)
    if binary:
        return data
    return data.decode('utf-8', errors='replace')


def cdp_eval(ws_url, expression):
    """通过 CDP 在页面执行 JS（表达式写文件避免转义问题）"""
    expr_file = os.path.join(WORKDIR, '_cdp_expr.js')
    os.makedirs(WORKDIR, exist_ok=True)
    with open(expr_file, 'w', encoding='utf-8') as f:
        f.write(expression)
    r = subprocess.run(['node', CDP_SCRIPT, ws_url, expr_file],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
    out = r.stdout.strip()
    if not out:
        return None
    out = out.split('\n')[-1]
    try:
        return json.loads(out).get('value')
    except Exception:
        print('[CDP输出]', out[:300])
        return None


def extract_aweme_id(url):
    m = re.search(r'(?:/video/|modal_id=|/note/)(\d{15,20})', url or '')
    return m.group(1) if m else None


def run(cmd, **kw):
    print('>>', ' '.join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    if r.stdout:
        print(r.stdout[-1500:])
    return r


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else WORKDIR
    ws_url = os.environ.get('DY_WS_URL', '')

    aweme_id = extract_aweme_id(url)
    if not aweme_id:
        print('[FAIL] 未找到 aweme_id')
        sys.exit(1)

    out_dir = os.path.join(workdir, aweme_id)
    os.makedirs(out_dir, exist_ok=True)
    print(f'=== 抖音视频处理: {aweme_id} ===')

    # ---- 1. 元数据（web API）----
    print('\n[1/6] 获取元数据...')
    detail_api = f'https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=190500&version_name=19.5.0&aweme_id={aweme_id}'
    j = json.loads(http_get(detail_api))
    a = j.get('aweme_detail') or {}
    if not a:
        print('[FAIL] 元数据获取失败')
        sys.exit(1)
    desc = a.get('desc') or ''
    author = ((a.get('author') or {}).get('nickname')) or '未知'
    create_time = a.get('create_time')
    duration = a.get('duration') or 0
    stats = a.get('statistics') or {}
    music_urls = ((a.get('music') or {}).get('play_url') or {}).get('url_list') or []
    print(f'  标题: {desc[:50]}')
    print(f'  作者: {author} | 时长: {duration/1000:.0f}s')

    # ---- 2. 下载音频（音乐直链）----
    print('\n[2/6] 下载音频...')
    audio_mp3 = os.path.join(out_dir, 'audio.mp3')
    if music_urls:
        r = subprocess.run(['curl.exe', '-s', '-L', '-o', audio_mp3, music_urls[0],
                            '-H', f'User-Agent: {UA}', '-H', 'Referer: https://www.douyin.com/',
                            '--max-time', '180'], capture_output=True, timeout=200)
    if not os.path.exists(audio_mp3) or os.path.getsize(audio_mp3) < 10000:
        # 兜底：下载视频提取音频
        video_urls = (((a.get('video') or {}).get('play_addr') or {}).get('url_list')) or []
        if video_urls:
            video_file = os.path.join(out_dir, 'video.mp4')
            subprocess.run(['curl.exe', '-s', '-L', '-o', video_file, video_urls[0],
                            '-H', f'User-Agent: {UA}', '-H', 'Referer: https://www.douyin.com/',
                            '--max-time', '180'], capture_output=True, timeout=200)
            if os.path.exists(video_file):
                run([FFMPEG, '-y', '-i', video_file, '-vn', '-ac', '1', '-ar', '16000', '-b:a', '64k', audio_mp3])
    if not os.path.exists(audio_mp3):
        print('[FAIL] 音频下载失败')
        sys.exit(1)
    print(f'  音频: {os.path.getsize(audio_mp3)} bytes')

    # ---- 3. FunASR ASR → SRT ----
    print('\n[3/6] FunASR ASR...')
    run([PY, GEN_SRT, audio_mp3])
    srt_file = os.path.join(out_dir, 'audio.srt')
    raw_file = os.path.join(out_dir, 'audio_raw.txt')
    if os.path.exists(audio_mp3.replace('.mp3', '.srt')):
        os.rename(audio_mp3.replace('.mp3', '.srt'), srt_file)
    if os.path.exists(audio_mp3.replace('.mp3', '_raw.txt')):
        os.rename(audio_mp3.replace('.mp3', '_raw.txt'), raw_file)
    if not os.path.exists(srt_file):
        print('[FAIL] SRT 未生成')
        sys.exit(1)
    raw_text = open(raw_file, encoding='utf-8').read() if os.path.exists(raw_file) else ''

    # ---- 4. 弹幕（CDP，可选）----
    print('\n[4/6] 弹幕（CDP）...')
    danmaku_list = []
    hl_text = ''
    if ws_url:
        expr = """(async () => {
          const all = [];
          const SEG = 32000;
          let start = 0;
          const total = %d;
          let guard = 0;
          while (start < total && guard < 50) {
            const end = Math.min(start + SEG, total);
            const url = '/aweme/v1/web/danmaku/get_v2/?device_platform=webapp&aid=6383&channel=channel_pc_web&app_name=aweme&format=json&group_id=%s&item_id=%s&start_time=' + start + '&end_time=' + end + '&duration=' + total + '&pc_client_type=1&pc_libra_divert=Windows&support_h265=1&support_dash=1&cpu_core_num=20&version_code=170400&version_name=17.4.0&cookie_enabled=true&screen_width=1920&screen_height=1080&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=150.0.0.0&browser_online=true&engine_name=Blink&engine_version=150.0.0.0&os_name=Windows&os_version=10&device_memory=32&platform=PC&downlink=10&effective_type=4g&round_trip_time=0';
            try {
              const r = await fetch(url, { credentials: 'include', headers: { 'accept': 'application/json' } });
              const jd = await r.json();
              const list = jd.danmaku_list || [];
              for (const d of list) {
                all.push({ offset_time: d.offset_time, text: d.text, digg_count: d.digg_count || 0, score: d.score || 0 });
              }
            } catch (e) {}
            start = end;
            guard++;
            await new Promise(r2 => setTimeout(r2, 400));
          }
          all.sort((a, b) => a.offset_time - b.offset_time);
          return JSON.stringify({ total: all.length, danmaku: all });
        })()""" % (duration, aweme_id, aweme_id)
        val = cdp_eval(ws_url, expr)
        if val:
            try:
                danmaku_list = json.loads(val).get('danmaku', [])
            except Exception:
                danmaku_list = []
        print(f'  弹幕数: {len(danmaku_list)}')

        # 高能分析
        if danmaku_list:
            # douyin_fusion.js 期望 danmaku_<id>.json + test_<id>.srt 与输出同目录（BASE=__dirname）
            dm_file = os.path.join(out_dir, f'danmaku_{aweme_id}.json')
            with open(dm_file, 'w', encoding='utf-8') as f:
                json.dump({'total': len(danmaku_list), 'danmaku': danmaku_list}, f, ensure_ascii=False)
            # 复制 SRT 为 fusion 期望的文件名
            srt_src = os.path.join(out_dir, 'audio.srt')
            srt_link = os.path.join(out_dir, f'test_{aweme_id}.srt')
            if os.path.exists(srt_src) and not os.path.exists(srt_link):
                import shutil
                shutil.copy(srt_src, srt_link)
            run(['node', FUSION, aweme_id], cwd=out_dir)
            hl_file = os.path.join(out_dir, f'{aweme_id}_AI_HIGHLIGHT.txt')
            if os.path.exists(hl_file):
                hl_text = open(hl_file, encoding='utf-8').read()
            print(f'  高能摘要: {len(hl_text)} 字')

    # ---- 5. 评论（CDP，可选）----
    print('\n[5/6] 评论（CDP）...')
    comments = []
    if ws_url:
        expr = """(async () => {
          const results = [];
          let cursor = 0;
          let hasMore = true;
          let guard = 0;
          while (hasMore && guard < 3) {
            const url = '/aweme/v1/web/comment/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=190500&version_name=19.5.0&aweme_id=%s&cursor=' + cursor + '&count=20&item_type=0&sort=0';
            try {
              const r = await fetch(url, { credentials: 'include', headers: { 'accept': 'application/json' } });
              const jc = await r.json();
              const list = jc.comments || [];
              for (const c of list) {
                results.push({ text: c.text, digg_count: c.digg_count || 0, reply_count: c.reply_comment_total || 0, nickname: c.user ? c.user.nickname : '', timestamp: c.create_time });
              }
              hasMore = !!jc.has_more && list.length > 0;
              cursor = jc.cursor || cursor;
            } catch (e) { hasMore = false; }
            guard++;
            if (!hasMore) break;
            await new Promise(r2 => setTimeout(r2, 300));
          }
          return JSON.stringify({ total: results.length, comments: results });
        })()""" % aweme_id
        val = cdp_eval(ws_url, expr)
        if val:
            try:
                comments = json.loads(val).get('comments', [])
            except Exception:
                comments = []
        print(f'  评论数: {len(comments)}')

    # ---- 6. 数据契约 ----
    print('\n[6/6] 生成数据契约...')
    dm_xml = ''
    if danmaku_list:
        # 转成类似 XML 的简单格式供 insert_notion 展示（弹幕明细段直接来自 danmaku 数组）
        pass
    result = {
        'platform': 'douyin',
        'aweme_id': aweme_id,
        'url': f'https://www.douyin.com/video/{aweme_id}',
        'title': desc,
        'author': author,
        'publish_time': create_time,
        'duration_sec': int(duration / 1000),
        'metrics': {
            'views': stats.get('play_count'),
            'likes': stats.get('digg_count'),
            'comments': stats.get('comment_count'),
            'collects': stats.get('collect_count'),
            'shares': stats.get('share_count'),
        },
        'asr_text': raw_text,
        'srt': open(srt_file, encoding='utf-8').read(),
        'danmaku_xml': '',
        'danmaku_list': danmaku_list,  # 自定义扩展：弹幕数组
        'highlight': hl_text,
        'comments': comments,
        'workdir': out_dir,
        'tags': ['抖音分析', '评论高能'] if comments else ['抖音分析'],
    }
    result_file = os.path.join(out_dir, f'{aweme_id}_result.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n[DONE] 全部完成!')
    print(f'  产物目录: {out_dir}')
    print(f'  数据契约: {result_file}')
    print(f'  ASR字数: {len(raw_text)} | 弹幕: {len(danmaku_list)} | 评论: {len(comments)} | 高能: {len(hl_text)}字')


if __name__ == '__main__':
    main()
