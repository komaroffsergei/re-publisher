from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from joblib import dump
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.content.classifier import build_classification_text, predict
from app.content.common import session_factory, settings_or_exit
from app.content.link_enricher import extract_html
from app.content.processor import processed_values
from app.content.rewriter import draft_from_template
from app.content.search import upsert_document
from app.models import (
    ContentItem,
    ContentPipelineState,
    LinkSnapshot,
    ModelVersion,
    PipelineEntry,
    PostClassification,
    PostLink,
    PostProcessed,
    PublicationDraft,
    PublicationTarget,
    RewriteAttempt,
    SearchDocument,
    Showcase,
    TelegramChat,
    TelegramPost,
)
from app.portfolio_pipeline import model

DEMO_CHAT_ID = -9_000_000_000_001
DEMO_FOLDER = "PORTFOLIO_DEMO"
DEMO_SHOWCASE = "portfolio-demo"
DEMO_MODEL_VERSION = "portfolio-demo-v1"

DEMO_MATERIALS: tuple[dict[str, Any], ...] = (
    {
        "key": "collector",
        "title": "Коллектор сохранил исходный материал и метаданные",
        "text": "Коллектор получил учебный пост из демонстрационного Telegram-канала. Исходный текст, дата, идентификатор сообщения и raw metadata сохранены отдельно от результатов обработки. Внешнее подключение к Telegram при подготовке этого набора не выполняется.",
        "stage": "received",
        "status": "received",
    },
    {
        "key": "normalization",
        "title": "Нормализация текста и извлечение признаков",
        "text": "Processor очищает пробелы, приводит текст к нормальной форме, считает слова и ссылки, извлекает домены, хэштеги и признаки кода. Для повторного запуска используется хэш очищенного текста, поэтому результат можно сопоставить с исходником.",
        "stage": "received",
        "status": "processed",
    },
    {
        "key": "classification",
        "title": "TF-IDF классификация материала по редакционному жанру",
        "text": "Локальная модель строит TF-IDF признаки по тексту поста, заголовку статьи, краткому содержанию и техническим флагам. LogisticRegression возвращает основную метку, дополнительные варианты и confidence. Результат сохраняется с версией модели.",
        "stage": "sorted",
        "status": "classified",
    },
    {
        "key": "article",
        "title": "Извлечение статьи и подготовка материала к рерайту",
        "text": "HTML extractor разобрал собственную тестовую страницу: выделил заголовок, основной текст и описание. Нормализованный материал связан с исходным постом и помещён в очередь редакционного рерайта без обращения к внешнему сайту.",
        "stage": "enriched",
        "status": "rewrite_pending",
    },
    {
        "key": "draft",
        "title": "Черновик подготовлен локальным шаблонным рерайтом",
        "text": "Редакционный pipeline собрал черновик из нормализованного материала и извлечённой статьи. Локальный шаблон сохраняет ссылку на источник, ограничивает длину и добавляет флаги для ручной проверки. Внешний LLM отключён.",
        "stage": "rewritten",
        "status": "needs_review",
        "draft": "needs_review",
    },
    {
        "key": "ready",
        "title": "Материал прошёл проверку и готов к публикации",
        "text": "Черновик прошёл локальную валидацию: заголовок и тело заполнены, ссылка на источник сохранена, ошибок формата нет. Запись находится в статусе готовности, однако реальная отправка в Telegram и MAX в демонстрационном наборе запрещена.",
        "stage": "ready",
        "status": "ready_for_publication",
        "draft": "ready_for_publication",
    },
    {
        "key": "blocked",
        "title": "Публикация материала заблокирована оператором",
        "text": "Материал обработан и классифицирован, но публикация запрещена операторским решением. Исходные данные и промежуточные результаты сохранены, поэтому причину можно проверить и снять блокировку без повторного сбора.",
        "stage": "received",
        "status": "blocked",
        "publication_allowed": False,
        "blocked_reason": "Демонстрационный материал: внешняя публикация отключена",
    },
    {
        "key": "retry",
        "title": "Ошибка рерайта сохранена с возможностью повторного запуска",
        "text": "Один этап завершился контролируемой ошибкой локального шаблона. Pipeline записал попытку, текст ошибки и время завершения. Оператор может повторить только проблемный этап, не создавая второй материал и не теряя предыдущие результаты.",
        "stage": "received",
        "status": "rewrite_failed",
        "last_error": "portfolio_demo: контролируемая ошибка шаблонного рерайта",
    },
    {
        "key": "search",
        "title": "Полнотекстовый поиск связывает исходники и черновики",
        "text": "Поисковый индекс PostgreSQL хранит документы исходных постов, обработанных материалов и редакционных черновиков. Русская tsvector-конфигурация позволяет искать одну тему по разным стадиям и фильтровать результаты по жанру, домену и каналу.",
        "stage": "sorted",
        "status": "classified",
    },
    {
        "key": "model-registry",
        "title": "Версия классификатора зарегистрирована в model registry",
        "text": "Артефакт TF-IDF и LogisticRegression имеет стабильную версию и путь хранения. Registry отделяет candidate, active и archived версии. Демонстрационная модель обучена только на собственном малом корпусе и не выдаётся за оценённую production-модель.",
        "stage": "enriched",
        "status": "rewrite_pending",
    },
    {
        "key": "review",
        "title": "Редактор проверяет утверждения и исходную ссылку",
        "text": "Карточка показывает исходный текст, извлечённую статью, классификацию и текущий черновик. Редактор видит risk flags, меняет текст и принимает решение. Все внешние действия остаются выключенными до явного подтверждения.",
        "stage": "rewritten",
        "status": "needs_review",
        "draft": "needs_review",
    },
    {
        "key": "controlled-release",
        "title": "Готовый черновик остаётся в контролируемой очереди",
        "text": "Материал прошёл реальные локальные этапы демонстрационного контура и готов к ручной проверке. Отправка во внешний канал не запускается: AUTO_PUBLISH отключён, целевой showcase не содержит реального адреса назначения.",
        "stage": "ready",
        "status": "ready_for_publication",
        "draft": "ready_for_publication",
    },
)


