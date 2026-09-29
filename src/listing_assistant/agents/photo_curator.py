"""Photo Quality Curator: cover choice, order and privacy flags (architecture doc §4, §8).

Quality metrics were measured by code at upload. The only model judgement used here is
the `view` the Vision Analyst reported for each photo; the ordering itself is code:
exterior first, then interior, then details, better-quality photos earlier.
"""

from dataclasses import dataclass

from listing_assistant.agent_io import PhotoView
from listing_assistant.agents.vision_analyst import VisionReport
from listing_assistant.models import ListingPhoto, PrivacyFlag
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import PhotoWithBytes

VIEW_PRIORITY = [
    PhotoView.EXTERIOR_FRONT,
    PhotoView.EXTERIOR_SIDE,
    PhotoView.EXTERIOR_REAR,
    PhotoView.INTERIOR_FRONT,
    PhotoView.DASHBOARD,
    PhotoView.INTERIOR_REAR,
    PhotoView.TRUNK,
    PhotoView.WHEEL,
    PhotoView.ENGINE_BAY,
    PhotoView.OTHER,
]


@dataclass(frozen=True)
class CurationResult:
    order: tuple[str, ...]  # photo IDs, cover first
    privacy_flags: dict[str, tuple[PrivacyFlag, ...]]
    near_duplicates: tuple[tuple[str, str, int], ...]

    @property
    def cover_id(self) -> str:
        return self.order[0]


def order_photos(photos: list[ListingPhoto], views: dict[str, PhotoView]) -> list[str]:
    def sort_key(photo: ListingPhoto):
        view = views.get(photo.id, PhotoView.OTHER)
        return (
            VIEW_PRIORITY.index(view),
            len(photo.quality_warnings),
            -photo.blur_score,
            photo.order_index,
        )

    return [photo.id for photo in sorted(photos, key=sort_key)]


def run_photo_curator(run: AgentRun, vision: VisionReport) -> CurationResult:
    photos: list[PhotoWithBytes] = run.call("get_listing_photos")
    listing_photos = [p.photo for p in photos]
    views = {r.photo_id: r.view for r in vision.photos if r.analyzed}
    flags = {r.photo_id: r.privacy_flags for r in vision.photos}
    duplicates = run.call("find_duplicates", photos=listing_photos)
    return CurationResult(
        order=tuple(order_photos(listing_photos, views)),
        privacy_flags={p.id: flags.get(p.id, ()) for p in listing_photos},
        near_duplicates=tuple(duplicates),
    )
