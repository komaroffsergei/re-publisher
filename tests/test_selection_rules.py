from types import SimpleNamespace

import pytest

from app.content.selection_rules import evaluate, matching_conditions, taxonomy_catalog, validate_expression
from app.content.selection_filters import assessment, needs_backfill
from app.taxonomy.jobs import text_sha256
from app.web.selection_routes import FilterInput, digest


def leaf(label="tool_description", compare="gte", threshold=60):
    return {"op": "condition", "label_id": label, "compare": compare, "threshold": threshold}


@pytest.mark.parametrize("compare,score,expected", [("gte", .6, True), ("gt", .6, False),
    ("lte", .6, True), ("lt", .6, False), ("gt", .601, True), ("lt", .599, True)])
def test_exact_threshold(compare, score, expected):
    assert evaluate(leaf(compare=compare), {"tool_description": score})[0] is expected


def test_nested_and_or_not():
    tree = {"op": "and", "children": [leaf(), {"op": "or", "children": [
        leaf("software_engineering"), {"op": "not", "children": [leaf("society")]}]}]}
    assert validate_expression(tree) == tree
    assert evaluate(tree, {"tool_description": .8, "software_engineering": .2, "society": .1})[0] is True
    assert evaluate(tree, {"tool_description": .5, "software_engineering": .9})[0] is False


def test_matching_conditions_excludes_failed_or_branch_and_keeps_negation():
    tree = {"op": "and", "children": [leaf(), {"op": "or", "children": [
        leaf("software_engineering"), {"op": "not", "children": [leaf("society")]}]}]}
    _, trace = evaluate(tree, {"tool_description": .8, "software_engineering": .2, "society": .1})
    proof = matching_conditions(trace)
    assert [(item["label_id"], item["score"], item["negated"]) for item in proof] == [
        ("tool_description", .8, False), ("society", .1, True)]


@pytest.mark.parametrize("op,expected", [("and", ["society"]), ("or", ["tool_description", "society"])])
def test_negated_groups_show_only_conditions_explaining_the_result(op, expected):
    tree = {"op": "not", "children": [{"op": op, "children": [leaf(), leaf("society")]}]}
    scores = {"tool_description": .8 if op == "and" else .2, "society": .1}
    proof = matching_conditions(evaluate(tree, scores)[1])
    assert [item["label_id"] for item in proof] == expected
    assert all(item["negated"] for item in proof)


def test_unknown_rejection_and_double_negation_in_match_proof():
    assert matching_conditions(evaluate(leaf(), {})[1]) == []
    assert matching_conditions(evaluate(leaf(), {"tool_description": .1})[1]) == []
    tree = {"op": "not", "children": [{"op": "not", "children": [leaf()]}]}
    proof = matching_conditions(evaluate(tree, {"tool_description": .8})[1])
    assert len(proof) == 1 and proof[0]["negated"] is False


@pytest.mark.parametrize("score", [None, float("nan"), -1, 1.1, True, "0.7"])
def test_missing_or_invalid_score_never_passes_negation(score):
    scores = {} if score is None else {"tool_description": score}
    assert evaluate({"op": "not", "children": [leaf()]}, scores)[0] is None


@pytest.mark.parametrize("tree", [leaf("unknown"), leaf(threshold=-1), leaf(threshold=101),
    leaf(threshold=float("nan")), leaf(compare="eval"), {"op": "and", "children": []},
    {"op": "not", "children": [leaf(), leaf()]}, {"op": "sql", "children": [leaf()]}])
def test_invalid_rules_rejected(tree):
    with pytest.raises(ValueError):
        validate_expression(tree)


def test_limits_and_subcategory():
    label = next(label["id"] for label in taxonomy_catalog()["labels"] if label["parent"])
    assert evaluate(validate_expression(leaf(label)), {label: .75})[0] is True
    with pytest.raises(ValueError):
        validate_expression({"op": "or", "children": [leaf()] * 101})
    deep = leaf()
    for _ in range(8):
        deep = {"op": "not", "children": [deep]}
    with pytest.raises(ValueError):
        validate_expression(deep)


def test_legacy_top_three_is_unknown_and_requires_backfill():
    post = SimpleNamespace(text="Text", is_deleted=False)
    job = SimpleNamespace(status="complete", current_run_id=1, text_sha256=text_sha256(post.text),
                          result={"top_3": [{"id": "tool_description", "score": .99}]})
    version = SimpleNamespace(expression={"op": "not", "children": [leaf()]})
    assert assessment(version, job, post)["outcome"] == "unknown"
    assert needs_backfill(job, post)
    job.result = {"taxonomy_version": taxonomy_catalog()["version"], "scores": {"tool_description": .8}}
    assert not needs_backfill(job, post)
    assert assessment(version, job, post)["outcome"] == "rejected"
    post.text = "Changed"
    assert assessment(version, job, post)["outcome"] == "unknown"


def test_preview_is_bound_to_draft():
    draft = FilterInput(name="Rule", mark_id=1, expression=leaf())
    token = digest(draft)
    draft.preview_digest = token
    assert digest(draft) == token
    draft.model_key = "minilm"
    assert digest(draft) != token


def test_absent_stale_or_failed_selected_model_needs_backfill():
    post = SimpleNamespace(text="Text", is_deleted=False)
    assert needs_backfill(None, post)
    job = SimpleNamespace(status="failed", text_sha256=text_sha256(post.text), result=None)
    assert needs_backfill(job, post)
    job.status = "running"
    assert not needs_backfill(job, post)
    post.text = "Edited"
    assert needs_backfill(job, post)
    post.text = ""
    assert not needs_backfill(None, post)
    post.text = "Text"; post.is_deleted = True
    assert not needs_backfill(None, post)


def test_dictionary_label_has_no_model_score():
    post = SimpleNamespace(text="Text", is_deleted=False)
    job = SimpleNamespace(status="complete", current_run_id=1, text_sha256=text_sha256(post.text),
        result={"taxonomy_version": taxonomy_catalog()["version"], "scores": {"tool_description": .8, "society": .42}})
    version = SimpleNamespace(expression=leaf(), assigned_label_id="society")
    result = assessment(version, job, post)
    assert result["outcome"] == "matched" and "assigned" not in result["trace"]
    del job.result["scores"]["society"]
    assert assessment(version, job, post)["outcome"] == "matched"
    del job.result["scores"]["tool_description"]
    assert assessment(version, job, post)["outcome"] == "unknown"


def test_catalog_contains_all_model_scores():
    assert len(taxonomy_catalog()["labels"]) == 47
    assert evaluate(validate_expression(leaf("is_ad")), {"is_ad": .8})[0] is True
    for name in ('is_ai_educational', 'is_ai_beginner_material', 'is_ml_research',
                 'is_ai_access_pricing', 'is_ai_workflow', 'is_ai_visual_media',
                 'is_agentic_coding', 'is_ai_tool_review', 'caption_has_context'):
        assert evaluate(validate_expression(leaf(name)), {name: .8})[0] is True
