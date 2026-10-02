"""Последовательный OCR, отдельная история и повторы. Telegram здесь нет."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select, update
from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import PipelineEntry, TelegramPost, TaxonomyClassification, TaxonomyRun
from app.ocr.models import OcrJob, OcrRun
from app.ocr.jobs import snapshot
from app.ocr.engine import compose_input, input_digest, file_digest, PREPROCESSING_VERSION
from app.ocr.media import first_frame
from app.runtime_status import heartbeat

logger = logging.getLogger(__name__)


class Reader:
    def __init__(self, settings):
        self.settings, self.process = settings, None
        self.version = "rapidocr-3.9.2@" + file_digest(Path(settings.ocr_model_dir) / "ocr-manifest.json")[:16] + ":" + PREPROCESSING_VERSION
        self.cache = Path(settings.cache_dir) / "ocr-results"
        self.cache.mkdir(parents=True, exist_ok=True)

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
            self.process = None

    async def read(self, item):
        key = hashlib.sha256((item["sha256"] + self.version + item["kind"]).encode()).hexdigest()
        cached = self.cache / f"{key}.json"
        if cached.is_file():
            value = json.loads(cached.read_text(encoding="utf-8"))
            if value.get("engine_version") == self.version and value.get("source_sha256") == item["sha256"]:
                return {**value, "cache_hit": True}
        frame = None
        started = time.perf_counter()
        try:
            path = Path(item["path"])
            if item["kind"] == "video_frame":
                frame = await first_frame(path, self.cache / "frames", self.settings.ocr_timeout_seconds)
                path = frame
            if not self.process or self.process.returncode is not None:
                self.process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.ocr.runner",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            self.process.stdin.write((json.dumps({"path": str(path)}) + "\n").encode())
            await self.process.stdin.drain()
            remaining = self.settings.ocr_timeout_seconds - (time.perf_counter() - started)
            answer = await asyncio.wait_for(self.process.stdout.readline(), max(.01, remaining))
            value = json.loads(answer)
            if "error" in value:
                raise ValueError("OCR could not read attachment: " + value["error"])
            result = {**value["result"], "source_sha256": item["sha256"], "cache_hit": False}
            temporary = cached.with_suffix(".part")
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cached)
            return result
        except (asyncio.TimeoutError, json.JSONDecodeError, BrokenPipeError):
            await self.close()
            raise
        finally:
            if frame:
                frame.unlink(missing_ok=True)


async def claim(factory):
    now = datetime.now(timezone.utc)
    async with factory() as session:
        async with session.begin():
            job = (await session.execute(select(OcrJob).where(OcrJob.status.in_(("queued", "waiting_media")),
                (OcrJob.retry_at.is_(None) | (OcrJob.retry_at <= now)))
                .order_by(OcrJob.entry_id).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
            if not job:
                return None
            run = await session.get(OcrRun, job.current_run_id)
            job.status, job.updated_at, run.status, run.started_at = "running", now, "running", now
            job.attempts += 1
            return job.entry_id, run.id


async def process(factory, settings, reader, entry_id, run_id):
    started = time.perf_counter()
    results, error = [], None
    async with factory() as session:
        post = (await session.execute(select(TelegramPost).join(PipelineEntry,
            PipelineEntry.source_post_id == TelegramPost.id).where(PipelineEntry.id == entry_id))).scalar_one()
        source, inputs, problem = await snapshot(session, post, settings)
        caption = post.text
        run = await session.get(OcrRun, run_id)
        if source != run.source_sha256 or post.is_deleted:
            problem = "Вход изменился во время OCR"
    try:
        if problem:
            raise ValueError(problem)
        for item in inputs:
            results.append(await reader.read(item))
        status = "needs_review" if any(r["status"] == "needs_review" for r in results) else "complete" if compose_input(caption, results) else "no_text"
    except Exception as exc:
        # Не сохраняем полный exception с путями/текстом, только понятную причину.
        status, error = "failed", problem or ("Таймаут OCR" if isinstance(exc, asyncio.TimeoutError) else "Не удалось прочитать вложение")
    now = datetime.now(timezone.utc)
    async with factory() as session:
        async with session.begin():
            # Единый порядок: карточка -> OCR job -> run.
            entry = (await session.execute(select(PipelineEntry).where(PipelineEntry.id == entry_id).with_for_update())).scalar_one()
            job = (await session.execute(select(OcrJob).where(OcrJob.entry_id == entry_id).with_for_update())).scalar_one()
            run = await session.get(OcrRun, run_id)
            post = await session.get(TelegramPost, entry.source_post_id)
            current, _inputs, _error = await snapshot(session, post, settings)
            if job.current_run_id != run_id:
                run.status, run.finished_at = "stale", now
                return
            if current != run.source_sha256 or post.is_deleted:
                status, error = "stale", "Пост или медиа изменились"
            run.status, run.error, run.results = status, error, results
            run.engine_version = reader.version
            run.elapsed_ms = max(1, round((time.perf_counter() - started) * 1000))
            run.finished_at = now
            run.input_sha256 = input_digest(caption, results) if status in {"complete", "no_text", "needs_review"} else None
            job.status, job.error, job.updated_at = status, error, now
            if status == "failed" and job.attempts <= 3:
                job.status = "queued"
                job.retry_at = now + timedelta(seconds=(10, 30, 120)[job.attempts - 1])
                next_run = OcrRun(entry_id=entry_id, source_sha256=current, status="queued", inputs=inputs, results=[])
                session.add(next_run)
                await session.flush()
                job.current_run_id = next_run.id
            # Классификатор читает только зафиксированный полный результат.
            waiting = list((await session.execute(select(TaxonomyClassification).where(
                TaxonomyClassification.pipeline_entry_id == entry_id,
                TaxonomyClassification.profile == "humor_ocr", TaxonomyClassification.status == "ocr"))).scalars())
            for classification in waiting:
                if status in {"complete", "no_text"}:
                    classification.ocr_run_id, classification.input_sha256 = run.id, run.input_sha256
                    classification.status = "queued"
                elif job.status != "queued":
                    classification.status = "needs_review" if status == "needs_review" else "failed"
                    classification.error = error or "OCR требует ручного разбора"
                    classification.finished_at = now
                classification_run = await session.get(TaxonomyRun, classification.current_run_id)
                if classification_run:
                    classification_run.status = classification.status
                    classification_run.error = classification.error
                    classification_run.input_sha256 = classification.input_sha256
                    classification_run.ocr_run_id = classification.ocr_run_id
                    if classification.status in {"failed", "needs_review"}:
                        classification_run.finished_at = now
            if entry.auto_enabled and entry.auto_state != "done":
                entry.auto_retry_at = None


async def run():
    settings = get_settings()
    if not settings.ocr_enabled:
        raise RuntimeError("OCR_ENABLED must be true")
    engine = create_engine(settings)
    factory, reader = create_session_factory(engine=engine), Reader(settings)
    async def pulse():
        while True:
            Path("/tmp/ocr-worker-heartbeat").touch()
            await heartbeat(factory, "ocr-worker")
            await asyncio.sleep(5)
    task = asyncio.create_task(pulse())
    try:
        async with factory() as session:
            await session.execute(update(OcrJob).where(OcrJob.status == "running").values(status="queued"))
            await session.execute(update(OcrRun).where(OcrRun.status == "running").values(status="queued"))
            await session.commit()
        while True:
            claimed = await claim(factory)
            if claimed:
                await process(factory, settings, reader, *claimed)
            else:
                # Изменённый вход уже запрошенной карточки подготавливается
                # заново. Постоянно недоступное медиа проходит ограниченные
                # повторы, а не остаётся в waiting_media навсегда.
                async with factory() as session:
                    pending = list((await session.execute(select(OcrJob).where(OcrJob.status == "stale").limit(20))).scalars())
                    from app.ocr.jobs import enqueue_ocr
                    for job in pending:
                        entry = await session.get(PipelineEntry, job.entry_id)
                        post = await session.get(TelegramPost, entry.source_post_id)
                        if not post.is_deleted:
                            await enqueue_ocr(session, entry, post, settings)
                    await session.commit()
                await asyncio.sleep(2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
