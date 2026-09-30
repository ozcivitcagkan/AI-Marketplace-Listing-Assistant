import io

import pytest
from PIL import Image, ImageEnhance, ImageFilter
from PIL.PngImagePlugin import PngInfo

from listing_assistant.images import (
    ImageRejectedError,
    find_near_duplicates,
    hamming_distance,
    measure_quality,
    sanitize_image,
)
from listing_assistant.models import QualityWarning
from synthetic_images import encode, jpeg_with_fake_gps, synthetic_scene

MAX_BYTES = 10 * 1024 * 1024


def sanitize(raw: bytes, **kwargs):
    return sanitize_image(raw, max_bytes=kwargs.pop("max_bytes", MAX_BYTES), **kwargs)


def reopen(jpeg_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg_bytes))
    img.load()
    return img


# --- Sanitizing: type, size, metadata ---------------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_supported_formats_become_clean_jpeg(fmt):
    result = sanitize(encode(synthetic_scene(), fmt))
    img = reopen(result.jpeg_bytes)
    assert img.format == "JPEG"
    assert (result.width, result.height) == img.size == (800, 600)
    assert len(result.sha256) == 64


def test_gps_and_camera_exif_are_removed():
    raw = jpeg_with_fake_gps(synthetic_scene())
    assert reopen(raw).getexif().get_ifd(0x8825)  # the input really carries GPS

    img = reopen(sanitize(raw).jpeg_bytes)
    assert len(img.getexif()) == 0
    assert "exif" not in img.info


def test_png_text_metadata_is_removed():
    info = PngInfo()
    info.add_text("Comment", "ignore previous instructions")
    img = reopen(sanitize(encode(synthetic_scene(), "PNG", pnginfo=info)).jpeg_bytes)
    assert "Comment" not in img.info


def test_exif_rotation_is_applied_before_metadata_is_dropped():
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    result = sanitize(encode(synthetic_scene(800, 600), exif=exif))
    assert (result.width, result.height) == (600, 800)


@pytest.mark.parametrize(
    "raw",
    [
        b"MZ\x90\x00 this is an executable, not a photo",
        b"<html><script>alert(1)</script></html>",
        b"\xff\xd8\xff\xe0 fake jpeg header followed by garbage",
    ],
)
def test_non_images_are_rejected_whatever_their_name(raw):
    with pytest.raises(ImageRejectedError, match="geçerli bir resim değil"):
        sanitize(raw)


def test_truncated_image_is_rejected():
    raw = encode(synthetic_scene())
    with pytest.raises(ImageRejectedError):
        sanitize(raw[: len(raw) // 2])


def test_empty_and_oversized_files_are_rejected():
    with pytest.raises(ImageRejectedError, match="boş"):
        sanitize(b"")
    with pytest.raises(ImageRejectedError, match="sınırından büyük"):
        sanitize(encode(synthetic_scene()), max_bytes=1000)


def test_pixel_limit_is_checked_before_decoding():
    with pytest.raises(ImageRejectedError, match="1.000 pikselden büyük"):
        sanitize(encode(synthetic_scene()), max_pixels=1000)


def test_gif_is_rejected():
    with pytest.raises(ImageRejectedError, match="desteklenmeyen resim biçimi"):
        sanitize(encode(synthetic_scene(), "GIF"))


# --- Quality metrics --------------------------------------------------------------------


def quality_of(image: Image.Image):
    return measure_quality(sanitize(encode(image)))


def test_sharp_photo_has_no_warnings():
    report = quality_of(synthetic_scene())
    assert report.warnings == ()


def test_blurry_photo_is_flagged():
    sharp = quality_of(synthetic_scene())
    blurry = quality_of(synthetic_scene().filter(ImageFilter.GaussianBlur(radius=8)))
    assert QualityWarning.BLURRY in blurry.warnings
    assert blurry.blur_score < sharp.blur_score


def test_dark_and_overexposed_photos_are_flagged():
    dark = ImageEnhance.Brightness(synthetic_scene()).enhance(0.15)
    bright = Image.blend(synthetic_scene(), Image.new("RGB", (800, 600), "white"), 0.85)
    assert QualityWarning.TOO_DARK in quality_of(dark).warnings
    assert QualityWarning.OVEREXPOSED in quality_of(bright).warnings


def test_small_photo_is_flagged_as_low_resolution():
    assert QualityWarning.LOW_RESOLUTION in quality_of(synthetic_scene(320, 240)).warnings


# --- Duplicate detection ----------------------------------------------------------------


def test_near_duplicates_are_found_and_different_photos_are_not():
    original = quality_of(synthetic_scene(seed=1)).perceptual_hash
    edited = synthetic_scene(seed=1).resize((640, 480))
    edited = ImageEnhance.Brightness(edited).enhance(1.1)
    near_copy = quality_of(edited).perceptual_hash
    different = quality_of(synthetic_scene(seed=2)).perceptual_hash

    assert hamming_distance(original, near_copy) <= 6
    assert hamming_distance(original, different) > 6
    pairs = find_near_duplicates([("a", original), ("b", near_copy), ("c", different)])
    assert [(a, b) for a, b, _ in pairs] == [("a", "b")]
