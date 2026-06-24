# Telegram Folder Collector Service

Collects Telegram messages from all chats, channels, and groups resolved from a user account's Telegram folder and stores them in PostgreSQL. The default folder is `MAX`.

This service uses a user MTProto session through Telethon. It does not use the Bot API.

## Configuration

Create `secrets/app.env`:

```env
TG_API_ID=123456
TG_API_HASH=replace-with-api-hash
TG_PHONE=
TG_SESSION_NAME=./sessions/max_collector
FOLDER_NAME=MAX
DB_DSN=postgresql+asyncpg://max_collector:max_collector@localhost:55432/max_collector
COLLECT_COMMENTS=true
DOWNLOAD_MEDIA=false
MEDIA_DIR=./media
SYNC_LIMIT_PER_CHAT=0
FOLDER_REFRESH_SECONDS=300
LOG_LEVEL=INFO
```

`TG_PHONE` is optional. If it is absent, the `login` command prompts for it.

For the Docker app container, `docker-compose.yml` overrides `DB_DSN` to use the Compose service hostname `postgres`.

## Local PostgreSQL

Start PostgreSQL:

```bash
docker compose up -d postgres
```

Apply migrations from the host:

```bash
python -m app.main migrate
```

Or apply them inside the app container:

```bash
docker compose run --rm app python -m app.main migrate
```

## Login

Create the Telethon session:

```bash
python -m app.main login
```

Enter the Telegram code and 2FA password if Telegram asks for them. Protect the generated `.session` file like a password.

The login command prints the delivery type returned by Telegram. `SentCodeTypeApp` means the code was sent to an already authorized Telegram app, not SMS. If Telegram allows it, request SMS delivery with:

```bash
python -m app.main login --force-sms
```

If Telegram does not deliver the code, use QR login instead:

```bash
python -m app.main login-qr
```

Open Telegram on your phone and scan it through Settings -> Devices -> Link Desktop Device.

## Inspect Folders

List folders:

```bash
python -m app.main inspect-folders
```

Inspect one folder:

```bash
python -m app.main inspect-folder --folder MAX
```

## Sync

Run an initial or incremental sync:

```bash
python -m app.main sync --folder MAX
```

For a bounded local test, set `SYNC_LIMIT_PER_CHAT=20` in `secrets/app.env`.

## Daemon

Run the long-lived service:

```bash
python -m app.main run --folder MAX
```

With Docker:

```bash
docker compose up --build app
```

## Comments And Media

Set `COLLECT_COMMENTS=false` to skip discussion comments.

Set `DOWNLOAD_MEDIA=true` to download media into `MEDIA_DIR`. Media download failures are logged and do not block message persistence.

## External PostgreSQL

Point `DB_DSN` to the external database:

```env
DB_DSN=postgresql+asyncpg://user:password@host:5432/database
```

Restrict database access with firewall rules, VPN, or private networking.

## Security Notes

- Do not commit `secrets/`, `.env`, session files, or media.
- Protect the Telethon `.session` file; it grants account access.
- Restrict PostgreSQL access to trusted networks.
- Use a dedicated database user with the minimum required privileges.

## Editorial Pipeline

The repository also contains a fully local editorial pipeline on top of `telegram_posts`:

1. clean and normalize posts;
2. extract URLs from text and Telegram raw entities;
3. fetch/enrich links with SSRF protections;
4. extract article metadata/text/images;
5. generate link summaries with YandexGPT or a local extractive fallback;
6. build `content_items`;
7. classify materials with the active TF-IDF LogisticRegression model;
8. route materials to showcases;
9. create Russian publication drafts through YandexGPT or local templates;
10. validate drafts and expose everything in the FastAPI web app.

For YandexGPT summaries and rewrites, add these values to `secrets/app.env`:

