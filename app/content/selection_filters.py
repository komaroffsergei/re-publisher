"""Отбор постов и постоянные признаки. Чтение доски ничего не назначает."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select

from app.models import (FilterApplication, FilterEvaluation, FilterMark, FilterMarkEvent, PipelineEntry,
                        PostFilterMark, SelectionFilter, SelectionFilterVersion, TaxonomyClassification,
                        TaxonomyRun, TelegramChat, TelegramPost)
from app.taxonomy.jobs import text_sha256
from app.taxonomy.profiles import profile_of
from app.content.selection_rules import evaluate, matching_conditions, taxonomy_catalog

logger = logging.getLogger(__name__)


async def has_marks(session, entry_id: int) -> bool:
    return (await session.execute(select(PostFilterMark.id).where(
        PostFilterMark.entry_id == entry_id, PostFilterMark.active.is_(True)).limit(1))).scalar_one_or_none() is not None


async def preserve_mark_stage(session, entry: PipelineEntry):
    if entry.stage in {"received", "sorted", "filtered"}:
        marked = await has_marks(session, entry.id)
        if marked:
            entry.stage = "filtered"
        elif entry.stage == "filtered":
            entry.stage = "sorted"


def assessment(version, job, post):
    fingerprint = text_sha256(post.text)
    run_id = job.current_run_id if job else None
    profile = profile_of(version)
    input_key = f"{profile}:{getattr(job, 'input_sha256', None) or fingerprint}:{run_id or 0}:{job.status if job else 'missing'}"
    scores = {}
    reason = None
    if post.is_deleted:
        reason = "Пост удалён"
    elif profile == "taxonomy" and not (post.text or "").strip():
        reason = "Нет содержательного текста"
    elif not job or profile_of(job) != profile or job.status != "complete" or job.text_sha256 != fingerprint:
        reason = "Нет актуальной оценки выбранной модели"
    elif (job.result or {}).get("taxonomy_version") != taxonomy_catalog(profile)["version"]:
        reason = "Нужно обновить полный набор оценок"
    else:
        scores = (job.result or {}).get("scores") or {}
        if not scores:
            reason = "Нужно обновить полный набор оценок"
    result, trace = evaluate(version.expression, scores)
    if reason:
        result = None
        trace["reason"] = reason
    return {"outcome": "matched" if result is True else "rejected" if result is False else "unknown",
            "trace": trace, "run_id": run_id, "input_key": input_key, "text_sha256": fingerprint}


async def assign_mark(session, entry, version, evaluation, context: str):
    # Один контекст (запуск модели/задание применения) назначает признак один раз.
    # Повтор партии после сбоя не отменяет выполненное пользователем ручное снятие.
    dedup_key = f"{context}:{evaluation.id}:{version.mark_id}"
    if (await session.execute(select(FilterMarkEvent.id).where(FilterMarkEvent.dedup_key == dedup_key))).scalar_one_or_none():
        return
    mark = await session.get(FilterMark, version.mark_id)
    if not mark or mark.archived:
        return
    assignment = (await session.execute(select(PostFilterMark).where(
        PostFilterMark.entry_id == entry.id, PostFilterMark.mark_id == mark.id))).scalar_one_or_none()
    if assignment is not None and not assignment.active and context.startswith("run:"):
        run = await session.get(TaxonomyRun, evaluation.run_id) if evaluation.run_id else None
        # Повтор уведомления о старом результате не считается новой классификацией.
        if not run or (assignment.removed_at and (run.finished_at or run.queued_at) <= assignment.removed_at):
            return
    now = datetime.now(timezone.utc)
    if assignment is None:
        assignment = PostFilterMark(entry_id=entry.id, mark_id=mark.id, active=True, assigned_at=now)
        session.add(assignment)
    elif not assignment.active:
        assignment.active = True
        assignment.assigned_at = now
        assignment.removed_at = None
    session.add(FilterMarkEvent(entry_id=entry.id, mark_id=mark.id, evaluation_id=evaluation.id,
                               action="assigned", dedup_key=dedup_key, created_at=now))
    await session.flush()
    await preserve_mark_stage(session, entry)
    if entry.stage != "ready":
        entry.auto_enabled = True
        entry.auto_state = "pending"
        entry.auto_retry_at = None


async def evaluate_post(session, entry, post, version, context: str, job=None):
    if job is None:
        job = (await session.execute(select(TaxonomyClassification).where(
            TaxonomyClassification.pipeline_entry_id == entry.id,
            TaxonomyClassification.model_key == version.model_key,
            TaxonomyClassification.profile == profile_of(version)))).scalar_one_or_none()
    values = assessment(version, job, post)
    evaluation = (await session.execute(select(FilterEvaluation).where(
        FilterEvaluation.entry_id == entry.id, FilterEvaluation.version_id == version.id,
        FilterEvaluation.input_key == values["input_key"]))).scalar_one_or_none()
    if evaluation is None:
        evaluation = FilterEvaluation(entry_id=entry.id, version_id=version.id, **values)
        session.add(evaluation)
        await session.flush()
    if values["outcome"] == "matched":
        await assign_mark(session, entry, version, evaluation, context)
    return values


async def evaluate_completed_job(session, entry, post, job):
    versions = (await session.execute(select(SelectionFilterVersion).join(
        SelectionFilter, SelectionFilter.active_version_id == SelectionFilterVersion.id).where(
        SelectionFilter.enabled.is_(True), SelectionFilter.archived.is_(False),
        SelectionFilterVersion.model_key == job.model_key,
        SelectionFilterVersion.profile == profile_of(job)))).scalars()
    for version in versions:
        await evaluate_post(session, entry, post, version, f"run:{job.current_run_id}", job)
    await preserve_mark_stage(session, entry)


async def remove_mark(session, entry, mark_id):
    assignment = (await session.execute(select(PostFilterMark).where(
        PostFilterMark.entry_id == entry.id, PostFilterMark.mark_id == mark_id))).scalar_one_or_none()
    if assignment is None or not assignment.active:
        return False
    assignment.active = False
    assignment.removed_at = datetime.now(timezone.utc)
    session.add(FilterMarkEvent(entry_id=entry.id, mark_id=mark_id, action="removed",
                               dedup_key=f"remove:{uuid4()}"))
    await session.flush()
    await preserve_mark_stage(session, entry)
    entry.last_operation_at = datetime.now(timezone.utc)
    return True


def scope_statement():
    return select(PipelineEntry, TelegramPost).join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id).join(
        TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id).where(
        TelegramChat.folder_name == "MAX", TelegramPost.is_deleted.is_(False),
        PipelineEntry.stage.in_(("sorted", "filtered", "marking", "ready")))


async def preview(session, version):
    counts = {"matched": 0, "rejected": 0, "unknown": 0, "new_marks": 0, "backfill_needed": 0}
    examples = []
    last = 0
    # Посты читаются партиями: недельная история не собирается в память целиком.
    while True:
        rows = (await session.execute(scope_statement().where(PipelineEntry.id > last).order_by(PipelineEntry.id).limit(100))).all()
        if not rows:
            break
        ids = [entry.id for entry, _post in rows]
        jobs = {job.pipeline_entry_id: job for job in (await session.execute(select(TaxonomyClassification).where(
            TaxonomyClassification.pipeline_entry_id.in_(ids), TaxonomyClassification.model_key == version.model_key,
            TaxonomyClassification.profile == profile_of(version)))).scalars()}
        marked = set((await session.execute(select(PostFilterMark.entry_id).where(
            PostFilterMark.entry_id.in_(ids), PostFilterMark.mark_id == version.mark_id,
            PostFilterMark.active.is_(True)))).scalars())
        for entry, post in rows:
            job = jobs.get(entry.id)
            values = assessment(version, job, post)
            counts[values["outcome"]] += 1
            if values["outcome"] == "matched" and entry.id not in marked:
                counts["new_marks"] += 1
            if needs_backfill(job, post, profile_of(version)):
                counts["backfill_needed"] += 1
            # В предпросмотре только совпадения с актуальными оценками. Счётчики
            # считаем по всей выборке, а тексты возвращаем ограниченным списком.
            if values["outcome"] == "matched" and len(examples) < 12:
                examples.append({"entry_id": entry.id, **values,
                    "text": (post.text or "")[:500],
                    "text_truncated": len(post.text or "") > 500,
                    "model_version": job.model_version,
                    "matching_conditions": matching_conditions(values["trace"])})
        last = ids[-1]
    return {**counts, "total": sum(counts[key] for key in ("matched", "rejected", "unknown")), "examples": examples}


def needs_backfill(job, post, profile="taxonomy"):
    if (profile == "taxonomy" and not (post.text or "").strip()) or post.is_deleted:
        return False
    if job is None:
        return True
    current_text = job.text_sha256 == text_sha256(post.text)
    if job.status in {"ocr", "queued", "loading", "running"} and current_text:
        return False  # Завершение уже поставленного запуска проверит включённые фильтры.
    scores = (job.result or {}).get("scores") or {}
    if profile == "humor_ocr" and job.status in {"media_only", "empty", "needs_review"} and current_text:
        return False
    return (profile_of(job) != profile or job.status != "complete" or not current_text or not scores
            or (job.result or {}).get("taxonomy_version") != taxonomy_catalog(profile)["version"])


async def application_batch(factory):
    async with factory() as session:
        async with session.begin():
            application = (await session.execute(select(FilterApplication).where(
                FilterApplication.status.in_(("queued", "running"))).order_by(FilterApplication.id)
                .with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
            if application is None:
                return
            version = await session.get(SelectionFilterVersion, application.version_id)
            selection_filter = await session.get(SelectionFilter, version.filter_id)
            if (selection_filter.active_version_id != version.id or not selection_filter.enabled or selection_filter.archived):
                application.status = "cancelled"
                return
            rows = (await session.execute(scope_statement().where(PipelineEntry.id > application.last_entry_id,
                PipelineEntry.id <= application.max_entry_id).order_by(PipelineEntry.id)
                .with_for_update(of=PipelineEntry).limit(20))).all()
            if not rows:
                application.status = "complete"
                return
            application.status = "running"
            from app.taxonomy.jobs import enqueue
            from fastapi import HTTPException
            for entry, post in rows:
                job = (await session.execute(select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry.id,
                    TaxonomyClassification.model_key == version.model_key,
                    TaxonomyClassification.profile == profile_of(version)))).scalar_one_or_none()
                values = await evaluate_post(session, entry, post, version, f"apply:{application.id}", job)
                if needs_backfill(job, post, profile_of(version)):
                    try:
                        await enqueue(session, entry.id, version.model_key, profile_of(version))
                        application.backfilled += 1
                    except HTTPException as exc:
                        if exc.status_code != 409:
                            raise
                        # Другая модель держит карточку. Не теряем её за курсором:
                        # следующая партия продолжится с этой записи после завершения запуска.
                        return
                application.processed += 1
                application.matched += values["outcome"] == "matched"
                application.unknown += values["outcome"] == "unknown"
                application.last_entry_id = entry.id
            application.updated_at = datetime.now(timezone.utc)


async def application_loop(factory):
    while True:
        try:
            await application_batch(factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Filter application batch failed; transaction rolled back")
            # Ошибка видна в настройке, а не превращается в бесконечный тихий retry.
            try:
                async with factory() as session:
                    application = (await session.execute(select(FilterApplication).where(
                        FilterApplication.status.in_(("queued", "running"))).order_by(FilterApplication.id)
                        .with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
                    if application:
                        application.status = "failed"
                        application.error = type(exc).__name__
                        await session.commit()
            except Exception:
                logger.exception("Could not save filter application failure")
            await asyncio.sleep(5)
        await asyncio.sleep(1)


def trace_text(trace):
    names = {label["id"]: label["name"] for label in taxonomy_catalog()["labels"]}
    if trace.get("reason"):
        return trace["reason"]
    if trace["op"] == "condition":
        sign = {"gte": "≥", "gt": ">", "lte": "≤", "lt": "<"}[trace["compare"]]
        actual = "нет оценки" if trace["score"] is None else f"{trace['score'] * 100:.1f}%"
        text = f"{names.get(trace['label_id'], trace['label_id'])} {sign} {trace['threshold']:g}% (оценка: {actual})"
    else:
        children = [trace_text(child) for child in trace.get("children", [])]
        text = f"НЕ ({children[0]})" if trace["op"] == "not" else "(" + (" И " if trace["op"] == "and" else " ИЛИ ").join(children) + ")"
    assigned = trace.get("assigned")
    if assigned:
        score = "нет оценки" if assigned["score"] is None else f"{assigned['score'] * 100:.2f}%"
        text += f" → {assigned['name']}: оценка модели {score}"
    return text


async def load_states(session, entries: dict):
    if not entries:
        return {}
    ids = list(entries)
    result = {entry_id: {"marks": [], "checks": []} for entry_id in ids}
    for assignment, mark in (await session.execute(select(PostFilterMark, FilterMark).join(
        FilterMark, FilterMark.id == PostFilterMark.mark_id).where(PostFilterMark.entry_id.in_(ids),
        PostFilterMark.active.is_(True)))).all():
        result[assignment.entry_id]["marks"].append({"id": mark.id, "label_id": mark.label_id,
            "name": mark.name, "color": mark.color,
            "archived": mark.archived, "assigned_at": assignment.assigned_at.isoformat(), "sources": []})
    sources = (await session.execute(select(FilterMarkEvent, FilterEvaluation, SelectionFilterVersion).join(
        FilterEvaluation, FilterEvaluation.id == FilterMarkEvent.evaluation_id).join(
        SelectionFilterVersion, SelectionFilterVersion.id == FilterEvaluation.version_id).where(
        FilterMarkEvent.entry_id.in_(ids), FilterMarkEvent.action == "assigned").order_by(FilterMarkEvent.id.desc()))).all()
    seen = set()
    for event, evaluation, version in sources:
        key = (event.entry_id, event.mark_id, version.id, evaluation.run_id)
        if key in seen:
            continue
        seen.add(key)
        for mark in result[event.entry_id]["marks"]:
            if mark["id"] == event.mark_id:
                mark["sources"].append({"filter_id": version.filter_id, "name": version.name,
                    "version": version.number, "model_key": version.model_key, "run_id": evaluation.run_id,
                    "assigned": evaluation.trace.get("assigned"),
                    "stale": evaluation.text_sha256 != text_sha256(entries[event.entry_id][2].text),
                    "assigned_at": event.created_at.isoformat(), "reason": trace_text(evaluation.trace)})
    versions = (await session.execute(select(SelectionFilterVersion).join(SelectionFilter,
        SelectionFilter.active_version_id == SelectionFilterVersion.id).where(
        SelectionFilter.enabled.is_(True), SelectionFilter.archived.is_(False)))).scalars().all()
    jobs = {(job.pipeline_entry_id, job.model_key, profile_of(job)): job for job in (await session.execute(
        select(TaxonomyClassification).where(TaxonomyClassification.pipeline_entry_id.in_(ids)))).scalars()}
    # Текущие совпадения считаются без записи: ручное снятие не отменяется polling.
    for entry_id, (_entry, _state, post) in entries.items():
        for version in versions:
            values = assessment(version, jobs.get((entry_id, version.model_key, profile_of(version))), post)
            result[entry_id]["checks"].append({"filter_id": version.filter_id, "name": version.name,
                "version": version.number, "model_key": version.model_key, "outcome": values["outcome"],
                "assigned": values["trace"].get("assigned"),
                "reason": trace_text(values["trace"])})
    return result
