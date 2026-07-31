#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过 GitHub Contents API 上传整个项目（绕过被 GFW 墙的 git 协议）
用法: python upload_via_api.py <repo_dir>
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request

REPO = 'jsheceghs-pixel/social-video-archive'
BRANCH = 'main'


def gh_api(method, path, data=None):
    """调用 gh api（走 api.github.com，直连可用）"""
    cmd = ['gh', 'api', path, '-X', method]
    if data is not None:
        cmd += ['--input', '-']
    p = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                       capture_output=True, text=True, encoding='utf-8')
    if p.returncode != 0:
        raise RuntimeError(f'gh api {method} {path} 失败: {p.stderr[-500:]}')
    if p.stdout.strip():
        return json.loads(p.stdout)
    return None


def upload_file(repo_path, rel_path):
    """上传单个文件（若已存在则更新）"""
    full = os.path.join(repo_path, rel_path)
    content = open(full, 'rb').read()
    b64 = base64.b64encode(content).decode()

    # 检查是否已存在（拿 sha）
    existing_sha = None
    try:
        info = gh_api('GET', f'/repos/{REPO}/contents/{rel_path}?ref={BRANCH}')
        if isinstance(info, dict) and info.get('sha'):
            existing_sha = info['sha']
    except RuntimeError:
        pass  # 404 = 不存在

    data = {
        'message': f'上传 {rel_path}',
        'content': b64,
        'branch': BRANCH,
    }
    if existing_sha:
        data['sha'] = existing_sha

    gh_api('PUT', f'/repos/{REPO}/contents/{rel_path}', data)
    print(f'  [OK] {rel_path}' + (' (更新)' if existing_sha else ''))


def main():
    repo_dir = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\Liyooo\social-video-archive'
    # 要上传的文件（相对路径）
    files = [
        'README.md',
        'config.example.json',
        '.gitignore',
        '工作流总览.html',
        'run.py',
        'upload_via_api.py',
        'scripts/config.py',
        'scripts/gen_srt.py',
        'scripts/bilibili_process.py',
        'scripts/douyin_process.py',
        'scripts/xiaohongshu_process.py',
        'scripts/kuaishou_process.py',
        'scripts/insert_notion.py',
        'scripts/douyin_fusion.js',
        'scripts/cdp_eval.js',
        'scripts/bili_dm_parse.py',
    ]
    print(f'上传 {len(files)} 个文件到 {REPO}@{BRANCH}...')
    for f in files:
        if os.path.exists(os.path.join(repo_dir, f)):
            upload_file(repo_dir, f)
        else:
            print(f'  [跳过] {f} 不存在')
    print('\n[DONE] 全部上传完成')


if __name__ == '__main__':
    main()
