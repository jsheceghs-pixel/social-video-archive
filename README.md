# Social Video Archive · 四平台视频工作流

B站 / 抖音 / 小红书 / 快手 视频 → 元数据 + ASR全文 + SRT字幕 + 弹幕高能分析 + 评论 → **数据契约 JSON** → 统一入库 **Notion「存档记录」**

## ✨ 能力总览

| 平台 | 脚本 | 元数据 | 音频/视频 | ASR→SRT | 弹幕 | 弹幕高能 | 评论 | 备注 |
|---|---|---|---|---|---|---|---|---|
| B站 | `bilibili_process.py` | ✅ 官方API | ✅ 音频直链 | ✅ FunASR | ✅ XML | ✅ fusion | ✅ API | 官方API直连，无需登录 |
| 抖音 | `douyin_process.py` | ✅ web API | ✅ 音频直链 | ✅ FunASR | ✅ CDP | ✅ fusion | ✅ CDP | 需 cookie + 浏览器CDP |
| 小红书 | `xiaohongshu_process.py` | ✅ socialkit | ✅ 视频直链 | ✅ FunASR | ❌ | ❌ | ✅ SSR | 仅视频笔记，链接需 xsec_token |
| 快手 | `kuaishou_process.py` | ✅ Apollo CDP | ✅ curl视频 | ✅ FunASR | ❌ | ❌ | ✅ CDP | 需浏览器CDP，urllib被拒 |

> 弹幕能力：B站/抖音有历史弹幕（带时间轴）；快手/小红书网页版确认无弹幕接口（弹幕锁在 App 内）。

## 🏗️ 架构（数据契约模式）

```
用户发链接
  ├─ bilibili_process.py    ─┐
  ├─ douyin_process.py      ─┤  每个平台产出 <id>_result.json
  ├─ xiaohongshu_process.py ─┤  （标准数据契约）
  └─ kuaishou_process.py    ─┘
                              ↓
                   insert_notion.py 统一入库
              （按链接去重 · 平台名中文映射 · 正文分块）
                              ↓
                  Notion「存档记录」(data_source)
```

**数据契约字段**：`platform / url / title / author / publish_time / duration_sec / metrics / asr_text / srt / danmaku_xml / danmaku_list / highlight / comments / tags / workdir`

## 🚀 快速开始

### 1. 环境准备

```bash
# Python 环境（需含 funasr）
pip install funasr          # 或使用已有 venv

# 外部依赖（路径可在 config.py / 环境变量覆盖）
# - gen_srt.py        FunASR→SRT（来自 danmaku-to-summary-ts 项目）
# - do_fusion_summary.js  B站弹幕融合（同上）
# - ffmpeg            音频/视频处理
# - node + ws 包      浏览器CDP辅助（cdp_eval.js）
# - social-media-toolkit  小红书元数据（可选）
```

### 2. 配置

复制并填写（**不提交到仓库**）：

```bash
cp config.example.json config/  # 见下方示例
export NOTION_TOKEN=ntn_xxx     # Notion API Token（必需）
```

### 3. 运行

**方式一：一键调度（推荐，不依赖对话）**

```bash
# B站（无需浏览器）
python run.py "https://www.bilibili.com/video/BV1xxxx"

# 抖音（自动检测浏览器 tab；需先 openclaw browser open 视频页）
python run.py "https://www.douyin.com/video/7xxx"

# 小红书（链接需带有效 xsec_token）
python run.py "https://www.xiaohongshu.com/explore/xxx?xsec_token=..."

# 快手（需先 openclaw browser open 详情页）
python run.py "https://www.kuaishou.com/short-video/5xxx"

# 可选参数
python run.py <链接> --update      # 已存在则覆盖更新（默认跳过）
python run.py <链接> --no-insert   # 只采集不入库
python run.py <链接> --ws-url ws://...  # 手动指定浏览器 wsUrl
```

自动流程：识别平台 → 调对应 process 脚本 → 产出 result.json → 入库 → 返回 Notion 链接。

**方式二：分步执行**

```bash
# 采集（各平台独立脚本）
python scripts/bilibili_process.py "https://www.bilibili.com/video/BV1xxxx"
# 入库
python scripts/insert_notion.py output/<id>/<id>_result.json [--update]
```

产物输出到 `output/<id>/`，数据契约在 `output/<id>/<id>_result.json`。

## ⚙️ 配置项（config.py / 环境变量）

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `SOCIAL_WORKDIR` | 产物输出目录 | `./output` |
| `PYTHON_BIN` | FunASR Python 解释器 | 本地默认 |
| `RUNNER_PYTHON` | run.py 调度用轻量解释器 | `sys.executable` |
| `GEN_SRT_PATH` | gen_srt.py 路径 | 本地默认 |
| `BILI_FUSION_PATH` | B站 do_fusion_summary.js | 本地默认 |
| `FUSION_PATH` | 抖音 douyin_fusion.js | `./scripts/douyin_fusion.js` |
| `FFMPEG_BIN` | ffmpeg 可执行文件 | 本地默认 |
| `CDP_EVAL_JS` | cdp_eval.js | `./scripts/cdp_eval.js` |
| `SOCIALKIT_DIR` | social-media-toolkit 目录 | 本地默认 |
| `DOUYIN_COOKIES` | douyin_cookies.json 路径 | `./config/douyin_cookies.json` |
| `NOTION_TOKEN` | Notion API Token | 空（必需） |

## 🔌 扩展新平台（如将来快手弹幕）

1. 新建 `xxx_process.py`，采集后输出**同款数据契约 JSON**
2. `insert_notion.py` 无需改动（按 `platform` 字段自动映射中文名）
3. 若新平台有弹幕：`danmaku_xml` / `danmaku_list` / `highlight` 字段直接生效

## 📌 已知优化点

- 抖音/快手依赖浏览器 CDP（弹幕/评论需签名）→ 可研究 a_bogus / Apollo 签名生成实现纯 API
- 小红书链接的 xsec_token 有时效（过期 404）→ 需用户重新复制链接
- FunASR 模型每次加载约 30s → 可常驻服务 + GPU 加速
- 各平台脚本需手动传 wsUrl → 可加统一调度入口（丢链接自动识别平台）

## 🔒 安全

- `config/`、`*.json`（含 cookie/token 的）已在 `.gitignore` 排除，**不要提交真实凭据**
- 抖音 cookie 有效期约 60 天，过期后需重新导出浏览器 cookie 生成 `douyin_cookies.json`

---

工作流总览图见 [工作流总览.html](工作流总览.html)（浏览器打开）。
