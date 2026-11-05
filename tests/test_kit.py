"""The authoring kit: STRATEGY_API.md is generated from the code and cannot drift."""

from helpers import ROOT

from backtester.context import Context
from backtester.kit import build_api_doc, kit_files
from backtester.validation import ALLOWED_IMPORTS, STAGE_NAMES


def test_committed_kit_matches_the_code():
    """If this fails, the API changed: run `backtest kit` and commit the result."""
    for name, text in kit_files().items():
        committed = (ROOT / "kit" / name).read_text(encoding="utf-8")
        assert committed == text, f"kit/{name} is out of date; run `backtest kit`"


def test_every_public_ctx_member_is_documented():
    doc = build_api_doc()
    public = [n for n in Context.__dict__ if not n.startswith("_")]
    assert public
    for name in public:
        assert f"### `ctx.{name}" in doc, name


def test_rules_and_stages_come_from_the_validator():
    doc = build_api_doc()
    for module in ALLOWED_IMPORTS:
        assert f"`{module}`" in doc
    for stage in STAGE_NAMES:
        assert f"| {stage} |" in doc


def test_doc_includes_template_fixture_helpers_and_reference_strategies():
    doc = build_api_doc()
    assert "class MyStrategy(Strategy):" in doc
    assert "### `make_fixture(" in doc and "### `run_on_fixture(" in doc
    assert "class MomentumRotation(Strategy):" in doc
    assert "def test_holds_the_leader_then_rotates_when_leadership_changes" in doc
