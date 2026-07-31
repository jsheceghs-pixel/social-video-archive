#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一配置模块（数据契约体系）
所有脚本的路径/凭据从这里读取，支持环境变量覆盖。

环境变量（可选，不设则用默认值）:
  SOCIAL_WORKDIR   产物输出目录（默认 ./output）
  PYTHON_BIN       带 FunASR 的 Python 解释器
  GEN_SRT_PATH     gen_srt.py 路径（FunASR→SRT）
  FUSION_PATH      弹幕融合脚本路径（B站 do_fusion_summary.js / 抖音 douyin_fusion.js）
  FFMPEG_BIN       ffmpeg 可执行文件
  CDP_EVAL_JS      cdp_eval.js 路径（浏览器 CDP 执行辅助）
  SOCIALKIT_DIR    social-media-toolkit 项目目录（小红书脚本用）
  DOUYIN_COOKIES   douyin_cookies.json 路径（抖音脚本用）
  NOTION_TOKEN     Notion API Token（insert_notion.py 用）
"""
import json
import os
import sys

# ---- 项目根目录（本文件所在目录的上级）----
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_or_default(key, default):
    return os.environ.get(key, default)


# ---- 产物输出目录 ----
WORKDIR = _env_or_default('SOCIAL_WORKDIR', os.path.join(PROJECT_ROOT, 'output'))

# ---- 外部工具路径（按需覆盖）----
# 带 FunASR 的 Python（采集/ASR 用）
PYTHON_BIN = _env_or_default(
    'PYTHON_BIN',
    r'D:\social-media-toolkit\.venv\Scripts\python.exe',
)
# 轻量 Python（run.py 调度用，无 FunASR 依赖也行）
RUNNER_PYTHON = _env_or_default('RUNNER_PYTHON', sys.executable)
GEN_SRT_PATH = _env_or_default(
    'GEN_SRT_PATH',
    os.path.join(PROJECT_ROOT, 'scripts', 'gen_srt.py'),  # 项目自带副本（自包含）
)
FUSION_PATH = _env_or_default(
    'FUSION_PATH',
    os.path.join(PROJECT_ROOT, 'scripts', 'douyin_fusion.js'),
)
FFMPEG_BIN = _env_or_default('FFMPEG_BIN', r'C:\Users\Liyooo\ffmpeg\bin\ffmpeg.exe')
CDP_EVAL_JS = _env_or_default(
    'CDP_EVAL_JS',
    os.path.join(PROJECT_ROOT, 'scripts', 'cdp_eval.js'),
)
CDP_NAVIGATE_JS = _env_or_default(
    'CDP_NAVIGATE_JS',
    os.path.join(PROJECT_ROOT, 'scripts', 'cdp_navigate.js'),
)
SOCIALKIT_DIR = _env_or_default('SOCIALKIT_DIR', r'D:\social-media-toolkit')

# ---- 凭据（不提交到仓库，见 config.example.json / .gitignore）----
DOUYIN_COOKIES = _env_or_default(
    'DOUYIN_COOKIES',
    os.path.join(PROJECT_ROOT, 'config', 'douyin_cookies.json'),
)
# 本机兼容：项目 config/ 下没有 cookie 时，回退到 OpenClaw workspace 的 cookie（迁移期）
if not os.path.exists(DOUYIN_COOKIES):
    _ws_cookies = os.path.expandvars(r'%USERPROFILE%\.openclaw\workspace\douyin\douyin_cookies.json')
    if os.path.exists(_ws_cookies):
        DOUYIN_COOKIES = _ws_cookies
NOTION_TOKEN = os.environ.get('NOTION_TOKEN', '')

# 本地敏感配置（config/secret.json，gitignore 排除，不提交仓库）
_SECRET_FILE = os.path.join(PROJECT_ROOT, 'config', 'secret.json')
if not NOTION_TOKEN and os.path.exists(_SECRET_FILE):
    try:
        with open(_SECRET_FILE, encoding='utf-8') as _f:
            _secret = json.load(_f)
        NOTION_TOKEN = _secret.get('NOTION_TOKEN', '') or NOTION_TOKEN
    except Exception:
        pass

# B站弹幕融合（danmaku-to-summary-ts 项目内）
BILI_FUSION_PATH = _env_or_default(
    'BILI_FUSION_PATH',
    r'D:\danmaku-to-summary-ts-main\src\scripts\do_fusion_summary.js',
)

# Notion 存档记录 data_source
DATA_SOURCE_ID = '3ab168dd-a52c-813a-aa80-000ba105b057'
