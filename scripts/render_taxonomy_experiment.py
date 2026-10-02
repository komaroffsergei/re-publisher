"""Render aggregate private evaluation as HTML/CSV; never publish raw posts."""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path


def calibration_svg(bins):
    points = []
    dots = []
    for item in bins:
        x = 20 + 120 * item["mean_score"]
        y = 140 - 120 * item["observed"]
        points.append(f"{x:.1f},{y:.1f}")
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#f97316"><title>n={item["n"]}</title></circle>')
    return ('<svg viewBox="0 0 160 160" role="img" aria-label="Калибровка: '
            'по горизонтали оценка модели, по вертикали фактическая доля меток">'
            '<path d="M20 140 L140 20 M20 20 L20 140 L140 140" fill="none" stroke="#9ca3af"/>'
            f'<polyline points="{" ".join(points)}" fill="none" stroke="#7c3aed" stroke-width="2"/>'
            f'{"".join(dots)}</svg>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("private report must stay outside Git")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    names = [category["id"] for category in taxonomy["categories"]]
    names += [child["id"] for category in taxonomy["categories"] for child in category["subcategories"]]
    names += taxonomy["binary_features"]
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "label-metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "test_positive", "test_negative", "baseline_f1", "minilm_f1",
                         "baseline_precision", "baseline_recall", "minilm_precision", "minilm_recall",
                         "baseline_brier", "minilm_brier", "baseline_ece10", "minilm_ece10"])
        for name in names:
            baseline = report["baseline"]["label_metrics"][name]
            minilm = report["minilm_epoch_2"]["label_metrics"][name]
            writer.writerow([name, baseline["positive"], baseline["negative"], baseline["f1"], minilm["f1"],
                             baseline["precision"], baseline["recall"], minilm["precision"], minilm["recall"],
                             baseline["brier"], minilm["brier"], baseline["ece_10"], minilm["ece_10"]])
    rows = []
    for name in names:
        left = report["baseline"]["label_metrics"][name]
        right = report["minilm_epoch_2"]["label_metrics"][name]
        rows.append(f'<tr><td><code>{html.escape(name)}</code></td><td>{left["positive"]}</td>'
                    f'<td>{left["f1"]:.2f}</td><td>{right["f1"]:.2f}</td>'
                    f'<td>{left["brier"]:.3f}</td><td>{right["brier"]:.3f}</td></tr>')
    charts = []
    for name in [category["id"] for category in taxonomy["categories"]] + taxonomy["binary_features"]:
        for key, title in (("baseline", "TF-IDF"), ("minilm_epoch_2", "MiniLM")):
            metric = report[key]["label_metrics"][name]
            charts.append('<article class="chart"><h3>' + html.escape(name) + ' · ' + title + '</h3>'
                          + calibration_svg(metric["calibration_bins"])
                          + f'<small>n={metric["positive"] + metric["negative"]}, positive={metric["positive"]}, ECE={metric["ece_10"]:.3f}</small></article>')
    timing = report["minilm_cpu_inference"]
    memory = timing["working_set_after_inference_bytes"]
    page = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAX taxonomy — проверка классификатора</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#10121d;color:#f5f1e9;margin:0 auto;max-width:1200px;padding:28px}}h1,h2{{color:#fb923c}}p{{max-width:85ch}}.note{{background:#262236;border-left:5px solid #f97316;padding:16px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border-bottom:1px solid #3f3a4b;padding:7px;text-align:left}}th{{position:sticky;top:0;background:#272234}}.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.chart{{background:#262236;padding:10px}}.chart h3{{font-size:13px;min-height:40px}}svg{{width:100%;height:160px}}small{{display:block}}code{{color:#c4b5fd}}</style></head><body>
<h1>Классификация постов MAX: итог эксперимента</h1><p>Таксономия {html.escape(report["taxonomy_version"])}. Разметка Codex: 3 000 текстов. Отложенная часть: 450, из них пригодны для оценки {report["test_accepted"]}; {report["test_needs_review"]} не имеют достаточно ясного текстового контекста.</p>
<p class="note"><strong>Релиз остановлен.</strong> MiniLM хуже простого TF-IDF и нарушает согласованность категорий. Это измерение согласия с метками Codex, а не независимая точность. Действующий publisher не менялся.</p>
<h2>Общий результат</h2><table><thead><tr><th>Показатель</th><th>TF-IDF</th><th>MiniLM</th></tr></thead><tbody>
<tr><td>Micro-F1</td><td>{report["baseline"]["micro_f1"]:.3f}</td><td>{report["minilm_epoch_2"]["micro_f1"]:.3f}</td></tr>
<tr><td>Macro-F1</td><td>{report["baseline"]["macro_f1"]:.3f}</td><td>{report["minilm_epoch_2"]["macro_f1"]:.3f}</td></tr>
<tr><td>Top-3: средняя полнота тем</td><td>{report["baseline"]["top3_mean_recall"]:.3f}</td><td>{report["minilm_epoch_2"]["top3_mean_recall"]:.3f}</td></tr>
<tr><td>Противоречия иерархии/событий</td><td>{report["baseline"]["parent_child_and_event_contradictions"]}</td><td>{report["minilm_epoch_2"]["parent_child_and_event_contradictions"]}</td></tr>
<tr><td>MAE сложности 0–5</td><td>{report["baseline"]["technical_complexity"]["mae"]:.3f}</td><td>{report["minilm_epoch_2"]["technical_complexity"]["mae"]:.3f}</td></tr></tbody></table>
<p>MiniLM на локальном CPU: 4 потока, p50 {timing["p50_seconds"]*1000:.1f} мс, p95 {timing["p95_seconds"]*1000:.1f} мс; рабочий набор после inference {memory/1024**3:.2f} GiB. Это локальный замер, не тест на VPS.</p>
<h2>Все метки</h2><table><thead><tr><th>ID</th><th>Положительных</th><th>F1 TF-IDF</th><th>F1 MiniLM</th><th>Brier TF-IDF</th><th>Brier MiniLM</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Калибровка по широким категориям и обязательным признакам</h2><p>Диагональ показывает идеальную калибровку. Каждая точка — диапазон оценок с количеством примеров во всплывающей подсказке. Малые классы не дают надёжного вывода.</p><div class="charts">{''.join(charts)}</div>
<p>Подробные значения всех 38 меток: <a href="label-metrics.csv">CSV</a>. Исходный JSON содержит также ошибочные решения и биновые калибровочные данные; хранится только в защищённом каталоге.</p></body></html>'''
    (args.output / "index.html").write_text(page, encoding="utf-8")
    print(json.dumps({"html": str(args.output / "index.html"), "csv": str(args.output / "label-metrics.csv")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
