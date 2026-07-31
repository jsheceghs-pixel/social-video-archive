# 解析 B 站弹幕 XML（raw deflate 解压 + 统计）
import zlib, re, sys

def decompress_bili_dm(raw):
    """B站弹幕接口返回 raw deflate 压缩，尝试多种解压方式"""
    for wbits in [-15, 15, 31, 47]:
        try:
            return zlib.decompress(raw, wbits)
        except Exception:
            continue
    try:
        import gzip
        return gzip.decompress(raw)
    except Exception:
        pass
    return raw

if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else r'C:/Users/Liyooo/.openclaw/workspace/douyin/bili_dm.xml.gz'
    dst = sys.argv[2] if len(sys.argv) > 2 else src.replace('.gz', '.xml').replace('.so', '.xml')

    raw = open(src, 'rb').read()
    data = decompress_bili_dm(raw)
    text = data.decode('utf-8', errors='replace')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(text)

    dms = re.findall(r'<d p="([^"]+)">([^<]+)</d>', text)
    print(f'弹幕数: {len(dms)}')
    for p, t in dms[:8]:
        attrs = p.split(',')
        print(f'  [{float(attrs[0]):.1f}s|uid{attrs[6]}] {t[:30]}')
