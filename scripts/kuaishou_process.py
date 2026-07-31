#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
快手视频一键处理脚本（数据契约模式）
输入: 快手链接（short-video 或 v.kuaishou 短链）
流程: 浏览器CDP → Apollo元数据+评论 → 视频下载 → ffmpeg提音频 → FunASR ASR → SRT → 数据契约
输出: <workdir>/<photo_id>/ + <photo_id>_result.json（供 insert_notion.py 入库）

依赖: openclaw browser 已打开快手详情页 tab（cdp_helper 需 wsUrl）
用法:
  python kuaishou_process.py "https://www.kuaishou.com/short-video/xxx" [wsUrl] [workdir]
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

import config
WORKDIR = config.WORKDIR
PY = config.PYTHON_BIN
GEN_SRT = config.GEN_SRT_PATH
FFMPEG = config.FFMPEG_BIN
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

# CDP 辅助：通过 WebSocket 执行 JS（需要 ws 包）
CDP_SCRIPT = config.CDP_EVAL_JS


def extract_photo_id(url):
    """从链接提取 photoId（支持短链解析后的 short-video 链接；新版 ID 为字母数字混合）"""
    m = re.search(r'/short-video/([0-9A-Za-z_-]{5,30})', url or '')
    return m.group(1) if m else None


def resolve_shortlink(ws_url, short_url):
    """通过浏览器解析 v.kuaishou.com 短链，返回跳转后的最终 URL"""
    expr = """(async () => {
      try {
        const res = await fetch('{url}', { credentials: 'include', redirect: 'follow' });
        return JSON.stringify({ finalUrl: res.url, status: res.status });
      } catch (e) { return JSON.stringify({ error: e.message }); }
    })()""".replace('{url}', short_url)
    val = cdp_eval(ws_url, expr)
    if not val:
        return None
    try:
        j = json.loads(val)
        return j.get('finalUrl')
    except Exception:
        return None


