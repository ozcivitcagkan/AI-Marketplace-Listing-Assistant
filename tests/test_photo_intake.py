import sqlite3

import pytest
from pydantic import ValidationError

from listing_assistant.db.repositories import AuditLogRepository, PhotoRepository
from listing_assistant.models import new_id
from listing_assistant.photo_intake import PhotoRejectedError, ingest_photo, photo_path
from synthetic_images import encode, jpeg_with_fake_gps, synthetic_scene


def stored_files(settings):
    return list(settings.photos_dir.rglob("*")) if settings.photos_dir.exists() else []


def test_ingest_stores_clean_file_and_row(conn, settings, listing):
    first = ingest_photo(conn, settings, listing.id, jpeg_with_fake_gps(synthetic_scene(seed=1)))
    second = ingest_photo(conn, settings, listing.id, encode(synthetic_scene(seed=2)))

    assert (first.order_index, first.is_cover) == (0, True)
    assert (second.order_index, second.is_cover) == (1, False)
    assert PhotoRepository(conn).list_for_listing(listing.id) == [first, second]

    saved = photo_path(settings, first.storage_key).read_bytes()
    assert b"SyntheticCamera" not in saved  # EXIF did not reach the disk


def test_upload_is_audited_without_raw_content(conn, settings, listing):
    photo = ingest_photo(conn, settings, listing.id, encode(synthetic_scene()))
    [entry] = AuditLogRepository(conn).list_for_resource(photo.id)
    assert entry.action == "photo.upload"
    assert set(entry.details) == {"listing_id", "sha256_prefix", "warnings"}


def test_exact_duplicate_is_rejected(conn, settings, listing):
    raw = encode(synthetic_scene())
    ingest_photo(conn, settings, listing.id, raw)
    with pytest.raises(PhotoRejectedError, match="zaten yüklendi"):
        ingest_photo(conn, settings, listing.id, raw)


def test_photo_count_limit(conn, settings, listing):
    for seed in range(settings.max_photos_per_listing):
        ingest_photo(conn, settings, listing.id, encode(synthetic_scene(seed=seed)))
    with pytest.raises(PhotoRejectedError, match="en fazla 3 fotoğraf"):
        ingest_photo(conn, settings, listing.id, encode(synthetic_scene(seed=99)))


def test_rejected_upload_leaves_nothing_behind(conn, settings, listing):
    with pytest.raises(PhotoRejectedError, match="geçerli bir resim değil"):
        ingest_photo(conn, settings, listing.id, b"MZ not really a .jpg")
    assert PhotoRepository(conn).count_for_listing(listing.id) == 0
    assert stored_files(settings) == []


def test_database_failure_removes_the_written_file(conn, settings):
    unknown_listing = new_id()  # passes format checks but violates the foreign key
    with pytest.raises(sqlite3.IntegrityError):
        ingest_photo(conn, settings, unknown_listing, encode(synthetic_scene()))
    assert [p for p in stored_files(settings) if p.is_file()] == []


@pytest.mark.parametrize("bad_listing_id", ["../../outside", "C:\\Windows", ""])
def test_malformed_listing_id_never_reaches_the_file_system(conn, settings, bad_listing_id):
    with pytest.raises(ValidationError):
        ingest_photo(conn, settings, bad_listing_id, encode(synthetic_scene()))
    assert stored_files(settings) == []


def test_photo_path_blocks_traversal(settings):
    with pytest.raises(ValueError, match="escapes"):
        photo_path(settings, "../../secrets.txt")
