from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
HASHTAG_RE = re.compile(r"(?<!\w)#([\w\d_]+)", re.UNICODE)
MENTION_RE = re.compile(r"(?<!\w)@([\w\d_]{3,})", re.UNICODE)
EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\u2600-\u27bf"
    "]+",
    flags=re.UNICODE,
)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_text(value: str | None) -> str:
    text = clean_text(value).lower()
    text = URL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sha256_text(value: str | None) -> str | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def word_count(value: str | None) -> int:
    return len(re.findall(r"[\wа-яА-ЯёЁ]+", value or "", flags=re.UNICODE))


def emoji_count(value: str | None) -> int:
    return len(EMOJI_RE.findall(value or ""))


def hashtags(value: str | None) -> list[str]:
    return sorted({match.group(1).lower() for match in HASHTAG_RE.finditer(value or "")})


def mentions(value: str | None) -> list[str]:
    return sorted({match.group(1).lower() for match in MENTION_RE.finditer(value or "")})


def has_code_markers(value: str | None) -> bool:
    text = value or ""
    return "```" in text or bool(re.search(r"\b(import|def|class|npm|pip|docker|SELECT|function)\b", text))


def detect_language(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    cyr = len(re.findall(r"[а-яА-ЯёЁ]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    if cyr >= max(8, lat):
        return "ru"
    if lat >= 8 and lat > cyr:
        try:
            from langdetect import detect

            return detect(text[:1000])
        except Exception:
            return "en"
    return None


def first_nonempty(values: Iterable[str | None]) -> str | None:
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return None

