import numpy as np
import pytest
from scripts.train_humor_models import route,known,metrics


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
