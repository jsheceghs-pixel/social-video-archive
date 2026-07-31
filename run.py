#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键调度入口：丢链接 → 自动识别平台 → 采集 → 入库 → 返回 Notion 链接

用法:
  python run.py "https://www.bilibili.com/video/BV1xxx"          # B站（无需浏览器）
  python run.py "https://www.douyin.com/video/7xxx"              # 抖音（自动检测浏览器）
  python run.py "https://www.xiaohongshu.com/explore/xxx?xsec_token=..."  # 小红书
  python run.py "https://www.kuaishou.com/short-video/5xxx"      # 快手
  python run.py <链接> --update    # 已存在则覆盖更新（默认跳过）
  python run.py <链接> --no-insert # 只采集不入库

自动流程:
  1. 根据 URL 判断平台
  2. 调对应平台 process 脚本（各平台内部逻辑原封不动）
  3. 等待 result.json 产出
  4. 调 insert_notion.py 入库
  5. 打印 Notion 页面链接

抖音/小红书/快手需要浏览器 CDP：run.py 会自动从 openclaw browser 检测
对应平台的 tab 并取 wsUrl；检测不到会提示你手动打开。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

# scripts/ 加入 path（config.py 在 scripts/ 下）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts'))
import config

# 平台识别规则（顺序敏感，短链先匹配）
PLATFORM_RULES = [
    ('bilibili',   r'bilibili\.com|b23\.tv|BV[0-9A-Za-z]{10}'),
    ('douyin',     r'douyin\.com|v\.douyin\.com|iesdouyin\.com'),
    ('xiaohongshu', r'xiaohongshu\.com|xhslink\.com'),
    ('kuaishou',   r'kuaishou\.com|v\.kuaishou\.com'),
]

# 各平台脚本路径（在 scripts/ 下）
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
PROCESS_SCRIPTS = {
    'bilibili':   os.path.join(SCRIPTS_DIR, 'bilibili_process.py'),
    'douyin':     os.path.join(SCRIPTS_DIR, 'douyin_process.py'),
    'xiaohongshu': os.path.join(SCRIPTS_DIR, 'xiaohongshu_process.py'),
    'kuaishou':   os.path.join(SCRIPTS_DIR, 'kuaishou_process.py'),
}

# 各平台需要的 wsUrl 环境变量
WS_ENV = {
    'douyin': 'DY_WS_URL',
    'xiaohongshu': 'XHS_WS_URL',
    'kuaishou': 'KS_WS_URL',  # kuaishou 脚本用第2个位置参数传 wsUrl
}

# 平台对应浏览器域名特征（用于自动检测 tab）
WS_DOMAIN = {
    'douyin': 'douyin.com',
    'xiaohongshu': 'xiaohongshu.com',
    'kuaishou': 'kuaishou.com',
}


def detect_platform(url):
    for plat, pattern in PLATFORM_RULES:
        if re.search(pattern, url or '', re.IGNORECASE):
            return plat
    return None


