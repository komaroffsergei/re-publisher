import numpy as np
import pytest
from scripts.train_humor_models import route, route_evidence, strip_source_marks, diagnostic_slices, known, metrics


def rows(values):
    return [{"labels":{"is_joke":j,"input_has_context":c}} for j,c in values]


def test_unknown_is_not_a_negative_training_target():
    ix,target=known(rows([("да","да"),("неясно","нет"),("нет","да")]),"is_joke")
    assert ix.tolist()==[0,2] and target.tolist()==[1,0]


def test_route_needs_50_matches_and_both_scores():
    data=rows([("да","да")]*60+[("нет","да")]*60+[("да","нет")]*10)
    scores=np.array([[.98,.99]]*60+[[.2,.99]]*60+[[.98,.1]]*10)
    chosen=route(scores,data)
    assert chosen["matched"]==60 and chosen["precision"]==1
    assert route(scores[:49],data[:49]) is None
    assert route(np.ones_like(scores),data) is None


def test_calibration_report_keeps_model_score_distinct_from_actual_label():
    result=metrics(np.array([[.9,.9],[.1,.9]]),rows([("да","да"),("нет","да")]))
    assert result["is_joke"]["precision"]==1
    assert result["is_joke"]["brier"]==pytest.approx(.01)
    assert sum(b["count"] for b in result["is_joke"]["calibration_bins"])==2


def row(joke="да", context="да", caption="текст", peer=1):
    return {"labels":{"is_joke":joke,"input_has_context":context},"caption":caption,
            "caption_length":len(caption),"sources":[{"peer":peer}]}


def test_route_does_not_hide_missing_context_or_uncertain_matches():
    rows=[row(),row("неясно","нет"),row("неясно","да"),row("нет","да")]
    result=route_evidence(np.ones((4,2)),rows,.9,.9)
    assert result["matched"]==4 and result["correct"]==1
    assert result["confirmed_wrong"]==2 and result["unresolved"]==1
    assert result["precision"]==.25
    # Gate cannot declare 100% by omitting uncertain/contextless inputs.
    assert route(np.ones((65,2)),[row() for _ in range(50)]+[row("неясно","нет") for _ in range(15)]) is None


def test_validation_requires_fifty_matches_and_ninety_two_percent_lower_bound():
    assert route(np.ones((49,2)),[row() for _ in range(49)]) is None
    rows=[row() for _ in range(46)]+[row("неясно","да") for _ in range(4)]
    result=route(np.ones((50,2)),rows)
    assert result["matched"]==50 and result["precision"]==.92
    assert result["unresolved"]==4


def test_brand_ablation_preserves_real_content_and_original():
    text="[Подпись]\n🖥️ IT Memes\n@ai_newz\n[Медиа 1]\nЯ работаю с IT Memes в пятницу\nШутка"
    result=strip_source_marks(text)
    assert "🖥️ IT Memes" not in result and "@ai_newz" not in result
    assert "Я работаю с IT Memes в пятницу" in result
    assert "[Медиа 1]" in result and text.startswith("[Подпись]\n🖥️ IT Memes")


def test_diagnostics_separate_empty_short_long_captions_and_sources():
    rows=[row(caption=""),row(caption="короткий"),row(caption="я"*501,peer=2)]
    result=diagnostic_slices(np.ones((3,2)),rows)
    assert result["caption:empty"]["rows"]==1
    assert result["caption:short"]["rows"]==1 and result["caption:long"]["rows"]==1
    assert result["source:1"]["rows"]==2 and result["source:2"]["rows"]==1
