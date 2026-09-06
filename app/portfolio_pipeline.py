"""Synthetic fixtures passed through the existing extraction and editorial modules."""
from functools import lru_cache
from html import escape
from time import perf_counter

from langdetect import DetectorFactory
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from app.content.classifier import build_classification_text, predict
from app.content.link_enricher import extract_html
from app.content.processor import processed_values
from app.content.rewriter import draft_from_template
from app.models import ContentItem, Showcase, TelegramPost

DetectorFactory.seed = 0
CORPUS = [
    {"id": "tool", "title": "TraceBox: учебный инструмент проверки RAG", "label": "tool",
     "text": "Команда учебного проекта собрала TraceBox — инструмент для проверки RAG. Он сохраняет запрос, найденные фрагменты и ответ в локальный отчёт. Разработчик видит, какой источник попал в контекст и где отсутствует подтверждение. Пример использует синтетические документы и не содержит результатов промышленного применения. Установка и тестовый запуск описаны в инструкции. Сервис не обещает точность модели: результат проверяет редактор."},
    {"id": "news", "title": "Учебный релиз: наблюдаемость очереди обработки", "label": "news",
     "text": "В учебном проекте вышел релиз панели наблюдения за очередью обработки. Новая версия показывает состояния материала, время извлечения текста и причину ошибки. События можно повторить после исправления входного примера. Команда добавила журнал переходов и проверки идемпотентности. Это собственный синтетический материал для демонстрации редакционного конвейера. Дата релиза и название продукта вымышлены; внешние публикации не выполняются."},
    {"id": "education", "title": "Практикум: источники, чанки и проверка ответа", "label": "education",
     "text": "Практикум объясняет, как устроить проверку ответа в RAG-приложении. Сначала студент извлекает текст из HTML, затем делит его на фрагменты и связывает результат с источником. Следующий шаг — сравнить ответ с документом и отметить утверждения без подтверждения. Упражнение выполняется на учебных данных. В конце нужно сохранить отчёт, повторить сценарий без контекста и описать ограничения. Это учебная программа, а не новость о коммерческом продукте."},
]


@lru_cache(maxsize=1)
def model():
    """Small reproducible demonstration model, not an evaluated production classifier."""
    samples = [(x["text"], x["label"]) for x in CORPUS]
    samples += [("инструмент установка библиотека разработчик локальный запуск", "tool"),
                ("вышел релиз новая версия обновление команда представила", "news"),
                ("практикум урок упражнение обучение студент инструкция", "education")]
    pipeline = make_pipeline(TfidfVectorizer(ngram_range=(1, 2)), LogisticRegression(random_state=42, max_iter=300))
    pipeline.fit([x[0] for x in samples], [x[1] for x in samples])
    return pipeline


def run_pipeline(fixture_id: str, simulate_error: bool = False):
    fixture = next(x for x in CORPUS if x["id"] == fixture_id)
    stages = []

    def measured(name, function):
        start = perf_counter()
        value = function()
        stages.append({"name": name, "state": "done", "ms": round((perf_counter() - start) * 1000, 2)})
        return value

    post = TelegramPost(id=1, text=fixture["text"], raw={})
    processed = measured("Очистка и признаки", lambda: processed_values(post))
    if simulate_error:
        stages.append({"name": "Извлечение HTML", "state": "failed", "ms": 0, "error": "Демонстрационный сбой чтения материала. Внешний запрос не выполнялся."})
        return {"status": "failed", "stages": stages, "fixture": fixture, "processed": processed}
    html = f'<html lang="ru"><head><title>{escape(fixture["title"])}</title></head><body><article><h1>{escape(fixture["title"])}</h1><p>{escape(fixture["text"])}</p></article></body></html>'
    url = f'https://publisher.komaroff-dev.ru/source/{fixture_id}'
    extracted = measured("Извлечение HTML", lambda: extract_html(html.encode(), url))
    classification_text = build_classification_text(processed["clean_text"], extracted["title"], link_summary_ru=extracted["extracted_text"], flags=processed)
    label, secondary, confidence, scores = measured("TF-IDF + LogisticRegression", lambda: predict(model(), classification_text))
    item = ContentItem(title=extracted["title"], main_text=processed["clean_text"], source_summary=extracted["extracted_text"], source_url=url, source_domain="publisher.komaroff-dev.ru")
    draft = measured("Шаблонный черновик — мок LLM", lambda: draft_from_template(item, Showcase(), {"max_body_chars": 1800}, label))
    return {"status": "draft", "stages": stages, "fixture": fixture, "processed": processed, "extracted": extracted,
            "classification": {"label": label, "secondary": secondary, "scores": scores, "model": "synthetic-tfidf-logreg-v1", "limitation": "Обучено на шести собственных примерах. Числа — выход классификатора, не измеренная точность."}, "draft": draft}
