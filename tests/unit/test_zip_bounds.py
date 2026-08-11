from __future__ import annotations

import io
import struct
import zipfile

import pytest

from topoforge.util.zip_bounds import preflight_zip_central_directory


def _zip_bytes(*, comment: bytes = b"") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("one.txt", b"one")
        archive.writestr("two.txt", b"two")
        archive.comment = comment
    return stream.getvalue()


def test_zip_preflight_bounds_classic_directory_and_restores_position() -> None:
    stream = io.BytesIO(_zip_bytes())
    stream.seek(7)

    result = preflight_zip_central_directory(
        stream,
        maximum_entries=2,
        maximum_central_directory_bytes=4096,
        maximum_comment_bytes=0,
        label="fixture",
    )

    assert result.entry_count == 2
    assert result.central_directory_bytes >= 2 * 46
    assert result.uses_zip64 is False
    assert stream.tell() == 7


def test_zip_preflight_rejects_count_and_comment_before_zipfile_parsing() -> None:
    with pytest.raises(ValueError, match="member count"):
        preflight_zip_central_directory(
            io.BytesIO(_zip_bytes()),
            maximum_entries=1,
            maximum_central_directory_bytes=4096,
        )
    with pytest.raises(ValueError, match="comment"):
        preflight_zip_central_directory(
            io.BytesIO(_zip_bytes(comment=b"not accepted")),
            maximum_entries=2,
            maximum_central_directory_bytes=4096,
            maximum_comment_bytes=0,
        )


def test_zip_preflight_parses_bounded_zip64_trailer() -> None:
    central_directory = struct.pack(
        "<4s6H3I5H2I",
        b"PK\x01\x02",
        *([0] * 16),
    )
    zip64_offset = len(central_directory)
    zip64_eocd = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        1,
        1,
        len(central_directory),
        0,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )

    result = preflight_zip_central_directory(
        io.BytesIO(central_directory + zip64_eocd + locator + eocd),
        maximum_entries=1,
        maximum_central_directory_bytes=46,
        maximum_comment_bytes=0,
        label="ZIP64 fixture",
    )

    assert result.entry_count == 1
    assert result.central_directory_offset == 0
    assert result.central_directory_bytes == 46
    assert result.uses_zip64 is True


def test_zip_preflight_rejects_forged_trailer_count_after_scanning_records() -> None:
    payload = bytearray(_zip_bytes())
    eocd_offset = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<2H", payload, eocd_offset + 8, 1, 1)

    with pytest.raises(ValueError, match="member count exceeds the maximum 1"):
        preflight_zip_central_directory(
            io.BytesIO(payload),
            maximum_entries=1,
            maximum_central_directory_bytes=4096,
        )


def test_zip_preflight_rejects_inconsistent_central_directory_offset() -> None:
    payload = bytearray(_zip_bytes())
    eocd_offset = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<L", payload, eocd_offset + 16, 1)

    with pytest.raises(ValueError, match="offsets are inconsistent"):
        preflight_zip_central_directory(
            io.BytesIO(payload),
            maximum_entries=2,
            maximum_central_directory_bytes=4096,
        )
