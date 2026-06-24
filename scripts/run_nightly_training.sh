#!/usr/bin/env bash
set -euo pipefail

python -m app.content.corpus_builder build-corpus
python -m app.content.trainer train-candidate
python -m app.content.evaluator evaluate-candidate
python -m app.content.model_promoter promote-if-better

