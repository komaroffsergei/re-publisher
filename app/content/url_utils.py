from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.content.text_utils import URL_RE

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "yclid",
}


@dataclass(frozen=True)
class ExtractedUrl:
    url: str
    position: int


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{url.strip()}")
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING_PARAMS],
        doseq=True,
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, netloc, path, "", query, ""))


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).hostname
    return host.lower() if host else None


def detect_url_type(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host in {"github.com", "gist.github.com"} or host.endswith(".github.io"):
        return "github"
    if host in {"arxiv.org", "www.arxiv.org"}:
        return "arxiv"
    if host in {"huggingface.co", "www.huggingface.co"}:
        return "huggingface"
    if host in {"youtube.com", "www.youtube.com", "youtu.be"}:
        return "youtube"
    if host in {"t.me", "telegram.me"}:
        return "telegram"
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "image"
    if parsed.scheme in {"http", "https"}:
        return "article"
    return "unknown"


def youtube_video_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        return video_id or None
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        query = dict(parse_qsl(parsed.query))
        if query.get("v"):
            return query["v"]
        parts = [part for part in parsed.path.split("/") if part]
        for marker in ["shorts", "embed", "live"]:
            if marker in parts:
                index = parts.index(marker)
                if index + 1 < len(parts):
                    return parts[index + 1]
    return None


def youtube_thumbnail_url(url: str | None) -> str | None:
    video_id = youtube_video_id(url)
    if not video_id:
        return None
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def extract_urls_from_raw(raw: dict[str, Any] | None) -> list[ExtractedUrl]:
    found: list[ExtractedUrl] = []
    raw = raw or {}
    for entity in raw.get("entities") or []:
        url = entity.get("url")
        if url:
            found.append(ExtractedUrl(url=url, position=int(entity.get("offset") or 0)))
    media = raw.get("media") or {}
    webpage = media.get("webpage") or {}
    webpage_url = webpage.get("url")
    if webpage_url:
        found.append(ExtractedUrl(url=webpage_url, position=0))
    return found


def extract_urls(text: str | None, raw: dict[str, Any] | None = None) -> list[ExtractedUrl]:
    found = [ExtractedUrl(match.group(0).rstrip(".,;:!?)］】"), match.start()) for match in URL_RE.finditer(text or "")]
    found.extend(extract_urls_from_raw(raw))
    seen: set[str] = set()
    deduped: list[ExtractedUrl] = []
    for item in sorted(found, key=lambda value: value.position):
        normalized = normalize_url(item.url)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(ExtractedUrl(url=item.url, position=item.position))
    return deduped


def resolve_public_ips(hostname: str) -> list[str]:
    ips: list[str] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
        address = sockaddr[0]
        ip = ipaddress.ip_address(address)
        if not is_public_ip(ip):
            raise ValueError(f"blocked private address: {address}")
        ips.append(address)
    return sorted(set(ips))


def is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    blocked_metadata = ipaddress.ip_address("169.254.169.254")
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip == blocked_metadata
    )


def validate_fetch_url(url: str) -> str:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    resolve_public_ips(parsed.hostname)
    return normalized


def safe_join_redirect(base_url: str, location: str) -> str:
    return urljoin(base_url, location)
