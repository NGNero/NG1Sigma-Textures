#!/usr/bin/env python3
"""Minimal Ninja Gaiden Sigma 1 databin / GT1G texture helper.

Uses only Python's standard library. It is intentionally limited to:
  * listing Sigma 1 databin entries;
  * extracting GT1G entries (types 0x07 and 0x13);
  * exporting supported GT1G textures as DDS (palette/index textures as RAW8);
  * rebuilding a GT1G container from edited DDS files while preserving the
    original per-texture flags and unedited textures;
  * appending a replacement compressed entry to a COPY of the databin.

This is a small, format-focused utility, not a general-purpose NGMC editor.
Always work on copies of the game files. Validate a patched archive in-game
before treating it as usable.
"""

from __future__ import annotations

import argparse
import csv
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

# Sigma 1 databin structures, little-endian. These match the field layout used
# by the public NGMC Toolbox Sigma 1 reader.
DAT_HEADER = struct.Struct("<4i2I2i3Ii")       # 0x30 bytes
S1_ITEM_HEADER = struct.Struct("<IiIIIhhBBBBi") # 0x20 bytes
GT1G_HEADER = struct.Struct("<4s4sIIIII")       # 0x1C bytes
DDS_HEADER_SIZE = 128

GT1G_TYPES = {0x07, 0x13}
FORMAT_NAMES = {
    0x01: "BGRA8",
    0x02: "RGBA8",
    0x06: "DXT1",
    0x59: "DXT1",
    0x08: "DXT5",
    0x5B: "DXT5",
    0x72: "INDEX8",
}


class FormatError(RuntimeError):
    """Raised when a game file does not match the expected structure."""


@dataclass
class DatabinEntry:
    index: int
    header_relative_offset: int
    header_position: int
    data_offset: int
    uncompressed_size: int
    compressed_size: int
    file_type: int
    raw_header: bytes


@dataclass
class Databin:
    path: Path
    data: bytes
    header_size: int
    file_data_offset: int
    data_start: int
    entries: list[DatabinEntry]


@dataclass
class Texture:
    index: int
    width: int
    height: int
    mip_count: int
    pixel_format: int
    header_bytes: bytes
    image_bytes: bytes
    record_bytes: bytes


@dataclass
class GT1G:
    path: Path
    data: bytes
    version: bytes
    file_size: int
    table_address: int
    texture_count: int
    header_tail: bytes
    textures: list[Texture]


@dataclass
class DDS:
    width: int
    height: int
    mip_count: int
    pixel_format: int
    image_bytes: bytes


def fail(message: str) -> NoReturn:
    raise FormatError(message)