def build_demo_projection(material: dict[str, Any], post_id: int) -> dict[str, Any]:
    """Run deterministic data through the same local functions used by the application."""
    source_url = f"https://portfolio-demo.invalid/materials/{material['key']}"
    post = TelegramPost(id=post_id, text=material["text"], raw={"portfolio_demo": True})
    processed = processed_values(post)
    html = (
        '<html lang="ru"><head>'
        f"<title>{escape(material['title'])}</title>"
        f'<meta name="description" content="{escape(material["text"][:180])}">'
        "</head><body><article>"
        f"<h1>{escape(material['title'])}</h1><p>{escape(material['text'])}</p>"
        "</article></body></html>"
    )
    extracted = extract_html(html.encode("utf-8"), source_url)
    classification_text = build_classification_text(
        processed["clean_text"],
        extracted.get("title"),
        extracted.get("description"),
        extracted.get("extracted_text"),
        processed.get("domains"),
        processed,
    )
    label, secondary, confidence, scores = predict(model(), classification_text)
    item = ContentItem(
        source_post_id=post_id,
        title=extracted.get("title"),
        main_text=processed["clean_text"],
        source_summary=extracted.get("extracted_text"),
        source_url=source_url,
        source_domain="portfolio-demo.invalid",
        source_lang=processed.get("language"),
        target_lang="ru",
    )
    draft = draft_from_template(item, Showcase(), {"max_body_chars": 1500}, label)
    return {
        "source_url": source_url,
        "processed": processed,
        "extracted": extracted,
        "classification": {
            "label": label,
            "secondary": secondary,
            "confidence": confidence,
            "scores": scores,
        },
        "item": item,
        "draft": draft,
    }


