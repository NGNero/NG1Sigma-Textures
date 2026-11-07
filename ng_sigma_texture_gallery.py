#!/usr/bin/env python3
"""Build a local, searchable HTML gallery for Ninja Gaiden Sigma DDS exports.

Python 3.12+, standard library only. Reads DDS files recursively, decodes the
first mip level of DXT1, DXT5, BGRA8 and RGBA8 textures, and writes small PNG
thumbnails plus index.html and textures.csv. Original files are never changed.
"""
from __future__ import annotations

import argparse
import csv
import html
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

DDS_HEADER_BYTES = 128


class DDSFormatError(Exception):
    pass


@dataclass(frozen=True)
class DDSInfo:
    width: int
    height: int
    mip_count: int
    format_name: str
    format_kind: str
    data_offset: int
    pixel_data: memoryview


def u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise DDSFormatError(f"DDS header truncated at byte {offset}")
    return struct.unpack_from('<I', data, offset)[0]


def parse_dds(data: bytes, source: Path) -> DDSInfo:
    if len(data) < DDS_HEADER_BYTES or data[:4] != b'DDS ':
        raise DDSFormatError('not a DDS file')
    if u32(data, 4) != 124:
        raise DDSFormatError('unexpected DDS header size')
    height, width = u32(data, 12), u32(data, 16)
    mip_count = max(1, u32(data, 28))
    pf_flags = u32(data, 80)
    fourcc = data[84:88]
    bit_count = u32(data, 88)
    r_mask, g_mask, b_mask, a_mask = struct.unpack_from('<4I', data, 92)
    if width < 1 or height < 1 or width > 32768 or height > 32768:
        raise DDSFormatError(f'invalid dimensions {width}x{height}')

    if pf_flags & 0x4:
        if fourcc == b'DXT1':
            kind, name = 'dxt1', 'DXT1'
            first_mip = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
        elif fourcc == b'DXT5':
            kind, name = 'dxt5', 'DXT5'
            first_mip = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
        else:
            raise DDSFormatError(f'unsupported FourCC {fourcc!r}')
    elif (pf_flags & 0x41) == 0x41 and bit_count == 32:
        if (r_mask, g_mask, b_mask, a_mask) == (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000):
            kind, name = 'bgra8', 'BGRA8'
        elif (r_mask, g_mask, b_mask, a_mask) == (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000):
            kind, name = 'rgba8', 'RGBA8'
        else:
            raise DDSFormatError('unsupported 32-bit channel masks')
        first_mip = width * height * 4
    else:
        raise DDSFormatError('unsupported DDS pixel format')

    if len(data) < DDS_HEADER_BYTES + first_mip:
        raise DDSFormatError(f'truncated top mip (need {first_mip} bytes)')
    return DDSInfo(width, height, mip_count, name, kind, DDS_HEADER_BYTES,
                   memoryview(data)[DDS_HEADER_BYTES:DDS_HEADER_BYTES + first_mip])


