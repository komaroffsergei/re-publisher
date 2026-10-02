"""Две модели на одном закрытом dataset. Семантические метки не создаются.

Порог, калибровка и эпоха выбираются на validation. Test читается только
после сохранения выбранных весов и порогов. Отчёт измеряет согласие с Codex.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

import joblib
import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import precision_recall_fscore_support

from channel_corpus import CHECKOUT, TOPICS, positive_group_counts
from train_codex_baseline import labels_from_taxonomy
from app.taxonomy.artifact import file_sha256


def validate_dataset(directory):
    encoded = (directory / 'dataset.jsonl').read_bytes()
    manifest = json.loads((directory / 'dataset-manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256(encoded).hexdigest() != manifest['dataset_sha256']:
        raise ValueError('Dataset changed after freezing')
    rows = [json.loads(s) for s in encoded.splitlines()]
    for row in rows:
        if row['provenance'] not in {'codex_agent', 'codex_agent_legacy_audited'}:
            raise ValueError('Only accepted Codex annotations are permitted')
    groups = {part: {r['group_id'] for r in rows if r['split'] == part} for part in ['train', 'validation', 'test']}
    if any(groups[a] & groups[b] for a, b in [('train', 'validation'), ('train', 'test'), ('validation', 'test')]):
        raise ValueError('Repeat leakage')
    counts = positive_group_counts(rows).get('train', {})
    if any(counts.get(t['id'], 0) < 1000 for t in TOPICS['topics']):
        raise ValueError('At least 1000 confirmed training positive repeat groups per topic are required')
    return rows, manifest


def known(rows, name):
    indices = [i for i, r in enumerate(rows) if r['labels'].get(name) in {'yes', 'no'}]
    return np.asarray(indices, dtype=int), np.asarray([rows[i]['labels'][name] == 'yes' for i in indices], dtype=int)


def deduplicate_groups(rows):
    seen, result = set(), []
    for row in sorted(rows, key=lambda r: r['sha']):
        if row['group_id'] not in seen:
            result.append(row); seen.add(row['group_id'])
    return result


def calibration(raw, rows, names):
    parameters = {}
    logits = np.log(np.clip(raw, 1e-7, 1 - 1e-7) / np.clip(1 - raw, 1e-7, 1))
    for column, name in enumerate(names):
        indices, target = known(rows, name)
        if target.sum() < 20 or len(target) - target.sum() < 20:
            continue
        model = LogisticRegression(C=1, max_iter=1000).fit(logits[indices, column, None], target)
        parameters[name] = {'a': float(model.coef_[0, 0]), 'b': float(model.intercept_[0])}
    return parameters


def calibrate(raw, names, parameters):
    output = raw.copy()
    logits = np.log(np.clip(raw, 1e-7, 1 - 1e-7) / np.clip(1 - raw, 1e-7, 1))
    for index, name in enumerate(names):
        if name in parameters:
            p = parameters[name]
            output[:, index] = 1 / (1 + np.exp(-np.clip(p['a'] * logits[:, index] + p['b'], -50, 50)))
    return output


def score_report(probabilities, rows, names, thresholds=None):
    result = {}
    for column, name in enumerate(names):
        indices, target = known(rows, name)
        if not len(indices):
            result[name] = {'evaluated': 0, 'threshold': None}
            continue
        scores = probabilities[indices, column]
        if thresholds is None:
            choices = np.linspace(.05, .99, 95)
            threshold = max(choices, key=lambda p: precision_recall_fscore_support(target, scores >= p, average='binary', zero_division=0)[2])
        else:
            threshold = thresholds.get(name, .5) or .5
        precision, recall, f1, _ = precision_recall_fscore_support(target, scores >= threshold, average='binary', zero_division=0)
        result[name] = {'evaluated': len(target), 'positive': int(target.sum()), 'negative': int(len(target) - target.sum()),
            'precision': float(precision), 'recall': float(recall), 'f1': float(f1), 'threshold': float(threshold),
            'brier': float(np.mean((scores - target) ** 2))}
    return result


def choose_route(probability, rows, names, name):
    # Runtime округляет до четырёх знаков. Подбор порога должен сравнивать
    # те же оценки, иначе значение на самой границе расходится с фильтром.
    probability = np.round(probability, 4)
    feature = names.index(name); context = names.index('caption_has_context')
    eligible = [i for i, row in enumerate(rows) if row['publication_allowed']
                and row['labels'].get(name) in {'yes', 'no'} and row['labels'].get('caption_has_context') in {'yes', 'no'}]
    best = None
    for threshold in np.linspace(.3, .99, 70):
        for context_threshold in [.5, .65, .8, .9]:
            matched = [i for i in eligible if probability[i, feature] >= threshold and probability[i, context] >= context_threshold]
            correct = sum(rows[i]['labels'][name] == 'yes' and rows[i]['labels']['caption_has_context'] == 'yes' for i in matched)
            positives = sum(rows[i]['labels'][name] == 'yes' and rows[i]['labels']['caption_has_context'] == 'yes' for i in eligible)
            if len(matched) >= 50 and correct / len(matched) >= .92:
                candidate = {'threshold': float(threshold), 'context_threshold': context_threshold,
                    'matched': len(matched), 'correct': correct, 'precision': correct / len(matched),
                    'recall': correct / positives if positives else 0}
                if best is None or (candidate['recall'], candidate['precision'], candidate['threshold']) > (best['recall'], best['precision'], best['threshold']):
                    best = candidate
    return best


def tfidf(train, validation, names, taxonomy_version, output):
    word = TfidfVectorizer(ngram_range=(1, 2), max_features=70000, sublinear_tf=True, min_df=2)
    char = TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), max_features=100000, sublinear_tf=True, min_df=2)
    x = hstack([word.fit_transform([r['text'] for r in train]), char.fit_transform([r['text'] for r in train])]).tocsr()
    v = hstack([word.transform([r['text'] for r in validation]), char.transform([r['text'] for r in validation])]).tocsr()
    models, scores = {}, np.zeros((len(validation), len(names)))
    for index, name in enumerate(names):
        indices, target = known(train, name)
        if len(set(target)) != 2:
            raise ValueError('Missing positive or negative training labels for ' + name)
        model = LogisticRegression(C=2, class_weight='balanced', max_iter=2000, solver='liblinear')
        model.fit(x[indices], target)
        models[name] = model
        scores[:, index] = model.predict_proba(v)[:, 1]
    complexity_indices = [i for i, r in enumerate(train) if isinstance(r.get('complexity'), int)]
    if not complexity_indices:
        raise ValueError('Technical complexity annotations missing')
    complexity = Ridge(alpha=3).fit(x[complexity_indices], [train[i]['complexity'] for i in complexity_indices])
    parameters = calibration(scores, validation, names)
    bundle = {'word': word, 'char': char, 'models': models, 'complexity': complexity,
              'label_names': names, 'taxonomy_version': taxonomy_version, 'calibration': parameters}
    joblib.dump(bundle, output)
    return calibrate(scores, names, parameters)


def minilm(train, validation, names, taxonomy_version, encoder_path, output, epochs, max_length):
    import torch
    from safetensors.torch import load_file, save_file
    from torch.utils.data import Dataset, DataLoader
    from transformers import AutoModel, AutoTokenizer
    from app.taxonomy.minilm import Classifier
    torch.set_num_threads(4)
    torch.manual_seed(20261001); random.seed(20261001); np.random.seed(20261001)
    tokenizer = AutoTokenizer.from_pretrained(encoder_path, local_files_only=True)
    encoder = AutoModel.from_pretrained(encoder_path, local_files_only=True)
    output.mkdir(parents=True, exist_ok=False)
    encoder.config.save_pretrained(output); tokenizer.save_pretrained(output / 'tokenizer')
    model = Classifier(encoder, len(names))
    class Data(Dataset):
        def __init__(self, rows): self.rows = rows
        def __len__(self): return len(self.rows)
        def __getitem__(self, index):
            r = self.rows[index]
            return (tokenizer(r['text'], max_length=max_length, truncation=True),
                    [float(r['labels'].get(n) == 'yes') for n in names],
                    [float(r['labels'].get(n) in {'yes', 'no'}) for n in names],
                    float(r['complexity']) if isinstance(r.get('complexity'), int) else -1.)
    def collate(items):
        encoded, targets, masks, complexity = zip(*items)
        return tokenizer.pad(encoded, return_tensors='pt'), torch.tensor(targets), torch.tensor(masks), torch.tensor(complexity)
    train_loader = DataLoader(Data(train), batch_size=8, shuffle=True, collate_fn=collate)
    validation_loader = DataLoader(Data(validation), batch_size=8, collate_fn=collate)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5, weight_decay=.01)
    best_f1, best, history = -1, None, []
    for epoch in range(1, epochs + 1):
        model.train()
        for inputs, targets, masks, complexity in train_loader:
            optimizer.zero_grad()
            logits, comp = model(inputs)
            loss = (torch.nn.functional.binary_cross_entropy_with_logits(logits, targets, reduction='none') * masks).sum() / masks.sum().clamp(min=1)
            valid = complexity >= 0
            if valid.any(): loss = loss + .1 * torch.nn.functional.mse_loss(comp[valid], complexity[valid])
            loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.); optimizer.step()
        model.eval(); raw = []
        with torch.inference_mode():
            for inputs, _, _, _ in validation_loader:
                logits, _ = model(inputs); raw.extend(torch.sigmoid(logits).tolist())
        raw = np.asarray(raw)
        report = score_report(raw, validation, names)
        topic_f1 = float(np.mean([report[t['id']].get('f1', 0) for t in TOPICS['topics']]))
        history.append({'epoch': epoch, 'validation_macro_f1': topic_f1})
        if topic_f1 > best_f1:
            best_f1, best = topic_f1, raw
            save_file(model.state_dict(), str(output / 'best.safetensors'))
            best_epoch = epoch
        print(json.dumps(history[-1]), flush=True)
    parameters = calibration(best, validation, names)
    training = {'taxonomy_version': taxonomy_version, 'names': names, 'max_length': max_length,
                'best_checkpoint': 'best.safetensors', 'best_epoch': best_epoch, 'history': history,
                'calibration': parameters, 'device': 'cpu', 'validation_selection': 'macro F1 of nine topics'}
    (output / 'training.json').write_text(json.dumps(training, indent=2), encoding='utf-8')
    return calibrate(best, names, parameters)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--encoder', type=Path, required=True)
    parser.add_argument('--epochs', type=int, default=4, choices=range(1, 11))
    parser.add_argument('--max-length', type=int, default=256, choices=[128, 256, 384, 512])
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(CHECKOUT):
        raise ValueError('Weights and private evaluation cannot be committed to Git')
    rows, dataset_manifest = validate_dataset(args.dataset)
    train = [r for r in rows if r['split'] == 'train']
    training_positive_groups = positive_group_counts(train).get('train', {})
    validation = deduplicate_groups([r for r in rows if r['split'] == 'validation'])
    taxonomy = json.loads((args.dataset / 'taxonomy.json').read_text(encoding='utf-8'))
    names = labels_from_taxonomy(taxonomy)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'taxonomy.json').write_text(json.dumps(taxonomy, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'dataset-manifest.json').write_text(json.dumps(dataset_manifest, indent=2), encoding='utf-8')
    validation_scores = {
        'tfidf': tfidf(train, validation, names, taxonomy['version'], args.output / 'baseline.joblib'),
        'minilm': minilm(train, validation, names, taxonomy['version'], args.encoder, args.output / 'minilm-v3', args.epochs, args.max_length)}
    models = {}
    for key, weights in [('tfidf', args.output / 'baseline.joblib'), ('minilm', args.output / 'minilm-v3/best.safetensors')]:
        auxiliary_files = [args.output / 'taxonomy.json']
        if key == 'minilm':
            auxiliary_files += [p for p in (args.output / 'minilm-v3').rglob('*') if p.is_file() and p != weights]
        auxiliary = {p.relative_to(args.output).as_posix(): file_sha256(p) for p in sorted(auxiliary_files)}
        checksum = file_sha256(weights)
        identity = hashlib.sha256(json.dumps({'weights': checksum, 'configuration': auxiliary}, sort_keys=True).encode()).hexdigest()[:12]
        models[key] = {'version': f'{taxonomy["version"]}-{key}-{identity}', 'weights_sha256': checksum,
                       'auxiliary_sha256': auxiliary, 'path': 'baseline.joblib' if key == 'tfidf' else 'minilm-v3'}
    model_manifest = {'models': models, 'dataset_sha256': dataset_manifest['dataset_sha256']}
    (args.output / 'model-manifest.json').write_text(json.dumps(model_manifest, indent=2), encoding='utf-8')
    chosen = {}
    for topic in TOPICS['topics']:
        candidates = [(key, choose_route(scores, validation, names, topic['id'])) for key, scores in validation_scores.items()]
        candidates = [(key, route) for key, route in candidates if route]
        if candidates:
            key, route = max(candidates, key=lambda item: (item[1]['recall'], item[1]['precision'], item[0] == 'tfidf'))
            chosen[topic['id']] = {'model_key': key, **route}
    # Никаких выборов по test ниже этой точки: эпохи, модель и пороги зафиксированы.
    (args.output / 'validation-routes.json').write_text(json.dumps(chosen, indent=2), encoding='utf-8')
    test = deduplicate_groups([r for r in rows if r['split'] == 'test'])
    from app.taxonomy.inference import TaxonomyModel
    from app.taxonomy.minilm import MiniLmTaxonomyModel
    report = {'agreement_with': 'Codex annotation, not independent human accuracy', 'routes': {}, 'models': {}}
    for key, model_class in [('tfidf', TaxonomyModel), ('minilm', MiniLmTaxonomyModel)]:
        model = model_class(args.output)
        scores, times = [], []
        for row in test:
            start = time.perf_counter(); result = model.classify(row['text']); times.append(time.perf_counter() - start)
            scores.append([result['scores'][name] for name in names])
        scores = np.asarray(scores)
        val_metrics = score_report(validation_scores[key], validation, names)
        thresholds = {name: values['threshold'] for name, values in val_metrics.items()}
        short = [i for i, r in enumerate(test) if r['short_caption']]
        allowed = [i for i, r in enumerate(test) if r['publication_allowed']]
        errors = []
        for column, name in enumerate(names):
            threshold = thresholds.get(name) or .5
            for i, row in enumerate(test):
                value = row['labels'].get(name)
                if value in {'yes', 'no'} and bool(scores[i, column] >= threshold) != (value == 'yes'):
                    errors.append({'sha': row['sha'], 'feature': name, 'label': value,
                        'score': float(scores[i, column]), 'threshold': float(threshold),
                        'short_caption': row['short_caption']})
        (args.output / f'{key}-test-errors.json').write_text(json.dumps(errors, indent=2), encoding='utf-8')
        report['models'][key] = {'validation': val_metrics, 'test': score_report(scores, test, names, thresholds),
            'short_caption_test': score_report(scores[short], [test[i] for i in short], names, thresholds),
            'publishable_source_test': score_report(scores[allowed], [test[i] for i in allowed], names, thresholds),
            'local_cpu_p95_seconds': float(np.percentile(times, 95)), 'version': model.model_version}
        for topic in TOPICS['topics']:
            route = chosen.get(topic['id'])
            if not route or route['model_key'] != key: continue
            ix, cx = names.index(topic['id']), names.index('caption_has_context')
            eligible = [i for i, r in enumerate(test) if r['publication_allowed'] and r['labels'].get(topic['id']) in {'yes', 'no'} and r['labels'].get('caption_has_context') in {'yes', 'no'}]
            matched = [i for i in eligible if scores[i, ix] >= route['threshold'] and scores[i, cx] >= route['context_threshold']]
            correct = sum(test[i]['labels'][topic['id']] == 'yes' and test[i]['labels']['caption_has_context'] == 'yes' for i in matched)
            passed = len(matched) >= 50 and correct / len(matched) >= .9
            report['routes'][topic['id']] = {**route, 'test_matched': len(matched), 'test_correct': correct,
                'enabled': passed, 'model_version': model.model_version,
                'train_positive': training_positive_groups.get(topic['id'], 0),
                'train_positive_rows': sum(r['labels'].get(topic['id']) == 'yes' for r in train),
                'test_sha256': hashlib.sha256(json.dumps([r['sha'] for r in test], sort_keys=True).encode()).hexdigest(),
                'split_sha256': dataset_manifest['dataset_sha256']}
        del model
    report['all_routes_ready'] = len(report['routes']) == 9 and all(r['enabled'] for r in report['routes'].values())
    (args.output / 'evaluation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'all_routes_ready': report['all_routes_ready']}))


if __name__ == '__main__':
    main()
