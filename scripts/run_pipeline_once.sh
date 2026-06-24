#!/usr/bin/env bash
set -euo pipefail

python -m app.content.processor process-new
python -m app.content.url_extractor extract-new
python -m app.content.link_enricher enrich-pending
python -m app.content.media_assets download-pending
python -m app.content.local_summary summarize-pending
python -m app.content.material_builder build-new
python -m app.content.local_translation translate-pending
python -m app.content.classifier classify-new
python -m app.content.router route-new
python -m app.content.rewriter rewrite-pending
python -m app.content.validators validate-drafts
python -m app.content.search reindex

