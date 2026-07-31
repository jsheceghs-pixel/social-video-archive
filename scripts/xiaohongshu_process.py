#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小红书视频笔记一键处理脚本（参数化版）
输入: 小红书链接（带 xsec_token）
流程: socialkit 元数据 → 视频下载 → 提取音频 → FunASR ASR → SRT → 数据契约
输出: <workdir>/<note_id>/ 目录 + <note_id>_result.json（标准数据契约，供 insert_notion.py 入库）

注意: 小红书网页版无弹幕，正文为 ASR + SRT（+ 可选评论）
用法:
  python xiaohongshu_process.py "https://www.xiaohongshu.com/explore/xxx?xsec_token=..." [workdir]
"""
import json
import os
import re
import subprocess
import sys

import config
sys.path.insert(0, config.SOCIALKIT_DIR)

WORKDIR = config.WORKDIR
PY = config.PYTHON_BIN
GEN_SRT = config.GEN_SRT_PATH
FFMPEG = config.FFMPEG_BIN
CDP_SCRIPT = config.CDP_EVAL_JS

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'


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


def cdp_navigate(ws_url, url, wait_ms=10000):
    """通过 CDP 导航页面并等待加载（评论提取前确保页面是目标笔记）"""
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


def extract_note_id(url):
    """支持 /explore/ 和 /discovery/item/ 两种笔记路径"""
    m = re.search(r'(?:/explore/|/discovery/item/)([0-9a-f]{24})', url or '')
    return m.group(1) if m else None


def download(url, dest, referer='https://www.xiaohongshu.com/'):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': referer})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, 'wb') as f:
        f.write(resp.read())
    return os.path.getsize(dest)


def run(cmd, **kw):
    print('>>', ' '.join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    if r.stdout:
        print(r.stdout[-1200:])
    return r


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else WORKDIR

    note_id = extract_note_id(url)
    if not note_id:
        print('[FAIL] 未找到笔记 ID')
        sys.exit(1)

    out_dir = os.path.join(workdir, note_id)
    os.makedirs(out_dir, exist_ok=True)
    print(f'=== 小红书视频处理: {note_id} ===')

    # ---- 1. socialkit 元数据 ----
    print('\n[1/5] 获取元数据（socialkit）...')
    from social_media_toolkit.platforms.core import PlatformRouter
    post = PlatformRouter().parse(url)
    if post.content_type != 'video' or not post.video_url:
        print(f'[FAIL] 不是视频笔记或未拿到视频直链 (type={post.content_type})')
        print(f'   body: {(post.body or "")[:100]}')
        sys.exit(1)
    print(f'  标题: {post.title}')
    print(f'  作者: {post.author_name} | 时长: {post.duration_sec}s')
    print(f'  视频: {post.video_url[:80]}...')

    # ---- 2. 下载视频 + 提取音频 ----
    print('\n[2/5] 下载视频...')
    video_file = os.path.join(out_dir, 'video.mp4')
    download(post.video_url, video_file, referer=post.page_url or url)
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
    if not os.path.exists(srt_file):
        print('[FAIL] SRT 未生成')
        sys.exit(1)
    raw_text = open(raw_file, encoding='utf-8').read() if os.path.exists(raw_file) else ''

    # ---- 4. 评论（从 SSR noteDetailMap 提取，需浏览器 CDP）----
    print('\n[4/5] 评论（SSR 提取）...')
    comments = []
    ws_url_xhs = os.environ.get('XHS_WS_URL', '')
    if ws_url_xhs:
        # 先导航到目标笔记页（确保 SSR 数据是当前笔记的）
        print('  导航到目标笔记页...')
        nav_state = cdp_navigate(ws_url_xhs, post.page_url or url)
        if nav_state:
            print(f'  页面: {str(nav_state)[:80]}')
        else:
            print('  [提示] 导航可能未完成，尝试直接提取')
        # 评论是异步加载的，轮询等待 firstRequestFinish=true 或 list 非空
        wait_expr = """(() => {
          const s = window.__INITIAL_STATE__;
          if (!s || !s.note) return JSON.stringify({ready: false});
          const noteMap = s.note.noteDetailMap || {};
          const firstKey = Object.keys(noteMap)[0];
          const c = firstKey ? (noteMap[firstKey].comments || {}) : {};
          const list = c.list || [];
          return JSON.stringify({ready: list.length > 0 || c.firstRequestFinish === true, count: list.length});
        })()"""
        ready = False
        for _attempt in range(8):
            val = cdp_eval(ws_url_xhs, wait_expr)
            if val:
                try:
                    st = json.loads(val)
                    if st.get('ready'):
                        ready = True
                        print(f'  评论加载完成: {st.get("count")} 条')
                        break
                except Exception:
                    pass
            print('  [等待] 评论加载中...')
            import time as _t
            _t.sleep(2)
        if not ready:
            print('  [提示] 等待超时，使用当前 SSR 数据')
        expr = """(() => {
          const s = window.__INITIAL_STATE__;
          if (!s || !s.note) return JSON.stringify({err: 'no ssr'});
          const noteMap = s.note.noteDetailMap || {};
          const firstKey = Object.keys(noteMap)[0];
          const commentsObj = noteMap[firstKey].comments || {};
          const list = commentsObj.list || [];
          const out = list.map(c => ({
            text: c.content || '',
            like_count: parseInt(c.likeCount || 0),
            reply_count: parseInt(c.subCommentCount || 0),
            nickname: (c.userInfo || {}).nickname || '',
            ip_label: c.ipLocation || null,
            timestamp: c.createTime ? Math.floor(c.createTime / 1000) : null
          }));
          return JSON.stringify({ total: out.length, comments: out });
        })()"""
        val = cdp_eval(ws_url_xhs, expr)
        if val:
            try:
                j = json.loads(val)
                comments = j.get('comments', [])
            except Exception:
                comments = []
        print(f'  评论数: {len(comments)}')

    # ---- 5. 数据契约 ----
    print('\n[5/5] 生成数据契约...')
    metrics = post.public_metrics or {}

    def parse_count(v):
        """解析 '3.1万' / '1280' / '1.2千' 等显示格式为整数"""
        if v is None:
            return None
        s = str(v).strip()
        m = re.match(r'^([\d.]+)\s*(万|w|千|k)?$', s, re.IGNORECASE)
        if not m:
            return None
        num = float(m.group(1))
        unit = (m.group(2) or '').lower()
        if unit in ('万', 'w'):
            return int(num * 10000)
        if unit in ('千', 'k'):
            return int(num * 1000)
        return int(num)

    result = {
        'platform': 'xiaohongshu',
        'note_id': note_id,
        'url': post.page_url or url,
        'title': post.title,
        'author': post.author_name,
        'author_id': post.author_id,
        'publish_time': int(post.publish_time / 1000) if post.publish_time else None,
        'duration_sec': post.duration_sec,
        'metrics': {
            'views': None,
            'likes': parse_count(metrics.get('likes')),
            'collects': parse_count(metrics.get('collects')),
            'comments': parse_count(metrics.get('comments')),
            'shares': parse_count(metrics.get('shares')),
        },
        'asr_text': raw_text,
        'srt': open(srt_file, encoding='utf-8').read(),
        'danmaku_xml': '',
        'highlight': '',
        'comments': comments,
        'workdir': out_dir,
        'tags': ['小红书分析'],
    }
    result_file = os.path.join(out_dir, f'{note_id}_result.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n[DONE] 全部完成!')
    print(f'  产物目录: {out_dir}')
    print(f'  数据契约: {result_file}')
    print(f'  ASR字数: {len(raw_text)}')


if __name__ == '__main__':
    main()
