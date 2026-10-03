from types import SimpleNamespace
import pytest
from app.content.selection_rules import validate_expression, evaluate, required_sources, matching_conditions
from app.content.selection_filters import assessment
from app.taxonomy.profiles import job_key
from app.taxonomy.jobs import text_sha256
from app.ocr.engine import compose_ocr, input_digest
from app.web.selection_routes import FilterInput


def score(source="text", threshold=60):
    return {"op":"condition", "input_source":source, "label_id":"is_joke", "compare":"gte", "threshold":threshold}


@pytest.mark.parametrize("size", [0,499,500,501])
@pytest.mark.parametrize("op", ["gte","gt","lte","lt","eq"])
def test_length_boundaries(size, op):
    tree=validate_expression({"op":"length","compare":op,"threshold":500})
    expected={"gte":size>=500,"gt":size>500,"lte":size<=500,"lt":size<500,"eq":size==500}[op]
    assert evaluate(tree, {}, size)[0] is expected


@pytest.mark.parametrize("value", [-1, True, 1.2, "500"])
def test_invalid_length(value):
    with pytest.raises(ValueError): validate_expression({"op":"length","compare":"lte","threshold":value})


def test_sources_are_independent_and_unknown_is_not_zero():
    tree={"op":"and","children":[score("text"),{"op":"not","children":[score("ocr")]}]}
    assert evaluate(tree,{"text":{"is_joke":.9},"ocr":{"is_joke":.1}})[0] is True
    assert evaluate(tree,{"text":{"is_joke":.9}})[0] is None
    _,proof=evaluate({"op":"and","children":[score("ocr"),{"op":"length","compare":"lte","threshold":500}]}, {"ocr":{"is_joke":.9}},124)
    assert [x["op"] for x in matching_conditions(proof)] == ["condition","length"]


def test_length_rejects_without_model_and_ocr_checkbox_is_required():
    tree={"op":"and","children":[score("ocr"),{"op":"length","compare":"lte","threshold":500}]}
    assert evaluate(tree,{},501)[0] is False
    with pytest.raises(ValueError): FilterInput(name="OCR",mark_id=1,expression=tree)
    draft=FilterInput(name="OCR",mark_id=1,requires_ocr=True,expression=tree)
    assert required_sources(draft) == {"ocr"}


def test_caption_length_unicode_and_distinct_job_keys():
    text="  Ёж🙂\n А  "
    assert len(text.strip()) == 6
    assert job_key("tfidf") != job_key("tfidf","taxonomy","ocr")
    assert job_key("tfidf","humor_ocr") == "humor_ocr:tfidf"


def test_media_input_contains_no_caption_and_has_its_own_digest():
    items=[{"blocks":[{"text":"OCR text","score":.99}],"media_sha256":"x"}]
    assert compose_ocr(items) == "OCR text"
    assert input_digest(None,items) != input_digest("caption",items)


def test_both_run_ids_are_kept_in_match_proof():
    post=SimpleNamespace(text="caption",is_deleted=False)
    from app.content.selection_rules import taxonomy_catalog
    def job(id,source,value):
        return SimpleNamespace(profile="taxonomy", input_source=source, current_run_id=id,status="complete",text_sha256=text_sha256(post.text),result={"taxonomy_version":taxonomy_catalog()["version"],"scores":{"is_joke":value}})
    version=SimpleNamespace(profile="taxonomy",requires_ocr=True,expression={"op":"and","children":[score("text"),score("ocr")]})
    result=assessment(version,{"text":job(1,"text",.8),"ocr":job(2,"ocr",.9)},post)
    assert result["outcome"] == "matched" and result["run_ids"] == {"text":1,"ocr":2}
