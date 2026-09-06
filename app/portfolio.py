"""Bounded public demo; intentionally does not mount the operational admin or collector."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from typing import Literal

from app.portfolio_pipeline import CORPUS, model, run_pipeline

WEB = Path(__file__).parent / "portfolio_web"
SECRET = os.environ.get("PORTFOLIO_SECRET", "")
slots = asyncio.Semaphore(2)


async def cleanup(pool):
    while True:
        await pool.execute("DELETE FROM portfolio_runs WHERE expires_at < now()")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    if len(SECRET) < 32:
        raise RuntimeError("A signing secret of at least 32 characters is required")
    if any(os.environ.get(x, "false").lower() not in {"false", "0", ""} for x in ("ENABLE_EXTERNAL_LLM", "AUTO_PUBLISH", "ENABLE_CODEX_NIGHTLY")):
        raise RuntimeError("Public demo must not enable external processing or publishing")
    app.state.pool = await asyncpg.create_pool(os.environ["DB_DSN"].replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=4)
    await app.state.pool.execute("""CREATE TABLE IF NOT EXISTS portfolio_runs (
        id text PRIMARY KEY, session_id text NOT NULL, fixture text NOT NULL,
        result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL DEFAULT now() + interval '1 hour');
        CREATE INDEX IF NOT EXISTS portfolio_runs_session ON portfolio_runs(session_id);
        CREATE INDEX IF NOT EXISTS portfolio_runs_expiry ON portfolio_runs(expires_at);""")
    model()
    task = asyncio.create_task(cleanup(app.state.pool))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await app.state.pool.close()


app = FastAPI(title="re-publisher synthetic demo", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.middleware("http")
async def session_context(request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    token = request.cookies.get("portfolio_publisher", "")
    parts = token.split(".")
    valid = len(parts) == 3 and len(parts[0]) == 48 and parts[1].isdigit() and int(parts[1]) > time.time() and hmac.compare_digest(hmac.new(SECRET.encode(), ".".join(parts[:2]).encode(), "sha256").hexdigest(), parts[2])
    if not valid:
        raw = secrets.token_hex(24) + "." + str(int(time.time()) + 3600)
        token = raw + "." + hmac.new(SECRET.encode(), raw.encode(), "sha256").hexdigest()
    request.state.session_id = hashlib.sha256(token.encode()).hexdigest()
    response = await call_next(request)
    if not valid:
        response.set_cookie("portfolio_publisher", token, max_age=3600, secure=True, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")


@app.get("/healthz")
async def health(request: Request):
    await request.app.state.pool.fetchval("SELECT 1")
    return {"status": "ready", "mode": "synthetic", "externalPublishing": False}


@app.get("/source/{fixture}", response_class=HTMLResponse)
async def source(fixture: str):
    item = next((x for x in CORPUS if x["id"] == fixture), None)
    if item is None:
        raise HTTPException(404)
    return f'<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/assets/style.css"><title>{escape(item["title"])}</title><main><a href="/">← re-publisher</a><p class="eyebrow">Собственный синтетический материал</p><h1>{escape(item["title"])}</h1><p>{escape(item["text"])}</p></main></html>'


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture: Literal["tool", "news", "education"]
    simulate_error: bool = False


async def owned(request, run_id):
    row = await request.app.state.pool.fetchrow("SELECT * FROM portfolio_runs WHERE id=$1 AND session_id=$2 AND expires_at>now()", run_id, request.state.session_id)
    if row is None:
        raise HTTPException(404, "Материал не найден в вашей сессии")
    return row


def serialize(row):
    return {"id": row["id"], "created_at": row["created_at"].isoformat(), "expires_at": row["expires_at"].isoformat(), **json.loads(row["result"])}


@app.get("/api/runs")
async def runs(request: Request):
    rows = await request.app.state.pool.fetch("SELECT * FROM portfolio_runs WHERE session_id=$1 AND expires_at>now() ORDER BY created_at DESC", request.state.session_id)
    return {"items": [serialize(x) for x in rows], "fixtures": CORPUS, "ttlSeconds": 3600}


@app.post("/api/runs", status_code=201)
async def create(body: RunInput, request: Request):
    pool = request.app.state.pool
    async with slots:
        run_id = str(uuid4())
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(914007)")
            count = await connection.fetchval("SELECT count(*) FROM portfolio_runs WHERE session_id=$1 AND expires_at>now()", request.state.session_id)
            total = await connection.fetchval("SELECT count(*) FROM portfolio_runs")
            if count >= 20 or total >= 1000:
                raise HTTPException(429, "Лимит демонстрации: 20 материалов на сессию. Сбросьте свой пример.")
            result = await asyncio.to_thread(run_pipeline, body.fixture, body.simulate_error)
            row = await connection.fetchrow("INSERT INTO portfolio_runs(id,session_id,fixture,result) VALUES($1,$2,$3,$4::jsonb) RETURNING *", run_id, request.state.session_id, body.fixture, json.dumps(jsonable_encoder(result), ensure_ascii=False))
        return serialize(row)


@app.get("/api/runs/{run_id}")
async def detail(run_id: str, request: Request):
    return serialize(await owned(request, run_id))


@app.post("/api/runs/{run_id}/retry")
async def retry(run_id: str, request: Request):
    async with slots:
        row = await owned(request, run_id)
        if json.loads(row["result"])["status"] != "failed":
            raise HTTPException(409, "Повтор доступен для материала с ошибкой")
        result = await asyncio.to_thread(run_pipeline, row["fixture"], False)
        result["retried"] = True
        row = await request.app.state.pool.fetchrow("UPDATE portfolio_runs SET result=$1::jsonb WHERE id=$2 AND session_id=$3 RETURNING *", json.dumps(jsonable_encoder(result), ensure_ascii=False), run_id, request.state.session_id)
        if row is None:
            raise HTTPException(404)
        return serialize(row)


@app.delete("/api/runs")
async def reset(request: Request):
    await request.app.state.pool.execute("DELETE FROM portfolio_runs WHERE session_id=$1", request.state.session_id)
    return {"reset": True}
