from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import or_, select

from app.content.text_utils import clean_text
from app.content.url_utils import detect_url_type, normalize_url
from app.models import LinkSnapshot, MediaAsset, PostLink

ARTICLE_LIKE_URL_TYPES = {"article", "arxiv", "telegram"}
LINK_SUMMARY_PENDING_STATUS = "link_summary_pending"
LINK_SUMMARY_FAILED_STATUS = "link_summary_failed"


@dataclass(frozen=True)
class LinkMaterial:
    link: PostLink
    snapshot: LinkSnapshot | None = None
    image_asset: MediaAsset | None = None


@dataclass(frozen=True)
class LinkSummaryGate:
    ok: bool
    status: str | None = None
    reason: str | None = None
    pending_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class TextSegment:
    text: str
    url: str | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class LinkSlot:
    anchor: str
    url: str
    url_type: str | None = None
    start: int | None = None
    end: int | None = None
    material: LinkMaterial | None = None


@dataclass(frozen=True)
class NumberedSection:
    number: int
    title: str
    text: str
    start: int
    end: int
    slots: list[LinkSlot]


def is_article_like_link(link: PostLink) -> bool:
    return str(link.url_type or "").lower() in ARTICLE_LIKE_URL_TYPES


def link_display_url(material: LinkMaterial) -> str:
    return (
        clean_text(material.snapshot.final_url if material.snapshot else None)
        or clean_text(material.link.final_url)
        or clean_text(material.link.canonical_url)
        or clean_text(material.link.original_url)
    )


def link_title(material: LinkMaterial) -> str:
    return (
        clean_text(material.snapshot.title if material.snapshot else None)
        or clean_text(material.snapshot.site_name if material.snapshot else None)
        or clean_text(material.link.domain)
        or link_display_url(material)
    )


def link_has_summary(material: LinkMaterial) -> bool:
    return bool(clean_text(material.snapshot.summary_short if material.snapshot else None))


def link_summary_gate(materials: list[LinkMaterial]) -> LinkSummaryGate:
    pending = 0
    failed = 0
    for material in materials:
        if not is_article_like_link(material.link) or link_has_summary(material):
            continue
        snapshot = material.snapshot
        status = str(material.link.extraction_status or "").lower()
        if status == "failed" or clean_text(snapshot.error if snapshot else None):
            failed += 1
        else:
            pending += 1
    if failed:
        return LinkSummaryGate(
            ok=False,
            status=LINK_SUMMARY_FAILED_STATUS,
            reason=f"article link summaries failed: {failed}",
            pending_count=pending,
            failed_count=failed,
        )
    if pending:
        return LinkSummaryGate(
            ok=False,
            status=LINK_SUMMARY_PENDING_STATUS,
            reason=f"article link summaries pending: {pending}",
            pending_count=pending,
            failed_count=failed,
        )
    return LinkSummaryGate(ok=True)


def summarized_article_materials(materials: list[LinkMaterial]) -> list[LinkMaterial]:
    return [material for material in materials if is_article_like_link(material.link) and link_has_summary(material)]


def format_link_summary_context(materials: list[LinkMaterial], *, max_summary_chars: int = 900) -> str:
    lines: list[str] = []
    for index, material in enumerate(summarized_article_materials(materials), start=1):
        summary = clip_for_material(material.snapshot.summary_short if material.snapshot else None, max_summary_chars)
        if not summary:
            continue
        lines.append(
            "\n".join(
                [
                    f"{index}. {link_title(material)}",
                    f"URL: {link_display_url(material)}",
                    f"Summary: {summary}",
                ]
            )
        )
    return clean_text("\n\n".join(lines))


def append_link_materials_section(body: str, materials: list[LinkMaterial], *, max_summary_chars: int = 700) -> str:
    rows: list[str] = []
    for index, material in enumerate(summarized_article_materials(materials), start=1):
        summary = clip_for_material(material.snapshot.summary_short if material.snapshot else None, max_summary_chars)
        if not summary:
            continue
        rows.append(f"{index}. {link_title(material)}\n{summary}\n{link_display_url(material)}")
    if not rows:
        return clean_text(body)
    section = "Материалы по ссылкам:\n" + "\n\n".join(rows)
    return clean_text(f"{body}\n\n{section}")


