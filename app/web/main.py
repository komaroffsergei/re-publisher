from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import typer
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text

from app.config import Settings, get_settings
from app.content.pipeline_stages import PIPELINE_STAGE_LABELS
from app.content.selection_filters import load_states
from app.content.selection_rules import taxonomy_catalog
from app.content.source_marking import telegram_post_source_url
from app.content.post_preparation import mark_source, album_posts
from app.pipeline_coordinator import reset_retry
from app.db import create_engine, create_session_factory
from app.logging_setup import setup_logging
from app.models import (
    FilterEvaluation,
    FilterMark,
    FilterMarkEvent,
    PipelineEntry,
    PostFilterMark,
    SelectionFilter,
    SelectionFilterVersion,
    TaxonomyClassification,
    TaxonomyRun,
    TelegramChat,
    TelegramPost,
    TelegramSyncState,
    ServiceRuntime,
)
from app.taxonomy.jobs import (
    enqueue as enqueue_taxonomy,
)
from app.taxonomy.jobs import (
    public_job,
    public_run,
)
from app.web.selection_routes import register_filter_routes
from app.web.publication_routes import register_publication_routes
from app.taxonomy.profiles import job_key, profile_of, PROFILES
from app.web.source_media import (
    album_primary,
    downloaded_media_path,
    source_media_items,
)

web_cli = typer.Typer(no_args_is_help=True)
security = HTTPBasic(auto_error=False)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
BOARD_STAGES = ("received", "sorted", "filtered", "marking", "ready")


def display_time(value):
    if not value:
        return "—"
    return (
        datetime.fromisoformat(value)
        .astimezone(ZoneInfo("Europe/Moscow"))
        .strftime("%d.%m.%Y · %H:%M")
    )


templates.env.filters["display_time"] = display_time


def now_moscow_iso():
    return datetime.now(ZoneInfo("Europe/Moscow")).isoformat()


def iso_datetime(value):
    return value.isoformat() if value else None


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings)
        app.state.db_engine = engine
        app.state.session_factory = create_session_factory(engine=engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Re Publisher", lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    register_filter_routes(app, require_auth, session_factory, templates)
    register_publication_routes(app, require_auth, session_factory, templates)
    register_routes(app)
    return app


@web_cli.callback()
def web_main() -> None:
    """Web app commands."""


def require_auth(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> None:
    settings: Settings = request.app.state.settings
    if not settings.web_basic_auth_user and not settings.web_basic_auth_password:
        return
    if credentials is None:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    user_ok = secrets.compare_digest(
        credentials.username, settings.web_basic_auth_user or ""
    )
    password_ok = secrets.compare_digest(
        credentials.password, settings.web_basic_auth_password or ""
    )
    if not (user_ok and password_ok):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def session_factory(request: Request):
    return request.app.state.session_factory


def parse_entry_ids(raw: str | None, *, limit: int = 500) -> list[int]:
    if not raw:
        return []
    entry_ids: list[int] = []
    seen: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            entry_id = int(chunk)
        except ValueError:
            continue
        if entry_id <= 0 or entry_id in seen:
            continue
        seen.add(entry_id)
        entry_ids.append(entry_id)
        if len(entry_ids) >= limit:
            break
    return entry_ids


async def board_state_for_entries(app, session, entry_ids):
    ids = parse_entry_ids(",".join(map(str, entry_ids)))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(PipelineEntry, TelegramPost)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .where(PipelineEntry.id.in_(ids), TelegramChat.folder_name == "MAX")
        )
    ).all()
    pairs = {entry.id: (entry, None, post) for entry, post in rows}
    jobs = {}
    for job in (
        await session.execute(
            select(TaxonomyClassification).where(
                TaxonomyClassification.pipeline_entry_id.in_(ids)
            )
        )
    ).scalars():
        jobs.setdefault(job.pipeline_entry_id, {})[job_key(job.model_key, profile_of(job))] = job
    selections = await load_states(session, pairs)
    states = {}
    for entry, post in rows:
        taxonomies = {
            key: public_job(job, post.text or "")
            for key, job in jobs.get(entry.id, {}).items()
        }
        humor_jobs = [j for j in jobs.get(entry.id, {}).values() if profile_of(j) == "humor_ocr"]
        if humor_jobs:
            from app.ocr.jobs import current_input
            from app.config import get_settings
            model_input, ocr_run = await current_input(session, entry.id, post, get_settings())
            for job in humor_jobs:
                if job.status in {"complete", "media_only", "empty"} and (model_input is None or not ocr_run or job.input_sha256 != ocr_run.input_sha256):
                    state = taxonomies[job_key(job.model_key, "humor_ocr")]
                    state.update(status="stale", result=None, elapsed_ms=None)
        active = any(
            job["status"] in {"ocr", "queued", "loading", "running"} for job in taxonomies.values()
        )
        selection = selections.get(entry.id, {"marks": [], "checks": []})
        states[entry.id] = {
            "entry_id": entry.id,
            "stage": entry.stage,
            "status": entry.status,
            "active": active,
            "taxonomies": taxonomies,
            "selection": selection,
            "deleted": post.is_deleted,
            "marked_source_url": entry.marked_source_url,
            "marked_text": entry.marked_text,
            "ready_at": iso_datetime(entry.ready_at),
            "auto_state": entry.auto_state if entry.auto_enabled else None,
            "auto_error": entry.last_error if entry.auto_enabled else None,
            "can_retry": entry.auto_enabled
            and entry.auto_state in {"blocked", "stopped"}
            and not post.is_deleted,
            "updated_at": iso_datetime(entry.updated_at),
            "last_operation_at": iso_datetime(entry.last_operation_at),
            "can_mark_source": not active
            and not post.is_deleted
            and entry.stage in {"filtered", "marking"}
            and any(
                job["status"] in {"complete", "media_only"}
                for job in taxonomies.values()
            )
            and (entry.stage == "marking" or bool(selection["marks"])),
        }
    return states


