"""Headless dashboard smoke test (issue #14 recurrence prevention).

Runs dashboard/app.py via streamlit's AppTest harness: the app must execute
without raising, and the 5-tab bar must be present. Skipped when streamlit
isn't importable (e.g. minimal CI images); the CI workflow installs it.
"""
import pytest
from pathlib import Path

streamlit = pytest.importorskip("streamlit")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

# AppTest.from_file resolves relative paths against the calling test file
# (tests/), not the repo root, so resolve dashboard/app.py absolutely.
APP_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"

EXPECTED_TABS = ["RFQs", "Pricing & quoting", "Performance",
                 "Engine status", "NFL correlation"]


def test_dashboard_renders_without_exceptions():
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    assert not at.exception, f"dashboard raised: {at.exception!r}"


def test_dashboard_has_five_tabs():
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    assert not at.exception, f"dashboard raised: {at.exception!r}"
    assert len(at.tabs) == 5, f"expected 5 tabs, got {len(at.tabs)}"
    labels = [t.label for t in at.tabs]
    assert labels == EXPECTED_TABS, f"tab labels: {labels}"


def test_live_pricing_is_wired_to_the_repo_params():
    """The dashboard builds a real NFL pricer from params/ for the live feed."""
    import dashboard.app as app
    from combo_mm.combo_markets import ComboMarketCatalog
    from combo_mm.config import PipelineConfig
    from combo_mm.quote_selections import QuoteSelectionStore

    quoter, note = app._live_quoter(PipelineConfig(paper_mode=True), ComboMarketCatalog(),
                                    QuoteSelectionStore())
    try:
        assert quoter is not None, note
        assert "nfl_" in note and ".json@" in note      # names the exact params file
        assert quoter.pricer.model_config.method == "market_lift"
    finally:
        if quoter is not None:
            quoter.stop()


def test_live_pricing_says_why_it_is_off_when_params_are_missing(tmp_path, monkeypatch):
    import dashboard.app as app
    from combo_mm.combo_markets import ComboMarketCatalog
    from combo_mm.config import PipelineConfig
    from combo_mm.quote_selections import QuoteSelectionStore

    monkeypatch.setattr(app, "REPO", tmp_path)
    quoter, note = app._live_quoter(PipelineConfig(paper_mode=True), ComboMarketCatalog(),
                                    QuoteSelectionStore())
    assert quoter is None and "refresh_params" in note


def test_model_quote_rows_render_the_columns_a_trader_reads():
    import dashboard.app as app
    from combo_mm.quote_selections import QuoteSelectionStore

    store = QuoteSelectionStore()
    store.record_priced_quote({
        "rfq_id": "R1", "priced_at": "2026-09-17T12:00:00Z", "status": "QUOTED",
        "reason_code": "QUOTED_OK", "side": "YES", "direction": "BUY", "size": "25",
        "size_unit": "shares", "fair": 0.528, "naive_yes": 0.368, "bid": 0.48, "ask": 0.576,
        "response_action": "SELL", "response_price": 0.576, "corr_adjustment_bps": 1602.0,
        "confidence": 0.89, "legs_label": "Bills ML | Bills -4.5"}, "auto")
    df = app._model_quote_rows(store.list_priced_quotes())
    assert list(df["we would"]) == ["SELL @ 0.576"]
    assert set(app._PRICE_COLS) <= set(df.columns)


def test_live_pricing_view_renders_a_real_quote():
    """The live pricing table, calibration block and spread components all execute."""
    probe = Path(__file__).resolve().parent / "dashboard_pricing_probe.py"
    at = AppTest.from_file(str(probe))
    at.run()
    assert not at.exception, f"live pricing view raised: {at.exception!r}"
    text = " ".join(str(m.value) for m in at.markdown)
    assert "lift over independence" in text            # per-game model detail rendered
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["RFQs priced"] == "2" and metrics["We would quote"] == "1"