def rgb565(value: int) -> tuple[int, int, int]:
    r = (value >> 11) & 31
    g = (value >> 5) & 63
    b = value & 31
    return ((r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31)


def color_table(c0: int, c1: int, force_four: bool) -> list[tuple[int, int, int, int]]:
    a = rgb565(c0)
    b = rgb565(c1)
    table = [(a[0], a[1], a[2], 255), (b[0], b[1], b[2], 255)]
    if c0 > c1 or force_four:
        table.append(tuple((2 * a[i] + b[i]) // 3 for i in range(3)) + (255,))
        table.append(tuple((a[i] + 2 * b[i]) // 3 for i in range(3)) + (255,))
    else:
        table.append(tuple((a[i] + b[i]) // 2 for i in range(3)) + (255,))
        table.append((0, 0, 0, 0))
    return table


def dxt1_block(block: memoryview) -> list[tuple[int, int, int, int]]:
    c0, c1, bits = struct.unpack_from('<HHI', block, 0)
    table = color_table(c0, c1, False)
    return [table[(bits >> (2 * i)) & 3] for i in range(16)]


def dxt5_block(block: memoryview) -> list[tuple[int, int, int, int]]:
    a0, a1 = block[0], block[1]
    alpha_bits = int.from_bytes(block[2:8], 'little')
    alphas = [a0, a1]
    if a0 > a1:
        alphas.extend(((7 - i) * a0 + i * a1) // 7 for i in range(1, 7))
    else:
        alphas.extend(((5 - i) * a0 + i * a1) // 5 for i in range(1, 5))
        alphas.extend((0, 255))
    colors = color_table(*struct.unpack_from('<HH', block, 8), True)
    color_bits = struct.unpack_from('<I', block, 12)[0]
    out = []
    for i in range(16):
        c = colors[(color_bits >> (2 * i)) & 3]
        alpha = alphas[(alpha_bits >> (3 * i)) & 7]
        out.append((c[0], c[1], c[2], alpha))
    return out


def composite_checker(rgba: tuple[int, int, int, int], x: int, y: int) -> tuple[int, int, int]:
    r, g, b, a = rgba
    # Neutral checkerboard makes transparent areas visible in the gallery.
    base = 205 if ((x // 8) + (y // 8)) % 2 == 0 else 155
    if a == 255:
        return r, g, b
    if a == 0:
        return base, base, base
    inv = 255 - a
    return ((r * a + base * inv) // 255,
            (g * a + base * inv) // 255,
            (b * a + base * inv) // 255)


def decode_thumbnail(info: DDSInfo, size: int) -> tuple[int, int, bytes]:
    """Decode only the top mip and sample it directly to a bounded thumbnail."""
    # Fill the thumbnail box even for small game textures; nearest-neighbour
    # enlargement is only for preview and never changes the source DDS.
    scale = size / max(info.width, info.height)
    out_w = max(1, int(round(info.width * scale)))
    out_h = max(1, int(round(info.height * scale)))
    # Fixed square canvas gives the HTML gallery a consistent grid.
    canvas = bytearray([184, 184, 184] * (size * size))
    origin_x = (size - out_w) // 2
    origin_y = (size - out_h) // 2
    x_to_out: dict[int, list[int]] = {}
    y_to_out: dict[int, list[int]] = {}
    for ox in range(out_w):
        sx = min(info.width - 1, int((ox + 0.5) * info.width / out_w))
        x_to_out.setdefault(sx, []).append(ox + origin_x)
    for oy in range(out_h):
        sy = min(info.height - 1, int((oy + 0.5) * info.height / out_h))
        y_to_out.setdefault(sy, []).append(oy + origin_y)

    def put(sx: int, sy: int, rgba: tuple[int, int, int, int]) -> None:
        out_xs = x_to_out.get(sx)
        out_ys = y_to_out.get(sy)
        if not out_xs or not out_ys:
            return
        for oy in out_ys:
            for ox in out_xs:
                r, g, b = composite_checker(rgba, ox, oy)
                pos = (oy * size + ox) * 3
                canvas[pos:pos + 3] = bytes((r, g, b))

    if info.format_kind in ('rgba8', 'bgra8'):
        pixels = info.pixel_data
        for sy in y_to_out:
            row = sy * info.width * 4
            for sx in x_to_out:
                p = row + sx * 4
                if info.format_kind == 'rgba8':
                    rgba = (pixels[p], pixels[p + 1], pixels[p + 2], pixels[p + 3])
                else:
                    rgba = (pixels[p + 2], pixels[p + 1], pixels[p], pixels[p + 3])
                put(sx, sy, rgba)
    else:
        block_bytes = 8 if info.format_kind == 'dxt1' else 16
        blocks_x = (info.width + 3) // 4
        blocks_y = (info.height + 3) // 4
        decoder = dxt1_block if info.format_kind == 'dxt1' else dxt5_block
        for by in range(blocks_y):
            for bx in range(blocks_x):
                block_index = by * blocks_x + bx
                start = block_index * block_bytes
                block = info.pixel_data[start:start + block_bytes]
                colors = decoder(block)
                for py in range(4):
                    sy = by * 4 + py
                    if sy >= info.height or sy not in y_to_out:
                        continue
                    for px in range(4):
                        sx = bx * 4 + px
                        if sx >= info.width or sx not in x_to_out:
                            continue
                        put(sx, sy, colors[py * 4 + px])

    return size, size, bytes(canvas)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF)


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    stride = width * 3
    raw = b''.join(b'\x00' + rgb[y * stride:(y + 1) * stride] for y in range(height))
    png = (b'\x89PNG\r\n\x1a\n' +
           png_chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0)) +
           png_chunk(b'IDAT', zlib.compress(raw, level=6)) +
           png_chunk(b'IEND', b''))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def html_path(path: Path) -> str:
    # Relative links keep the whole gallery folder relocatable.
    return quote(path.as_posix(), safe='/._-')


def discover(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob('*') if p.is_file() and p.suffix.lower() == '.dds'),
                  key=lambda p: str(p).casefold())


def generate_gallery(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    out = args.out.resolve()
    if not source.is_dir():
        raise SystemExit(f'Source folder does not exist: {source}')
    if out == source or source in out.parents:
        raise SystemExit('Output folder must not be inside the DDS source folder.')
    files = discover(source)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f'No .dds files found in {source}')
        return 0

    thumbs_dir = out / 'thumbs'
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str | int]] = []
    skipped: list[tuple[str, str]] = []
    print(f'Found {len(files):,} DDS file(s). Creating {args.size}px thumbnails...')

    for number, file_path in enumerate(files, start=1):
        rel = file_path.relative_to(source)
        try:
            data = file_path.read_bytes()
            info = parse_dds(data, file_path)
            _w, _h, rgb = decode_thumbnail(info, args.size)
            thumb_name = f'{number:07d}.png'
            write_png(thumbs_dir / thumb_name, args.size, args.size, rgb)
            container = rel.parent.as_posix() if rel.parent != Path('.') else '(root)'
            rows.append({
                'number': number,
                'container': container,
                'filename': rel.name,
                'relative_path': rel.as_posix(),
                'width': info.width,
                'height': info.height,
                'mips': info.mip_count,
                'format': info.format_name,
                'thumbnail': f'thumbs/{thumb_name}',
                'source_absolute': str(file_path),
            })
        except (OSError, DDSFormatError, struct.error, ValueError) as exc:
            skipped.append((str(rel), str(exc)))
        if number % 2500 == 0 or number == len(files):
            print(f'Processed {number:,}/{len(files):,} | thumbnails: {len(rows):,} | skipped: {len(skipped):,}')

    csv_path = out / 'textures.csv'
    fieldnames = ['number', 'container', 'filename', 'relative_path', 'width', 'height', 'mips', 'format', 'source_absolute']
    with csv_path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})

    # Generate a searchable local HTML page. The source DDS link is intentionally local.
    cards = []
    for row in rows:
        text_key = f"{row['container']} {row['filename']} {row['width']}x{row['height']} {row['format']}".lower()
        source_uri = Path(str(row['source_absolute'])).as_uri()
        cards.append(
            '<article class="card" data-search="' + html.escape(text_key, quote=True) + '">'
            '<a class="preview" href="' + html.escape(source_uri, quote=True) + '" title="Open original DDS">'
            '<img loading="lazy" src="' + html.escape(html_path(Path(str(row['thumbnail']))), quote=True) + '" alt="">'
            '</a><div class="meta"><strong>' + html.escape(str(row['container'])) + '</strong>'
            '<span>' + html.escape(str(row['filename'])) + '</span>'
            '<span>' + str(row['width']) + ' × ' + str(row['height']) + ' · ' + html.escape(str(row['format']))
            + ' · ' + str(row['mips']) + ' mip(s)</span></div></article>'
        )
    raw8_count = sum(1 for p in source.rglob('*.raw8') if p.is_file())
    skipped_html = ''.join('<li><code>' + html.escape(name) + '</code>: ' + html.escape(reason) + '</li>'
                           for name, reason in skipped)
    document = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ninja Gaiden Sigma texture gallery</title><style>
:root{color-scheme:dark;font:14px/1.45 system-ui,Segoe UI,sans-serif;background:#0d0f13;color:#fbfdff}
body{margin:0;padding:24px}h1{font-size:22px;margin:0 0 6px}.sub{color:#aab2c0;margin:0 0 18px}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:#0d0f13;padding:12px 0;z-index:2}
input{background:#171b23;color:#fbfdff;border:1px solid #343b49;border-radius:10px;padding:10px 12px;min-width:min(420px,80vw)}
#count{color:#b9c2d0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px}
.card{min-width:0;background:#151922;border-radius:12px;overflow:hidden}.preview{display:grid;place-items:center;background:#202530;padding:6px}
.preview img{width:100%;height:auto;aspect-ratio:1;object-fit:contain;image-rendering:auto}.meta{display:grid;gap:2px;padding:8px 10px;color:#adb7c6;font-size:12px;overflow-wrap:anywhere}
.meta strong{color:#fbfdff;font-size:12px}.empty{padding:30px;color:#aab2c0}.errors{margin-top:28px;color:#aab2c0}.errors summary{cursor:pointer}
code{color:#d4dcff}
</style></head><body>
<h1>Ninja Gaiden Sigma · DDS texture gallery</h1>
<p class="sub">Local preview only. Original DDS files are not modified. Click a thumbnail to open its source DDS.</p>
<div class="toolbar"><input id="search" type="search" placeholder="Search container, texture ID, dimensions, format…" autocomplete="off"><span id="count"></span></div>
<div id="grid" class="grid">__CARDS__</div>
__ERRORS__
<script>
const search=document.getElementById('search'),cards=[...document.querySelectorAll('.card')],count=document.getElementById('count');
function filter(){const q=search.value.trim().toLowerCase();let shown=0;for(const c of cards){const ok=!q||c.dataset.search.includes(q);c.hidden=!ok;if(ok)shown++;}count.textContent=`Showing ${shown.toLocaleString()} of ${cards.length.toLocaleString()} textures`;}
search.addEventListener('input',filter);filter();
</script></body></html>'''
    errors_section = ''
    if skipped:
        errors_section = '<details class="errors"><summary>Skipped files (' + str(len(skipped)) + ')</summary><ul>' + skipped_html + '</ul></details>'
    if raw8_count:
        errors_section += f'<p class="sub">Also found {raw8_count:,} RAW8/indexed texture file(s). These need their palette to preview accurately and are not included in this DDS gallery.</p>'
    (out / 'index.html').write_text(document.replace('__CARDS__', '\n'.join(cards)).replace('__ERRORS__', errors_section), encoding='utf-8')
    print(f'\nGallery: {out / "index.html"}')
    print(f'CSV index: {csv_path}')
    print(f'Created {len(rows):,} thumbnail(s); skipped {len(skipped):,} file(s).')
    if raw8_count:
        print(f'Found {raw8_count:,} RAW8 file(s); palette-based previews are not supported yet.')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Create a local searchable gallery of Sigma DDS textures.')
    parser.add_argument('source', type=Path, help='Root folder containing dds_original exports')
    parser.add_argument('--out', type=Path, default=Path('texture_gallery'), help='Output gallery folder')
    parser.add_argument('--size', type=int, default=160, help='Thumbnail canvas size (default: 160px)')
    parser.add_argument('--limit', type=int, default=0, help='Only process the first N DDS files (useful for a quick test)')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.size < 32 or args.size > 512:
        print('ERROR: --size must be between 32 and 512.', file=sys.stderr)
        return 2
    if args.limit < 0:
        print('ERROR: --limit cannot be negative.', file=sys.stderr)
        return 2
    try:
        return generate_gallery(args)
    except (OSError, MemoryError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
