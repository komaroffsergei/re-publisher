from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.content.selection_filters import load_states, preview, remove_mark, trace_text
from app.content.selection_rules import taxonomy_catalog, validate_expression
from app.config import get_settings
from app.models import (FilterApplication, FilterEvaluation, FilterMark, FilterMarkEvent, PipelineEntry, PostFilterMark,
                        SelectionFilter, SelectionFilterVersion, TaxonomyClassification, TelegramChat, TelegramPost)


class MarkInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#a78bfa", pattern=r"^#[0-9a-fA-F]{6}$")
    archived: bool = False

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Укажите название")
        return value


class EmptyInput(BaseModel):
    pass


class FilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    model_key: Literal["tfidf", "minilm"] = "tfidf"
    profile: Literal["taxonomy", "humor_ocr"] = "taxonomy"
    mark_id: int = Field(gt=0)
    expression: dict
    filter_id: int | None = None
    base_version_id: int | None = None
    preview_digest: str | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("Укажите название фильтра")
        return value.strip()

    @field_validator("expression")
    @classmethod
    def validate_tree(cls, value, info: ValidationInfo):
        return validate_expression(value, info.data.get("profile", "taxonomy"))


def digest(draft):
    data = draft.model_dump(exclude={"preview_digest"})
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def input_data(request, model):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Неверный источник запроса")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Нужен application/json")
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


async def check_mark(session, draft):
    mark = await session.get(FilterMark, draft.mark_id)
    if mark is None or (mark.archived and draft.enabled):
        raise HTTPException(409, "Выберите действующий признак из словаря")


def check_automatic_profile(draft, settings):
    if draft.enabled and draft.profile == "humor_ocr" and not settings.humor_auto_enabled:
        raise HTTPException(409, "Автоматический отбор юмора ещё не допущен. Сохраните фильтр выключенным; ручная проверка и предпросмотр доступны.")


async def enqueue_application(session, version):
    max_id = (await session.execute(select(func.max(PipelineEntry.id)))).scalar_one() or 0
    application = FilterApplication(version_id=version.id, max_entry_id=max_id)
    session.add(application)
    await session.flush()
    return application


