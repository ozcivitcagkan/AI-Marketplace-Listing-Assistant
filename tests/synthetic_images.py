"""Synthetic test images generated in code. No real photos are used anywhere in the tests."""

import io

import numpy as np
from PIL import Image


def synthetic_scene(width: int = 800, height: int = 600, seed: int = 0) -> Image.Image:
    """Random coloured 20px blocks: sharp edges, and a different picture per seed."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, size=(height // 20 + 1, width // 20 + 1, 3), dtype=np.uint8)
    pixels = np.kron(blocks, np.ones((20, 20, 1), dtype=np.uint8))[:height, :width]
    return Image.fromarray(pixels, "RGB")


def encode(image: Image.Image, fmt: str = "JPEG", **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def jpeg_with_fake_gps(image: Image.Image) -> bytes:
    """JPEG carrying EXIF camera info and an obviously fake GPS position."""
    exif = Image.Exif()
    exif[0x010F] = "SyntheticCamera"  # Make
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"  # GPSLatitudeRef
    gps[2] = (12.0, 34.0, 56.0)  # GPSLatitude
    gps[3] = "E"
    gps[4] = (65.0, 43.0, 21.0)
    return encode(image, exif=exif)
