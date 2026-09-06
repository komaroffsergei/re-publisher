# re-publisher: публичный демонстрационный сценарий

Поддомен: https://publisher.komaroff-dev.ru/. Подготовка демо: 7 сентября 2026.
Дата подготовки не является датой начала разработки исходного проекта.

## Что выполняется

`app.portfolio` — отдельная точка входа. Операционный admin и Telegram collector не монтируются.
Три собственных материала проходят исходные `processed_values`, `extract_html`,
`build_classification_text`, `predict` и `draft_from_template`.
HTML читается из строки, а не загружается по пользовательскому адресу.
Измерения этапов снимаются `perf_counter` на сервере и не являются внешним бенчмарком.

Telegram input заменён фиксированным корпусом, LLM — существующим шаблонным редактором.
TF-IDF + LogisticRegression работают реально. Модель обучается на шести синтетических
примерах с фиксированным random_state. Числа классификатора не характеризуют качество
на независимой выборке. Внешняя публикация, shell, Codex, платные API и загрузки отключены.

## Хранилище и границы

Общий PostgreSQL, отдельные БД/пользователь publisher. Только таблица `portfolio_runs`
с результатами демонстрации; рабочая схема исходного collector не разворачивается.
Подписанная cookie Secure/HttpOnly/SameSite=Lax, срок один час. В БД хранится SHA-256
сессии. Все чтения, повтор и сброс ограничены этой сессией. Очистка каждые пять минут.
Лимиты: 20 материалов на сессию, 1000 всего, два места выполнения. Общий лимит
проверяется под PostgreSQL advisory lock. Разрешены только три fixture ID.
Повтор сбоя обновляет тот же материал, не создавая новую запись.

## Code-map

| Функция | Экран / API | Модуль | Данные | Проверка |
|---|---|---|---|---|
| Выбор материала | форма / GET api/runs | portfolio_web/app.js, portfolio_pipeline.CORPUS | синтетический корпус | production browser QA |
| Очистка | POST api/runs | content/processor.processed_values | text_hash, язык, признаки | test_content_utils.py |
| Извлечение | POST api/runs | content/link_enricher.extract_html | текст HTML | test_portfolio_pipeline.py |
| Классификация | результат | content/classifier.predict | TF-IDF модель | test_portfolio_pipeline.py |
| Черновик | результат | content/rewriter.draft_from_template | portfolio_runs.result | test_portfolio_pipeline.py |
| Повтор и сброс | POST retry / DELETE runs | portfolio.py | только своя session_id | production API isolation QA |

Перед изменением проследить функцию от экрана до модуля и хранилища. После изменения
актуализировать карту, схемы и связанные сценарии. Исходный сборщик и публичное демо
имеют разные схемы развёртывания; нельзя выдавать демонстрационную таблицу за рабочую БД.

## Сборка и эксплуатация

Сборка `docker build -f Dockerfile.portfolio -t portfolio-publisher:VERSION .` вне VPS.
Python base закреплён digest, полный набор зависимостей — requirements.portfolio.lock.
Запуск: DB_DSN и PORTFOLIO_SECRET передаются окружением, секреты не включаются в образ.
384 MiB RAM, 0.7 CPU, healthcheck /healthz проверяет БД, Docker logs 3×5 MB.
Нет публичных портов БД. Nginx проксирует localhost:18404.
Workflow по умолчанию заменён ручной проверкой без SSH, prune и удаления логов сервера.
