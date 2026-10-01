"""Версия относится к реально прочитанным весам, а не к строке в worker.

Для старых артефактов без имени релиза показываем ключ модели и хеш весов.
Новый manifest может задавать понятное имя, но его контрольная сумма обязательна.
"""
import hashlib
import json
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_version(directory: Path, key: str, weights: Path) -> str:
    checksum = file_sha256(weights)
    manifest_path = directory / 'model-manifest.json'
    if not manifest_path.exists():
        return f'{key}@{checksum[:16]}'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    metadata = manifest['models'][key]
    if metadata['weights_sha256'] != checksum:
        raise ValueError('Loaded weights do not match model manifest')
    for relative, expected in metadata.get('auxiliary_sha256', {}).items():
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory.resolve()) or file_sha256(target) != expected:
            raise ValueError('Loaded model configuration does not match manifest')
    return metadata['version']


def checkpoint_path(directory: Path, training: dict) -> Path:
    relative = training.get('best_checkpoint', 'epoch-2.safetensors')
    result = (directory / relative).resolve()
    if not result.is_relative_to(directory.resolve()) or result.suffix != '.safetensors':
        raise ValueError('Invalid checkpoint path')
    return result


def configured_artifact(directory: Path, key: str) -> Path:
    manifest = directory / 'model-manifest.json'
    relative = ('baseline.joblib' if key == 'tfidf' else 'minilm-v2')
    if manifest.exists():
        relative = json.loads(manifest.read_text(encoding='utf-8'))['models'][key]['path']
    result = (directory / relative).resolve()
    if not result.is_relative_to(directory.resolve()):
        raise ValueError('Model path is outside the artifact')
    return result
