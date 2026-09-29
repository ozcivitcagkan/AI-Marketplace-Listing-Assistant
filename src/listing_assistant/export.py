"""Export package: copyable text plus the photos in their approved order (doc §1, §2).

No marketplace integration: the seller copies the text and uploads the photos manually.
Title and description are also written as separate files, because marketplace forms ask
for them in separate boxes. The ZIP is built deterministically (fixed timestamps), so the
same approved listing always produces byte-identical output.
"""

import io
import zipfile
from dataclasses import dataclass, field

from listing_assistant.listing_format import RenderedListing

_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ExportPackage:
    draft_version: int
    title: str
    description: str
    photo_count: int
    zip_bytes: bytes = field(repr=False)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.description}\n"


def build_export(
    listing: RenderedListing, draft_version: int, ordered_photos: list[bytes]
) -> ExportPackage:
    buffer = io.BytesIO()
    files = {
        "listing.txt": listing.text,
        "title.txt": listing.title + "\n",
        "description.txt": listing.description + "\n",
    }
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(zipfile.ZipInfo(name, _FIXED_TIMESTAMP), content.encode("utf-8"))
        for index, jpeg in enumerate(ordered_photos, start=1):
            archive.writestr(zipfile.ZipInfo(f"photos/{index:02d}.jpg", _FIXED_TIMESTAMP), jpeg)
    return ExportPackage(
        draft_version=draft_version,
        title=listing.title,
        description=listing.description,
        photo_count=len(ordered_photos),
        zip_bytes=buffer.getvalue(),
    )
