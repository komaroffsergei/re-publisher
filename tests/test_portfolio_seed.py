from app.portfolio_seed import DEMO_MATERIALS, build_demo_projection


def test_portfolio_materials_cover_required_states_and_are_unique():
    keys = [row["key"] for row in DEMO_MATERIALS]
    assert len(keys) == 12
    assert len(set(keys)) == len(keys)
    assert {row["status"] for row in DEMO_MATERIALS} >= {
        "received",
        "processed",
        "classified",
        "rewrite_pending",
        "needs_review",
        "ready_for_publication",
        "blocked",
        "rewrite_failed",
    }
    assert all(row.get("publication_allowed", True) is False for row in DEMO_MATERIALS if row["status"] == "blocked")


def test_portfolio_projection_uses_real_local_pipeline_functions():
    for index, material in enumerate(DEMO_MATERIALS, start=1):
        result = build_demo_projection(material, index)
        assert result["processed"]["post_id"] == index
        assert result["processed"]["word_count"] > 10
        assert result["extracted"]["title"] == material["title"]
        assert result["extracted"]["extracted_text"]
        assert result["classification"]["label"] in {"tool", "news", "education"}
        assert 0 <= result["classification"]["confidence"] <= 1
        assert result["draft"]["title"] == material["title"]
        assert result["draft"]["body"]
        assert result["source_url"].startswith("https://portfolio-demo.invalid/")


def test_portfolio_seed_has_no_separate_hidden_folder():
    import inspect
    from app.portfolio_seed import seed_portfolio_demo

    source = inspect.getsource(seed_portfolio_demo)
    assert '"folder_name": settings.folder_name' in source
    assert "PORTFOLIO_DEMO" not in source