def clip_for_material(text: str | None, max_chars: int) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0].strip()


async def load_link_materials(session, post_id: int) -> list[LinkMaterial]:
    result = await session.execute(
        select(PostLink, LinkSnapshot, MediaAsset)
        .outerjoin(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
        .outerjoin(MediaAsset, MediaAsset.id == LinkSnapshot.image_asset_id)
        .where(PostLink.post_id == post_id)
        .order_by(PostLink.position_index.nulls_last(), PostLink.id)
    )
    return [LinkMaterial(link=link, snapshot=snapshot, image_asset=image_asset) for link, snapshot, image_asset in result.all()]


async def load_media_assets_for_post(session, post_id: int, materials: list[LinkMaterial]) -> list[MediaAsset]:
    image_asset_ids = [material.snapshot.image_asset_id for material in materials if material.snapshot and material.snapshot.image_asset_id]
    conditions = [MediaAsset.source_post_id == post_id]
    if image_asset_ids:
        conditions.append(MediaAsset.id.in_(image_asset_ids))
    result = await session.execute(select(MediaAsset).where(or_(*conditions)).order_by(MediaAsset.id))
    seen: set[int] = set()
    assets: list[MediaAsset] = []
    for asset in result.scalars():
        if asset.id in seen:
            continue
        seen.add(asset.id)
        assets.append(asset)
    return assets


def normalized_url_key(url: str | None) -> str:
    if not clean_text(url):
        return ""
    try:
        return normalize_url(clean_text(url))
    except Exception:
        return clean_text(url)


def material_url_keys(material: LinkMaterial) -> set[str]:
    values = {
        material.link.original_url,
        material.link.canonical_url,
        material.link.final_url,
    }
    if material.snapshot:
        values.update({material.snapshot.canonical_url, material.snapshot.final_url})
    return {key for key in (normalized_url_key(value) for value in values) if key}


def material_index_by_url(materials: list[LinkMaterial]) -> dict[str, LinkMaterial]:
    index: dict[str, LinkMaterial] = {}
    for material in materials:
        for key in material_url_keys(material):
            index.setdefault(key, material)
    return index


def link_slots_for_post(text: str | None, raw: dict[str, Any] | None, materials: list[LinkMaterial]) -> list[LinkSlot]:
    material_index = material_index_by_url(materials)
    slots: list[LinkSlot] = []
    for segment in telegram_link_segments(text, raw):
        if not segment.url:
            continue
        key = normalized_url_key(segment.url)
        material = material_index.get(key)
        url_type = material.link.url_type if material else detect_url_type(key or segment.url)
        slots.append(
            LinkSlot(
                anchor=clean_text(segment.text),
                url=segment.url,
                url_type=url_type,
                start=segment.start,
                end=segment.end,
                material=material,
            )
        )
    return slots


NUMBERED_SECTION_RE = re.compile(r"(?m)^\s*(\d{1,2})[.)]\s+([^\n]+)")


def parse_numbered_sections(text: str | None, slots: list[LinkSlot]) -> tuple[str, list[NumberedSection]]:
    source = text or ""
    matches = list(NUMBERED_SECTION_RE.finditer(source))
    if not matches:
        return clean_text(source), []
    preamble = clean_text(source[: matches[0].start()])
    sections: list[NumberedSection] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        section_slots = [slot for slot in slots if slot.start is not None and start <= slot.start < end]
        sections.append(
            NumberedSection(
                number=int(match.group(1)),
                title=clean_text(match.group(2)),
                text=clean_text(source[start:end]),
                start=start,
                end=end,
                slots=section_slots,
            )
        )
    return preamble, sections


def render_segments_range(segments: list[TextSegment], start: int, end: int) -> str:
    parts: list[str] = []
    for segment in segments:
        if segment.start is None or segment.end is None:
            continue
        overlap_start = max(start, segment.start)
        overlap_end = min(end, segment.end)
        if overlap_end <= overlap_start:
            continue
        left = overlap_start - segment.start
        right = overlap_end - segment.start
        text = segment.text[left:right]
        parts.append(markdown_link_for_segment(TextSegment(text=text, url=segment.url)))
    return clean_text("".join(parts))


def section_summary_rows(section: NumberedSection, *, max_summary_chars: int = 550) -> list[str]:
    rows: list[str] = []
    seen_urls: set[str] = set()
    for slot in section.slots:
        material = slot.material
        if not material or not is_article_like_link(material.link) or not link_has_summary(material):
            continue
        url = link_display_url(material)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        label = clean_text(slot.anchor) or link_title(material)
        summary = clip_for_material(material.snapshot.summary_short if material.snapshot else None, max_summary_chars)
        if summary:
            rows.append(f"- {label}: {summary}")
    return rows


def build_enriched_numbered_digest_body(
    text: str | None,
    raw: dict[str, Any] | None,
    materials: list[LinkMaterial],
    *,
    max_summary_chars: int = 550,
) -> tuple[str | None, dict[str, Any]]:
    slots = link_slots_for_post(text, raw, materials)
    preamble, sections = parse_numbered_sections(text, slots)
    if len(sections) < 2:
        return None, {"numbered_sections": len(sections), "link_slots": len(slots)}
    segments = telegram_link_segments(text, raw)
    parts: list[str] = []
    if preamble:
        parts.append(preamble)
    enriched_sections = 0
    for section in sections:
        section_text = render_segments_range(segments, section.start, section.end)
        rows = section_summary_rows(section, max_summary_chars=max_summary_chars)
        if rows:
            enriched_sections += 1
            section_text = clean_text(f"{section_text}\n\nSummary материалов:\n" + "\n".join(rows))
        parts.append(section_text)
    metadata = {
        "numbered_sections": len(sections),
        "link_slots": len(slots),
        "enriched_sections": enriched_sections,
    }
    return clean_text("\n\n".join(part for part in parts if clean_text(part))), metadata


def telegram_link_segments(text: str | None, raw: dict[str, Any] | None) -> list[TextSegment]:
    source = text or ""
    if not source:
        return []
    entities = raw.get("entities") if isinstance(raw, dict) else None
    link_entities: list[tuple[int, int, int, int, str]] = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        offset = entity.get("offset")
        length = entity.get("length")
        if offset is None or length is None:
            continue
        offset = int(offset)
        length = int(length)
        start = utf16_offset_to_index(source, offset)
        end = utf16_offset_to_index(source, offset + length)
        if end <= start:
            continue
        url = clean_text(entity.get("url"))
        if not url and entity.get("_") == "MessageEntityUrl":
            url = source[start:end]
        if url and meaningful_link_label(source[start:end]):
            link_entities.append((start, end, offset, length, url))
    if not link_entities:
        return [TextSegment(source, start=0, end=len(source))]

    segments: list[TextSegment] = []
    cursor = 0
    for start, end, _offset, _length, url in sorted(link_entities, key=lambda item: (item[0], -(item[1] - item[0]))):
        if start < cursor:
            continue
        if start > cursor:
            segments.append(TextSegment(source[cursor:start], start=cursor, end=start))
        if end > start:
            segments.append(TextSegment(source[start:end], url, start=start, end=end))
        cursor = max(cursor, end)
    if cursor < len(source):
        segments.append(TextSegment(source[cursor:], start=cursor, end=len(source)))
    return segments


def telegram_text_with_markdown_links(text: str | None, raw: dict[str, Any] | None) -> str:
    return "".join(markdown_link_for_segment(segment) for segment in telegram_link_segments(text, raw))


def markdown_link_for_segment(segment: TextSegment) -> str:
    if not segment.url:
        return segment.text
    prefix_len = len(segment.text) - len(segment.text.lstrip())
    suffix_len = len(segment.text) - len(segment.text.rstrip())
    prefix = segment.text[:prefix_len]
    suffix = segment.text[len(segment.text) - suffix_len :] if suffix_len else ""
    label_text = segment.text[prefix_len : len(segment.text) - suffix_len if suffix_len else len(segment.text)]
    if not meaningful_link_label(label_text):
        return segment.text
    label = label_text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    url = segment.url.replace(")", "%29")
    return f"{prefix}[{label}]({url}){suffix}"


MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,240})\]\((https?://[^)\s]+)\)")


def markdown_link_segments(text: str | None) -> list[TextSegment]:
    source = text or ""
    if not source:
        return []
    segments: list[TextSegment] = []
    cursor = 0
    for match in MARKDOWN_LINK_RE.finditer(source):
        if match.start() > cursor:
            segments.append(TextSegment(source[cursor : match.start()]))
        segments.append(TextSegment(match.group(1), match.group(2)))
        cursor = match.end()
    if cursor < len(source):
        segments.append(TextSegment(source[cursor:]))
    return segments or [TextSegment(source)]


def restore_missing_markdown_links(body: str, source_segments: list[TextSegment]) -> tuple[str, list[str]]:
    restored_body = clean_text(body)
    if not restored_body:
        return restored_body, []

    existing_urls = set(re.findall(r"https?://[^\s)]+", restored_body))
    restored_urls: list[str] = []
    fallback_items: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for segment in source_segments:
        url = clean_text(segment.url)
        label = clean_text(segment.text)
        if not url or url in seen_urls or url in existing_urls or not meaningful_link_label(label):
            continue
        seen_urls.add(url)
        linked_label = f"[{label}]({url})"
        if label in restored_body and linked_label not in restored_body:
            restored_body = restored_body.replace(label, linked_label, 1)
            restored_urls.append(url)
        else:
            fallback_items.append((label, url, linked_label))
    if fallback_items:
        numbered_body = append_numbered_fallback_links(restored_body, source_segments, fallback_items)
        if numbered_body != restored_body:
            restored_body = numbered_body
        else:
            fallback_links = [item[2] for item in fallback_items]
            restored_body = clean_text(f"{restored_body}\n\nСсылки из исходного поста: {' | '.join(fallback_links)}")
        restored_urls.extend(item[1] for item in fallback_items)
    return restored_body, restored_urls


def append_numbered_fallback_links(
    body: str,
    source_segments: list[TextSegment],
    fallback_items: list[tuple[str, str, str]],
) -> str:
    if not NUMBERED_SECTION_RE.search(body):
        return body
    source_text = "".join(segment.text for segment in source_segments)
    slots = [
        LinkSlot(anchor=clean_text(segment.text), url=segment.url, start=segment.start, end=segment.end)
        for segment in source_segments
        if clean_text(segment.url)
    ]
    _preamble, source_sections = parse_numbered_sections(source_text, slots)
    if not source_sections:
        return body
    fallback_by_url = {url: linked_label for _label, url, linked_label in fallback_items}
    links_by_number: dict[int, list[str]] = {}
    for section in source_sections:
        for slot in section.slots:
            linked_label = fallback_by_url.get(slot.url)
            if linked_label:
                links_by_number.setdefault(section.number, []).append(linked_label)
    if not links_by_number:
        return body
    matches = list(NUMBERED_SECTION_RE.finditer(body))
    if not matches:
        return body
    rebuilt: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        rebuilt.append(body[cursor:section_end].rstrip())
        number = int(match.group(1))
        links = links_by_number.get(number)
        if links:
            rebuilt.append("\n\nСсылки: " + " | ".join(links))
        cursor = section_end
    if cursor < len(body):
        rebuilt.append(body[cursor:])
    return clean_text("".join(rebuilt))


def meaningful_link_label(label: str) -> bool:
    stripped = label.strip()
    if not stripped or stripped in {"|", "-", "—", "•"}:
        return False
    return any(char.isalnum() for char in stripped) or len(stripped) > 1


def utf16_offset_to_index(text: str, offset: int) -> int:
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)