def u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        fail(f"Read outside file at 0x{offset:X}.")
    return struct.unpack_from("<I", data, offset)[0]


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def read_databin(path: Path) -> Databin:
    data = path.read_bytes()
    if len(data) < DAT_HEADER.size:
        fail("The selected file is too small to be a Sigma 1 databin.")

    fields = DAT_HEADER.unpack_from(data, 0)
    header_size = fields[4]
    file_data_offset = fields[5]
    file_count = fields[8]
    data_start = header_size + file_data_offset

    # IMPORTANT: Sigma 1's headerSize is the relative base for item-header
    # offsets, not the byte length of the complete 0x30-byte fixed structure.
    # The original NGMC reader explicitly seeks to header.headerSize + offset;
    # 0x20 is therefore a valid value even though DAT_HEADER.size is 0x30.
    if header_size < 0x20:
        fail(f"Invalid databin headerSize: 0x{header_size:X} (minimum 0x20).")
    if data_start < DAT_HEADER.size or data_start > len(data):
        fail(
            "Invalid databin data-section start: "
            f"headerSize (0x{header_size:X}) + fileDataOffset "
            f"(0x{file_data_offset:X}) = 0x{data_start:X}."
        )
    if file_count > 2_000_000:
        fail(f"Unreasonable entry count: {file_count}.")
    # The item-offset table follows the fixed 0x30-byte structure. It must
    # exist in the archive's metadata area before the compressed-data section;
    # it is NOT required to fit inside headerSize (which can be 0x20).
    table_end = DAT_HEADER.size + file_count * 4
    if table_end > len(data) or table_end > data_start:
        fail("The Sigma 1 entry-offset table is outside the databin metadata section.")

    entries: list[DatabinEntry] = []
    for index in range(file_count):
        header_relative = u32(data, DAT_HEADER.size + index * 4)
        header_position = header_size + header_relative
        # Match the original NGMC reader: item-header offsets are based at
        # headerSize, but their target is not required to be before
        # headerSize + fileDataOffset. That latter value is the base for
        # compressed payloads, not a proven upper bound for item headers.
        # Only reject an address that would make the 0x20-byte header read
        # extend beyond the actual archive.
        if header_position + S1_ITEM_HEADER.size > len(data):
            fail(
                f"Entry {index}: item header is outside the databin file "
                f"(headerSize=0x{header_size:X}, entryOffset=0x{header_relative:X}, "
                f"computedPosition=0x{header_position:X}, fileSize=0x{len(data):X})."
            )

        values = S1_ITEM_HEADER.unpack_from(data, header_position)
        item_data_offset = values[0]
        raw_size = values[2]
        compressed_size = values[3]
        file_type = values[9]  # byte 0x1A in the Sigma 1 item header

        payload_position = data_start + item_data_offset
        payload_end = payload_position + compressed_size
        if payload_position < data_start or payload_end > len(data):
            fail(f"Entry {index}: compressed payload points outside the databin.")

        entries.append(DatabinEntry(
            index=index,
            header_relative_offset=header_relative,
            header_position=header_position,
            data_offset=item_data_offset,
            uncompressed_size=raw_size,
            compressed_size=compressed_size,
            file_type=file_type,
            raw_header=data[header_position:header_position + S1_ITEM_HEADER.size],
        ))

    return Databin(path, data, header_size, file_data_offset, data_start, entries)


def decompress_entry(databin: Databin, entry: DatabinEntry) -> bytes:
    start = databin.data_start + entry.data_offset
    end = start + entry.compressed_size
    try:
        unpacked = zlib.decompress(databin.data[start:end])
    except zlib.error as exc:
        fail(f"Entry {entry.index}: zlib decompression failed: {exc}")
    if len(unpacked) != entry.uncompressed_size:
        fail(
            f"Entry {entry.index}: expected {entry.uncompressed_size} decompressed bytes, "
            f"got {len(unpacked)}."
        )
    return unpacked


def command_list(args: argparse.Namespace) -> None:
    archive = read_databin(args.databin)
    print(f"File: {archive.path}")
    print(f"Entries: {len(archive.entries):,}")
    print(f"Databin data start: 0x{archive.data_start:X}")
    print()
    print(f"{'ID':>7}  {'TYPE':>6}  {'RAW SIZE':>12}  {'PACKED':>12}  DESCRIPTION")
    texture_count = 0
    for entry in archive.entries:
        if entry.file_type in GT1G_TYPES:
            label = f"GT1G texture container (type 0x{entry.file_type:02X})"
            texture_count += 1
        else:
            label = f"Sigma 1 type 0x{entry.file_type:02X}"
        print(
            f"{entry.index:7d}  0x{entry.file_type:02X}  "
            f"{entry.uncompressed_size:12,d}  {entry.compressed_size:12,d}  {label}"
        )
    print(f"\nGT1G candidates: {texture_count}")


