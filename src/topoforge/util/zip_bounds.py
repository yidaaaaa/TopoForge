"""Bound ZIP central-directory metadata before constructing ``zipfile.ZipFile``."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_EOCD_MAX_COMMENT_BYTES = 65_535
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sLQL")
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2L4Q")
_ZIP64_EOCD_MINIMUM_PAYLOAD_BYTES = 44
_ZIP64_EOCD_MAXIMUM_PAYLOAD_BYTES = 1024 * 1024
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_CENTRAL_DIRECTORY_HEADER_BYTES = 46


@dataclass(frozen=True, slots=True)
class ZipCentralDirectoryBounds:
    """Validated ZIP trailer values used to bound later ``ZipFile`` parsing."""

    file_size: int
    entry_count: int
    central_directory_offset: int
    central_directory_bytes: int
    comment_bytes: int
    uses_zip64: bool


def _read_exact_at(stream: BinaryIO, offset: int, size: int, *, label: str) -> bytes:
    if offset < 0 or size < 0:
        raise ValueError(f"{label} has an invalid ZIP metadata offset")
    stream.seek(offset)
    payload = stream.read(size)
    if len(payload) != size:
        raise ValueError(f"{label} has a truncated ZIP metadata record")
    return payload


def _find_eocd(stream: BinaryIO, file_size: int, *, label: str) -> tuple[int, tuple[int, ...]]:
    tail_size = min(file_size, _EOCD_STRUCT.size + _EOCD_MAX_COMMENT_BYTES)
    tail_offset = file_size - tail_size
    tail = _read_exact_at(stream, tail_offset, tail_size, label=label)
    search_end = len(tail)
    while True:
        index = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if index < 0:
            raise ValueError(f"{label} has no canonical ZIP end-of-central-directory record")
        if index + _EOCD_STRUCT.size <= len(tail):
            fields = _EOCD_STRUCT.unpack_from(tail, index)
            comment_bytes = int(fields[-1])
            if index + _EOCD_STRUCT.size + comment_bytes == len(tail):
                return tail_offset + index, tuple(int(value) for value in fields[1:])
        search_end = index


def _scan_central_directory(
    stream: BinaryIO,
    *,
    offset: int,
    size: int,
    maximum_entries: int,
    declared_entries: int,
    label: str,
) -> int:
    """Count bounded central-directory records without constructing member objects."""
    position = offset
    boundary = offset + size
    observed = 0
    while position < boundary:
        remaining = boundary - position
        if remaining < _CENTRAL_DIRECTORY_HEADER_BYTES:
            raise ValueError(f"{label} central directory ends with a truncated record")
        header = _read_exact_at(
            stream,
            position,
            _CENTRAL_DIRECTORY_HEADER_BYTES,
            label=label,
        )
        if header[:4] != _CENTRAL_DIRECTORY_SIGNATURE:
            raise ValueError(f"{label} central directory has an invalid record signature")
        name_bytes, extra_bytes, comment_bytes = struct.unpack_from("<3H", header, 28)
        record_bytes = _CENTRAL_DIRECTORY_HEADER_BYTES + name_bytes + extra_bytes + comment_bytes
        if record_bytes > remaining:
            raise ValueError(f"{label} central directory has a truncated member record")
        observed += 1
        if observed > maximum_entries:
            raise ValueError(f"{label} member count exceeds the maximum {maximum_entries}")
        position += record_bytes
    if position != boundary or observed != declared_entries:
        raise ValueError(
            f"{label} central-directory member count {observed} "
            f"disagrees with its trailer count {declared_entries}"
        )
    return observed


def preflight_zip_central_directory(
    stream: BinaryIO,
    *,
    maximum_entries: int,
    maximum_central_directory_bytes: int,
    maximum_comment_bytes: int = _EOCD_MAX_COMMENT_BYTES,
    label: str = "ZIP archive",
) -> ZipCentralDirectoryBounds:
    """Validate ZIP/ZIP64 trailer bounds without allocating central-directory objects."""
    if maximum_entries < 1 or maximum_central_directory_bytes < _EOCD_STRUCT.size:
        raise ValueError("ZIP preflight limits must be positive")
    if not 0 <= maximum_comment_bytes <= _EOCD_MAX_COMMENT_BYTES:
        raise ValueError("ZIP comment limit is outside the format range")
    original_offset = stream.tell()
    try:
        stream.seek(0, 2)
        file_size = stream.tell()
        if file_size < _EOCD_STRUCT.size:
            raise ValueError(f"{label} is too small to contain a ZIP trailer")
        eocd_offset, fields = _find_eocd(stream, file_size, label=label)
        (
            disk_number,
            central_disk_number,
            entries_on_disk,
            entry_count,
            central_bytes,
            central_offset,
            comment_bytes,
        ) = fields
        if comment_bytes > maximum_comment_bytes:
            raise ValueError(f"{label} ZIP comment exceeds its byte bound")
        if disk_number != 0 or central_disk_number != 0 or entries_on_disk != entry_count:
            raise ValueError(f"{label} uses an unsupported multi-disk ZIP layout")

        locator_offset = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
        locator = (
            _read_exact_at(
                stream,
                locator_offset,
                _ZIP64_LOCATOR_STRUCT.size,
                label=label,
            )
            if locator_offset >= 0
            else b""
        )
        uses_zip64 = locator.startswith(_ZIP64_LOCATOR_SIGNATURE)
        has_sentinel = (
            entry_count == 0xFFFF
            or entries_on_disk == 0xFFFF
            or central_bytes == 0xFFFFFFFF
            or central_offset == 0xFFFFFFFF
        )
        trailer_start = eocd_offset
        if uses_zip64:
            (
                _signature,
                zip64_disk_number,
                zip64_eocd_offset,
                total_disks,
            ) = _ZIP64_LOCATOR_STRUCT.unpack(locator)
            if zip64_disk_number != 0 or total_disks != 1:
                raise ValueError(f"{label} uses an unsupported multi-disk ZIP64 layout")
            header = _read_exact_at(
                stream,
                zip64_eocd_offset,
                _ZIP64_EOCD_STRUCT.size,
                label=label,
            )
            (
                signature,
                record_bytes,
                _version_made,
                _version_needed,
                zip64_disk,
                zip64_central_disk,
                zip64_entries_on_disk,
                zip64_entry_count,
                zip64_central_bytes,
                zip64_central_offset,
            ) = _ZIP64_EOCD_STRUCT.unpack(header)
            if signature != _ZIP64_EOCD_SIGNATURE:
                raise ValueError(f"{label} ZIP64 locator does not name a ZIP64 EOCD record")
            if not (
                _ZIP64_EOCD_MINIMUM_PAYLOAD_BYTES
                <= record_bytes
                <= _ZIP64_EOCD_MAXIMUM_PAYLOAD_BYTES
            ):
                raise ValueError(f"{label} ZIP64 EOCD size is outside its safety bound")
            if zip64_eocd_offset + 12 + record_bytes != locator_offset:
                raise ValueError(f"{label} ZIP64 trailer offsets are inconsistent")
            if (
                zip64_disk != 0
                or zip64_central_disk != 0
                or zip64_entries_on_disk != zip64_entry_count
            ):
                raise ValueError(f"{label} uses an unsupported multi-disk ZIP64 layout")
            for legacy, sentinel, extended, field_name in (
                (entry_count, 0xFFFF, zip64_entry_count, "entry count"),
                (entries_on_disk, 0xFFFF, zip64_entries_on_disk, "disk entry count"),
                (central_bytes, 0xFFFFFFFF, zip64_central_bytes, "central-directory size"),
                (central_offset, 0xFFFFFFFF, zip64_central_offset, "central-directory offset"),
            ):
                if legacy != sentinel and legacy != extended:
                    raise ValueError(f"{label} ZIP64 {field_name} disagrees with its EOCD")
            entry_count = int(zip64_entry_count)
            central_bytes = int(zip64_central_bytes)
            central_offset = int(zip64_central_offset)
            trailer_start = int(zip64_eocd_offset)
        elif has_sentinel:
            raise ValueError(f"{label} has ZIP64 sentinel values without a ZIP64 locator")

        if entry_count < 1 or entry_count > maximum_entries:
            raise ValueError(
                f"{label} member count {entry_count} exceeds the maximum {maximum_entries}"
            )
        if central_bytes < entry_count * 46:
            raise ValueError(f"{label} central directory is too small for its member count")
        if central_bytes > maximum_central_directory_bytes:
            raise ValueError(
                f"{label} central directory exceeds the "
                f"{maximum_central_directory_bytes}-byte safety limit"
            )
        if central_offset < 0 or central_offset + central_bytes != trailer_start:
            raise ValueError(f"{label} central-directory offsets are inconsistent")
        _scan_central_directory(
            stream,
            offset=central_offset,
            size=central_bytes,
            maximum_entries=maximum_entries,
            declared_entries=entry_count,
            label=label,
        )
        return ZipCentralDirectoryBounds(
            file_size=file_size,
            entry_count=entry_count,
            central_directory_offset=central_offset,
            central_directory_bytes=central_bytes,
            comment_bytes=comment_bytes,
            uses_zip64=uses_zip64,
        )
    finally:
        stream.seek(original_offset)
