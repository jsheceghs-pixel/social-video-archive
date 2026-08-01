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
    """通过 CDP /json 端点检测对应平台 tab 的 wsUrl（不依赖 openclaw 命令）"""
    domain = WS_DOMAIN.get(platform)
    if not domain:
        return None
    # 浏览器 CDP 调试端口（openclaw browser 固定 18800）
    for port in (18800, 9222, 9223):
        try:
            r = subprocess.run(
                ['curl.exe', '-s', '--max-time', '5', f'http://127.0.0.1:{port}/json'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            data = json.loads(r.stdout or '[]')
            for tab in data:
                if tab.get('type') == 'page' and domain in tab.get('url', ''):
                    ws = tab.get('webSocketDebuggerUrl')
                    if ws:
                        return ws
        except Exception:
            continue
    return None


def create_tab(url=None):
    """创建独立采集 tab（不干扰用户正在看的页面），返回 (tabId, wsUrl)
    用 about:blank 创建，实际导航由各 process 脚本的 cdp_navigate 完成（避免 URL 编码/JS 问题）"""
    target = url or 'about:blank'
    import urllib.parse
    enc = urllib.parse.quote(target, safe=':/?&=%')
    for port in (18800, 9222, 9223):
        try:
            r = subprocess.run(
                ['curl.exe', '-s', '-X', 'PUT', '--max-time', '10', f'http://127.0.0.1:{port}/json/new?{enc}'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            j = json.loads(r.stdout or '{}')
            if j.get('webSocketDebuggerUrl'):
                return j.get('id'), j.get('webSocketDebuggerUrl')
        except Exception:
            continue
    return None, None

def resolve_shortlink(url):
    """解析短链/分享链接（v.douyin.com / v.kuaishou.com / kuaishou.com/f/ 等），返回跳转后的最终 URL"""
    # 短链域名 + 快手 /f/ 分享路径（跳转后才是 short-video 详情页）
    short_domains = ('v.douyin.com', 'v.kuaishou.com', 'xhslink.com', 'b23.tv', 'kuaishou.com/f/', 'kuaishou.com/fw/')
    if not any(d in (url or '') for d in short_domains):
        return url
    print(f'[短链] 解析跳转...')
    try:
        r = subprocess.run(
            ['curl.exe', '-s', '-L', '-o', 'NUL', '-w', '%{url_effective}', '--max-time', '30', url,
             '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
        final = r.stdout.strip()
        if final:
            print(f'  跳转至: {final[:90]}')
            return final
    except Exception as e:
        print(f'  [提示] 短链解析失败: {e}')
    return url


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
    ap.add_argument('--quiet', action='store_true', help='精简输出（只显示关键结果，日志落盘）')
    args = ap.parse_args()

    def say(msg):
        """quiet 模式只显示关键行（以 [OK]/[DONE]/[FAIL]/== 开头）"""
        if not args.quiet:
            print(msg)
        elif msg.startswith(('[OK]', '[DONE]', '[FAIL]', '==', '  采集耗时', '   页面:', '   椤甸潰:', '   鏍囬')):
            print(msg)

    link = args.link.strip()
    platform = detect_platform(link)
    if not platform:
        print(f'[FAIL] 无法识别平台: {link[:60]}')
        print('支持的平台: B站 / 抖音 / 小红书 / 快手')
        sys.exit(1)
    say(f'== 平台: {platform} ==')
    say(f'== 链接: {link[:90]} ==')

    # 短链解析（v.douyin.com / v.kuaishou.com / xhslink.com / b23.tv）
    link = resolve_shortlink(link)

    # 构造子进程环境
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['SOCIAL_WORKDIR'] = args.workdir

    # 日志文件（quiet 模式落盘用；默认也写，方便排查）
    log_dir = os.path.join(args.workdir, '_logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'run_{platform}_{int(time.time())}.log')
    log_fh = open(log_file, 'w', encoding='utf-8')

    def run_captured(cmd, env_extra=None):
        """运行子进程：默认透传输出；quiet 模式重定向到日志文件。返回 (returncode, 日志内容)"""
        e = dict(env)
        if env_extra:
            e.update(env_extra)
        if args.quiet:
            r = subprocess.run(cmd, env=e, stdout=log_fh, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
            return r.returncode, ''
        else:
            r = subprocess.run(cmd, env=e)
            return r.returncode, ''

    # 需要 wsUrl 的平台：优先创建独立采集 tab（不干扰用户浏览），失败才回退现有 tab
    ws_env_name = WS_ENV.get(platform)
    if ws_env_name:
        ws_url = args.ws_url
        tab_id = None
        if not ws_url:
            say('[浏览器] 创建独立采集 tab（不干扰你正在看的页面）...')
            tab_id, ws_url = create_tab(link)
        if not ws_url:
            say('  [回退] 新 tab 创建失败，尝试使用现有 tab...')
            ws_url = find_ws_url(platform)
        if ws_url:
            say(f'[OK] 浏览器 wsUrl: {ws_url[:60]}...')
            if platform == 'kuaishou':
                # 快手脚本 wsUrl 是第2个位置参数
                cmd = [config.PYTHON_BIN, PROCESS_SCRIPTS[platform], link, ws_url, args.workdir]
            else:
                env[ws_env_name] = ws_url
                cmd = [config.PYTHON_BIN, PROCESS_SCRIPTS[platform], link, args.workdir]
        else:
            say(f'[提示] 未检测到 {platform} 浏览器 tab')
            say(f'  {platform} 采集弹幕/评论需要浏览器 CDP。请先:')
            say('    1. openclaw browser open "<视频链接>"')
            say('    2. 重试本命令，或 --ws-url ws://... 手动指定')
            log_fh.close()
            sys.exit(1)
    else:
        cmd = [config.PYTHON_BIN, PROCESS_SCRIPTS[platform], link, args.workdir]

    say(f'\n[1/2] 开始采集（{platform}）...')
    t0 = time.time()
    rc, _ = run_captured(cmd)
    if rc != 0:
        print('[FAIL] 采集脚本异常退出')
        print(f'  日志: {log_file}')
        # 失败时打印日志尾部（诊断用）
        log_fh.flush()
        with open(log_file, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        print('  --- 日志尾部 ---')
        print(''.join(lines[-25:]))
        log_fh.close()
        sys.exit(1)
    say(f'  采集耗时: {time.time() - t0:.0f}s')

    # 找 result.json
    result_file = find_result_file(platform, args.workdir, link)
    if not result_file:
        print('[FAIL] 未找到 result.json 产物')
        print(f'  日志: {log_file}')
        log_fh.close()
        sys.exit(1)
    say(f'[OK] 数据契约: {result_file}')

    # 入库
    if args.no_insert:
        say('\n[2/2] 跳过入库（--no-insert）')
        say(f'产物目录: {os.path.dirname(result_file)}')
        log_fh.close()
        sys.exit(0)

    say('\n[2/2] 入库 Notion...')
    insert_cmd = [config.RUNNER_PYTHON, os.path.join(SCRIPTS_DIR, 'insert_notion.py'), result_file]
    if args.update:
        insert_cmd.append('--update')
    rc2, _ = run_captured(insert_cmd)
    if rc2 != 0:
        print('[FAIL] 入库失败')
        print(f'  日志: {log_file}')
        log_fh.flush()
        with open(log_file, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        print('  --- 日志尾部 ---')
        print(''.join(lines[-25:]))
        log_fh.close()
        sys.exit(1)
    say('\n[DONE] 全部完成')
    # quiet 模式下入库结果在日志文件里，提取 Notion 链接显示
    if args.quiet:
        log_fh.flush()
        with open(log_file, encoding='utf-8', errors='replace') as f:
            content = f.read()
        m = re.search(r'页面: (https://app\.notion\.com/[^\s]+)', content)
        if m:
            print(f'[OK] Notion: {m.group(1)}')
        elif 'HTTP_STATUS:200' in content:
            print('[OK] 已写入 Notion（页面链接见日志）')
    log_fh.close()


if __name__ == '__main__':
    main()