def command_extract(args: argparse.Namespace) -> None:
    archive = read_databin(args.databin)
    output = args.out
    output.mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    extracted = 0

    for entry in archive.entries:
        if entry.file_type not in GT1G_TYPES:
            continue
        payload = decompress_entry(archive, entry)
        ext = ".GT1G" if payload[:4] == b"GT1G" else ".bin"
        filename = f"{entry.index:05d}_type{entry.file_type:02X}{ext}"
        target = output / filename
        target.write_bytes(payload)
        manifest_rows.append((entry.index, f"0x{entry.file_type:02X}", filename,
                              entry.uncompressed_size, entry.compressed_size))
        extracted += 1
        print(f"Extracted entry {entry.index}: {filename} ({len(payload):,} bytes)")

    manifest = output / "texture_entries.csv"
    with manifest.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("entry_id", "databin_type", "filename", "uncompressed_size", "compressed_size"))
        writer.writerows(manifest_rows)

    print(f"\nExtracted {extracted} texture-container candidate(s) to: {output}")
    print(f"Manifest: {manifest}")


def mip_level_size(width: int, height: int, pixel_format: int) -> int:
    if pixel_format in (0x01, 0x02):
        return width * height * 4
    if pixel_format == 0x72:
        return width * height
    if pixel_format in (0x06, 0x59):
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
    if pixel_format in (0x08, 0x5B):
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    fail(f"Unsupported GT1G pixel format 0x{pixel_format:02X}.")


def mip_chain_size(width: int, height: int, mip_count: int, pixel_format: int) -> int:
    return sum(mip_level_size(max(1, width >> level), max(1, height >> level), pixel_format)
               for level in range(mip_count))


def parse_gt1g(path: Path) -> GT1G:
    data = path.read_bytes()
    if len(data) < GT1G_HEADER.size:
        fail(f"{path}: file is too small to be GT1G.")

    magic, version, file_size, table_address, texture_count, _dat14, _dat18 = GT1G_HEADER.unpack_from(data, 0)
    if magic != b"GT1G":
        fail(f"{path}: missing GT1G signature.")
    if file_size < GT1G_HEADER.size or file_size > len(data):
        fail(f"{path}: invalid GT1G fileSize ({file_size:,}; actual {len(data):,}).")
    if table_address < GT1G_HEADER.size or table_address + texture_count * 4 > file_size:
        fail(f"{path}: invalid texture offset table.")
    if texture_count > 100_000:
        fail(f"{path}: unreasonable texture count ({texture_count}).")

    textures: list[Texture] = []
    for index in range(texture_count):
        relative = u32(data, table_address + index * 4)
        record_start = table_address + relative
        if record_start < table_address + texture_count * 4 or record_start + 8 > file_size:
            fail(f"Texture {index}: invalid record offset 0x{record_start:X}.")

        mip_byte, pixel_format, dimensions, flags = struct.unpack_from("<BBHI", data, record_start)
        mip_count = mip_byte >> 4
        width_exp = dimensions & 0x0F
        height_exp = dimensions >> 4
        if mip_count < 1 or width_exp > 15 or height_exp > 15:
            fail(f"Texture {index}: invalid dimensions or mipmap count.")
        width, height = 1 << width_exp, 1 << height_exp
        metadata_size = 8 + (12 if (flags >> 24) == 0x10 else 0)
        record_header_end = record_start + metadata_size
        image_size = mip_chain_size(width, height, mip_count, pixel_format)
        record_end = record_header_end + image_size
        if record_end > file_size:
            fail(f"Texture {index}: image data extends beyond the GT1G file.")

        header_bytes = data[record_start:record_header_end]
        image_bytes = data[record_header_end:record_end]
        textures.append(Texture(index, width, height, mip_count, pixel_format,
                               header_bytes, image_bytes, data[record_start:record_end]))

    return GT1G(path, data[:file_size], version, file_size, table_address,
                texture_count, data[:table_address], textures)


