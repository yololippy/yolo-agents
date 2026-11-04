"""Shared, bounded image contracts for product storage and runtime output."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
import zlib


ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
MAX_MESSAGE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MEDIA_STORE_BYTES = 512 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 25_000_000


class MediaValidationError(ValueError):
    pass


class MediaStorageFullError(RuntimeError):
    pass


def validate_filename(filename: object) -> str | None:
    if filename is None:
        return None
    if (not isinstance(filename, str) or not filename or len(filename) > 255
            or filename in (".", "..") or "/" in filename or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)):
        raise MediaValidationError("invalid image filename")
    return filename


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if (len(data) >= 45 and data.startswith(b"\x89PNG\r\n\x1a\n")
            and data[8:12] == b"\x00\x00\x00\r" and data[12:16] == b"IHDR"
            and int.from_bytes(data[29:33], "big") == zlib.crc32(data[12:29])
            and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")):
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        return None
    offset = 2
    starts_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9):
            continue
        if marker == 0xDA or offset + 2 > len(data):
            return None
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if marker in starts_of_frame and length >= 7:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if (len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP"
            or int.from_bytes(data[4:8], "little") + 8 != len(data)):
        return None
    offset, canvas, image = 12, None, None
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        start, end = offset + 8, offset + 8 + size
        if end > len(data):
            return None
        payload = data[start:end]
        if kind == b"VP8X" and len(payload) >= 10:
            canvas = (1 + int.from_bytes(payload[4:7], "little"),
                      1 + int.from_bytes(payload[7:10], "little"))
        elif kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            image = 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        elif kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            image = (int.from_bytes(payload[6:8], "little") & 0x3FFF,
                     int.from_bytes(payload[8:10], "little") & 0x3FFF)
        elif kind == b"ANMF" and len(payload) >= 16:
            image = (1 + int.from_bytes(payload[6:9], "little"),
                     1 + int.from_bytes(payload[9:12], "little"))
        offset = end + (size & 1)
    if offset != len(data) or image is None:
        return None
    return canvas or image


def inspect_image(data: object, media_type: object) -> tuple[bytes, str, int, int]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise MediaValidationError("image byte count is outside the supported range")
    if not isinstance(media_type, str) or media_type not in ALLOWED_IMAGE_TYPES:
        raise MediaValidationError("unsupported image media type")
    dimensions = {
        "image/jpeg": _jpeg_dimensions,
        "image/png": _png_dimensions,
        "image/webp": _webp_dimensions,
    }[media_type](data)
    if dimensions is None:
        raise MediaValidationError("image bytes do not match the declared media type")
    width, height = dimensions
    if (width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
            or width * height > MAX_IMAGE_PIXELS):
        raise MediaValidationError("image dimensions exceed the supported range")
    return data, media_type, width, height


def validate_media_ids(values: object) -> tuple[str, ...]:
    from uuid import UUID

    if not isinstance(values, list) or len(values) > MAX_IMAGES_PER_MESSAGE:
        raise MediaValidationError("media_ids must be a bounded array")
    result: list[str] = []
    for value in values:
        try:
            normalized = str(UUID(value))
        except (AttributeError, TypeError, ValueError):
            raise MediaValidationError("media_ids must contain UUIDs") from None
        if normalized in result:
            raise MediaValidationError("media_ids must be unique")
        result.append(normalized)
    return tuple(result)


@dataclass(frozen=True)
class RuntimeMedia:
    """One image returned by an Agent runtime; ChatService owns persistence."""

    data: bytes
    media_type: str
    filename: str | None = None
    alt_text: str | None = None

    def __post_init__(self) -> None:
        inspect_image(self.data, self.media_type)
        validate_filename(self.filename)
        if (self.alt_text is not None and (not isinstance(self.alt_text, str)
                or len(self.alt_text) > 2000 or any(ord(character) < 32 and character not in "\n\t"
                                                     for character in self.alt_text))):
            raise MediaValidationError("invalid image alt text")


def public_media(value: dict[str, Any]) -> dict[str, Any]:
    """Fail closed if an internal media row is accidentally exposed."""
    fields = (
        "id", "kind", "media_type", "byte_count", "sha256", "width", "height",
        "filename", "alt_text", "created_at",
    )
    result = {field: value.get(field) for field in fields}
    if (result["kind"] != "image" or result["media_type"] not in ALLOWED_IMAGE_TYPES
            or type(result["byte_count"]) is not int or type(result["width"]) is not int
            or type(result["height"]) is not int
            or not isinstance(result["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", result["sha256"]) is None):
        raise MediaValidationError("invalid stored media metadata")
    return result
