/**
 * 抖音弹幕高能分析 v5（双通道选弹幕 + L1/L2/L3 内容对齐）
 * 输入: danmaku_<video_id>.json (弹幕) + test_<video_id>.srt (字幕)
 * 输出: <video_id>_AI_HIGHLIGHT.txt (高能片段 + 弹幕精华)
 *
 * v5 变更（2026-08-01 用户确认）：
 * 1. 双通道选弹幕（主框架，与 v4 相同）：
 *    - 通道1 🔥：密度高能段（30s桶前35%）→ 段内弹幕按赞取前5
 *    - 通道2 🔶：全视频高赞弹幕榜 → 按赞取前10
 * 2. 对齐引擎改为 L1/L2/L3（替换 v4 的"弹幕时刻±2.5s找块"，解决弹幕滞后错位）：
 *    - L1：弹幕 ≥2字连续子串出现在文案（窗口 [T-5s, T+25s] 内）→ 该句（多命中取时间最近）
 *    - L2：共同字比例 ≥55% → 该句（多命中取时间最近）
 *    - L3：都不中 → 输出整窗口 [T-5s, T+25s] 内全部文案
 * 3. 合并去重：同一文案块只输出一次，弹幕两边合并；按时间线排序
 *
 * 用法: node douyin_fusion.js <video_id>
 */
const fs = require('fs');
const path = require('path');

const BASE = __dirname;

// 文件定位：cwd 优先（仓库版由 douyin_process.py 以 cwd=out_dir 调用），
// 再回退 __dirname（workspace 独立跑时文件在脚本旁）
function resolveFile(name) {
  const cwdPath = path.join(process.cwd(), name);
  if (fs.existsSync(cwdPath)) return cwdPath;
  const basePath = path.join(BASE, name);
  if (fs.existsSync(basePath)) return basePath;
  return cwdPath; // 都不存在时返回 cwd（报错信息更直观）
}

// ====== 核心参数 ======
const TIME_WINDOW_SEC = 30;      // 30秒一个热力桶（通道1高能段判定）
const DENSITY_PERCENTILE = 0.35; // 保留弹幕最密集的前 35% 时段
const TOP_DM_PER_SEG = 5;        // 通道1：每高能段取高赞弹幕条数
const TOP_DM_GLOBAL = 10;        // 通道2：全视频高赞弹幕榜条数

// L1/L2/L3 对齐参数
const L1_MIN_SUBSTR = 2;         // L1：弹幕连续子串最小长度（字）
const L2_RATIO = 0.55;           // L2：共同字比例阈值
const ANCHOR_BEFORE_MS = 5000;   // 对齐窗口：弹幕时刻往前 5s
const ANCHOR_AFTER_MS = 25000;   // 对齐窗口：弹幕时刻往后 25s
const MAX_BLOCK_CHARS = 150;     // 低能段文案截断长度

// 垃圾词过滤（弹幕文本清洗）
const STOP_WORDS = new Set(['晚上好', '晚安', '来了', '打call', '拜拜', '卡了', '嗯', '好', '草', '哈哈', '确实', '牛', '可爱', '感谢观看', '谢谢观看']);
const FILLER_REGEX = /^(呃|那个|就是|然后|哪怕|其实|我觉得|算是|哎呀|有点|怎么说呢|所以|这种|啊|哦)+/g;
// 关键信息词（低能段若含这些词则完整保留，不截断）
const KEYWORD_REGEX = /总结|最后|结局|真相|关键|大结局/;

function parseSrtTimestamp(timeStr) {
  const match = timeStr.match(/(\d{2}):(\d{2}):(\d{2}),(\d{3})/);
  if (!match) return 0;
  const [_, h, m, s, ms] = match;
  return (parseInt(h) * 3600 + parseInt(m) * 60 + parseInt(s)) * 1000 + parseInt(ms);
}

function aggressiveClean(text) {
  if (!text) return '';
  return text.trim().replace(/(.)\1{2,}/g, '$1').replace(FILLER_REGEX, '')
    .replace(/（.*?）/g, '').replace(/\(.*?\)/g, '');
}