def make_dds(width: int, height: int, mip_count: int, pixel_format: int, image_bytes: bytes) -> bytes:
    if pixel_format in (0x06, 0x59):
        pf = struct.pack("<8I", 32, 0x4, int.from_bytes(b"DXT1", "little"), 0, 0, 0, 0, 0)
        top_size = mip_level_size(width, height, pixel_format)
        flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000
    elif pixel_format in (0x08, 0x5B):
        pf = struct.pack("<8I", 32, 0x4, int.from_bytes(b"DXT5", "little"), 0, 0, 0, 0, 0)
        top_size = mip_level_size(width, height, pixel_format)
        flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000
    elif pixel_format == 0x01:
        pf = struct.pack("<8I", 32, 0x41, 0, 32,
                         0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        top_size = width * 4
        flags = 0x1 | 0x2 | 0x4 | 0x8 | 0x1000
    elif pixel_format == 0x02:
        pf = struct.pack("<8I", 32, 0x41, 0, 32,
                         0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
        top_size = width * 4
        flags = 0x1 | 0x2 | 0x4 | 0x8 | 0x1000
    else:
        fail(f"Pixel format 0x{pixel_format:02X} cannot be represented as a standard DDS by this script.")

    if mip_count > 1:
        flags |= 0x20000
        caps = 0x1000 | 0x8 | 0x400000
    else:
        caps = 0x1000
    header = struct.pack(
        "<7I11I",
        124, flags, height, width, top_size, 0, mip_count,
        *([0] * 11),
    ) + pf + struct.pack("<5I", caps, 0, 0, 0, 0)
    if len(header) != 124:
        fail("Internal error: generated DDS header is not 124 bytes.")
    expected = mip_chain_size(width, height, mip_count, pixel_format)
    if len(image_bytes) != expected:
        fail(f"Texture data length mismatch: expected {expected}, got {len(image_bytes)}.")
    return b"DDS " + header + image_bytes


def command_export_dds(args: argparse.Namespace) -> None:
    container = parse_gt1g(args.gt1g)
    args.out.mkdir(parents=True, exist_ok=True)
    count_dds = 0
    count_raw = 0

    print(f"GT1G {container.version.decode('ascii', errors='replace')} | {container.texture_count} textures")
    print(f"{'INDEX':>5}  {'SIZE':>12}  {'MIPS':>4}  {'FORMAT':>8}  OUTPUT")
    for texture in container.textures:
        name = FORMAT_NAMES.get(texture.pixel_format, f"0x{texture.pixel_format:02X}")
        stem = f"{texture.index:03d}"
        if texture.pixel_format == 0x72:
            target = args.out / f"{stem}.raw8"
            target.write_bytes(texture.image_bytes)
            count_raw += 1
        else:
            target = args.out / f"{stem}.dds"
            target.write_bytes(make_dds(texture.width, texture.height, texture.mip_count,
                                        texture.pixel_format, texture.image_bytes))
            count_dds += 1
        print(f"{texture.index:5d}  {texture.width:5d}x{texture.height:<5d}  {texture.mip_count:4d}  {name:>8}  {target.name}")

    print(f"\nExported {count_dds} DDS texture(s) and {count_raw} indexed RAW8 texture(s) to {args.out}")
    if count_raw:
        print("Note: RAW8/indexed textures are preserved but are not suitable for AI upscaling with this script.")


def parse_dds(path: Path) -> DDS:
    data = path.read_bytes()
    if len(data) < DDS_HEADER_SIZE or data[:4] != b"DDS ":
        fail(f"{path}: not a valid DDS file.")
    if u32(data, 4) != 124:
        fail(f"{path}: unexpected DDS header size.")

    height = u32(data, 12)
    width = u32(data, 16)
    mip_count = u32(data, 28) or 1
    pf_flags = u32(data, 80)
    fourcc = data[84:88]
    bit_count = u32(data, 88)
    r_mask, _g_mask, b_mask, _a_mask = struct.unpack_from("<4I", data, 92)

    if pf_flags & 0x4:
        if fourcc == b"DXT1":
            pixel_format = 0x06
        elif fourcc == b"DXT5":
            pixel_format = 0x08
        else:
            fail(f"{path}: only DXT1 and DXT5 compressed DDS textures are supported.")
    elif (pf_flags & 0x41) == 0x41 and bit_count == 32:
        if r_mask == 0x00FF0000 and b_mask == 0x000000FF:
            pixel_format = 0x01  # BGRA8
        elif r_mask == 0x000000FF and b_mask == 0x00FF0000:
            pixel_format = 0x02  # RGBA8
        else:
            fail(f"{path}: unsupported 32-bit DDS channel masks.")
    else:
        fail(f"{path}: unsupported DDS pixel format.")

    if width < 1 or height < 1 or mip_count > 15:
        fail(f"{path}: invalid dimensions or mipmap count.")
    if width & (width - 1) or height & (height - 1):
        fail(f"{path}: GT1G requires power-of-two dimensions; got {width}x{height}.")
    if width.bit_length() - 1 > 15 or height.bit_length() - 1 > 15:
        fail(f"{path}: dimensions exceed GT1G's supported exponent range.")

    image_bytes = data[DDS_HEADER_SIZE:]
    expected = mip_chain_size(width, height, mip_count, pixel_format)
    if len(image_bytes) < expected:
        fail(f"{path}: DDS image data is truncated (need {expected}, have {len(image_bytes)}).")
    if len(image_bytes) > expected:
        image_bytes = image_bytes[:expected]

    return DDS(width, height, mip_count, pixel_format, image_bytes)


def same_format(original: int, replacement: int) -> bool:
    if original in (0x06, 0x59):
        return replacement == 0x06
    if original in (0x08, 0x5B):
        return replacement == 0x08
    return original == replacement


def rebuild_gt1g(original_path: Path, dds_dir: Path) -> bytes:
    container = parse_gt1g(original_path)
    rebuilt_records: list[bytes] = []

    for texture in container.textures:
        dds_path = dds_dir / f"{texture.index:03d}.dds"
        if not dds_path.exists():
            rebuilt_records.append(texture.record_bytes)
            continue
        if texture.pixel_format == 0x72:
            fail(f"Texture {texture.index} is indexed/paletted. Leave it unchanged for now.")

        replacement = parse_dds(dds_path)
        if not same_format(texture.pixel_format, replacement.pixel_format):
            fail(
                f"Texture {texture.index}: original format is {FORMAT_NAMES[texture.pixel_format]}, "
                f"but replacement is {FORMAT_NAMES[replacement.pixel_format]}."
            )

        width_exp = replacement.width.bit_length() - 1
        height_exp = replacement.height.bit_length() - 1
        header = bytearray(texture.header_bytes)
        header[0] = ((replacement.mip_count & 0x0F) << 4) | (header[0] & 0x0F)
        struct.pack_into("<H", header, 2, (height_exp << 4) | width_exp)
        rebuilt_records.append(bytes(header) + replacement.image_bytes)

    # Preserve the complete prefix (including the normal-map flags table),
    # then rewrite the relative texture-offset table and texture records.
    table = bytearray()
    relative = container.texture_count * 4
    records = bytearray()
    for record in rebuilt_records:
        table += struct.pack("<I", relative)
        records += record
        relative += len(record)

    output = bytearray(container.header_tail + table + records)
    struct.pack_into("<I", output, 8, len(output))
    struct.pack_into("<I", output, 12, container.table_address)
    struct.pack_into("<I", output, 16, container.texture_count)
    return bytes(output)


def command_rebuild(args: argparse.Namespace) -> None:
    output = rebuild_gt1g(args.original, args.dds_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists() and not args.force:
        fail(f"Output already exists: {args.out}. Use --force to replace it.")
    args.out.write_bytes(output)
    print(f"Built {args.out} ({len(output):,} bytes)")
    print("Unedited textures and original per-texture flags were preserved.")


def patch_databin(source: Path, replacement_path: Path, entry_index: int, output_path: Path) -> None:
    archive = read_databin(source)
    if entry_index < 0 or entry_index >= len(archive.entries):
        fail(f"Entry ID {entry_index} is outside the archive (0..{len(archive.entries)-1}).")
    entry = archive.entries[entry_index]
    if entry.file_type not in GT1G_TYPES:
        fail(f"Entry {entry_index} is type 0x{entry.file_type:02X}, not a GT1G texture container.")

    replacement = replacement_path.read_bytes()
    if replacement[:4] != b"GT1G":
        fail("Replacement file does not have a GT1G signature.")
    if len(replacement) > 0xFFFFFFFF:
        fail("Replacement GT1G is larger than the databin's 32-bit size field.")

    compressed = zlib.compress(replacement, level=9)
    append_position = align(len(archive.data), 16)
    new_offset = append_position - archive.data_start
    if new_offset > 0xFFFFFFFF or len(compressed) > 0xFFFFFFFF:
        fail("Replacement offset or compressed size exceeds the databin's 32-bit fields.")

    patched = bytearray(archive.data)
    if append_position > len(patched):
        patched.extend(b"\x00" * (append_position - len(patched)))
    patched.extend(compressed)

    # Sigma 1 item header fields: offset @ +0, uncompressed size @ +8,
    # compressed size @ +12. Other metadata and every original payload remain untouched.
    struct.pack_into("<I", patched, entry.header_position, new_offset)
    struct.pack_into("<I", patched, entry.header_position + 8, len(replacement))
    struct.pack_into("<I", patched, entry.header_position + 12, len(compressed))

    # Validate the newly written pointer and zlib stream before writing output.
    check = bytes(patched[archive.data_start + new_offset:
                          archive.data_start + new_offset + len(compressed)])
    if zlib.decompress(check) != replacement:
        fail("Internal validation failed after patching; output was not written.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(patched)
    print(f"Patched copy written to: {output_path}")
    print(f"Replaced databin entry: {entry_index}")
    print(f"Original type: 0x{entry.file_type:02X}")
    print(f"Replacement size: {len(replacement):,} bytes")
    print(f"Compressed size: {len(compressed):,} bytes")
    print("Original archive entries were not moved; the replacement was appended.")
    print("Use the original databin again for each experiment to avoid accumulating old appended data.")


def command_patch(args: argparse.Namespace) -> None:
    if not args.experimental:
        fail("Archive insertion is experimental and has not been verified against your game archive. Re-run with --experimental only after backing up the original databin.")
    if args.out.resolve() == args.databin.resolve():
        fail("Output must be a different file from the original databin.")
    if args.out.exists() and not args.force:
        fail(f"Output already exists: {args.out}. Use --force to replace it.")
    patch_databin(args.databin, args.replacement, args.entry, args.out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Minimal Sigma 1 databin / GT1G extractor, DDS exporter and patcher."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="List Sigma 1 databin entries and find GT1G candidates.")
    p.add_argument("databin", type=Path)
    p.set_defaults(func=command_list)

    p = sub.add_parser("extract", help="Extract only GT1G entries (types 0x07 and 0x13).")
    p.add_argument("databin", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=command_extract)

    p = sub.add_parser("export-dds", help="Export textures from one GT1G container as DDS.")
    p.add_argument("gt1g", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=command_export_dds)

    p = sub.add_parser("rebuild-gt1g", help="Rebuild a GT1G using edited, same-index DDS files.")
    p.add_argument("original", type=Path)
    p.add_argument("--dds-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=command_rebuild)

    p = sub.add_parser("patch", help="Append a replacement GT1G to a COPY of the databin.")
    p.add_argument("databin", type=Path)
    p.add_argument("--entry", type=int, required=True, help="Entry ID shown by the list command.")
    p.add_argument("--replacement", type=Path, required=True, help="Rebuilt .GT1G file.")
    p.add_argument("--out", type=Path, required=True, help="New databin output path; never the original.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--experimental", action="store_true",
                   help="Acknowledge that archive insertion is not yet verified against a real game databin.")
    p.set_defaults(func=command_patch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (FormatError, OSError, struct.error, OverflowError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
