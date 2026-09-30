"""Конструктор использует дерево данных, а не SQL, Python или eval от посетителя."""
from __future__ import annotations

import json
import math
from pathlib import Path
from functools import lru_cache
from app.taxonomy.labels import FEATURE_NAMES


@lru_cache(maxsize=1)
def taxonomy_catalog() -> dict:
    taxonomy = json.loads((Path(__file__).resolve().parents[2] / "config/max_taxonomy.json").read_text(encoding="utf-8"))
    labels = [{"id": category["id"], "name": category["name"], "parent": None}
              for category in taxonomy["categories"]]
    labels += [{"id": child["id"], "name": child["name"], "parent": category["id"]}
               for category in taxonomy["categories"] for child in category["subcategories"]]
    labels += [{"id": key, "name": FEATURE_NAMES[key], "parent": None, "kind": "feature"}
               for key in taxonomy["binary_features"]]
    return {"version": taxonomy["version"], "labels": labels}


def label_name(label_id):
    labels = {label["id"]: label for label in taxonomy_catalog()["labels"]}
    item = labels[label_id]
    return f"{labels[item['parent']]['name']} / {item['name']}" if item["parent"] else item["name"]


def validate_expression(expression: dict) -> dict:
    labels = {label["id"] for label in taxonomy_catalog()["labels"]}
    count = 0

    def visit(node, depth=0):
        nonlocal count
        count += 1
        if count > 100 or depth > 6 or not isinstance(node, dict):
            raise ValueError("Не более 100 условий и 6 уровней вложенности")
        op = node.get("op")
        if op == "condition":
            value = node.get("threshold")
            if node.get("label_id") not in labels or node.get("compare") not in {"gte", "gt", "lte", "lt"}:
                raise ValueError("Неизвестная категория или сравнение")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError("Порог должен быть от 0 до 100%")
            return {"op": op, "label_id": node["label_id"], "compare": node["compare"], "threshold": value}
        children = node.get("children")
        if op not in {"and", "or", "not"} or not isinstance(children, list) or not children:
            raise ValueError("Добавьте условия в группу И / ИЛИ / НЕ")
        if op == "not" and len(children) != 1:
            raise ValueError("У НЕ должно быть ровно одно условие или группа")
        return {"op": op, "children": [visit(child, depth + 1) for child in children]}

    return visit(expression)


def evaluate(expression: dict, scores: dict[str, float]) -> tuple[bool | None, dict]:
    """Трёхзначная логика: НЕ неизвестного остаётся неизвестным."""
    op = expression["op"]
    if op == "condition":
        score = scores.get(expression["label_id"])
        valid = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) and 0 <= score <= 1
        threshold = expression["threshold"] / 100
        result = None
        if valid:
            result = {"gte": score >= threshold, "gt": score > threshold,
                      "lte": score <= threshold, "lt": score < threshold}[expression["compare"]]
        return result, {**expression, "score": score if valid else None, "result": result}
    evaluated = [evaluate(child, scores) for child in expression["children"]]
    values = [value for value, _trace in evaluated]
    if op == "not":
        result = None if values[0] is None else not values[0]
    elif op == "and":
        result = False if False in values else (None if None in values else True)
    else:
        result = True if True in values else (None if None in values else False)
    return result, {"op": op, "result": result, "children": [trace for _value, trace in evaluated]}


def matching_conditions(trace: dict) -> list[dict]:
    """Только условия, объясняющие совпадение; ложные ветки ИЛИ не показываем.

    Под НЕ доказательством может быть несоблюдённый порог. Отсутствие оценки
    остаётся неизвестным и никогда не становится основанием совпадения.
    """
    def visit(node, expected):
        if node.get("result") is not expected:
            return []
        if node["op"] == "condition":
            return [{**node, "negated": not expected}]
        if node["op"] == "not":
            return visit(node["children"][0], not expected)
        return [condition for child in node["children"]
                for condition in visit(child, expected)]

    return visit(trace, True)
