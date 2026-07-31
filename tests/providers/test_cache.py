from pathlib import Path

from topoforge.providers.cache import CacheIdentity, CacheStatus, ContentAddressedCache


def identity(url: str = "https://example.test/object.tif") -> CacheIdentity:
    return CacheIdentity(
        provider_id="fixture",
        dataset_id="fixture-dem",
        dataset_version="2026",
        url=url,
    )


def test_cache_stores_by_content_hash_and_reopens_verified_entry(tmp_path: Path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    source = tmp_path / "object.tif"
    source.write_bytes(b"deterministic fixture bytes")
    request = identity()

    stored = cache.store(
        request,
        source,
        etag='"fixture-etag"',
        last_modified="Thu, 31 Jul 2026 00:00:00 GMT",
        media_type="image/tiff",
        response_status=200,
        fetched_at="2026-07-31T00:00:00+00:00",
        attempts=1,
    )
    reopened = cache.lookup(request)

    assert stored.status is CacheStatus.HIT
    assert reopened.status is CacheStatus.HIT
    assert reopened.path is not None and reopened.path.read_bytes() == source.read_bytes()
    assert reopened.entry is not None
    assert reopened.entry.object_relpath == (
        f"objects/{reopened.entry.object_sha256[:2]}/{reopened.entry.object_sha256}"
    )
    assert reopened.entry.etag == '"fixture-etag"'
    assert cache.summary().content_objects == 1


def test_corrupt_object_is_reported_and_request_can_be_recovered(tmp_path: Path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    source = tmp_path / "object.bin"
    source.write_bytes(b"first bytes")
    request = identity()
    stored = cache.store(
        request,
        source,
        etag=None,
        last_modified=None,
        media_type=None,
        response_status=200,
        fetched_at="2026-07-31T00:00:00+00:00",
        attempts=1,
    )
    assert stored.path is not None
    stored.path.write_bytes(b"corrupt")

    corrupt = cache.lookup(request)
    assert corrupt.status is CacheStatus.CORRUPT
    assert "length" in corrupt.reason or "SHA-256" in corrupt.reason

    cache.invalidate(request)
    assert cache.lookup(request).status is CacheStatus.MISS


def test_request_identity_is_deterministic_and_separates_versions() -> None:
    first = identity()
    same = identity()
    newer = first.model_copy(update={"dataset_version": "2027"})

    assert first.request_key == same.request_key
    assert first.request_key != newer.request_key