def find_ws_url(platform):
    """从 openclaw browser tabs 检测对应平台 tab 的 wsUrl"""
    domain = WS_DOMAIN.get(platform)
    if not domain:
        return None
    try:
        r = subprocess.run(['openclaw', 'browser', 'tabs', '--json'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        out = r.stdout.strip()
        if not out:
            return None
        # tabs 输出可能含前缀文本，找 JSON 起始
        start = out.find('{')
        if start < 0:
            return None
        data = json.loads(out[start:])
        for tab in data.get('tabs', []):
            url = tab.get('url', '')
            if domain in url and tab.get('type') == 'page' and tab.get('wsUrl'):
                return tab['wsUrl']
    except Exception as e:
        print(f'  [提示] 检测浏览器 tab 失败: {e}')
    return None


def find_result_file(platform, workdir, link):
    """在产物目录找刚生成的 result.json（按修改时间最新的匹配平台前缀）"""
    # 平台对应的 ID 前缀
    id_patterns = {
        'bilibili': r'BV[0-9A-Za-z]{10}',
        'douyin': r'\d{15,20}',
        'xiaohongshu': r'[0-9a-f]{24}',
        'kuaishou': r'\d{10,20}',
    }
    # 从链接提取 ID
    m = re.search(id_patterns.get(platform, ''), link or '')
    id_hint = m.group(0) if m else None

    candidates = []
    for root, dirs, files in os.walk(workdir):
        for f in files:
            if f.endswith('_result.json'):
                full = os.path.join(root, f)
                # 平台匹配：文件路径含平台ID特征
                if id_hint and id_hint in full:
                    candidates.append(full)
                elif platform == 'bilibili' and 'BV' in f:
                    candidates.append(full)
                elif platform == 'kuaishou' and 'ks_' in f:
                    candidates.append(full)
    if not candidates:
        return None
    # 取最新的
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def main():
    ap = argparse.ArgumentParser(description='四平台视频一键采集入库')
    ap.add_argument('link', help='视频链接')
    ap.add_argument('--update', action='store_true', help='链接已存在则覆盖更新')
    ap.add_argument('--no-insert', action='store_true', help='只采集，不入库')
    ap.add_argument('--workdir', default=config.WORKDIR, help='产物目录')
    ap.add_argument('--ws-url', help='手动指定浏览器 wsUrl（跳过自动检测）')
    args = ap.parse_args()

    link = args.link.strip()
    platform = detect_platform(link)
    if not platform:
        print(f'[FAIL] 无法识别平台: {link[:60]}')
        print('支持的平台: B站 / 抖音 / 小红书 / 快手')
        sys.exit(1)
    print(f'== 平台: {platform} ==')
    print(f'== 链接: {link[:90]} ==')

    # 构造子进程环境
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['SOCIAL_WORKDIR'] = args.workdir

    # 需要 wsUrl 的平台：自动检测（或 --ws-url 手动指定）
    ws_env_name = WS_ENV.get(platform)
    if ws_env_name:
        ws_url = args.ws_url or find_ws_url(platform)
        if ws_url:
            print(f'[OK] 浏览器 wsUrl: {ws_url[:60]}...')
            if platform == 'kuaishou':
                # 快手脚本 wsUrl 是第2个位置参数
                cmd = [config.PYTHON_BIN, PROCESS_SCRIPTS[platform], link, ws_url, args.workdir]
            else:
                env[ws_env_name] = ws_url
                cmd = [config.PYTHON_BIN, PROCESS_SCRIPTS[platform], link, args.workdir]
        else:
            print(f'[提示] 未检测到 {platform} 浏览器 tab')
            print(f'  {platform} 采集弹幕/评论需要浏览器 CDP。请先:')
            print('    1. openclaw browser open "<视频链接>"')
            print('    2. 重试本命令，或 --ws-url ws://... 手动指定')
            sys.exit(1)
    else:
        cmd = [config.PYTHON_BIN, PROCESS_SCRIPTS[platform], link, args.workdir]

    print(f'\n[1/2] 开始采集（{platform}）...')
    t0 = time.time()
    r = subprocess.run(cmd, env=env, cwd=os.path.dirname(PROCESS_SCRIPTS[platform]))
    if r.returncode != 0:
        print('[FAIL] 采集脚本异常退出')
        sys.exit(1)
    print(f'  采集耗时: {time.time() - t0:.0f}s')

    # 找 result.json
    result_file = find_result_file(platform, args.workdir, link)
    if not result_file:
        print('[FAIL] 未找到 result.json 产物')
        sys.exit(1)
    print(f'[OK] 数据契约: {result_file}')

    # 入库
    if args.no_insert:
        print('\n[2/2] 跳过入库（--no-insert）')
        print(f'产物目录: {os.path.dirname(result_file)}')
        sys.exit(0)

    print('\n[2/2] 入库 Notion...')
    insert_cmd = [config.RUNNER_PYTHON, os.path.join(SCRIPTS_DIR, 'insert_notion.py'), result_file]
    if args.update:
        insert_cmd.append('--update')
    r2 = subprocess.run(insert_cmd, env=env, cwd=SCRIPTS_DIR)
    if r2.returncode != 0:
        print('[FAIL] 入库失败')
        sys.exit(1)
    print('\n[DONE] 全部完成')


if __name__ == '__main__':
    main()
