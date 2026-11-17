"""Photo screening for grievance evidence.

This is a simple image-quality check (not real AI). It rejects files that are not
valid images, are far too small, or are blank / fully black / fully white.
The image is re-encoded as JPEG, which also removes hidden EXIF data (such as GPS).
"""
from PIL import Image, ImageStat, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 40_000_000  # guard against decompression bombs


def inspect_and_sanitize(src_path: str, dest_path: str, category: str = ""):
    """Return dict. On success the cleaned JPEG is written to dest_path."""
    try:
        with Image.open(src_path) as probe:
            probe.verify()  # structural integrity check
        with Image.open(src_path) as img:
            img.load()
            width, height = img.size
            if width < 180 or height < 180:
                return {"ok": False, "reason": f"photo is too small ({width}x{height}px, minimum 180x180)"}

            rgb = img.convert("RGB")
            sample = rgb.copy()
            sample.thumbnail((256, 256))
            stats = ImageStat.Stat(sample)
            variance = sum(stats.stddev) / 3.0
            brightness = sum(stats.mean) / 3.0

            if variance < 8.0:
                return {"ok": False, "reason": "photo looks blank or a single flat colour"}
            if brightness < 10.0 or brightness > 248.0:
                return {"ok": False, "reason": "photo is completely dark or completely white"}

            rgb.save(dest_path, "JPEG", quality=85, optimize=True)  # strips EXIF
            return {
                "ok": True,
                "confidence": 0.95 if variance > 25.0 else 0.85,
                "category": category or "Civic Infrastructure",
                "notes": f"Photo accepted ({width}x{height}px).",
            }
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return {"ok": False, "reason": "file is not a valid image"}