// 弹幕文本清洗：删 emoji 括号及其内容 + 标点，保留正文
function cleanDm(t) {
  return t
    .replace(/\[[^\]]*\]/g, '')
    .replace(/【[^】]*】/g, '')
    .replace(/\([^)]*\)/g, '')
    .replace(/（[^）]*）/g, '')
    .replace(/[《》]/g, '')
    .replace(/[～~！!。.，,？?、：:；;…]/g, '')
    .trim();
}

function formatSec(ms) {
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTimeLabel(ms) {
  return `[${Math.floor(ms / 60000)}m${String(Math.floor((ms % 60000) / 1000)).padStart(2, '0')}s]`;
}

// 弹幕的连续子串集合（>=minLen）
function substrings(s, minLen) {
  const set = new Set();
  for (let i = 0; i <= s.length - minLen; i++) {
    set.add(s.slice(i, i + minLen));
  }
  return set;
}

// ====== L1/L2/L3 对齐引擎 ======
// 输入: 弹幕去壳文本 dmText, 弹幕时刻 T, 字幕块列表 subtitles
// 输出: { level: 'L1'|'L2'|'L3', blocks: [块...] }
//   L1/L2 → 单个命中块；L3 → 窗口内全部块
function alignDanmaku(dmText, T, subtitles) {
  const winStart = T - ANCHOR_BEFORE_MS;
  const winEnd = T + ANCHOR_AFTER_MS;
  const winSubs = subtitles.filter(s => s.start < winEnd && s.end > winStart);
  if (winSubs.length === 0) return { level: 'L3', blocks: [] };

  // L1: >=2字连续子串精准匹配（多命中取时间最近）
  if (dmText.length >= L1_MIN_SUBSTR) {
    const subsSet = substrings(dmText, L1_MIN_SUBSTR);
    let best = null, bestDist = Infinity;
    for (const s of winSubs) {
      for (const sub of subsSet) {
        if (s.text.includes(sub)) {
          const dist = Math.abs(s.start - T);
          if (dist < bestDist) { bestDist = dist; best = s; }
          break;
        }
      }
    }
    if (best) return { level: 'L1', blocks: [best] };
  }

  // L2: 共同字比例 >= 55%（多命中取时间最近）
  const dmChars = [...new Set(dmText.split(''))];
  let best2 = null, best2Dist = Infinity;
  for (const s of winSubs) {
    const common = dmChars.filter(ch => s.text.includes(ch)).length;
    const ratio = common / dmText.length;
    if (ratio >= L2_RATIO) {
      const dist = Math.abs(s.start - T);
      if (dist < best2Dist) { best2Dist = dist; best2 = s; }
    }
  }
  if (best2) return { level: 'L2', blocks: [best2] };

  // L3: 输出整窗口文案（合并为一个虚拟块，避免弹幕重复挂到每个块）
  const winKey = winSubs.map(s => s.start).join('|');
  const mergedBlock = {
    start: winSubs[0].start,
    end: winSubs[winSubs.length - 1].end,
    text: winSubs.map(s => s.text).join('。'),
    l3Window: true,
    l3Subs: winSubs, // 记录窗口内包含的文案块（用于合并去重）
  };
  return { level: 'L3', blocks: [mergedBlock] };
}

function main() {
  const videoId = process.argv[2] || '7665048999434390793';
  const dmFile = resolveFile(`danmaku_${videoId}.json`);
  const srtFile = resolveFile(`test_${videoId}.srt`);
  const outFile = path.join(path.dirname(dmFile), `${videoId}_AI_HIGHLIGHT.txt`);

  if (!fs.existsSync(dmFile)) { console.error('弹幕文件不存在:', dmFile); return; }
  if (!fs.existsSync(srtFile)) { console.error('SRT文件不存在:', srtFile); return; }

  // --- 1. 解析弹幕 ---
  const dmData = JSON.parse(fs.readFileSync(dmFile, 'utf8'));
  const danmaku = dmData.danmaku || [];
  let maxDuration = 0;
  for (const d of danmaku) {
    if (d.offset_time > maxDuration) maxDuration = d.offset_time;
  }
  maxDuration = Math.max(maxDuration, 243067); // 视频时长兜底
  console.log(`💬 总弹幕数: ${danmaku.length}, 视频时长约 ${Math.floor(maxDuration / 60000)} 分钟`);

  // --- 2. 热力桶 + 纯密度阈值（通道1高能段判定） ---
  const windowMs = TIME_WINDOW_SEC * 1000;
  const totalBuckets = Math.ceil(maxDuration / windowMs) + 1;
  const densityArr = new Array(totalBuckets).fill(0);
  danmaku.forEach(d => {
    const idx = Math.floor(d.offset_time / windowMs);
    if (idx < totalBuckets) densityArr[idx]++;
  });
  const sortedDensity = [...densityArr].sort((a, b) => b - a);
  const thresholdIndex = Math.floor(totalBuckets * DENSITY_PERCENTILE);
  const thresholdCount = sortedDensity[thresholdIndex] || 1;
  console.log(`📊 热力阈值: 弹幕数 >= ${thresholdCount} 的桶为高能桶（纯密度判定）`);

  // --- 3. 高能桶合并为连续段落（通道1） ---
  const segments = []; // { start, end, isHigh }
  let cur = null;
  for (let i = 0; i < totalBuckets; i++) {
    const isHigh = densityArr[i] >= thresholdCount;
    if (isHigh) {
      if (!cur || !cur.isHigh) {
        cur = { start: i * windowMs, end: (i + 1) * windowMs, isHigh: true };
        segments.push(cur);
      } else {
        cur.end = (i + 1) * windowMs;
      }
    } else {
      if (cur && cur.isHigh) {
        cur = { start: i * windowMs, end: (i + 1) * windowMs, isHigh: false };
        segments.push(cur);
      } else if (cur) {
        cur.end = (i + 1) * windowMs;
      } else {
        cur = { start: i * windowMs, end: (i + 1) * windowMs, isHigh: false };
        segments.push(cur);
      }
    }
  }
  const highCount = segments.filter(s => s.isHigh).length;
  console.log(`🗂 段落切分: 共 ${segments.length} 段，其中高能段 ${highCount} 个`);

  // --- 4. 解析 SRT（保留每块的 start/end） ---
  const srtContent = fs.readFileSync(srtFile, 'utf8');
  const blocks = srtContent.split(/\n\s*\n/);
  const subtitles = []; // { start, end, text }
  for (const block of blocks) {
    const lines = block.split('\n').map(l => l.trim()).filter(l => l);
    if (lines.length < 3) continue;
    const timeLine = lines.find(l => l.includes('-->'));
    if (!timeLine) continue;
    const [startStr, endStr] = timeLine.split(' --> ');
    const start = parseSrtTimestamp(startStr);
    const end = parseSrtTimestamp(endStr);
    const rawText = lines.slice(lines.indexOf(timeLine) + 1).join('');
    const text = aggressiveClean(rawText);
    if (text.length < 2 || STOP_WORDS.has(text)) continue;
    subtitles.push({ start, end, text });
  }
  subtitles.sort((a, b) => a.start - b.start);

  // --- 5. 双通道选弹幕 + L1/L2/L3 对齐 ---
  // merged: Map<key, { blk, dms: Set, fromHigh: bool, fromTop: bool, level }>
  // 普通块 key = 块对象；L3 虚拟块 key = 'l3:'+start（窗口相同的弹幕共享一块）
  const merged = new Map();
  function addToBlock(blk, dm, fromHigh, level) {
    if (!blk) return;
    const key = blk.l3Window ? `l3:${blk.start}` : blk;
    if (!merged.has(key)) merged.set(key, { blk, dms: new Set(), fromHigh: false, fromTop: false, level });
    const entry = merged.get(key);
    entry.dms.add(dm);
    if (fromHigh) entry.fromHigh = true;
    else entry.fromTop = true;
    // 层级取更高优先级的（L1 > L2 > L3）
    const rank = { L1: 3, L2: 2, L3: 1 };
    if (rank[level] > rank[entry.level]) entry.level = level;
  }

  // 通道1：高能段内高赞弹幕 → 对齐
  for (const seg of segments) {
    if (!seg.isHigh) continue;
    const rangeDms = danmaku
      .filter(d => d.offset_time >= seg.start && d.offset_time < seg.end)
      .sort((a, b) => (b.digg_count || 0) - (a.digg_count || 0))
      .slice(0, TOP_DM_PER_SEG);
    for (const d of rangeDms) {
      const dmText = cleanDm(d.text);
      if (!dmText) continue;
      const res = alignDanmaku(dmText, d.offset_time, subtitles);
      for (const blk of res.blocks) addToBlock(blk, d, true, res.level);
    }
  }

  // 通道2：全视频高赞弹幕榜 → 对齐
  const topGlobal = danmaku
    .slice()
    .sort((a, b) => (b.digg_count || 0) - (a.digg_count || 0))
    .slice(0, TOP_DM_GLOBAL);
  for (const d of topGlobal) {
    const dmText = cleanDm(d.text);
    if (!dmText) continue;
    const res = alignDanmaku(dmText, d.offset_time, subtitles);
    for (const blk of res.blocks) addToBlock(blk, d, false, res.level);
  }

  // --- 6. 输出（去重合并后按时间线排序） ---
  const output = [];
  output.push('【抖音弹幕高能摘要 v5】双通道(密度高能段🔥+高赞榜🔶) + L1/L2/L3内容对齐');
  output.push(`视频ID: ${videoId} | 弹幕总数: ${danmaku.length} | 高能: 前${DENSITY_PERCENTILE*100}%桶×前${TOP_DM_PER_SEG} | 高赞榜: 前${TOP_DM_GLOBAL} | 对齐: L1子串≥${L1_MIN_SUBSTR}字 / L2共同字≥${Math.round(L2_RATIO*100)}% / L3整窗口`);
  output.push('---');

  const sortedEntries = [...merged.entries()].sort((a, b) => a[1].blk.start - b[1].blk.start);
  // 分区：L1/L2 命中的块进 🔥/🔶；L3 虚拟块统一进 L3 兜底区（避免重叠重复）
  const highL12 = [], topL12 = [], l3Blocks = [];
  for (const [, entry] of sortedEntries) {
    const blk = entry.blk;
    if (blk.l3Window) { l3Blocks.push([blk, entry]); continue; }
    if (entry.fromHigh && entry.fromTop) highL12.push([blk, entry]);
    else if (entry.fromHigh) highL12.push([blk, entry]);
    else topL12.push([blk, entry]);
  }

  const levelIcon = { L1: 'L1', L2: 'L2', L3: 'L3' };

  if (highL12.length > 0) {
    output.push('【🔥 密度高能段对应文案】');
    for (const [blk, entry] of highL12.sort((a, b) => a[0].start - b[0].start)) {
      const dms = [...entry.dms].sort((a, b) => (b.digg_count || 0) - (a.digg_count || 0));
      const dmsText = dms.map(d => `${d.text}(x${d.digg_count || 0}赞)@${formatSec(d.offset_time)}`).join(' / ');
      output.push(`  [${formatSec(blk.start)}-${formatSec(blk.end)}] 🔥[${levelIcon[entry.level]}] ${blk.text}  (💬 ${dmsText})`);
    }
  }

  if (topL12.length > 0) {
    output.push('【🔶 高赞弹幕榜补漏（低能段）】');
    for (const [blk, entry] of topL12.sort((a, b) => a[0].start - b[0].start)) {
      const dms = [...entry.dms].sort((a, b) => (b.digg_count || 0) - (a.digg_count || 0));
      const dmsText = dms.map(d => `${d.text}(x${d.digg_count || 0}赞)@${formatSec(d.offset_time)}`).join(' / ');
      output.push(`  [${formatSec(blk.start)}-${formatSec(blk.end)}] 🔶[${levelIcon[entry.level]}] ${blk.text}  (💬 ${dmsText})`);
    }
  }

  // L3 兜底区：按起始时间排序，窗口重叠的合并（按包含的文案块去重拼接）
  if (l3Blocks.length > 0) {
    // 已被 L1/L2 精准命中的文案块（按 start 记录），L3 窗口要剔除它们，避免重复输出
    const l12Starts = new Set();
    for (const [, entry] of merged) {
      if (!entry.blk.l3Window) l12Starts.add(entry.blk.start);
    }
    l3Blocks.sort((a, b) => a[0].start - b[0].start);
    const mergedL3 = []; // { blk, dms:Set, fromHigh, fromTop }
    for (const [blk, entry] of l3Blocks) {
      // 剔除已被 L1/L2 命中的块
      const keptSubs = blk.l3Subs.filter(s => !l12Starts.has(s.start));
      if (keptSubs.length === 0) {
        // 窗口内全部被 L1/L2 覆盖 → 该 L3 弹幕无需输出（已有精准对应）
        continue;
      }
      const keptBlk = { ...blk, l3Subs: keptSubs, start: keptSubs[0].start, end: keptSubs[keptSubs.length - 1].end, text: keptSubs.map(s => s.text).join('。') };
      const last = mergedL3[mergedL3.length - 1];
      if (last && keptBlk.start <= last.blk.end) {
        // 重叠 → 合并：扩展范围，文案块按 start 去重拼接
        last.blk.end = Math.max(last.blk.end, keptBlk.end);
        const seen = new Set(last.blk.l3Subs.map(s => s.start));
        for (const s of keptBlk.l3Subs) {
          if (!seen.has(s.start)) {
            last.blk.l3Subs.push(s);
            seen.add(s.start);
          }
        }
        last.blk.l3Subs.sort((a, b) => a.start - b.start);
        last.blk.text = last.blk.l3Subs.map(s => s.text).join('。');
        for (const dm of entry.dms) last.dms.add(dm);
        last.fromHigh = last.fromHigh || entry.fromHigh;
        last.fromTop = last.fromTop || entry.fromTop;
      } else {
        mergedL3.push({ blk: keptBlk, dms: new Set(entry.dms), fromHigh: entry.fromHigh, fromTop: entry.fromTop });
      }
    }
    if (mergedL3.length > 0) {
      output.push('【L3 兜底窗口（无文字对应，输出弹幕时刻附近整段文案）】');
      for (const item of mergedL3) {
        const dms = [...item.dms].sort((a, b) => (b.digg_count || 0) - (a.digg_count || 0));
        const dmsText = dms.map(d => `${d.text}(x${d.digg_count || 0}赞)@${formatSec(d.offset_time)}`).join(' / ');
        const tag = item.fromHigh && item.fromTop ? '🔥🔶' : item.fromHigh ? '🔥' : '🔶';
        output.push(`  [${formatSec(item.blk.start)}-${formatSec(item.blk.end)}] ${tag}[L3] ${item.blk.text}  (💬 ${dmsText})`);
      }
    }
  }

  // 低能段衔接（未命中任何通道的中间段落，精简保留）
  for (const seg of segments) {
    if (seg.isHigh) continue;
    const segSubs = subtitles.filter(s => s.start >= seg.start && s.start < seg.end);
    if (segSubs.length === 0) continue;
    const covered = segSubs.filter(s => merged.has(s));
    if (covered.length === segSubs.length) continue;
    const body = segSubs.filter(s => !merged.has(s)).map(s => s.text).join('。');
    if (!body) continue;
    const hasKeyword = KEYWORD_REGEX.test(body);
    const short = (!hasKeyword && body.length > MAX_BLOCK_CHARS)
      ? body.slice(0, MAX_BLOCK_CHARS) + '……'
      : body;
    output.push(`▫️ ${formatTimeLabel(seg.start)}-${formatTimeLabel(seg.end - 1000)} ${short}`);
  }

  fs.writeFileSync(outFile, output.join('\n'), 'utf8');
  const size = (fs.statSync(outFile).size / 1024).toFixed(1);
  console.log(`\n✅ 高能摘要生成: ${outFile}`);
  console.log(`📦 大小: ${size}KB`);
  console.log('\n===== 摘要内容 =====');
  console.log(output.join('\n'));
}

main();
