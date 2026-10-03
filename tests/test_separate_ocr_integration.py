from pathlib import Path
from sqlalchemy import select, func
import pytest
import test_selection_integration as shared
import test_ocr_integration as ocr
from app.models import PipelineEntry,TelegramPost,TaxonomyClassification,FilterApplication
from app.ocr.models import OcrJob,OcrRun
from app.ocr.jobs import enqueue_ocr,current_input,invalidate_ocr,snapshot
from app.ocr.worker import process,claim
from app.taxonomy.jobs import enqueue,invalidate_if_edited

db=shared.db
client=shared.client
pytestmark=shared.pytestmark


async def test_independent_inputs_caption_cache_and_progress(db,client,tmp_path):
    id=await shared.seed(db); settings=ocr.configure(tmp_path)
    await ocr.media_post(db,id,settings)
    async with db() as session:
        entry=await session.get(PipelineEntry,id);post=await session.get(TelegramPost,entry.source_post_id)
        post.text="caption distinct from OCR";await session.commit()
        job=await enqueue(session,id,"tfidf","taxonomy","ocr");assert job.status == "ocr"
        await session.commit()
    await process(db,settings,ocr.Reader(),*(await claim(db)))
    async with db() as session:
        entry=await session.get(PipelineEntry,id);post=await session.get(TelegramPost,entry.source_post_id)
        value,run=await current_input(session,id,post,settings,"ocr")
        assert "caption" not in value and "размножаются" in value
        assert run.completed_inputs == run.total_inputs == 1
        before=run.source_sha256; post.text="new caption";await invalidate_if_edited(session,post.id,post.text)
        await invalidate_ocr(session,post,settings);await session.commit()
        assert (await session.get(OcrJob,id)).status == "complete"
        assert (await snapshot(session,post,settings))[0] == before
        assert (await session.execute(select(func.count(OcrRun.id)))).scalar_one() == 1
        jobs=list((await session.execute(select(TaxonomyClassification).where(TaxonomyClassification.pipeline_entry_id==id))).scalars())
        assert len(jobs) == 3 and next(j for j in jobs if j.input_source == "ocr").status == "queued"
    state=(await client.get(f"/api/pipeline/board-state?entry_ids={id}")).json()["entries"][str(id)]
    assert "tfidf:ocr" in state["taxonomies"] and state["ocr"]["completed_inputs"] == 1


async def test_ocr_filter_apply_does_not_start_old_buffer(db,client):
    await shared.seed(db)
    mark=(await client.post('/api/pipeline/marks',json={"name":"OCR label"})).json()["id"]
    draft={"name":"OCR filter","model_key":"tfidf","requires_ocr":True,"mark_id":mark,
           "expression":{"op":"and","children":[{"op":"length","compare":"lte","threshold":500},
            {"op":"condition","input_source":"ocr","label_id":"is_joke","compare":"gte","threshold":90}]}}
    invalid=await client.post('/api/pipeline/filters/preview',json={**draft,"requires_ocr":False});assert invalid.status_code==422
    preview=await client.post('/api/pipeline/filters/preview',json=draft);assert preview.status_code==200
    saved=await client.post('/api/pipeline/filters/apply',json={**draft,"preview_digest":preview.json()["preview_digest"]})
    assert saved.status_code==200 and saved.json()["application_id"] is None
    async with db() as session:
        assert (await session.scalar(select(func.count()).select_from(OcrJob))) == 0
        assert (await session.scalar(select(func.count()).select_from(FilterApplication))) == 0


async def test_long_caption_does_not_skip_media(db,client,tmp_path):
    id=await shared.seed(db);settings=ocr.configure(tmp_path);await ocr.media_post(db,id,settings)
    async with db() as session:
        entry=await session.get(PipelineEntry,id);post=await session.get(TelegramPost,entry.source_post_id)
        post.text="a"*501
        job=await enqueue_ocr(session,entry,post,settings);await session.commit()
        run=await session.get(OcrRun,job.current_run_id);assert len(run.inputs)==1