def cdp_eval(ws_url, expression):
    """通过 CDP 在页面执行 JS，返回结果（表达式写入临时文件避免转义问题）"""
    expr_file = os.path.join(WORKDIR, '_cdp_expr.js')
    with open(expr_file, 'w', encoding='utf-8') as f:
        f.write(expression)
    r = subprocess.run(['node', CDP_SCRIPT, ws_url, expr_file],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
    out = r.stdout.strip()
    if not out:
        return None
    out = out.split('\n')[-1]  # 取最后一行 JSON
    try:
        return json.loads(out).get('value')
    except Exception:
        print('[CDP输出]', out[:300])
        return None


def cdp_navigate(ws_url, url, wait_ms=10000):
    """通过 CDP 导航页面并等待加载（确保 Apollo 数据是目标视频的）"""
    nav_script = config.CDP_NAVIGATE_JS
    r = subprocess.run(['node', nav_script, ws_url, url, str(wait_ms)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=wait_ms // 1000 + 30)
    out = r.stdout.strip()
    if not out:
        return None
    out = out.split('\n')[-1]
    try:
        return json.loads(out).get('value')
    except Exception:
        print('[CDP导航]', out[:300])
        return None


def run(cmd, **kw):
    print('>>', ' '.join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    if r.stdout:
        print(r.stdout[-1200:])
    return r


def download(url, dest, referer='https://www.kuaishou.com/'):
    """用 curl 下载（urllib 的 TLS 指纹会被快手 CDN 拒绝）"""
    r = subprocess.run([
        'curl.exe', '-s', '-L', '-o', dest,
        url,
        '-H', f'User-Agent: {UA}',
        '-H', f'Referer: {referer}',
        '--max-time', '180',
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=200)
    if not os.path.exists(dest):
        raise RuntimeError(f'下载失败: {r.stderr[-300:]}')
    return os.path.getsize(dest)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    ws_url = sys.argv[2] if len(sys.argv) > 2 else None
    workdir = sys.argv[3] if len(sys.argv) > 3 else WORKDIR

    # 短链先解析
    if 'v.kuaishou.com' in (url or ''):
        print('[短链] 解析 v.kuaishou.com 短链...')
        if not ws_url:
            print('[FAIL] 短链解析需要浏览器 wsUrl')
            sys.exit(1)
        final_url = resolve_shortlink(ws_url, url)
        if final_url:
            print('  跳转至:', final_url[:100])
            url = final_url
        else:
            print('[FAIL] 短链解析失败')
            sys.exit(1)

    photo_id = extract_photo_id(url)
    if not photo_id:
        print('[FAIL] 未找到 photoId（需 short-video 链接）')
        sys.exit(1)

    out_dir = os.path.join(workdir, f'ks_{photo_id}')
    os.makedirs(out_dir, exist_ok=True)
    print(f'=== 快手视频处理: {photo_id} ===')

    if not ws_url:
        print('[FAIL] 需要浏览器 wsUrl 参数（openclaw browser 打开详情页后获取）')
        sys.exit(1)

    # ---- 1. CDP 提取 Apollo 元数据 ----
    print('\n[1/5] CDP 提取 Apollo 数据...')
    # 先导航到目标视频详情页（确保 Apollo 数据是当前视频的）
    print('  导航到视频详情页...')
    nav_state = cdp_navigate(ws_url, url)
    if nav_state:
        print(f'  页面: {str(nav_state)[:80]}')
    else:
        print('  [提示] 导航可能未完成，尝试直接提取')
    expr = """(() => {
      const cache = window.__APOLLO_STATE__ && window.__APOLLO_STATE__.defaultClient;
      if (!cache) return JSON.stringify({err: 'no apollo'});
      const photoKey = Object.keys(cache).find(k => k.includes('VisionVideoDetailPhoto:') && !k.includes('.'));
      const authorKey = Object.keys(cache).find(k => k.includes('VisionVideoDetailAuthor:') && !k.includes('.'));
      const photo = photoKey ? cache[photoKey] : null;
      const author = authorKey ? cache[authorKey] : null;
      if (!photo) return JSON.stringify({err: 'no photo'});
      return JSON.stringify({
        photoId: '{pid}',
        caption: photo.caption,
        duration: photo.duration,
        likeCount: photo.realLikeCount || photo.likeCount,
        viewCount: photo.viewCount,
        coverUrl: photo.coverUrl,
        photoUrl: photo.photoUrl,
        timestamp: photo.timestamp,
        author: author ? author.name : '',
        authorId: author ? author.id : ''
      });
    })()""".replace('{pid}', photo_id)
    val = cdp_eval(ws_url, expr)
    if not val or 'err' in str(val):
        print('[FAIL] Apollo 提取失败:', str(val)[:200])
        sys.exit(1)
    meta = json.loads(val)
    print(f'  标题: {meta.get("caption")}')
    print(f'  作者: {meta.get("author")} | 时长: {meta.get("duration", 0)/1000:.1f}s')
    print(f'  点赞: {meta.get("likeCount")} | 播放: {meta.get("viewCount")}')

    # ---- 2. 下载视频 + 提音频 ----
    print('\n[2/5] 下载视频...')
    photo_url = meta.get('photoUrl')
    if not photo_url:
        print('[FAIL] 无视频直链')
        sys.exit(1)
    video_file = os.path.join(out_dir, 'video.mp4')
    download(photo_url, video_file, referer=url)
    print(f'  视频: {os.path.getsize(video_file)} bytes')

    audio_mp3 = os.path.join(out_dir, 'audio.mp3')
    print('\n[2b/5] 提取音频...')
    run([FFMPEG, '-y', '-i', video_file, '-vn', '-ac', '1', '-ar', '16000', '-b:a', '64k', audio_mp3])

    # ---- 3. FunASR ASR → SRT ----
    print('\n[3/5] FunASR ASR...')
    run([PY, GEN_SRT, audio_mp3])
    srt_file = os.path.join(out_dir, 'audio.srt')
    raw_file = os.path.join(out_dir, 'audio_raw.txt')
    if os.path.exists(audio_mp3.replace('.mp3', '.srt')):
        os.rename(audio_mp3.replace('.mp3', '.srt'), srt_file)
    if os.path.exists(audio_mp3.replace('.mp3', '_raw.txt')):
        os.rename(audio_mp3.replace('.mp3', '_raw.txt'), raw_file)
    raw_text = open(raw_file, encoding='utf-8').read() if os.path.exists(raw_file) else ''

    # ---- 4. CDP 拉评论 ----
    print('\n[4/5] CDP 拉取评论...')
    cmt_expr = """(async () => {
      const results = [];
      let pcursor = '';
      for (let i = 0; i < 3; i++) {
        const res = await fetch('/rest/v/photo/comment/list', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ photoId: '{pid}', pcursor, count: 20 })
        });
        const j = await res.json();
        const list = j.rootCommentsV2 || [];
        for (const c of list) {
          results.push({ text: c.content, like_count: c.likeCount || 0, reply_count: c.commentCount || 0, nickname: c.author_name || '', timestamp: c.timestamp });
        }
        pcursor = j.pcursorV2 || '';
        if (!list.length) break;
        await new Promise(r2 => setTimeout(r2, 300));
      }
      return JSON.stringify({ total: results.length, comments: results });
    })()""".replace('{pid}', photo_id)
    cmt_val = cdp_eval(ws_url, cmt_expr)
    comments = []
    if cmt_val:
        try:
            comments = json.loads(cmt_val).get('comments', [])
        except Exception:
            comments = []
    print(f'  评论数: {len(comments)}')

    # ---- 5. 数据契约 ----
    print('\n[5/5] 生成数据契约...')
    ts = meta.get('timestamp')
    result = {
        'platform': 'kuaishou',
        'photo_id': photo_id,
        'url': url,
        'title': meta.get('caption', ''),
        'author': meta.get('author', ''),
        'publish_time': int(ts / 1000) if ts else None,
        'duration_sec': int(meta.get('duration', 0) / 1000),
        'metrics': {
            'views': meta.get('viewCount'),
            'likes': meta.get('likeCount'),
            'comments': None,
        },
        'asr_text': raw_text,
        'srt': open(srt_file, encoding='utf-8').read() if os.path.exists(srt_file) else '',
        'danmaku_xml': '',
        'highlight': '',
        'comments': comments,
        'workdir': out_dir,
        'tags': ['快手分析'],
    }
    result_file = os.path.join(out_dir, f'ks_{photo_id}_result.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n[DONE] 全部完成!')
    print(f'  产物目录: {out_dir}')
    print(f'  数据契约: {result_file}')
    print(f'  ASR字数: {len(raw_text)} | 评论: {len(comments)}')


if __name__ == '__main__':
    main()
