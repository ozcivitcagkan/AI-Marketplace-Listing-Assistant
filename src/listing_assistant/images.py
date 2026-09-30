"""Pure image checks: safe decoding, metadata removal, re-encoding, quality metrics.

No file system or database access here, so every function is easy to test.
"""

import hashlib
import io
from dataclasses import dataclass
from itertools import combinations

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from listing_assistant.models import QualityWarning
from listing_assistant.text_utils import group_thousands

ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
# A tiny file can declare billions of pixels (decompression bomb); check before decoding.
MAX_IMAGE_PIXELS = 40_000_000
JPEG_QUALITY = 90

MIN_SHORT_SIDE = 480
MIN_LONG_SIDE = 640
# Measured on a fixed-size copy so scores are comparable across photo resolutions.
ANALYSIS_SIZE = 512
BLUR_THRESHOLD = 100.0
DARK_THRESHOLD = 50.0
BRIGHT_THRESHOLD = 205.0
NEAR_DUPLICATE_MAX_DISTANCE = 6


class ImageRejectedError(ValueError):
    pass


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.0f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} bayt"


@dataclass(frozen=True)
class SanitizedImage:
    jpeg_bytes: bytes
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class QualityReport:
    blur_score: float
    brightness: float
    perceptual_hash: str
    warnings: tuple[QualityWarning, ...]


def sanitize_image(
    raw: bytes, *, max_bytes: int, max_pixels: int = MAX_IMAGE_PIXELS
) -> SanitizedImage:
    """Decode untrusted bytes and return a metadata-free JPEG, or raise ImageRejectedError."""
    if not raw:
        raise ImageRejectedError("dosya boş")
    if len(raw) > max_bytes:
        raise ImageRejectedError(f"dosya {human_size(max_bytes)} sınırından büyük")

    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise ImageRejectedError(
                    f"desteklenmeyen resim biçimi: {img.format} (JPEG, PNG veya WEBP yükleyin)"
                )
            if getattr(img, "n_frames", 1) != 1:
                raise ImageRejectedError("hareketli veya çok kareli resimler desteklenmiyor")
            width, height = img.size  # read from the header; pixels are not decoded yet
            if width * height > max_pixels:
                raise ImageRejectedError(f"resim {group_thousands(max_pixels)} pikselden büyük")
            img.load()
            # Apply the EXIF rotation now, because the EXIF block is about to be dropped.
            rgb = ImageOps.exif_transpose(img).convert("RGB")
    except ImageRejectedError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ImageRejectedError("dosya geçerli bir resim değil") from exc

    # Rebuilt from raw pixels only: EXIF, GPS, ICC profiles and text chunks cannot survive.
    clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    buffer = io.BytesIO()
    clean.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    jpeg_bytes = buffer.getvalue()
    return SanitizedImage(
        jpeg_bytes=jpeg_bytes,
        width=clean.width,
        height=clean.height,
        sha256=hashlib.sha256(jpeg_bytes).hexdigest(),
    )


def measure_quality(image: SanitizedImage) -> QualityReport:
    with Image.open(io.BytesIO(image.jpeg_bytes)) as img:
        gray = img.convert("L")
    fingerprint = perceptual_hash(gray)
    gray.thumbnail((ANALYSIS_SIZE, ANALYSIS_SIZE))
    pixels = np.asarray(gray, dtype=np.float64)

    blur_score = laplacian_variance(pixels)
    brightness = float(pixels.mean())

    warnings = []
    short_side, long_side = sorted((image.width, image.height))
    if short_side < MIN_SHORT_SIDE or long_side < MIN_LONG_SIDE:
        warnings.append(QualityWarning.LOW_RESOLUTION)
    if blur_score < BLUR_THRESHOLD:
        warnings.append(QualityWarning.BLURRY)
    if brightness < DARK_THRESHOLD:
        warnings.append(QualityWarning.TOO_DARK)
    elif brightness > BRIGHT_THRESHOLD:
        warnings.append(QualityWarning.OVEREXPOSED)

    return QualityReport(
        blur_score=round(blur_score, 2),
        brightness=round(brightness, 2),
        perceptual_hash=fingerprint,
        warnings=tuple(warnings),
    )


def laplacian_variance(pixels: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian: high for sharp edges, low for blur."""
    center = pixels[1:-1, 1:-1]
    laplacian = (
        pixels[:-2, 1:-1] + pixels[2:, 1:-1] + pixels[1:-1, :-2] + pixels[1:-1, 2:] - 4 * center
    )
    return float(laplacian.var())


def perceptual_hash(gray: Image.Image) -> str:
    """64-bit difference hash: is each pixel brighter than its right neighbour?"""
    small = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = int("".join("1" if bit else "0" for bit in bits), 2)
    return f"{value:016x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()


def find_near_duplicates(
    hashes: list[tuple[str, str]], max_distance: int = NEAR_DUPLICATE_MAX_DISTANCE
) -> list[tuple[str, str, int]]:
    """Given (photo_id, perceptual_hash) pairs, return pairs that look almost identical."""
    pairs = []
    for (id_a, hash_a), (id_b, hash_b) in combinations(hashes, 2):
        distance = hamming_distance(hash_a, hash_b)
        if distance <= max_distance:
            pairs.append((id_a, id_b, distance))
    return pairs


MODEL_MAX_SIDE = 1568


def prepare_for_model(jpeg_bytes: bytes, max_side: int = MODEL_MAX_SIDE) -> bytes:
    """Downscale before sending to a vision model: full resolution only adds cost."""
    with Image.open(io.BytesIO(jpeg_bytes)) as img:
        rgb = img.convert("RGB")
    rgb.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
