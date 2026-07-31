#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一 Notion 入库脚本（数据契约模式）
输入: <平台>_process.py 产出的标准 result.json
流程: 标准 JSON → 组装正文 → curl POST → Notion「存档记录」

用法:
  python insert_notion.py <result.json> [--dry-run]

标准 JSON 契约字段:
  platform: bilibili|douyin|xiaohongshu
  url: 原始链接
  title / author / publish_time(epoch秒) / duration_sec
  metrics: {views, danmaku, comments, likes, collects, shares, coins}
  asr_text / srt / danmaku_xml / highlight / comments(列表)
  tags: [..]
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

import config
DATA_SOURCE_ID = config.DATA_SOURCE_ID
NOTION_TOKEN = config.NOTION_TOKEN  # 环境变量 NOTION_TOKEN


def query_existing_by_url(token, url):
    """按链接查询是否已存在（去重）"""
    body = json.dumps({
        'filter': {
            'property': '链接',
            'url': {'equals': url},
        },
        'page_size': 10,
    })
    req = urllib.request.Request(
        f'https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query',
        data=body.encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Notion-Version': '2026-03-11',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(resp.read().decode('utf-8'))
    results = j.get('results') or []
    return [p for p in results if not p.get('in_trash')]


def load_token():
    """优先环境变量，其次从旧脚本提取（兼容迁移期）"""
    if NOTION_TOKEN:
        return NOTION_TOKEN
    # fallback: 从旧 insert_douyin.py 提取（仅本机迁移用，仓库不含）
    legacy = os.environ.get('LEGACY_DOUYIN_SCRIPT', '')
    if legacy and os.path.exists(legacy):
        txt = open(legacy, encoding='utf-8').read()
        m = re.search(r"NOTION_TOKEN\s*=\s*'([^']+)'", txt)
        if m:
            return m.group(1)
    return None


def chunks(s, n=1900):
    return [s[i:i + n] for i in range(0, len(s), n)][:10]


def build_properties(r):
    platform = r.get('platform', '')
    # 平台名标准化（Notion select 用中文）
    PLATFORM_NAMES = {
        'bilibili': 'B站',
        'douyin': '抖音',
        'xiaohongshu': '小红书',
        'kuaishou': '快手',
        'youtube': 'YouTube',
    }
    platform = PLATFORM_NAMES.get(platform, platform)
    title = (r.get('title') or f"{platform}_{r.get('bvid') or r.get('aweme_id') or 'unknown'}")[:190]
    author = r.get('author') or '未知'
    metrics = r.get('metrics') or {}
    pub_time = r.get('publish_time')
    pub_date = None
    if pub_time:
        try:
            pub_date = datetime.datetime.fromtimestamp(int(pub_time)).strftime('%Y-%m-%d')
        except Exception:
            pass

    # 标签
    tags = list(r.get('tags') or [])
    tags = tags[:5]

    # 互动数据
    m = metrics
    metric_str = ' '.join(f"{k}{v}" for k, v in [
        ('播放', m.get('views')), ('弹幕', m.get('danmaku')), ('赞', m.get('likes')),
        ('评论', m.get('comments')), ('收藏', m.get('collects')), ('转发', m.get('shares')),
        ('投币', m.get('coins')),
    ] if v)

    props = {
        '标题': {'title': [{'type': 'text', 'text': {'content': title}}]},
        '平台': {'select': {'name': platform}},
        '链接': {'url': r.get('url', '')},
        '作者': {'rich_text': [{'type': 'text', 'text': {'content': author}}]},
        '提取日期': {'date': {'start': datetime.date.today().isoformat()}},
        '字数': {'number': len(r.get('asr_text') or '')},
        '标签': {'multi_select': [{'name': t} for t in tags]},
        '互动数据': {'rich_text': [{'type': 'text', 'text': {'content': metric_str}}]},
        '状态': {'select': {'name': '已完成'}},
    }
    if pub_date:
        props['发布时间'] = {'date': {'start': pub_date}}

    # 正文段落（按平台组织）
    sections = []
    if r.get('asr_text'):
        sections.append(('【ASR 识别全文】', r['asr_text']))
    if r.get('srt'):
        sections.append(('【SRT 字幕】', r['srt']))
    if r.get('highlight'):
        sections.append(('【弹幕高能分析】', r['highlight']))
    if r.get('danmaku_xml'):
        # 弹幕明细：取前 50 条
        dms = re.findall(r'<d p="([^"]+)">([^<]+)</d>', r['danmaku_xml'])
        lines = []
        for p, t in dms[:50]:
            sec = float(p.split(',')[0])
            lines.append(f"[{sec:.1f}s] {t}")
        if lines:
            sections.append((f'【弹幕明细】(前{len(lines)}条)', '\n'.join(lines)))
    # 抖音弹幕数组（douyin_process 扩展字段）
    dm_list = r.get('danmaku_list') or []
    if dm_list:
        lines = []
        for d in dm_list[:50]:
            sec = d.get('offset_time', 0) / 1000
            lines.append(f"[{sec:.1f}s|{d.get('digg_count', 0)}赞] {d.get('text', '')}")
        if lines:
            sections.append((f'【弹幕明细】(共{len(dm_list)}条，前{len(lines)}条)', '\n'.join(lines)))
    comments = r.get('comments') or []
    if comments:
        clines = []
        for c in comments[:20]:
            clines.append(f"[{c.get('digg_count', 0)}赞] {c.get('nickname', '')}: {c.get('text', '')}")
        sections.append(('【评论高能分析】(前20条高赞)', '\n'.join(clines)))

    body = '\n\n' + '━' * 40 + '\n\n'.join(f"{head}\n{content}" for head, content in sections)
    props['正文'] = {'rich_text': [{'type': 'text', 'text': {'content': c}} for c in chunks(body)]}
    return props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('result_json')
    ap.add_argument('--dry-run', action='store_true', help='只打印 payload 不入库')
    ap.add_argument('--update', action='store_true', help='链接已存在时更新正文（默认跳过）')
    args = ap.parse_args()

    r = json.load(open(args.result_json, encoding='utf-8'))
    token = load_token()
    if not token:
        print('[FAIL] 未找到 Notion token')
        sys.exit(1)

    props = build_properties(r)

    # 去重：按链接检查是否已存在
    url = r.get('url', '')
    existing_id = None
    if url:
        print('[去重] 检查链接是否已存在:', url[:80])
        existing = query_existing_by_url(token, url)
        if existing:
            existing_id = existing[0]['id']
            if not args.update:
                ids = [p['id'] for p in existing]
                print(f'[SKIP] 该链接已存在 {len(existing)} 条记录，跳过入库（--update 可覆盖更新）: {ids}')
                return
            print(f'[UPDATE] 链接已存在，将覆盖更新: {existing_id}')
        else:
            print('[OK] 链接不存在，新建入库')

    page = {'parent': {'data_source_id': DATA_SOURCE_ID}, 'properties': props}
    payload = json.dumps(page, ensure_ascii=False)

    if args.dry_run:
        print('[DRY-RUN] payload 长度:', len(payload))
        print(json.dumps(props, ensure_ascii=False, indent=1)[:1500])
        return

    payload_file = os.path.join(os.path.dirname(args.result_json), '_notion_payload.json')
    with open(payload_file, 'w', encoding='utf-8') as f:
        f.write(payload)

    # 已存在 → PATCH 更新；不存在 → POST 新建
    if existing_id:
        cmd = [
            'curl.exe', '-s', '-w', '\nHTTP_STATUS:%{http_code}',
            f'https://api.notion.com/v1/pages/{existing_id}',
            '-X', 'PATCH',
            '-H', f'Authorization: Bearer {token}',
            '-H', 'Notion-Version: 2026-03-11',
            '-H', 'Content-Type: application/json',
            '--data-binary', f'@{payload_file}',
        ]
    else:
        cmd = [
            'curl.exe', '-s', '-w', '\nHTTP_STATUS:%{http_code}',
            'https://api.notion.com/v1/pages',
            '-H', f'Authorization: Bearer {token}',
            '-H', 'Notion-Version: 2026-03-11',
            '-H', 'Content-Type: application/json',
            '--data-binary', f'@{payload_file}',
        ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = result.stdout + result.stderr
    print(out[-800:])

    if 'HTTP_STATUS:200' in out:
        m = re.search(r'"url":"(https://app\.notion\.com/[^"]+)"', out)
        print('\n[OK] 已写入 Notion「存档记录」' + ('（更新）' if existing_id else '（新建）'))
        print(f'   标题: {r.get("title", "")[:40]}')
        if m:
            print(f'   页面: {m.group(1)}')
        else:
            print(f'   页面ID: {existing_id or "?"}')
    else:
        print('\n[FAIL] 入库失败，见上方响应')


if __name__ == '__main__':
    main()