async def _remove_demo(session) -> dict[str, int]:
    post_ids = list(
        (await session.execute(select(TelegramPost.id).where(TelegramPost.chat_peer_id == DEMO_CHAT_ID))).scalars()
    )
    item_ids: list[int] = []
    draft_ids: list[int] = []
    if post_ids:
        item_ids = list(
            (await session.execute(select(ContentItem.id).where(ContentItem.source_post_id.in_(post_ids)))).scalars()
        )
        draft_ids = list(
            (await session.execute(select(PublicationDraft.id).where(PublicationDraft.source_post_id.in_(post_ids)))).scalars()
        )
        for entity_type, ids in (
            ("telegram_post", post_ids),
            ("content_item", item_ids),
            ("publication_draft", draft_ids),
        ):
            if ids:
                await session.execute(
                    delete(SearchDocument).where(
                        SearchDocument.entity_type == entity_type,
                        SearchDocument.entity_id.in_(ids),
                    )
                )
        await session.execute(delete(TelegramPost).where(TelegramPost.id.in_(post_ids)))
    await session.execute(delete(TelegramChat).where(TelegramChat.peer_id == DEMO_CHAT_ID))
    await session.execute(delete(Showcase).where(Showcase.slug == DEMO_SHOWCASE))
    await session.execute(delete(ModelVersion).where(ModelVersion.model_version == DEMO_MODEL_VERSION))
    await session.commit()
    return {"posts": len(post_ids), "items": len(item_ids), "drafts": len(draft_ids)}


