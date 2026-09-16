"""LegBookCache: staleness flags, missing symbols, ordering."""
from combo_mm import LegBookCache

NOW = 1_789_567_200_000  # fixed virtual now (ms)


def _cache():
    return LegBookCache(staleness_ms=2000)


def test_fresh_book_not_stale():
    c = _cache()
    c.update("A", 0.5, 0.52, 100.0, 100.0,
             updated_at="2026-09-16T14:00:00Z", seq=1)  # == NOW
    snap = c.get(["A"], now_ms=NOW)["A"]
    assert not snap.missing and not snap.stale
    assert snap.bid == 0.5 and snap.ask == 0.52


def test_old_book_is_stale():
    c = _cache()
    c.update("A", 0.5, 0.52, updated_at="2026-09-16T13:59:00Z", seq=1)
    snap = c.get(["A"], now_ms=NOW)["A"]
    assert snap.stale
    assert c.is_stale("A", now_ms=NOW)


def test_unknown_symbol_flagged_missing_never_raises():
    c = _cache()
    snap = c.get(["NOPE"], now_ms=NOW)["NOPE"]
    assert snap.missing and snap.stale
    assert snap.bid is None and snap.ask is None
    assert c.is_stale("NOPE", now_ms=NOW)
    # batch with mixed known/unknown
    c.update("A", 0.5, 0.52, updated_at="2026-09-16T14:00:00Z", seq=1)
    out = c.get(["A", "NOPE"], now_ms=NOW)
    assert not out["A"].missing and out["NOPE"].missing


def test_out_of_order_seq_ignored():
    c = _cache()
    c.update("A", 0.5, 0.52, updated_at="2026-09-16T14:00:00Z", seq=5)
    c.update("A", 0.1, 0.12, updated_at="2026-09-16T13:00:00Z", seq=3)
    assert c.get(["A"], now_ms=NOW)["A"].bid == 0.5


def test_custom_staleness_budget():
    c = LegBookCache(staleness_ms=60_000)
    c.update("A", 0.5, 0.52, updated_at="2026-09-16T13:59:30Z", seq=1)
    assert not c.is_stale("A", now_ms=NOW)
