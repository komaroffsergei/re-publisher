from __future__ import annotations

from typing import Any


def image_url_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    metadata = metadata or {}
    for key in ["image_url", "og:image", "twitter:image"]:
        value = metadata.get(key)
        if value:
            return str(value)
    return None


__all__ = ["image_url_from_metadata"]