```env
ENABLE_EXTERNAL_LLM=true
SUMMARY_BACKEND=yandexgpt
REWRITE_BACKEND=yandexgpt
YANDEX_API_KEY=...
YANDEX_API_KEY_ID=...
YANDEX_FOLDER_ID=...
YANDEX_SUMMARY_MODEL_URI=gpt://<folder-id>/yandexgpt-5-lite
YANDEX_REWRITE_MODEL_URI=gpt://<folder-id>/yandexgpt-5.1
```

Check credentials without printing secrets:

```bash
python -m app.content.yandex_gpt check-credentials
```

Run experimental YandexGPT Lite genre-axis classification without changing the active TF-IDF classifier:

```bash
python -m app.content.yandex_genre_classifier classify --limit 3 --dry-run
```

After reviewing token usage, write a 100-row run to the separate `yandex_genre_classifications` table:

```bash
python -m app.content.yandex_genre_classifier classify --limit 100 --write --run-id yandex_axes_manual_001
python -m app.content.yandex_genre_classifier report --run-id yandex_axes_manual_001
```

Retrain the existing `tfidf_logreg` model line as a candidate from the initial trainable corpus plus the saved Yandex run:

```bash
python -m app.content.yandex_genre_trainer train --run-id yandex_axes_manual_001
```

Apply migrations:

```bash
python -m app.main migrate
```

Import the prepared first model from the local corpus package:

```bash
python -m app.content.model_registry register-initial-model --package-dir "C:\Users\New\Downloads\telegram_labeled_corpus_package"
```

Run the pipeline once:

```bash
bash scripts/run_pipeline_once.sh
```

Or run the all-in-one nightly command:

```bash
python -m app.content.nightly_maintainer run
```

Start the web app:

```bash
python -m app.web.main run --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080`.

## Classification Model

The initial active model is expected at:

```text
models/active/tfidf_logreg.joblib
```

It is copied from `telegram_labeled_corpus_package/tfidf_logreg_model_full_corpus.joblib` and registered in `model_versions`.

The model labels are:

- `business_market`
- `community_chat`
- `education_guide`
- `humor_meme`
- `news_digest`
- `opinion_commentary`
- `promo_career_event`
- `technical_research`
- `tool_product`

Build a new corpus and candidate model:

```bash
python -m app.content.corpus_builder build-corpus
python -m app.content.trainer train-candidate
python -m app.content.evaluator evaluate-candidate
python -m app.content.model_promoter promote-if-better
```

`promote-if-better` refuses promotion if metric gates do not pass or the active model has no comparable test metrics.

## Drafts And Publishing

Drafts are review-only by default. The publisher sends nothing unless all of these are true:

- draft status is `approved`;
- `AUTO_PUBLISH=true`;
- `BOT_TOKEN` is configured;
- the target showcase has `target_chat_id` or `target_username`.

## Optional Codex Labeling

Codex labeling is disabled by default. Export a batch manually:

```bash
python -m app.content.nightly_maintainer export-codex-labeling-batch
```

Codex should write:

- `artifacts/codex_labels/<date>/auto_labeled_new_posts.jsonl`
- `artifacts/codex_labels/<date>/taxonomy_proposal.yaml`
- `artifacts/codex_labels/<date>/codex_labeling_report.md`

Import validated proposals:

```bash
python -m app.content.nightly_maintainer import-codex-labels artifacts/codex_labels/<date>/auto_labeled_new_posts.jsonl
```

Imported Codex labels are stored as `post_labels.source='codex_agent'` and `status='proposed'` unless `AUTO_ACCEPT_CODEX_LABELS=true` and confidence checks pass.

## Docker

Run collector and web:

```bash
docker compose up --build app web
```

Run the worker profile once:

```bash
docker compose --profile worker run --rm app-worker
```

Runtime data is mounted into:

- `media/`
- `models/`
- `cache/`
- `artifacts/`
- `reports/`

## Nightly Scheduling

Cron example:

```cron
15 2 * * * cd /opt/tg-content && docker compose --profile worker run --rm app-worker
```

Systemd should run the same command from the repository directory with access to `secrets/app.env`.