async def seed_portfolio_demo(*, remove: bool = False) -> dict[str, Any]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        if remove:
            result = await _remove_demo(session)
            artifact = Path(settings.artifacts_dir) / "portfolio" / f"{DEMO_MODEL_VERSION}.joblib"
            artifact.unlink(missing_ok=True)
            return {"removed": result}

        now = datetime.now(timezone.utc)
        chat_values = {
            "peer_id": DEMO_CHAT_ID,
            "title": "Portfolio Demo · редакционный поток",
            "username": "portfolio_demo",
            "chat_type": "channel",
            "folder_name": DEMO_FOLDER,
            "raw": {"portfolio_demo": True, "external_collection": False},
            "updated_at": now,
        }
        chat_stmt = insert(TelegramChat.__table__).values(**chat_values)
        await session.execute(
            chat_stmt.on_conflict_do_update(
                index_elements=[TelegramChat.__table__.c.peer_id],
                set_={key: chat_stmt.excluded[key] for key in chat_values if key != "peer_id"},
            )
        )

        showcase_values = {
            "slug": DEMO_SHOWCASE,
            "title": "Portfolio Demo",
            "description": "Синтетические материалы; внешняя публикация отключена.",
            "target_type": "disabled",
            "default_rewrite_template": "news_short",
            "is_active": False,
            "updated_at": now,
        }
        showcase_stmt = insert(Showcase.__table__).values(**showcase_values)
        showcase_id = int(
            (
                await session.execute(
                    showcase_stmt.on_conflict_do_update(
                        index_elements=[Showcase.__table__.c.slug],
                        set_={key: showcase_stmt.excluded[key] for key in showcase_values if key != "slug"},
                    ).returning(Showcase.__table__.c.id)
                )
            ).scalar_one()
        )

        artifact = Path(settings.artifacts_dir) / "portfolio" / f"{DEMO_MODEL_VERSION}.joblib"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        dump(model(), artifact)
        active_model = (
            await session.execute(
                select(ModelVersion.id)
                .where(
                    ModelVersion.model_name == "tfidf_logreg",
                    ModelVersion.status == "active",
                    ModelVersion.model_version != DEMO_MODEL_VERSION,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        model_values = {
            "model_name": "tfidf_logreg",
            "model_version": DEMO_MODEL_VERSION,
            "model_type": "TfidfVectorizer + LogisticRegression",
            "artifact_path": str(artifact),
            "label_schema_version": "portfolio-demo-v1",
            "train_corpus_hash": "synthetic-local-corpus-v1",
            "train_size": 6,
            "val_size": 0,
            "test_size": 0,
            "metrics": {"evaluated": False, "scope": "synthetic portfolio demonstration"},
            "confusion_matrix": {},
            "status": "candidate" if active_model else "active",
        }
        model_stmt = insert(ModelVersion.__table__).values(**model_values)
        await session.execute(
            model_stmt.on_conflict_do_update(
                constraint="uq_model_versions_name_version",
                set_={key: model_stmt.excluded[key] for key in model_values if key not in {"model_name", "model_version"}},
            )
        )

        seeded: list[dict[str, Any]] = []
        for index, material in enumerate(DEMO_MATERIALS, start=1):
            post_values = {
                "chat_peer_id": DEMO_CHAT_ID,
                "message_id": 910_000 + index,
                "sender_peer_id": DEMO_CHAT_ID,
                "date": now - timedelta(hours=index * 3),
                "text": material["text"],
                "views": 120 + index * 17,
                "forwards": index % 4,
                "replies_count": index % 3,
                "raw": {
                    "portfolio_demo": True,
                    "fixture": material["key"],
                    "source": "seed-portfolio-demo",
                    "external_actions": False,
                },
                "is_deleted": False,
                "updated_at": now,
            }
            post_stmt = insert(TelegramPost.__table__).values(**post_values)
            post_id = int(
                (
                    await session.execute(
                        post_stmt.on_conflict_do_update(
                            constraint="uq_telegram_posts_chat_message",
                            set_={key: post_stmt.excluded[key] for key in post_values if key not in {"chat_peer_id", "message_id"}},
                        ).returning(TelegramPost.__table__.c.id)
                    )
                ).scalar_one()
            )
            projection = build_demo_projection(material, post_id)
            processed_values_row = dict(projection["processed"])
            processed_stmt = insert(PostProcessed.__table__).values(**processed_values_row)
            await session.execute(
                processed_stmt.on_conflict_do_update(
                    index_elements=[PostProcessed.__table__.c.post_id],
                    set_={key: processed_stmt.excluded[key] for key in processed_values_row if key != "post_id"},
                )
            )

            link_values = {
                "post_id": post_id,
                "original_url": projection["source_url"],
                "canonical_url": projection["source_url"],
                "final_url": projection["source_url"],
                "domain": "portfolio-demo.invalid",
                "url_type": "article",
                "position_index": 0,
                "is_primary": True,
                "extraction_status": "done",
                "updated_at": now,
            }
            link_stmt = insert(PostLink.__table__).values(**link_values)
            link_id = int(
                (
                    await session.execute(
                        link_stmt.on_conflict_do_update(
                            constraint="uq_post_links_post_original_url",
                            set_={key: link_stmt.excluded[key] for key in link_values if key not in {"post_id", "original_url"}},
                        ).returning(PostLink.__table__.c.id)
                    )
                ).scalar_one()
            )
            extracted = projection["extracted"]
            snapshot_values = {
                "link_id": link_id,
                "canonical_url": projection["source_url"],
                "final_url": projection["source_url"],
                "domain": "portfolio-demo.invalid",
                "http_status": 200,
                "content_type": "text/html; charset=utf-8",
                "fetched_at": now,
                "title": extracted.get("title"),
                "description": extracted.get("description"),
                "site_name": "Portfolio Demo",
                "author": "Sergey Komarov",
                "published_at": now - timedelta(hours=index * 3),
                "extracted_text": extracted.get("extracted_text"),
                "extracted_text_hash": processed_values_row["text_hash"],
                "extraction_method": extracted.get("extraction_method") or "trafilatura/bs4",
                "extraction_quality_score": extracted.get("extraction_quality_score") or 1,
                "summary_short": extracted.get("extracted_text"),
                "summary_model": "local-source-text",
                "summary_generated_at": now,
                "raw_metadata": {"portfolio_demo": True, "network_fetch": False},
                "error": None,
                "updated_at": now,
            }
            snapshot_stmt = insert(LinkSnapshot.__table__).values(**snapshot_values)
            snapshot_id = int(
                (
                    await session.execute(
                        snapshot_stmt.on_conflict_do_update(
                            constraint="uq_link_snapshots_link_id",
                            set_={key: snapshot_stmt.excluded[key] for key in snapshot_values if key != "link_id"},
                        ).returning(LinkSnapshot.__table__.c.id)
                    )
                ).scalar_one()
            )

            item_values = {
                "source_post_id": post_id,
                "primary_link_id": link_id,
                "primary_snapshot_id": snapshot_id,
                "title": material["title"],
                "main_text": processed_values_row["clean_text"],
                "source_summary": extracted.get("extracted_text"),
                "source_url": projection["source_url"],
                "source_domain": "portfolio-demo.invalid",
                "source_lang": processed_values_row.get("language"),
                "target_lang": "ru",
                "translated_title": material["title"],
                "translated_summary": extracted.get("extracted_text"),
                "content_hash": processed_values_row["text_hash"],
                "quality_score": 1,
                "status": "ready",
                "updated_at": now,
            }
            item_stmt = insert(ContentItem.__table__).values(**item_values)
            item_id = int(
                (
                    await session.execute(
                        item_stmt.on_conflict_do_update(
                            constraint="uq_content_items_source_post_id",
                            set_={key: item_stmt.excluded[key] for key in item_values if key != "source_post_id"},
                        ).returning(ContentItem.__table__.c.id)
                    )
                ).scalar_one()
            )

            classification = projection["classification"]
            classification_values = {
                "post_id": post_id,
                "content_item_id": item_id,
                "classifier_name": "tfidf_logreg",
                "classifier_version": DEMO_MODEL_VERSION,
                "label_primary": classification["label"],
                "label_secondary": classification["secondary"],
                "label_scores": classification["scores"],
                "confidence": classification["confidence"],
                "explanation": "TF-IDF LogisticRegression over post, extracted article and local metadata. Synthetic portfolio corpus.",
                "needs_review": classification["confidence"] < 0.55,
            }
            classification_stmt = insert(PostClassification.__table__).values(**classification_values)
            classification_id = int(
                (
                    await session.execute(
                        classification_stmt.on_conflict_do_update(
                            constraint="uq_post_classifications_model",
                            set_={key: classification_stmt.excluded[key] for key in classification_values if key not in {"post_id", "classifier_name", "classifier_version"}},
                        ).returning(PostClassification.__table__.c.id)
                    )
                ).scalar_one()
            )

            target_values = {
                "content_item_id": item_id,
                "showcase_id": showcase_id,
                "route_reason": f"portfolio_demo label={classification['label']}",
                "route_score": classification["confidence"],
                "status": "pending",
                "updated_at": now,
            }
            target_stmt = insert(PublicationTarget.__table__).values(**target_values)
            target_id = int(
                (
                    await session.execute(
                        target_stmt.on_conflict_do_update(
                            constraint="uq_publication_targets_item_showcase",
                            set_={key: target_stmt.excluded[key] for key in target_values if key not in {"content_item_id", "showcase_id"}},
                        ).returning(PublicationTarget.__table__.c.id)
                    )
                ).scalar_one()
            )

            draft_id: int | None = None
            if material.get("draft"):
                draft = projection["draft"]
                existing_draft = (
                    await session.execute(
                        select(PublicationDraft.id).where(
                            PublicationDraft.source_post_id == post_id,
                            PublicationDraft.publication_target_id == target_id,
                        )
                    )
                ).scalar_one_or_none()
                draft_values = {
                    "publication_target_id": target_id,
                    "source_post_id": post_id,
                    "rewrite_model": "local_template",
                    "rewrite_template": "news_short",
                    "title": draft["title"],
                    "body": draft["body"],
                    "source_url": draft["source_url"],
                    "source_domain": draft["source_domain"],
                    "tags": [classification["label"], "portfolio-demo"],
                    "claims": [],
                    "risk_flags": draft["risk_flags"],
                    "similarity_to_original": draft["similarity_to_original"],
                    "validation_errors": [],
                    "status": material["draft"],
                    "updated_at": now,
                }
                if existing_draft:
                    draft_id = int(existing_draft)
                    await session.execute(
                        update(PublicationDraft).where(PublicationDraft.id == draft_id).values(**draft_values)
                    )
                else:
                    draft_id = int(
                        (
                            await session.execute(
                                insert(PublicationDraft.__table__).values(**draft_values).returning(PublicationDraft.__table__.c.id)
                            )
                        ).scalar_one()
                    )

            publication_allowed = material.get("publication_allowed", True)
            entry_values = {
                "source_post_id": post_id,
                "content_item_id": item_id,
                "classification_id": classification_id,
                "classification_model_version": DEMO_MODEL_VERSION,
                "genre_primary": classification["label"],
                "genre_secondary": classification["secondary"],
                "confidence": classification["confidence"],
                "difficulty_score": 35 + index,
                "promo_score": 10 + index,
                "opinion_score": 15 + index,
                "event_score": 45 + index,
                "is_eligible": material["stage"] not in {"received"} or bool(material.get("draft")),
                "eligibility_reason": "portfolio_demo deterministic classification",
                "publication_allowed": publication_allowed,
                "blocked_reason": material.get("blocked_reason"),
                "latest_draft_id": draft_id,
                "stage": material["stage"],
                "status": material["status"],
                "last_error": material.get("last_error"),
                "last_operation_at": now - timedelta(minutes=index),
                "updated_at": now,
            }
            entry_stmt = insert(PipelineEntry.__table__).values(**entry_values)
            entry_id = int(
                (
                    await session.execute(
                        entry_stmt.on_conflict_do_update(
                            constraint="uq_pipeline_entries_source_post",
                            set_={key: entry_stmt.excluded[key] for key in entry_values if key != "source_post_id"},
                        ).returning(PipelineEntry.__table__.c.id)
                    )
                ).scalar_one()
            )
            state_values = {
                "post_id": post_id,
                "processing_status": "done",
                "link_status": "done",
                "enrichment_status": "done",
                "summary_status": "done",
                "translation_status": "done",
                "material_status": "done",
                "classification_status": "done",
                "routing_status": "done",
                "rewrite_status": material["status"] if material["status"].startswith("rewrite") else (material.get("draft") or "pending"),
                "publication_status": "blocked" if not publication_allowed else "pending",
                "last_error": material.get("last_error"),
                "retry_count": 1 if material.get("last_error") else 0,
                "updated_at": now,
            }
            state_stmt = insert(ContentPipelineState.__table__).values(**state_values)
            await session.execute(
                state_stmt.on_conflict_do_update(
                    index_elements=[ContentPipelineState.__table__.c.post_id],
                    set_={key: state_stmt.excluded[key] for key in state_values if key != "post_id"},
                )
            )

            await session.execute(delete(RewriteAttempt).where(RewriteAttempt.pipeline_entry_id == entry_id))
            if material.get("last_error") or draft_id:
                attempt_failed = bool(material.get("last_error"))
                await session.execute(
                    insert(RewriteAttempt.__table__).values(
                        pipeline_entry_id=entry_id,
                        publication_draft_id=draft_id,
                        source_post_id=post_id,
                        rewrite_model="local_template",
                        status="failed" if attempt_failed else "done",
                        error=material.get("last_error"),
                        request_meta={"portfolio_demo": True, "network": False, "events": ["local template started"]},
                        response_raw={"events": ["local template failed" if attempt_failed else "draft saved"]},
                        risk_flags=[],
                        validation_errors=[],
                        started_at=now - timedelta(seconds=2),
                        finished_at=now,
                    )
                )

            await upsert_document(
                session,
                {
                    "entity_type": "telegram_post",
                    "entity_id": post_id,
                    "title": "Portfolio Demo · редакционный поток",
                    "body": material["text"],
                    "source_text": material["text"],
                    "labels": [],
                    "source_chat": "Portfolio Demo · редакционный поток",
                    "language": processed_values_row.get("language"),
                },
            )
            await upsert_document(
                session,
                {
                    "entity_type": "content_item",
                    "entity_id": item_id,
                    "title": material["title"],
                    "body": extracted.get("extracted_text"),
                    "source_text": material["text"],
                    "labels": [classification["label"]],
                    "source_domain": "portfolio-demo.invalid",
                    "source_chat": "Portfolio Demo · редакционный поток",
                    "language": processed_values_row.get("language"),
                },
            )
            if draft_id:
                await upsert_document(
                    session,
                    {
                        "entity_type": "publication_draft",
                        "entity_id": draft_id,
                        "title": projection["draft"]["title"],
                        "body": projection["draft"]["body"],
                        "labels": [classification["label"], "portfolio-demo"],
                        "source_domain": "portfolio-demo.invalid",
                    },
                )
            seeded.append({"key": material["key"], "post_id": post_id, "entry_id": entry_id, "draft_id": draft_id})

        await session.commit()
        return {
            "seeded": len(seeded),
            "drafts": sum(1 for row in seeded if row["draft_id"]),
            "external_collection": False,
            "external_publication": False,
            "rows": seeded,
        }
