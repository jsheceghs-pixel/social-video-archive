/**
 * 抖音弹幕高能分析（对标 B站 do_fusion_summary.js）
 * 输入: danmaku_<video_id>.json (弹幕) + test_<video_id>.srt (字幕)
 * 输出: <video_id>_AI_HIGHLIGHT.txt (高能片段 + 弹幕精华)
 * 
 * 用法: node douyin_fusion.js <video_id>
 */
const fs = require('fs');
const path = require('path');

// ====== 核心参数（与 B站版对齐）======
const TIME_WINDOW_SEC = 30;      // 30秒一个热力桶
const DENSITY_PERCENTILE = 0.35; // 保留弹幕最密集的前 35% 时段
const LOW_ENERGY_SAMPLE_RATE = 0.1; // 低能区随机保留 10% 字幕

// 垃圾词过滤
const STOP_WORDS = new Set(['晚上好', '晚安', '来了', '打call', '拜拜', '卡了', '嗯', '好', '草', '哈哈', '确实', '牛', '可爱', '感谢观看', '谢谢观看']);
const FILLER_REGEX = /^(呃|那个|就是|然后|哪怕|其实|我觉得|算是|哎呀|有点|怎么说呢|所以|这种|啊|哦)+/g;

const BASE = __dirname;

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

function main() {
  const videoId = process.argv[2] || '7665048999434390793';
  const dmFile = path.join(BASE, `danmaku_${videoId}.json`);
  const srtFile = path.join(BASE, `test_${videoId}.srt`);
  const outFile = path.join(BASE, `${videoId}_AI_HIGHLIGHT.txt`);

  if (!fs.existsSync(dmFile)) { console.error('弹幕文件不存在:', dmFile); return; }
  if (!fs.existsSync(srtFile)) { console.error('SRT文件不存在:', srtFile); return; }

  // --- 1. 解析弹幕 ---
  const dmData = JSON.parse(fs.readFileSync(dmFile, 'utf8'));
  const danmaku = dmData.danmaku || [];
  let maxDuration = 0;
  for (const d of danmaku) {
    if (d.offset_time > maxDuration) maxDuration = d.offset_time;
  }
  // 用视频时长兜底
  maxDuration = Math.max(maxDuration, 243067);
  console.log(`💬 总弹幕数: ${danmaku.length}, 视频时长约 ${Math.floor(maxDuration / 60000)} 分钟`);

  // --- 2. 热力桶 ---
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
  console.log(`📊 热力阈值: 弹幕数 >= ${thresholdCount} 的时段为高能区`);

  // 输出热力分布
  console.log('📈 各时段弹幕密度:');
  densityArr.forEach((cnt, i) => {
    const s = i * TIME_WINDOW_SEC;
    const bar = '█'.repeat(Math.min(cnt, 30));
    if (cnt >= thresholdCount) console.log(`  [${Math.floor(s/60)}m${String(s%60).padStart(2,'0')}s] ${String(cnt).padStart(3)} ${bar} 🔥`);
  });

  // --- 3. 解析 SRT ---
  const srtContent = fs.readFileSync(srtFile, 'utf8');
  const blocks = srtContent.split(/\n\s*\n/);
  const subtitles = [];
  for (const block of blocks) {
    const lines = block.split('\n').map(l => l.trim()).filter(l => l);
    if (lines.length < 3) continue;
    const timeLine = lines.find(l => l.includes('-->'));
    if (!timeLine) continue;
    const [startStr] = timeLine.split(' --> ');
    const ms = parseSrtTimestamp(startStr);
    const rawText = lines.slice(lines.indexOf(timeLine) + 1).join('');
    const text = aggressiveClean(rawText);
    if (text.length < 2 || STOP_WORDS.has(text)) continue;

    const bucketIdx = Math.floor(ms / windowMs);
    const currentDensity = densityArr[bucketIdx] || 0;
    const isHighEnergy = currentDensity >= thresholdCount;
    const isKeyword = /总结|最后|结局|真相|关键|大结局/.test(text);
    if (isHighEnergy || isKeyword || Math.random() < LOW_ENERGY_SAMPLE_RATE) {
      subtitles.push({ ms, text, isHighEnergy });
    }
  }
  subtitles.sort((a, b) => a.ms - b.ms);

  // --- 4. 聚合输出 ---
  const output = [];
  output.push(`【抖音弹幕高能摘要】(保留率: 前${DENSITY_PERCENTILE * 100}%热度 + ${LOW_ENERGY_SAMPLE_RATE * 100}%随机)`);
  output.push(`视频ID: ${videoId} | 弹幕总数: ${danmaku.length}`);
  output.push(`---`);

  let currentBlock = { startTime: -1, lines: [], isHighlight: false };

  const flushBlock = () => {
    if (currentBlock.lines.length === 0) return;
    const timeLabel = `[${Math.floor(currentBlock.startTime / 60000)}m${String(Math.floor((currentBlock.startTime % 60000) / 1000)).padStart(2, '0')}s]`;
    const icon = currentBlock.isHighlight ? '🔥' : '▫️';
    const body = currentBlock.lines.join('。');

    // 该时段精华弹幕（按点赞排序）
    const sTime = currentBlock.startTime;
    const eTime = currentBlock.startTime + (TIME_WINDOW_SEC * 1000 * 2);
    const rangeDms = danmaku
      .filter(d => d.offset_time >= sTime && d.offset_time < eTime)
      .sort((a, b) => (b.digg_count || 0) - (a.digg_count || 0));

    const topDm = rangeDms.slice(0, 5)
      .map(d => `${d.text}(x${d.digg_count || 0}赞)`)
      .join(' / ');

    let finalLine = `${timeLabel} ${icon} ${body}`;
    if (topDm) finalLine += `  (💬 ${topDm})`;
    output.push(finalLine);
    currentBlock = { startTime: -1, lines: [], isHighlight: false };
  };

  for (const sub of subtitles) {
    if (currentBlock.startTime !== -1 && (sub.ms - currentBlock.lastMs > 60000)) flushBlock();
    if (currentBlock.startTime === -1) {
      currentBlock.startTime = sub.ms;
      currentBlock.isHighlight = sub.isHighEnergy;
    }
    currentBlock.lines.push(sub.text);
    currentBlock.lastMs = sub.ms;
    if (currentBlock.lines.join('').length > 150) flushBlock();
  }
  flushBlock();

  fs.writeFileSync(outFile, output.join('\n'), 'utf8');
  const size = (fs.statSync(outFile).size / 1024).toFixed(1);
  console.log(`\n✅ 高能摘要生成: ${outFile}`);
  console.log(`📦 大小: ${size}KB`);
  console.log('\n===== 摘要内容 =====');
  console.log(output.join('\n'));
}

main();