async def dashboard_data(session):
    # Только исходные посты MAX. Количество прогонов не выдаём за количество постов.
    scope = (
        select(TelegramPost.id)
        .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
        .where(TelegramChat.folder_name == "MAX")
    )
    stage_rows = (
        await session.execute(
            select(PipelineEntry.stage, func.count())
            .where(PipelineEntry.source_post_id.in_(scope))
            .group_by(PipelineEntry.stage)
        )
    ).all()
    jobs = (
        await session.execute(
            select(
                TaxonomyClassification.model_key,
                TaxonomyClassification.status,
                func.count(),
            )
            .where(TaxonomyClassification.source_post_id.in_(scope))
            .group_by(TaxonomyClassification.model_key, TaxonomyClassification.status)
        )
    ).all()
    runs = list(
        (
            await session.execute(
                select(TaxonomyRun)
                .where(TaxonomyRun.source_post_id.in_(scope))
                .order_by(TaxonomyRun.id.desc())
                .limit(12)
            )
        ).scalars()
    )
    timings = (
        await session.execute(
            select(
                TaxonomyRun.model_key, func.count(), func.avg(TaxonomyRun.elapsed_ms)
            )
            .where(
                TaxonomyRun.source_post_id.in_(scope),
                TaxonomyRun.status == "complete",
                TaxonomyRun.elapsed_ms.is_not(None),
            )
            .group_by(TaxonomyRun.model_key)
        )
    ).all()
    media = (
        await session.execute(
            select(TelegramPost.media_download_status, func.count())
            .where(TelegramPost.id.in_(scope), TelegramPost.media_type.is_not(None))
            .group_by(TelegramPost.media_download_status)
        )
    ).all()
    sync = (
        await session.execute(
            select(
                func.max(TelegramSyncState.last_synced_at),
                func.count().filter(TelegramSyncState.error.is_not(None)),
            )
            .join(TelegramChat, TelegramChat.peer_id == TelegramSyncState.chat_peer_id)
            .where(TelegramChat.folder_name == "MAX")
        )
    ).one()
    service_rows = list((await session.execute(select(ServiceRuntime))).scalars())
    automation = dict(
        (
            await session.execute(
                select(PipelineEntry.auto_state, func.count())
                .where(
                    PipelineEntry.auto_enabled.is_(True),
                    PipelineEntry.source_post_id.in_(scope),
                )
                .group_by(PipelineEntry.auto_state)
            )
        ).all()
    )
    return {
        "services": [
            {
                "name": row.name,
                "alive": bool(
                    row.heartbeat_at
                    and (datetime.now(timezone.utc) - row.heartbeat_at).total_seconds()
                    < 60
                    and not row.error
                ),
                "error": row.error,
                "last_success_at": iso_datetime(row.last_success_at),
                "started_at": iso_datetime(row.started_at),
            }
            for row in service_rows
        ],
        "automation": automation,
        "stages": dict(stage_rows),
        "total": sum(count for _, count in stage_rows),
        "jobs": [
            {"model": key, "status": status, "count": count}
            for key, status, count in jobs
        ],
        "timings": [
            {"model": key, "count": count, "ms": round(ms)}
            for key, count, ms in timings
        ],
        "runs": [
            {
                "id": run.id,
                "entry_id": run.pipeline_entry_id,
                "model": run.model_key,
                "status": run.status,
                "elapsed_ms": run.elapsed_ms,
                "version": run.model_version,
                "at": iso_datetime(run.finished_at or run.queued_at),
            }
            for run in runs
        ],
        "media": dict(media),
        "last_sync": iso_datetime(sync[0]),
        "sync_errors": sync[1],
        "updated_at": now_moscow_iso(),
    }


