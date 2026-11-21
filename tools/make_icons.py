"""Draw the PNG app icons (envelope on teal) without any image library."""
import struct, zlib, sys
from pathlib import Path

def png(size, path):
    teal, white = (15, 118, 110), (255, 255, 255)
    s = size / 64
    def inside_env(x, y):
        # envelope outline: rect 12..52 x 19..47, stroke 4; flap lines from (13,21) to (32,35) to (51,21)
        X, Y = x / s, y / s
        outer = 10 <= X <= 54 and 17 <= Y <= 49
        inner = 14 <= X <= 50 and 21 <= Y <= 45
        if outer and not inner:
            return True
        for (x1, y1, x2, y2) in ((13, 21, 32, 35), (51, 21, 32, 35)):
            dx, dy = x2 - x1, y2 - y1
            t = max(0, min(1, ((X - x1) * dx + (Y - y1) * dy) / (dx * dx + dy * dy)))
            if ((X - x1 - t * dx) ** 2 + (Y - y1 - t * dy) ** 2) ** .5 <= 2:
                return True
        return False
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            # 4x supersampling for smooth edges
            hits = sum(inside_env(x + (i + .5) / 2, y + (j + .5) / 2) for i in range(2) for j in range(2)) / 4
            row += bytes(round(teal[k] * (1 - hits) + white[k] * hits) for k in range(3))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b""))

for n in (192, 512):
    png(n, f"app/static/icon-{n}.png")
print("ok")
