"""Photo upload pipeline (the code half of the Intake Guard and Photo Quality agents).

limits -> decode + sanitize -> exact-duplicate check -> quality metrics -> store file + row.
The pipeline takes raw bytes only: user-supplied file names never reach the disk or the DB.
"""

import sqlite3
from pathlib import Path

from listing_assistant.config import Settings
from listing_assistant.db.repositories import AuditLogRepository, PhotoRepository
from listing_assistant.images import ImageRejectedError, measure_quality, sanitize_image
from listing_assistant.models import ActorType, AuditEntry, ListingPhoto, new_id


class PhotoRejectedError(ValueError):
    pass


def photo_path(settings: Settings, storage_key: str) -> Path:
    """Resolve a storage key to a file path that is guaranteed to stay inside photos_dir."""
    root = settings.photos_dir.resolve()
    path = (root / storage_key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("storage key escapes the photos directory")
    return path


def ingest_photo(
    conn: sqlite3.Connection, settings: Settings, listing_id: str, raw: bytes
) -> ListingPhoto:
    photos = PhotoRepository(conn)
    existing = photos.count_for_listing(listing_id)
    if existing >= settings.max_photos_per_listing:
        raise PhotoRejectedError(
            f"bir ilana en fazla {settings.max_photos_per_listing} fotoğraf eklenebilir"
        )

    try:
        image = sanitize_image(raw, max_bytes=settings.max_photo_bytes)
    except ImageRejectedError as exc:
        raise PhotoRejectedError(str(exc)) from exc

    if photos.exists_with_sha256(listing_id, image.sha256):
        raise PhotoRejectedError("bu fotoğraf zaten yüklendi")

    report = measure_quality(image)
    photo_id = new_id()
    # Building the model first validates listing_id before it is used in a file path.
    photo = ListingPhoto(
        id=photo_id,
        listing_id=listing_id,
        storage_key=f"{listing_id}/{photo_id}.jpg",
        sha256=image.sha256,
        perceptual_hash=report.perceptual_hash,
        width=image.width,
        height=image.height,
        blur_score=report.blur_score,
        brightness=report.brightness,
        quality_warnings=list(report.warnings),
        order_index=existing,
        is_cover=existing == 0,
    )

    path = photo_path(settings, photo.storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image.jpeg_bytes)
    try:
        photos.add(photo)
    except Exception:
        # Never leave an orphan file that no database row points to.
        path.unlink(missing_ok=True)
        raise

    AuditLogRepository(conn).record(
        AuditEntry(
            actor_type=ActorType.USER,
            actor_name="local_user",
            action="photo.upload",
            resource_type="listing_photo",
            resource_id=photo.id,
            details={
                "listing_id": listing_id,
                "sha256_prefix": image.sha256[:12],
                "warnings": ",".join(report.warnings),
            },
        )
    )
    return photo
