"""paper_backtest: metrics, realized vs expected P&L, no-future-info."""
from combo_mm import PipelineConfig, fixtures, paper_backtest
from combo_mm.paper_backtest import combo_settlement_value, run_backtest


def _run(**kw):
    session, combos = fixtures.build_session()
    return run_backtest(session, combos, fixtures.build_drop_copy_feed(),
                        PipelineConfig(), **kw)


def test_backtest_counts():
    result, _ = _run()
    assert result.rfqs_received == 11
    assert result.rfqs_quoted + result.rfqs_rejected == result.rfqs_received
    assert result.rfqs_quoted == 10
    # RFQ-001 closed at t=700; its stream-visible update at t=800 must NOT
    # be re-quoted (quoting a closed RFQ would be a live-fire bug).
    rfq1 = next(pr for pr in result.per_rfq if pr["rfq_id"] == "RFQ-001")
    assert rfq1["reason_code"] == "RFQ_CLOSED"
    assert result.rfqs_expired == 1   # RFQ-003
    assert result.rfqs_executed == 1  # RFQ-001
    assert abs(result.quote_rate - 10 / 11) < 1e-9
    # execution_rate = executed / quoted (RFQ-001 executed, 10 RFQs quoted)
    assert abs(result.execution_rate - 1 / 10) < 1e-9


def test_expected_and_realized_pnl():
    result, _ = _run()
    assert result.expected_pnl > 0  # sum of quoted half-spread edge
    # RFQ-001: our fill is SELL 100 @ 0.57 (taker bought against our ask),
    # combo settles 1.0 * (1 - 0.0) = 1.0
    assert abs(result.realized_pnl - (0.57 - 1.0) * 100) < 1e-9
    assert result.n_fills == 1


def test_swings_and_curves():
    result, _ = _run()
    assert result.max_downswing >= 0.0
    assert result.max_upswing >= 0.0
    # single losing fill: monotonic fall, no upswing
    assert result.max_downswing == abs(result.realized_pnl)
    assert result.max_upswing == 0.0
    assert len(result.equity_curve) == 1
    assert len(result.exposure_curve) == 1
    assert result.exposure_curve[0][1] == 100 * 0.57  # |net| * fill price
    assert len(result.per_rfq) == 11


def test_combo_settlement_value():
    legs = [
        {"symbol": "A", "side": "YES", "settlement_price": "1.0"},
        {"symbol": "B", "side": "NO", "settlement_price": "0.0"},
    ]
    assert combo_settlement_value(legs) == 1.0
    legs[0]["settlement_price"] = None
    assert combo_settlement_value(legs) is None


def test_no_future_information():
    """A book snapshot arriving AFTER the RFQ must not inform its quote:
    the quoter sees a missing leg and declines MISSING_LEG."""
    from combo_mm.pricing import MISSING_LEG

    session = [
        {"t": 0, "kind": "event", "stream": True, "raw": {
            "event_id": "e1", "event_type": "rfq_created", "rfq_id": "RX",
            "symbol": "POTUS-2028", "exchange_ts": fixtures._ts(100),
            "payload": {"qtyDecimal": "10",
                        "comboLegs": [{"symbol": "LATE-LEG", "side": "YES"}]},
        }},
        {"t": 200, "kind": "book", "symbol": "LATE-LEG", "bid": 0.5,
         "ask": 0.52, "bid_size": 10.0, "ask_size": 10.0, "seq": 1,
         "ts": fixtures._ts(200)},
    ]
    result, store = run_backtest(session, fixtures.COMBOS,
                                 fixtures.build_drop_copy_feed(),
                                 PipelineConfig())
    decisions = [d for d in store.get_shadow_decisions(limit=10000)
                 if d["rfq_id"] == "RX"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == MISSING_LEG


def test_backtest_returns_usable_store():
    result, store = _run()
    assert store.state_digest() == store.state_digest()
    assert len(store.get_shadow_decisions(limit=10000)) >= 11
