#!/usr/bin/env python3
"""Remove prompt-bearing metadata from PNG/JPEG files without re-encoding pixels."""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}
JPEG_DROP = {0xE1, 0xED, 0xFE}  # EXIF/XMP APP1, IPTC APP13, COM


class MetadataError(ValueError):
    pass


def _png_chunks(data: bytes) -> list[tuple[int, int, bytes]]:
    if not data.startswith(PNG_SIGNATURE):
        raise MetadataError("not a PNG file")
    chunks: list[tuple[int, int, bytes]] = []
    position = 8
    saw_iend = False
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(data):
            raise MetadataError("truncated PNG chunk")
        chunks.append((position, end, chunk_type))
        position = end
        if chunk_type == b"IEND":
            saw_iend = True
            break
    if not saw_iend:
        raise MetadataError("PNG has no IEND chunk")
    return chunks


def strip_png_bytes(data: bytes) -> tuple[bytes, list[str]]:
    chunks = _png_chunks(data)
    removed: list[str] = []
    output = bytearray(PNG_SIGNATURE)
    for start, end, chunk_type in chunks:
        name = chunk_type.decode("latin-1", errors="replace")
        if chunk_type in PNG_DROP:
            removed.append(name)
            continue
        output.extend(data[start:end])
    return bytes(output), removed


def strip_jpeg_bytes(data: bytes) -> tuple[bytes, list[str]]:
    if not data.startswith(b"\xff\xd8"):
        raise MetadataError("not a JPEG file")
    output = bytearray(b"\xff\xd8")
    removed: list[str] = []
    position = 2
    length = len(data)
    while position < length:
        if data[position] != 0xFF:
            raise MetadataError("malformed JPEG marker stream")
        marker_start = position
        while position < length and data[position] == 0xFF:
            position += 1
        if position >= length:
            raise MetadataError("truncated JPEG marker")
        marker = data[position]
        position += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            output.extend(data[marker_start:position])
            if marker == 0xD9:
                output.extend(data[position:])
                return bytes(output), removed
            continue
        if position + 2 > length:
            raise MetadataError("truncated JPEG segment header")
        segment_length = struct.unpack(">H", data[position : position + 2])[0]
        if segment_length < 2:
            raise MetadataError("invalid JPEG segment length")
        segment_end = position + segment_length
        if segment_end > length:
            raise MetadataError("truncated JPEG segment")
        if marker == 0xDA:  # SOS: copy the scan and all following data verbatim
            output.extend(data[marker_start:])
            return bytes(output), removed
        if marker in JPEG_DROP:
            names = {0xE1: "APP1/EXIF", 0xED: "APP13/IPTC", 0xFE: "COM"}
            removed.append(names[marker])
        else:
            output.extend(data[marker_start:segment_end])
        position = segment_end
    return bytes(output), removed


def detect_image_format(data: bytes) -> str:
    if data.startswith(PNG_SIGNATURE):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    raise MetadataError("unsupported image format (only PNG and JPEG are accepted)")


def strip_image_bytes(data: bytes) -> tuple[bytes, list[str], str]:
    image_format = detect_image_format(data)
    if image_format == "png":
        stripped, removed = strip_png_bytes(data)
        return stripped, removed, "png"
    stripped, removed = strip_jpeg_bytes(data)
    return stripped, removed, "jpeg"


def strip_file(path: str | Path, in_place: bool = True, check_only: bool = False) -> dict[str, Any]:
    file_path = Path(path)
    data = file_path.read_bytes()
    stripped, removed, image_format = strip_image_bytes(data)
    changed = stripped != data
    result: dict[str, Any] = {
        "file": str(file_path),
        "format": image_format,
        "changed": changed,
        "removed": removed,
        "supported": True,
    }
    if check_only or not in_place or not changed:
        return result
    file_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=".strip-", suffix=file_path.suffix, dir=str(file_path.parent)
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(stripped)
        os.replace(temp_name, file_path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return result


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Strip prompt metadata from PNG/JPEG files.")
    parser.add_argument("files", nargs="+", metavar="FILE")
    parser.add_argument("--check", action="store_true", help="Report metadata without writing.")
    parser.add_argument("--json", action="store_true", help="Print JSON lines.")
    args = parser.parse_args()
    exit_code = 0
    for value in args.files:
        try:
            result = strip_file(value, check_only=args.check)
        except (OSError, MetadataError) as exc:
            result = {"file": value, "supported": False, "error": str(exc)}
            exit_code = 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            if result.get("error"):
                print(f"FAIL {value}: {result['error']}")
            elif result["changed"]:
                print(f"{'WOULD STRIP' if args.check else 'STRIPPED'} {value}: {', '.join(result['removed'])}")
            else:
                print(f"OK {value}: no removable metadata")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
