"""Dead-man's checker for the correlation model actually affecting live prices."""
import importlib.util
from pathlib import Path

from combo_mm.quote_selections import QuoteSelectionStore

REPO = Path(__file__).resolve().parents[1]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_correlation_lift", REPO / "scripts" / "check_correlation_lift.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quoted(quotes: QuoteSelectionStore, rfq_id: str, corr_bps: float) -> None:
    quotes.record_priced_quote({
        "rfq_id": rfq_id, "priced_at": "2026-09-17T12:00:00Z", "status": "QUOTED",
        "reason_code": "QUOTED_OK", "response_action": "SELL", "response_price": .40,
        "size": 10, "size_unit": "shares", "fair": .40, "naive": .40,
        "corr_adjustment_bps": corr_bps, "side": "YES"}, "auto")


def test_checker_missing_db_is_no_data(tmp_path):
    checker = _load_checker()
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 2


def test_checker_no_quotes_yet_is_no_data(tmp_path):
    checker = _load_checker()
    quotes = QuoteSelectionStore(tmp_path / "rfq_capture.db")
    quotes.close()
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 2


def test_checker_flags_degenerate_correlation(tmp_path):
    checker = _load_checker()
    quotes = QuoteSelectionStore(tmp_path / "rfq_capture.db")
    for i in range(10):
        _quoted(quotes, f"rfq-{i}", corr_bps=0.02)
    quotes.close()
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 2


def test_checker_passes_when_model_moves_prices(tmp_path):
    checker = _load_checker()
    quotes = QuoteSelectionStore(tmp_path / "rfq_capture.db")
    for i in range(10):
        _quoted(quotes, f"rfq-{i}", corr_bps=50.0 + i)
    quotes.close()
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 0


def test_checker_honors_custom_thresholds(tmp_path):
    checker = _load_checker()
    quotes = QuoteSelectionStore(tmp_path / "rfq_capture.db")
    for i in range(10):
        _quoted(quotes, f"rfq-{i}", corr_bps=2.0)
    quotes.close()
    # 2 bps clears the default 1 bps threshold...
    assert checker.main(["--data-dir", str(tmp_path), "--quiet"]) == 0
    # ...but not a stricter one.
    assert checker.main(["--data-dir", str(tmp_path), "--quiet",
                         "--degenerate-bps", "10"]) == 2


def test_checker_output_has_no_secrets(tmp_path, capsys):
    checker = _load_checker()
    quotes = QuoteSelectionStore(tmp_path / "rfq_capture.db")
    _quoted(quotes, "rfq-0", corr_bps=50.0)
    quotes.close()
    assert checker.main(["--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "POLYMARKET" not in out