def register_routes(app: FastAPI) -> None:
    auth = Depends(require_auth)

    @app.get("/api/pipeline/board-state", dependencies=[auth])
    async def pipeline_board_state(request: Request, entry_ids: str = ""):
        ids = parse_entry_ids(entry_ids, limit=500)
        async with session_factory(request)() as session:
            entries = await board_state_for_entries(request.app, session, ids)
        return {
            "updated_at": now_moscow_iso(),
            "entries": {str(entry_id): state for entry_id, state in entries.items()},
        }

    @app.post("/api/pipeline/{entry_id}/mark-source", dependencies=[auth])
    async def pipeline_mark_source(request: Request, entry_id: int):
        origin = request.headers.get("origin")
        if (
            origin
            and urlsplit(origin).hostname
            != request.headers.get("host", "").split(":")[0]
        ):
            raise HTTPException(403, detail="Неверный источник запроса")
        if (
            request.headers.get("content-type", "").split(";")[0].strip()
            != "application/json"
        ):
            raise HTTPException(415, detail="Нужен application/json")
        async with session_factory(request)() as session:
            row = (
                await session.execute(
                    select(PipelineEntry, TelegramPost, TelegramChat)
                    .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                    .join(
                        TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id
                    )
                    .where(PipelineEntry.id == entry_id)
                    .with_for_update(of=PipelineEntry)
                )
            ).first()
            if row is None or row[2].folder_name != "MAX":
                raise HTTPException(404, detail="Карточка не найдена")
            entry, post, chat = row
            await mark_source(session, entry, post, chat, manual=True)
            reset_retry(entry)
            await session.commit()
            return {
                "stage": entry.stage,
                "status": entry.status,
                "source_url": entry.marked_source_url,
                "marked_text": entry.marked_text,
            }

    @app.post("/api/pipeline/{entry_id}/automation/retry", dependencies=[auth])
    async def automation_retry(request: Request, entry_id: int):
        origin = request.headers.get("origin")
        if (
            origin
            and urlsplit(origin).hostname
            != request.headers.get("host", "").split(":")[0]
        ):
            raise HTTPException(403, "Неверный источник запроса")
        if (
            request.headers.get("content-type", "").split(";")[0].strip()
            != "application/json"
        ):
            raise HTTPException(415, "Нужен application/json")
        async with session_factory(request)() as session:
            row = (
                await session.execute(
                    select(PipelineEntry, TelegramPost)
                    .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                    .join(
                        TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id
                    )
                    .where(
                        PipelineEntry.id == entry_id, TelegramChat.folder_name == "MAX"
                    )
                    .with_for_update(of=PipelineEntry)
                )
            ).first()
            if not row:
                raise HTTPException(404, "Карточка не найдена")
            entry, post = row
            if (
                post.is_deleted
                or not entry.auto_enabled
                or entry.auto_state not in {"blocked", "stopped"}
            ):
                raise HTTPException(
                    409, "Повтор доступен только остановленной карточке"
                )
            for item in await album_posts(session, post):
                if item.media_type and item.media_download_status in {
                    "missing",
                    "failed",
                }:
                    item.media_download_status = "pending"
            reset_retry(entry)
            entry.auto_state = "pending"
            await session.commit()
            return {"status": "pending"}

    @app.post("/api/pipeline/{entry_id}/taxonomy/{model_key}", dependencies=[auth])
    @app.post("/api/pipeline/{entry_id}/taxonomy", dependencies=[auth])
    async def pipeline_taxonomy(
        request: Request, entry_id: int, model_key: str = "tfidf"
    ):
        if not request.app.state.settings.taxonomy_enabled:
            raise HTTPException(409, detail="Сортировка отключена")
        origin = request.headers.get("origin")
        if (
            origin
            and urlsplit(origin).hostname
            != request.headers.get("host", "").split(":")[0]
        ):
            raise HTTPException(403, detail="Неверный источник запроса")
        if (
            request.headers.get("content-type", "").split(";")[0].strip()
            != "application/json"
        ):
            raise HTTPException(415, detail="Нужен application/json")
        async with session_factory(request)() as session:
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(422, "Нужен JSON-объект")
            if not isinstance(body, dict) or set(body) - {"profile"} or body.get("profile", "taxonomy") not in PROFILES:
                raise HTTPException(422, "Неизвестный профиль или поле")
            job = await enqueue_taxonomy(session, entry_id, model_key, body.get("profile", "taxonomy"))
            await session.commit()
            return public_job(job)

    @app.get("/api/pipeline/{entry_id}/taxonomy-runs", dependencies=[auth])
    async def pipeline_taxonomy_runs(
        request: Request, entry_id: int, before_id: int | None = None
    ):
        async with session_factory(request)() as session:
            row = (
                await session.execute(
                    select(TelegramPost, TelegramChat)
                    .join(
                        PipelineEntry, PipelineEntry.source_post_id == TelegramPost.id
                    )
                    .join(
                        TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id
                    )
                    .where(PipelineEntry.id == entry_id)
                )
            ).first()
            if row is None or row[1].folder_name != "MAX":
                raise HTTPException(404, detail="Карточка не найдена")
            statement = select(TaxonomyRun).where(
                TaxonomyRun.pipeline_entry_id == entry_id
            )
            if before_id is not None:
                statement = statement.where(TaxonomyRun.id < before_id)
            runs = list(
                (
                    await session.execute(
                        statement.order_by(TaxonomyRun.id.desc()).limit(51)
                    )
                ).scalars()
            )
            has_more = len(runs) > 50
            public_runs = [public_run(run, row[0].text) for run in runs[:50]]
            if any(profile_of(run) == "humor_ocr" for run in runs[:50]):
                from app.ocr.jobs import current_input
                value, ocr_run = await current_input(session, entry_id, row[0], request.app.state.settings)
                for item, run in zip(public_runs, runs[:50]):
                    item["is_current_input"] = item["is_current_text"] and (profile_of(run) == "taxonomy" or value is not None and ocr_run is not None and run.input_sha256 == ocr_run.input_sha256)
            return {
                "runs": public_runs,
                "next_before_id": runs[49].id if has_more else None,
                "catalog": taxonomy_catalog(),
                "catalogs": {p: taxonomy_catalog(p) for p in PROFILES},
            }

    @app.get("/api/pipeline/{entry_id}/ocr", dependencies=[auth])
    @app.post("/api/pipeline/{entry_id}/ocr", dependencies=[auth])
    async def pipeline_ocr(request: Request, entry_id: int):
        from app.web.selection_routes import input_data, EmptyInput
        from app.ocr.jobs import enqueue_ocr, public_ocr
        if request.method == "POST":
            await input_data(request, EmptyInput)
        async with session_factory(request)() as session:
            statement = select(PipelineEntry, TelegramPost).join(TelegramPost,
                TelegramPost.id == PipelineEntry.source_post_id).join(TelegramChat,
                TelegramChat.peer_id == TelegramPost.chat_peer_id).where(PipelineEntry.id == entry_id,
                TelegramChat.folder_name == "MAX")
            if request.method == "POST":
                statement = statement.with_for_update(of=PipelineEntry)
            row = (await session.execute(statement)).first()
            if not row:
                raise HTTPException(404, "Карточка не найдена")
            entry, post = row
            if request.method == "POST":
                active = (await session.execute(select(TaxonomyClassification.id).where(
                    TaxonomyClassification.pipeline_entry_id == entry_id,
                    TaxonomyClassification.status.in_(("queued", "loading", "running"))).limit(1))).scalar_one_or_none()
                if active:
                    raise HTTPException(409, "Дождитесь завершения классификации")
                await enqueue_ocr(session, entry, post, request.app.state.settings, retry=True)
                await session.commit()
            return await public_ocr(session, entry_id, post, request.app.state.settings)

    @app.get("/source-media/{post_id}", dependencies=[auth])
    async def source_media_file(request: Request, post_id: int):
        settings: Settings = request.app.state.settings
        async with session_factory(request)() as session:
            post = (
                await session.execute(
                    select(TelegramPost)
                    .join(
                        TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id
                    )
                    .where(
                        TelegramPost.id == post_id, TelegramChat.folder_name == "MAX"
                    )
                )
            ).scalar_one_or_none()
        if post is None:
            raise HTTPException(404)
        path = downloaded_media_path(post, settings.media_dir)
        if path is None:
            raise HTTPException(404)
        item = source_media_items([post], settings.media_dir)[0]
        inline = item["kind"] in {"image", "video"}
        return FileResponse(
            path,
            media_type=item["mime_type"] if inline else "application/octet-stream",
            filename=path.name,
            content_disposition_type="inline" if inline else "attachment",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.get("/health")
    async def health(request: Request):
        async with session_factory(request)() as session:
            await session.execute(text("SELECT 1"))
        return {"ok": True}

    @app.get("/", dependencies=[auth])
    async def home():
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse, dependencies=[auth])
    async def dashboard(request: Request):
        async with session_factory(request)() as session:
            data = await dashboard_data(session)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"data": data, "stage_labels": PIPELINE_STAGE_LABELS},
        )

    @app.get("/api/dashboard", dependencies=[auth])
    async def dashboard_api(request: Request):
        async with session_factory(request)() as session:
            return await dashboard_data(session)

    @app.get(
        "/api/pipeline/board-fragment", response_class=HTMLResponse, dependencies=[auth]
    )
    @app.get("/pipeline", response_class=HTMLResponse, dependencies=[auth])
    async def pipeline(
        request: Request,
        q: str = "",
        mark: str = "",
        selection_filter: str = "",
        order: str = "desc",
        limit: int = 30,
        active: bool = False,
    ):
        limit = max(1, min(limit, 100))
        try:
            mark = int(mark) if mark else None
            selection_filter = int(selection_filter) if selection_filter else None
        except ValueError as exc:
            raise HTTPException(422, "Некорректный ID лейбла или фильтра") from exc
        statement = (
            select(PipelineEntry, TelegramPost, TelegramChat)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .where(TelegramChat.folder_name == "MAX")
        )
        if q.strip():
            # LIKE wildcard characters from a search are treated literally.
            query = (
                q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            statement = statement.where(
                TelegramPost.text.ilike(f"%{query}%", escape="\\")
            )
        if mark is not None:
            statement = statement.where(
                PipelineEntry.id.in_(
                    select(PostFilterMark.entry_id).where(
                        PostFilterMark.mark_id == mark, PostFilterMark.active.is_(True)
                    )
                )
            )
        if selection_filter is not None:
            statement = statement.where(
                PipelineEntry.id.in_(
                    select(FilterMarkEvent.entry_id)
                    .join(
                        FilterEvaluation,
                        FilterEvaluation.id == FilterMarkEvent.evaluation_id,
                    )
                    .join(
                        SelectionFilterVersion,
                        SelectionFilterVersion.id == FilterEvaluation.version_id,
                    )
                    .join(
                        PostFilterMark,
                        (PostFilterMark.entry_id == FilterMarkEvent.entry_id)
                        & (PostFilterMark.mark_id == FilterMarkEvent.mark_id)
                        & PostFilterMark.active.is_(True),
                    )
                    .where(
                        SelectionFilterVersion.filter_id == selection_filter,
                        FilterMarkEvent.action == "assigned",
                    )
                )
            )
        if active:
            statement = statement.where(
                PipelineEntry.id.in_(
                    select(TaxonomyClassification.pipeline_entry_id).where(
                        TaxonomyClassification.status.in_(("queued", "running"))
                    )
                )
            )
        columns = []
        async with session_factory(request)() as session:
            for stage in BOARD_STAGES:
                scoped = statement.where(PipelineEntry.stage == stage)
                count = (
                    await session.execute(
                        select(func.count()).select_from(scoped.subquery())
                    )
                ).scalar_one()
                rows = list(
                    (
                        await session.execute(
                            scoped.order_by(
                                PipelineEntry.id.asc()
                                if order == "asc"
                                else PipelineEntry.id.desc()
                            ).limit(limit)
                        )
                    ).all()
                )
                columns.append(
                    {
                        "stage": stage,
                        "name": PIPELINE_STAGE_LABELS[stage],
                        "rows": rows,
                        "count": count,
                    }
                )
            states = await board_state_for_entries(
                request.app,
                session,
                [row[0].id for col in columns for row in col["rows"]],
            )
            marks = list(
                (
                    await session.execute(
                        select(FilterMark)
                        .where(FilterMark.archived.is_(False))
                        .order_by(FilterMark.name)
                    )
                ).scalars()
            )
            filters = list(
                (
                    await session.execute(
                        select(SelectionFilter)
                        .where(SelectionFilter.archived.is_(False))
                        .order_by(SelectionFilter.name)
                    )
                ).scalars()
            )
        return templates.TemplateResponse(
            request,
            "_board_columns.html"
            if request.url.path.endswith("board-fragment")
            else "pipeline.html",
            {
                "columns": columns,
                "states": states,
                "marks": marks,
                "filters": filters,
                "q": q,
                "mark": mark,
                "selection_filter": selection_filter,
                "order": order,
                "limit": limit,
                "active": active,
            },
        )

    @app.get("/pipeline/{entry_id}", response_class=HTMLResponse, dependencies=[auth])
    async def pipeline_detail(request: Request, entry_id: int):
        async with session_factory(request)() as session:
            row = (
                await session.execute(
                    select(PipelineEntry, TelegramPost, TelegramChat)
                    .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                    .join(
                        TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id
                    )
                    .where(
                        PipelineEntry.id == entry_id, TelegramChat.folder_name == "MAX"
                    )
                )
            ).first()
            if row is None:
                raise HTTPException(404, "Карточка не найдена")
            entry, post, chat = row
            album_posts = [post]
            if post.grouped_id is not None:
                album_posts = list(
                    (
                        await session.execute(
                            select(TelegramPost)
                            .where(
                                TelegramPost.chat_peer_id == post.chat_peer_id,
                                TelegramPost.grouped_id == post.grouped_id,
                                TelegramPost.is_deleted.is_(False),
                            )
                            .order_by(TelegramPost.message_id)
                        )
                    ).scalars()
                ) or [post]
            primary = album_primary(album_posts)
            primary_entry = (
                await session.execute(
                    select(PipelineEntry.id).where(
                        PipelineEntry.source_post_id == primary.id
                    )
                )
            ).scalar_one_or_none()
            state = (await board_state_for_entries(request.app, session, [entry_id]))[
                entry_id
            ]
        return templates.TemplateResponse(
            request,
            "pipeline_detail.html",
            {
                "entry": entry,
                "post": post,
                "chat": chat,
                "state": state,
                "stage_labels": PIPELINE_STAGE_LABELS,
                "source_url": telegram_post_source_url(primary, chat),
                "primary_entry_id": primary_entry,
                "source_media": source_media_items(
                    album_posts, request.app.state.settings.media_dir
                ),
            },
        )


app = create_app()


@web_cli.command("run")
def run(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Run the FastAPI web app with uvicorn."""

    import uvicorn

    uvicorn.run("app.web.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    web_cli()
