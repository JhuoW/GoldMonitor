"""Rebuild the native Windows ICO using only the Python standard library."""
from pathlib import Path
import struct
import zlib


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def inside_polygon(x, y, points):
    result = False
    previous = points[-1]
    for current in points:
        ax, ay = previous
        bx, by = current
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            result = not result
        previous = current
    return result


def pixel(x, y):
    # All coordinates use the 128-unit SVG design space.
    dx, dy = max(34 - x, 0, x - 94), max(34 - y, 0, y - 94)
    if dx * dx + dy * dy > 30 * 30:
        return (0, 0, 0, 0)
    color = (17, 23, 32, 255)
    if inside_polygon(x, y, [(35, 47), (47, 35), (81, 35), (93, 47)]):
        color = (255, 240, 192, 255)
    if inside_polygon(x, y, [(35, 47), (93, 47), (106, 87), (22, 87)]):
        t = (y - 47) / 40
        color = (int(255 - 42 * t), int(227 - 64 * t), int(160 - 91 * t), 255)
    if inside_polygon(x, y, [(35, 47), (42, 87), (22, 87)]):
        color = (184, 135, 54, 255)
    for left, right, center in [(53, 80, 61), (50, 83, 71)]:
        px = max(left, min(right, x))
        if (x - px) ** 2 + (y - center) ** 2 <= 2.5 ** 2:
            color = (102, 74, 32, 255)
    if (x - 98) ** 2 + (y - 32) ** 2 <= 8 ** 2:
        color = (98, 201, 155, 255)
    return color


def png(size):
    scale = 4
    data = bytearray()
    for row in range(size):
        data.append(0)
        for col in range(size):
            samples = [pixel((col + (sx + 0.5) / scale) * 128 / size,
                             (row + (sy + 0.5) / scale) * 128 / size)
                       for sy in range(scale) for sx in range(scale)]
            alpha = sum(p[3] for p in samples)
            rgb = [round(sum(p[channel] * p[3] for p in samples) / alpha) if alpha else 0
                   for channel in range(3)]
            data.extend(rgb + [round(alpha / (scale * scale))])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(data), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    sizes = (16, 32, 48, 64, 128, 256)
    images = [png(size) for size in sizes]
    offset = 6 + len(sizes) * 16
    entries = []
    for size, image in zip(sizes, images):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    target = Path(__file__).with_name("gold.ico")
    target.write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(images))
    print(target)
