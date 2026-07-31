#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
B站视频一键处理脚本（参数化版）
输入: B站链接 或 bvid
流程: 元数据 → 音频下载 → FunASR ASR → SRT → 弹幕XML → 弹幕融合高能分析
输出: <workdir>/<bvid>/ 目录下所有产物 + <bvid>_result.json（标准数据契约，供 insert_notion.py 入库）

用法:
  python bilibili_process.py "https://www.bilibili.com/video/BV1xxxx" [workdir]
"""
import json
import os
import re
import subprocess
import sys
import zlib
import urllib.request

# ========== 配置（统一从 config.py 读取）==========
import config
WORKDIR = config.WORKDIR
PY = config.PYTHON_BIN
GEN_SRT = config.GEN_SRT_PATH
FUSION = config.BILI_FUSION_PATH
FFMPEG = config.FFMPEG_BIN

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'


def http_get(url, referer='https://www.bilibili.com/', timeout=60, binary=False):
    """B站 API GET，用 curl（urllib 偶发卡死/被 B站 TLS 指纹拒绝）"""
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), 'bili_http_tmp')
    cmd = ['curl.exe', '-s', '-L', '--max-time', str(timeout), '-o', tmp,
           url, '-H', f'User-Agent: {UA}', '-H', f'Referer: {referer}']
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    if not os.path.exists(tmp):
        raise RuntimeError(f'curl 下载失败: {url[:60]}')
    data = open(tmp, 'rb').read()
    os.remove(tmp)
    if binary:
        return data
    return data.decode('utf-8', errors='replace')


def run(cmd, **kw):
    """运行命令并打印输出"""
    print('>>', ' '.join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    if r.stdout:
        print(r.stdout[-1500:])
    if r.stderr:
        tail = r.stderr[-800:]
        if 'Traceback' in tail or 'Error' in tail or 'error' in tail:
            print('[stderr]', tail)
    return r


def extract_bvid(text):
    m = re.search(r'(BV[0-9A-Za-z]{10})', text or '')
    return m.group(1) if m else None


def decompress_bili_dm(raw):
    for wbits in [-15, 15, 31, 47]:
        try:
            return zlib.decompress(raw, wbits)
        except Exception:
            continue
    return raw


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    link = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else WORKDIR

    bvid = extract_bvid(link)
    if not bvid:
        print('[FAIL] 未找到 BV 号')
        sys.exit(1)

    out_dir = os.path.join(workdir, bvid)
    os.makedirs(out_dir, exist_ok=True)
    print(f'=== B站处理: {bvid} ===')
    print(f'输出目录: {out_dir}')

    # ---- 1. 元数据 ----
    print('\n[1/6] 获取元数据...')
    view = json.loads(http_get(f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'))
    if view.get('code') != 0:
        print(f'[FAIL] view API: {view.get("message")}')
        sys.exit(1)
    d = view['data']
    cid = d['pages'][0]['cid']
    title = d['title']
    author = d['owner']['name']
    pubdate = d['pubdate']
    duration = d['duration']
    stat = d.get('stat', {})
    print(f'  标题: {title}')
    print(f'  cid: {cid} | 时长: {duration}s | 作者: {author}')

    # ---- 2. 下载音频（官方 playurl 直链）----
    print('\n[2/6] 下载音频（playurl 直链）...')
    playurl = json.loads(http_get(f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=16&fnval=16'))
    if playurl.get('code') != 0:
        print(f'[FAIL] playurl: {playurl.get("message")}')
        sys.exit(1)
    dash = (playurl.get('data') or {}).get('dash') or {}
    audio_list = dash.get('audio') or []
    audio_url = None
    if audio_list:
        # 选中等码率（避开最大轨，下载更快）
        audio_list.sort(key=lambda a: a.get('bandwidth', 0))
        audio_url = audio_list[len(audio_list) // 2].get('baseUrl') or audio_list[len(audio_list) // 2].get('base_url')
    if not audio_url:
        durl = (playurl.get('data') or {}).get('durl') or []
        if durl:
            audio_url = durl[0].get('url')
    if not audio_url:
        print('[FAIL] 未找到音频直链，请检查视频是否需要登录')
        sys.exit(1)

    audio_raw = os.path.join(out_dir, 'audio.m4s')
    http_get(audio_url, referer=f'https://www.bilibili.com/video/{bvid}', binary=True)
    # urllib 下载（带 referer）
    req = urllib.request.Request(audio_url, headers={'User-Agent': UA, 'Referer': f'https://www.bilibili.com/video/{bvid}'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(audio_raw, 'wb') as f:
        f.write(resp.read())
    print(f'  音频已下载: {os.path.getsize(audio_raw)} bytes')

    # 转成 mp3 供 FunASR
    audio_mp3 = os.path.join(out_dir, 'audio.mp3')
    run([FFMPEG, '-y', '-i', audio_raw, '-vn', '-ac', '1', '-ar', '16000', '-b:a', '64k', audio_mp3])

    # ---- 3. FunASR ASR → SRT ----
    print('\n[3/6] FunASR ASR...')
    r = run([PY, GEN_SRT, audio_mp3])
    srt_file = os.path.join(out_dir, 'audio.srt')
    raw_file = os.path.join(out_dir, 'audio_raw.txt')
    # gen_srt.py 输出到音频同目录同名 .srt / _raw.txt
    if os.path.exists(audio_mp3.replace('.mp3', '.srt')):
        os.rename(audio_mp3.replace('.mp3', '.srt'), srt_file)
    if os.path.exists(audio_mp3.replace('.mp3', '_raw.txt')):
        os.rename(audio_mp3.replace('.mp3', '_raw.txt'), raw_file)
    if not os.path.exists(srt_file):
        print('[FAIL] SRT 未生成')
        sys.exit(1)
    raw_text = open(raw_file, encoding='utf-8').read() if os.path.exists(raw_file) else ''

    # ---- 4. 弹幕 XML ----
    print('\n[4/6] 拉取弹幕 XML...')
    dm_raw = http_get(f'https://api.bilibili.com/x/v1/dm/list.so?oid={cid}', binary=True)
    dm_xml = os.path.join(out_dir, 'danmaku.xml')
    text = decompress_bili_dm(dm_raw).decode('utf-8', errors='replace')
    with open(dm_xml, 'w', encoding='utf-8') as f:
        f.write(text)
    dm_count = len(re.findall(r'<d p="', text))
    print(f'  弹幕数: {dm_count}')

    # ---- 5. 弹幕融合高能分析 ----
    print('\n[5/6] 弹幕融合高能分析...')
    run(['node', FUSION, srt_file, dm_xml], cwd=os.path.dirname(FUSION))
    hl_file = os.path.join(out_dir, 'audio_AI_HIGHLIGHT.txt')
    hl_text = ''
    if os.path.exists(hl_file):
        hl_text = open(hl_file, encoding='utf-8').read()
        print(f'  高能摘要: {len(hl_text)} 字')

    # ---- 6. 输出标准数据契约 JSON ----
    print('\n[6/6] 生成数据契约...')
    result = {
        'platform': 'bilibili',
        'bvid': bvid,
        'url': f'https://www.bilibili.com/video/{bvid}',
        'title': title,
        'author': author,
        'publish_time': pubdate,
        'duration_sec': duration,
        'metrics': {
            'views': stat.get('view'),
            'danmaku': stat.get('danmaku'),
            'comments': stat.get('reply'),
            'likes': stat.get('like'),
            'collects': stat.get('favorite'),
            'shares': stat.get('share'),
            'coins': stat.get('coin'),
        },
        'asr_text': raw_text,
        'srt': open(srt_file, encoding='utf-8').read(),
        'danmaku_count': dm_count,
        'danmaku_xml': text,
        'highlight': hl_text,
        'workdir': out_dir,
        'tags': ['B站分析', '弹幕高能'],
    }
    result_file = os.path.join(out_dir, f'{bvid}_result.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n[DONE] 全部完成!')
    print(f'  产物目录: {out_dir}')
    print(f'  数据契约: {result_file}')
    srt_content = open(srt_file, encoding='utf-8').read() if os.path.exists(srt_file) else ''
    print(f'  ASR字数: {len(raw_text)} | SRT段数: {srt_content.count(chr(10)+chr(10))+1 if srt_content else 0} | 弹幕: {dm_count} | 高能摘要: {len(hl_text)}字')


if __name__ == '__main__':
    main()