def register_filter_routes(app, require_auth, session_factory, templates):
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get("/marks", response_class=HTMLResponse)
    async def marks_page(request: Request):
        return templates.TemplateResponse(request, "filter_marks.html", {})

    @router.get("/pipeline/filters", response_class=HTMLResponse)
    async def filters_page(request: Request):
        return templates.TemplateResponse(request, "selection_filters.html", {})

    @router.get("/api/pipeline/marks")
    async def list_marks(request: Request):
        async with session_factory(request)() as session:
            rows = (await session.execute(select(FilterMark, func.count(PostFilterMark.id)).outerjoin(
                PostFilterMark, (PostFilterMark.mark_id == FilterMark.id) & PostFilterMark.active.is_(True))
                .group_by(FilterMark.id).order_by(FilterMark.id))).all()
            return {"marks": [{"id": mark.id, "name": mark.name,
                "label_id": mark.label_id, "description": mark.description,
                "color": mark.color, "archived": mark.archived, "count": count} for mark, count in rows]}

    @router.post("/api/pipeline/marks")
    @router.put("/api/pipeline/marks/{mark_id}")
    async def save_mark(request: Request, mark_id: int | None = None):
        data = await input_data(request, MarkInput)
        async with session_factory(request)() as session:
            mark = await session.get(FilterMark, mark_id) if mark_id else FilterMark()
            if mark is None:
                raise HTTPException(404, "Признак не найден")
            if data.archived and (await session.execute(select(SelectionFilter.id).join(
                SelectionFilterVersion, SelectionFilterVersion.id == SelectionFilter.active_version_id).where(
                SelectionFilterVersion.mark_id == mark_id, SelectionFilter.enabled.is_(True),
                SelectionFilter.archived.is_(False)).limit(1))).scalar_one_or_none():
                raise HTTPException(409, "Сначала отключите фильтры, назначающие этот признак")
            for field, value in data.model_dump().items():
                setattr(mark, field, value)
            mark.updated_at = datetime.now(timezone.utc)
            session.add(mark)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(409, "Такое название признака уже существует") from exc
            return {"id": mark.id}

    @router.get("/api/pipeline/filters")
    async def list_filters(request: Request):
        async with session_factory(request)() as session:
            rows = (await session.execute(select(SelectionFilter, SelectionFilterVersion, FilterMark).join(
                SelectionFilterVersion, SelectionFilterVersion.id == SelectionFilter.active_version_id).join(
                FilterMark, FilterMark.id == SelectionFilterVersion.mark_id).order_by(SelectionFilter.id))).all()
            applications = {}
            for job in (await session.execute(select(FilterApplication).order_by(FilterApplication.id.desc()))).scalars():
                applications.setdefault(job.version_id, {key: getattr(job, key) for key in (
                    "id", "status", "processed", "matched", "unknown", "backfilled", "error")})
            matches = dict((await session.execute(select(FilterEvaluation.version_id, func.count(func.distinct(FilterEvaluation.entry_id)))
                .join(TaxonomyClassification, (TaxonomyClassification.pipeline_entry_id == FilterEvaluation.entry_id)
                      & (TaxonomyClassification.current_run_id == FilterEvaluation.run_id))
                .join(TelegramPost, TelegramPost.id == TaxonomyClassification.source_post_id)
                .where(FilterEvaluation.outcome == "matched", TaxonomyClassification.status == "complete",
                       TelegramPost.is_deleted.is_(False))
                .group_by(FilterEvaluation.version_id))).all())
            return {"catalog": taxonomy_catalog(), "catalogs": {p: taxonomy_catalog(p) for p in ("taxonomy", "humor_ocr")}, "filters": [{"id": item.id, "name": item.name,
                "enabled": item.enabled, "archived": item.archived, "base_version_id": version.id,
                "number": version.number, "model_key": version.model_key, "mark_id": version.mark_id,
                "profile": version.profile,
                "assigned_label_id": version.assigned_label_id,
                "mark_name": mark.name,
                "expression": version.expression, "matches": matches.get(version.id, 0),
                "application": applications.get(version.id)} for item, version, mark in rows]}

    @router.post("/api/pipeline/filters/preview")
    async def preview_filter(request: Request):
        draft = await input_data(request, FilterInput)
        async with session_factory(request)() as session:
            await check_mark(session, draft)
            values = draft.model_dump()
            result = await preview(session, SimpleNamespace(**values))
            return {**result, "preview_digest": digest(draft)}

    @router.post("/api/pipeline/filters/apply")
    async def apply_filter(request: Request):
        draft = await input_data(request, FilterInput)
        check_automatic_profile(draft, get_settings())
        if draft.preview_digest != digest(draft):
            raise HTTPException(409, "Сначала обновите предпросмотр этих условий")
        async with session_factory(request)() as session:
            await check_mark(session, draft)
            item = (await session.execute(select(SelectionFilter).where(SelectionFilter.id == draft.filter_id)
                .with_for_update())).scalar_one_or_none() if draft.filter_id else SelectionFilter()
            if item is None or item.archived:
                raise HTTPException(404, "Фильтр не найден или архивирован")
            if draft.filter_id and item.active_version_id != draft.base_version_id:
                raise HTTPException(409, "Фильтр уже изменён. Перезагрузите его настройки")
            number = 1
            if item.active_version_id:
                previous = await session.get(SelectionFilterVersion, item.active_version_id)
                number = previous.number + 1
            item.name, item.enabled, item.archived = draft.name, draft.enabled, False
            item.updated_at = datetime.now(timezone.utc)
            session.add(item)
            await session.flush()
            version = SelectionFilterVersion(filter_id=item.id, number=number, name=draft.name,
                model_key=draft.model_key, profile=draft.profile, mark_id=draft.mark_id, expression=draft.expression)
            session.add(version)
            await session.flush()
            item.active_version_id = version.id
            # Новый профиль не запускает массовый OCR старого буфера при сохранении
            # фильтра. Его оценки появляются при ручном запуске и для новых постов.
            application = await enqueue_application(session, version) if draft.enabled and draft.profile == "taxonomy" else None
            await session.commit()
            return {"id": item.id, "version_id": version.id, "application_id": application.id if application else None}

    @router.post("/api/pipeline/filters/{filter_id}/archive")
    async def archive_filter(request: Request, filter_id: int):
        await input_data(request, EmptyInput)
        async with session_factory(request)() as session:
            item = await session.get(SelectionFilter, filter_id)
            if item is None:
                raise HTTPException(404, "Фильтр не найден")
            item.archived, item.enabled = True, False
            await session.commit()
            return {"id": item.id}

    @router.get("/api/pipeline/{entry_id}/marks")
    async def post_marks(request: Request, entry_id: int):
        async with session_factory(request)() as session:
            row = (await session.execute(select(PipelineEntry, TelegramPost).join(TelegramPost,
                TelegramPost.id == PipelineEntry.source_post_id).join(TelegramChat,
                TelegramChat.peer_id == TelegramPost.chat_peer_id).where(PipelineEntry.id == entry_id,
                TelegramChat.folder_name == "MAX"))).first()
            if row is None:
                raise HTTPException(404, "Карточка не найдена")
            return (await load_states(session, {entry_id: (row[0], None, row[1])}))[entry_id]

    @router.post("/api/pipeline/{entry_id}/marks/{mark_id}/remove")
    async def remove_post_mark(request: Request, entry_id: int, mark_id: int):
        await input_data(request, EmptyInput)
        async with session_factory(request)() as session:
            entry = (await session.execute(select(PipelineEntry).join(TelegramPost,
                TelegramPost.id == PipelineEntry.source_post_id).join(TelegramChat,
                TelegramChat.peer_id == TelegramPost.chat_peer_id).where(PipelineEntry.id == entry_id,
                TelegramChat.folder_name == "MAX").with_for_update(of=PipelineEntry))).scalar_one_or_none()
            if entry is None:
                raise HTTPException(404, "Карточка не найдена")
            await remove_mark(session, entry, mark_id)
            await session.commit()
            return {"stage": entry.stage}

    @router.get("/api/pipeline/{entry_id}/marks/history")
    async def mark_history(request: Request, entry_id: int):
        async with session_factory(request)() as session:
            exists = (await session.execute(select(PipelineEntry.id).join(TelegramPost,
                TelegramPost.id == PipelineEntry.source_post_id).join(TelegramChat,
                TelegramChat.peer_id == TelegramPost.chat_peer_id).where(PipelineEntry.id == entry_id,
                TelegramChat.folder_name == "MAX"))).scalar_one_or_none()
            if exists is None:
                raise HTTPException(404, "Карточка не найдена")
            rows = (await session.execute(select(FilterMarkEvent, FilterMark, FilterEvaluation, SelectionFilterVersion)
                .join(FilterMark, FilterMark.id == FilterMarkEvent.mark_id)
                .outerjoin(FilterEvaluation, FilterEvaluation.id == FilterMarkEvent.evaluation_id)
                .outerjoin(SelectionFilterVersion, SelectionFilterVersion.id == FilterEvaluation.version_id)
                .where(FilterMarkEvent.entry_id == entry_id).order_by(FilterMarkEvent.id.desc()).limit(100))).all()
            return {"events": [{"id": event.id, "mark_id": mark.id, "mark_name": mark.name,
                "action": event.action, "at": event.created_at.isoformat(),
                "filter_name": version.name if version else None, "version": version.number if version else None,
                "model_key": version.model_key if version else None, "run_id": evaluation.run_id if evaluation else None,
                "reason": trace_text(evaluation.trace) if evaluation else "Снят вручную"}
                for event, mark, evaluation, version in rows]}

    app.include_router(router)
