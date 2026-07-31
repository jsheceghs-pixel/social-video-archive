"""
FunASR 时间戳分段 → 精准 SRT 生成
支持 Paraformer-large 输出格式: 
  text=完整带标点文本, timestamp=[[start_ms,end_ms], ...] (逐token时间戳)
策略:
  1. 按标点 。！？ 断句
  2. 推算每个句子的起止时间 (利用timestamp映射)
  3. 输出标准 SRT
"""
import sys, os, re, json
import time as time_module

os.environ["PATH"] = r"C:\Users\Liyooo\ffmpeg\bin" + os.pathsep + os.environ.get("PATH", "")
os.environ["FFMPEG_BIN"] = r"C:\Users\Liyooo\ffmpeg\bin\ffmpeg.exe"

def to_srt(ms):
    h = int(ms // 3600000)
    m = int((ms % 3600000) // 60000)
    s = int((ms % 60000) // 1000)
    ms_remain = int(ms % 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms_remain:03d}"

def srt_format(segments, output_path=None):
    """segments: [(start_ms, end_ms, text), ...]"""
    lines = []
    for i, (start, end, text) in enumerate(segments, 1):
        text = text.strip()
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{to_srt(start)} --> {to_srt(end)}")
        lines.append(text)
        lines.append("")
    result = "\n".join(lines)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"[OK] SRT saved: {output_path}")
    return result


def extract_sentences(text):
    """Split text into sentences by 。！？"""
    # Keep delimiter attached to preceding sentence
    parts = re.split(r'(?<=[。！？.!?])', text)
    sentences = [p.strip() for p in parts if p.strip()]
    if not sentences:
        sentences = [text.strip()]
    return sentences


def text_to_timestamp_mapping(text, timestamps):
    """
    将文本按token数量比例映射到timestamps上
    每个timestamp token对应文本中的若干字符
    text: "这才是伍六七系列。接下来的重磅..."
    timestamps: [[110,230], [230,410], ...]
    
    返回: tokens_text, tokens[0].start, tokens[0].end
    """
    # Count non-space characters in text
    chars = list(text)
    n_chars = len(chars)
    n_ts = len(timestamps)
    
    if n_ts == 0:
        return [(0, 0, text)]
    
    # Map: distribute characters across timestamps roughly equally
    # Each timestamp token covers ceil(n_chars/n_ts) chars
    char_per_token = max(1, n_chars // n_ts)
    
    result = []
    for i, ts in enumerate(timestamps):
        start = int(ts[0])
        end = int(ts[1])
        char_start = i * char_per_token
        char_end = min(char_start + char_per_token, n_chars)
        char_slice = chars[char_start:char_end]
        result.append((start, end, "".join(char_slice)))
    
    return result


def build_sentence_timestamps(text, timestamps):
    """
    核心: 将文本分句 + 推算每个句子的起止时间
    """
    if not text or not timestamps:
        if text:
            return [(0, 0, text.strip())]
        return []
    
    # Map tokens to characters
    token_texts = text_to_timestamp_mapping(text, timestamps)
    
    # Find sentence boundaries in the token-text mapping
    sentences = []
    current_chars = []
    current_start = None
    
    for start, end, chars in token_texts:
        if current_start is None:
            current_start = start
        current_chars.append(chars)
        current_end = end
        
        # Check if any character in this token is a sentence-ending punctuation
        if any(c in "。！？.!?" for c in chars):
            full = "".join(current_chars)
            # Maybe split at exact punctuation position
            idx = -1
            for c in "。！？.!?":
                pos = full.rfind(c)
                if pos > idx:
                    idx = pos
            
            if idx >= 0:
                sentence = full[:idx+1].strip()
                if sentence:
                    # For the last timestamp, we need to look at punctuation position
                    # Estimate: find proportion of chars before punctuation
                    sentences.append((current_start, current_end, sentence))
                
                # Continue with remaining chars (after punctuation)
                remaining = full[idx+1:].strip()
                if remaining:
                    current_chars = [remaining]
                    current_start = current_end  # approximate
                else:
                    current_chars = []
                    current_start = None
            else:
                pass  # no punct found, keep accumulating
        elif len("".join(current_chars)) >= 80:
            # Force break on long text
            full = "".join(current_chars).strip()
            if full:
                sentences.append((current_start, current_end, full))
            current_chars = []
            current_start = None
    
    # Remaining text
    if current_chars:
        text = "".join(current_chars).strip()
        if text:
            sentences.append((current_start, current_end, text))
    
    # If no sentences were formed (shouldn't happen), use full text
    if not sentences:
        sentences = [(int(timestamps[0][0]), int(timestamps[-1][1]), text.strip())]
    
    return sentences


def process_audio(audio_path):
    """Process audio file, return SRT text"""
    if not os.path.exists(audio_path):
        print(f"[ERR] File not found: {audio_path}")
        return None
    
    print(f"[1/3] Loading FunASR model...")
    t0 = time_module.time()
    
    from funasr import AutoModel
    
    model = AutoModel(
        model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        model_revision="master",
        vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
        device="cpu",
        disable_update=True,
    )
    
    t1 = time_module.time()
    print(f"    Model loaded in {t1 - t0:.1f}s")
    
    print(f"[2/3] Running ASR on: {os.path.basename(audio_path)}")
    result = model.generate(input=audio_path)
    
    t2 = time_module.time()
    print(f"    ASR done in {t2 - t1:.1f}s")
    
    # Parse result
    if isinstance(result, list):
        r = result[0]
    else:
        r = result
    
    text = r.get("text", "")
    timestamps = r.get("timestamp", [])
    
    print(f"    Text: {len(text)} chars")
    print(f"    Timestamps: {len(timestamps)} entries")
    print(f"    First 3 ts: {timestamps[:3]}")
    
    print(f"[3/3] Building sentence segments...")
    segments = build_sentence_timestamps(text, timestamps)
    print(f"    Generated {len(segments)} subtitle segments")
    
    # Save SRT
    base_dir = os.path.dirname(audio_path)
    base_name = os.path.splitext(os.path.basename(audio_path))[0]
    srt_path = os.path.join(base_dir, base_name + ".srt")
    srt_format(segments, output_path=srt_path)
    
    # Save also raw text for debug
    raw_path = os.path.join(base_dir, base_name + "_raw.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[OK] Raw text saved: {raw_path}")
    
    print(f"[DONE] Total segments: {len(segments)}")
    return text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gen_srt.py <audio_file>")
        print("       python gen_srt.py <audio_dir>")
        sys.exit(1)
    
    path = sys.argv[1]
    if os.path.isdir(path):
        exts = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma")
        files = [f for f in os.listdir(path) if f.lower().endswith(exts)]
        print(f"Found {len(files)} audio files")
        for fname in sorted(files):
            fpath = os.path.join(path, fname)
            print(f"\n{'='*60}")
            print(f"Processing: {fname}")
            process_audio(fpath)
    else:
        process_audio(path)
