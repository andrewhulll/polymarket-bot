"""The live dashboard server: read-only JSON API over the capture database."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def repo_path(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO))
    return REPO


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rfq (rfq_id TEXT PRIMARY KEY, symbol TEXT, creator_user_id TEXT,
                          qty_decimal REAL, cash_order_qty REAL, created_time TEXT,
                          updated_time TEXT, rest_remainder INTEGER NOT NULL DEFAULT 0,
                          status TEXT NOT NULL, last_event_id TEXT);
        CREATE TABLE rfq_screen (rfq_id TEXT PRIMARY KEY, screen TEXT, n_legs INTEGER,
                          n_resolved INTEGER, n_nfl_legs INTEGER, checks_json TEXT,
                          direction TEXT, side TEXT, submission_deadline TEXT,
                          seq INTEGER NOT NULL DEFAULT 0, condition_id TEXT,
                          rank INTEGER NOT NULL DEFAULT 0,
                          catalog_version INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE rfq_legs (rfq_id TEXT, symbol TEXT, side TEXT, settlement_price REAL);
        CREATE TABLE priced_quotes (rfq_id TEXT, trigger TEXT, status TEXT,
                          reason_code TEXT, reason_detail TEXT, response_price REAL,
                          response_action TEXT, size TEXT, size_unit TEXT, fair REAL,
                          naive REAL, detail_json TEXT, priced_at TEXT, side TEXT);
        CREATE TABLE live_trades (rfq_id TEXT, price REAL, size TEXT, executed_at TEXT);
        CREATE TABLE quote_latency (id INTEGER PRIMARY KEY, rfq_id TEXT,
                          wait_ms REAL, compute_ms REAL, source TEXT);
        CREATE TABLE live_engine_health (id INTEGER PRIMARY KEY, started_at TEXT,
                          messages_processed INTEGER, errors INTEGER,
                          gateway_connected INTEGER, heartbeat_at TEXT, buffer_drops INTEGER);
        CREATE TABLE quotes (quote_id TEXT, rfq_id TEXT, symbol TEXT, buy_price REAL,
                          sell_price REAL, buy_qty_decimal TEXT, sell_qty_decimal TEXT,
                          created_time TEXT, status TEXT, origin TEXT NOT NULL DEFAULT 'shadow',
                          model_version TEXT, params_version TEXT, decided_by TEXT);
        CREATE TABLE books (symbol TEXT PRIMARY KEY, bid REAL, ask REAL,
                          bid_size REAL, ask_size REAL, seq INTEGER, ts TEXT);
        CREATE TABLE raw_events (id INTEGER PRIMARY KEY, event_id TEXT, event_type TEXT,
                          rfq_id TEXT, quote_id TEXT, symbol TEXT, event_key TEXT,
                          client_derived INTEGER, payload_json TEXT, source TEXT,
                          recorded_at TEXT);
        CREATE TABLE fills (fill_id TEXT PRIMARY KEY, rfq_id TEXT, quote_id TEXT,
                          symbol TEXT, side TEXT, price REAL, qty REAL, executed_time TEXT,
                          source TEXT, drop_copy_seq INTEGER, event_id TEXT);
        CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, rfq_id TEXT, decision TEXT,
                          reason TEXT, fair_price REAL, buy_price REAL, sell_price REAL,
                          spread_bps REAL, expected_edge_bps REAL, buy_qty REAL, sell_qty REAL,
                          components_json TEXT, ts TEXT);
        CREATE TABLE risk_events (id INTEGER PRIMARY KEY, ts TEXT, rfq_id TEXT,
                          quote_id TEXT, game_id TEXT, action TEXT, reason TEXT,
                          detail_json TEXT, policy_version TEXT);
        CREATE TABLE exposure_snapshots (id INTEGER PRIMARY KEY, source_id TEXT, ts TEXT,
                          level TEXT, key TEXT, pending_wcl REAL, executed_wcl REAL,
                          total_wcl REAL, equity REAL, buying_power REAL);
        CREATE TABLE kill_switch_events (id INTEGER PRIMARY KEY, ts TEXT, state TEXT,
                          trigger TEXT, detail_json TEXT);
    """)
    conn.execute("INSERT INTO rfq VALUES ('R1','NFL-KC-BUF-1',NULL,25,NULL,"
                 "'2026-09-17T12:00:00Z',NULL,0,'OPEN',NULL)");
    conn.execute("INSERT INTO rfq VALUES ('R2','NFL-DAL-PHI-1',NULL,10,NULL,"
                 "'2026-09-17T12:01:00Z',NULL,0,'OPEN',NULL)");
    conn.execute("INSERT INTO rfq VALUES ('R3','NFL-KC-BUF-2',NULL,10,NULL,"
                 "'2026-09-17T12:02:00Z',NULL,0,'OPEN',NULL)");
    conn.execute("INSERT INTO rfq_screen VALUES ('R1','QUOTABLE',2,2,2,"
                 "'{\"known legs\": true}', 'BUY','YES',NULL,1,NULL,0,3)");
    conn.execute("INSERT INTO rfq_screen VALUES ('R2','NON_NFL',2,2,0,"
                 "'{\"known legs\": true}', 'BUY','YES',NULL,2,NULL,90,3)");
    conn.execute("INSERT INTO rfq_screen VALUES ('R3','QUOTABLE',1,1,1,"
                 "'{\"known legs\": true}', 'BUY','YES',NULL,3,NULL,0,3)");
    conn.execute("INSERT INTO rfq_legs VALUES ('R1','NFL-KC-BUF-1-ML','YES',0.8)");
    conn.execute("INSERT INTO rfq_legs VALUES ('R1','NFL-KC-BUF-1-SPREAD','YES',0.9)");
    conn.execute("INSERT INTO rfq_legs VALUES ('R3','NFL-KC-BUF-2-TOTAL','YES',0.7)");
    conn.execute("INSERT INTO books VALUES ('NFL-KC-BUF-1-ML',0.55,0.57,100,100,42,"
                 "'2026-09-17T12:00:00Z')");
    conn.execute("INSERT INTO books VALUES ('NFL-KC-BUF-1-SPREAD',0.50,0.54,80,80,43,"
                 "'2026-09-17T12:00:00Z')");
    detail = json.dumps({"games": [{"game": "KC@BUF"}],
                         "legs": [{"canonical": "ML"}, {"canonical": "SPREAD"}]})
    conn.execute("INSERT INTO priced_quotes VALUES ('R1','auto','QUOTED','OK','fine',"
                 "0.60,'SELL','25','shares',0.58,0.65,?,"
                 "'2026-09-17T12:00:01Z','YES')", (detail,))
    detail3 = json.dumps({"games": [{"game": "KC@BUF"}], "legs": [{"canonical": "TOTAL"}]})
    conn.execute("INSERT INTO priced_quotes VALUES ('R3','auto','QUOTED','OK','fine',"
                 "0.40,'BUY','10','shares',0.42,0.38,?,"
                 "'2026-09-17T12:02:01Z','YES')", (detail3,))
    conn.execute("INSERT INTO live_trades VALUES ('R1',0.62,'25','2026-09-17T12:00:05Z')");
    conn.execute("INSERT INTO quotes VALUES ('Q1','R1','NFL-KC-BUF-1',0.55,0.60,'25','25',"
                 "'2026-09-17T12:00:01Z','shadow','shadow','joint_v1','nfl_2026_w02','live_quoter')");
    conn.execute("INSERT INTO quotes VALUES ('Q3','R3','NFL-KC-BUF-2',0.38,0.42,'10','10',"
                 "'2026-09-17T12:02:01Z','shadow','shadow','joint_v1','nfl_2026_w02','live_quoter')");
    conn.execute("INSERT INTO quote_latency (rfq_id,wait_ms,compute_ms,source) "
                 "VALUES ('R1',120.5,30.2,'live_capture')");
    conn.execute("INSERT INTO quote_latency (rfq_id,wait_ms,compute_ms,source) "
                 "VALUES ('R3',250.0,45.0,'live_capture')");
    conn.execute("INSERT INTO live_engine_health VALUES (1,'2026-09-17T11:00:00Z',"
                 "1000,2,1,'2026-09-17T12:05:00Z',0)");
    conn.execute("INSERT INTO raw_events (event_type,rfq_id,symbol,client_derived,source,recorded_at) "
                 "VALUES ('rfq_posted','R1','NFL-KC-BUF-1',1,'gateway','2026-09-17T12:00:00Z')");
    conn.execute("INSERT INTO raw_events (event_type,rfq_id,symbol,client_derived,source,recorded_at) "
                 "VALUES ('quote_decided','R1','NFL-KC-BUF-1',0,'engine','2026-09-17T12:00:01Z')");
    conn.execute("INSERT INTO fills VALUES ('F1','R1','Q1','NFL-KC-BUF-1','SELL',0.60,25,"
                 "'2026-09-17T12:00:05Z','live',7,'E1')");
    conn.execute("INSERT INTO shadow_decisions (rfq_id,decision,reason,fair_price,buy_price,"
                 "sell_price,spread_bps,expected_edge_bps,ts) VALUES ('R1','QUOTE','ok',0.58,"
                 "0.55,0.60,135.0,20.0,'2026-09-17T12:00:01Z')");
    conn.execute("INSERT INTO risk_events (ts,rfq_id,action,reason,policy_version) VALUES "
                 "('2026-09-17T12:03:00Z','R3','throttle','max_quotes_per_min','v3')");
    conn.execute("INSERT INTO exposure_snapshots (ts,equity,buying_power,pending_wcl,executed_wcl,total_wcl) "
                 "VALUES ('2026-09-17T12:00:00Z',50000.0,48000.0,100.0,50.0,150.0)");
    conn.execute("INSERT INTO exposure_snapshots (ts,equity,buying_power,pending_wcl,executed_wcl,total_wcl) "
                 "VALUES ('2026-09-17T12:05:00Z',50010.0,47900.0,120.0,60.0,180.0)");
    conn.execute("INSERT INTO kill_switch_events (ts,state,trigger) VALUES "
                 "('2026-09-17T12:04:00Z','tripped','max_downswing')");
    conn.execute("ALTER TABLE priced_quotes ADD COLUMN after_deadline INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


@pytest.fixture()
def server(repo_path, tmp_path, monkeypatch):
    from dashboard import server as srv
    make_db(tmp_path / "rfq_capture.db")
    monkeypatch.setitem(srv._CONFIG, "data_dir", str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    thread.join(timeout=5)


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as res:
        return res.status, res.read()


def get_json(base: str, path: str):
    status, body = get(base, path)
    assert status == 200, path
    return json.loads(body)


def post_json(base: str, path: str, payload: dict, timeout: int = 30):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_serves_the_frontend(server):
    status, body = get(server, "/")
    assert status == 200 and b"Combo RFQ bot" in body
    status, _ = get(server, "/static/js/core.js")
    assert status == 200
    status, _ = get(server, "/static/style.css")
    assert status == 200
    status, body = get(server, "/static/vendor/vega-embed.min.js")
    assert status == 200 and len(body) > 10000


def test_health_reports_the_database(server):
    health = get_json(server, "/api/health")
    assert health["ok"] and health["waiting"] is False
    assert health["db"].endswith("rfq_capture.db")


def test_rfqs_endpoint_filters_and_paginates(server):
    all_rfqs = get_json(server, "/api/rfqs")
    assert all_rfqs["total"] == 3 and len(all_rfqs["rows"]) == 3
    assert next(r for r in all_rfqs["rows"] if r["rfq_id"] == "R1")["trade_price"] == 0.62
    assert next(r for r in all_rfqs["rows"] if r["rfq_id"] == "R3")["trade_price"] is None
    quotable = get_json(server, "/api/rfqs?only_quotable=1")
    assert quotable["total"] == 2
    assert quotable["rows"][0]["rfq_id"] == "R3"
    assert quotable["rows"][0]["quotable"] is True


def test_rfqs_screen_and_search_params(server):
    by_screen = get_json(server, "/api/rfqs?screen=NON_NFL")
    assert by_screen["total"] == 1 and by_screen["rows"][0]["rfq_id"] == "R2"
    by_id = get_json(server, "/api/rfqs?search=R1")
    assert by_id["total"] == 1 and by_id["rows"][0]["rfq_id"] == "R1"
    by_symbol = get_json(server, "/api/rfqs?search=NFL-KC-BUF")
    assert by_symbol["total"] == 2
    both = get_json(server, "/api/rfqs?screen=QUOTABLE&search=R3")
    assert both["total"] == 1 and both["rows"][0]["rfq_id"] == "R3"
    none = get_json(server, "/api/rfqs?search=ZZZ")
    assert none["total"] == 0 and none["rows"] == []


def test_rfq_detail_endpoint(server):
    d = get_json(server, "/api/rfqs/R1")
    assert d["rfq"]["rfq_id"] == "R1" and d["rfq"]["symbol"] == "NFL-KC-BUF-1"
    assert d["screen"]["screen"] == "QUOTABLE"
    assert d["screen"]["checks"] == {"known legs": True}
    assert len(d["legs"]) == 2
    assert d["legs"][0]["bid"] == 0.55 and d["legs"][0]["ask"] == 0.57
    assert d["legs"][0]["settlement_price"] == 0.8
    assert [e["event_type"] for e in d["events"]] == ["rfq_posted", "quote_decided"]
    assert d["pricing"]["response_price"] == 0.60
    assert d["pricing"]["model_version"] == "joint_v1"
    assert d["pricing"]["params_version"] == "nfl_2026_w02"
    assert d["pricing"]["decided_by"] == "live_quoter"
    assert d["pricing"]["detail"]["games"][0]["game"] == "KC@BUF"
    assert d["trade"]["price"] == 0.62
    try:
        get(server, "/api/rfqs/NOPE")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_pricing_endpoint_and_single_rfq(server):
    pricing = get_json(server, "/api/pricing")
    assert pricing["total"] == 2
    row = [r for r in pricing["rows"] if r["rfq_id"] == "R1"][0]
    assert row["status"] == "QUOTED" and row["response_price"] == 0.60
    one = get_json(server, "/api/pricing/R1")
    assert one["rfq_id"] == "R1" and one["edge_vs_market"] is not None
    assert one["model_version"] == "joint_v1" and one["decided_by"] == "live_quoter"
    try:
        get(server, "/api/pricing/NOPE")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_pricing_model_edge(server):
    one = get_json(server, "/api/pricing/R1")
    # SELL: sign -1; market 0.62 (accepted trade), fair 0.58, our price 0.60.
    # Edges are ours: negative = price against us, positive = in our favor.
    assert one["market_price"] == 0.62 and one["market_source"] == "accepted trade"
    assert abs(one["model_edge"] - 0.04) < 1e-9    # -1 * (0.58 - 0.62): fair < market, selling is right
    assert abs(one["edge_vs_market"] - (-0.02)) < 1e-9  # -1 * (0.62 - 0.60): sold below market
    # No accepted trade: the leg-implied naive price is the market reference,
    # so the quote is still scored (BUY 0.40 sign +1; naive 0.38).
    no_trade = get_json(server, "/api/pricing/R3")
    assert no_trade["naive"] == 0.38
    assert no_trade["market_price"] == 0.38
    assert no_trade["market_source"] == "leg-implied naive"
    assert abs(no_trade["edge_vs_market"] - (-0.02)) < 1e-9  # 1 * (0.38 - 0.40)


def test_performance_endpoint(server):
    p = get_json(server, "/api/performance")
    # R1 fills against its accepted trade, R3 against the naive reference.
    assert p["quoted"] == 2 and p["shadow_fills"] == 2
    assert p["win_rate"] == 1.0
    assert p["by_family"][0]["family"] == "Moneyline + Spread"
    assert len(p["curve"]) == 2


def test_fills_endpoint(server):
    fills = get_json(server, "/api/fills")
    assert fills["total"] == 2 and len(fills["rows"]) == 2
    newest, older = fills["rows"][0], fills["rows"][1]
    # R3 (newest): BUY 0.40 vs naive 0.38, fair 0.42, single leg settled YES at 1.0
    assert newest["rfq_id"] == "R3"
    assert newest["market_source"] == "leg-implied naive"
    assert abs(newest["quote_edge"] - (-0.02)) < 1e-9   # 1 * (0.38 - 0.40): bought above ref
    assert abs(newest["model_edge"] - 0.04) < 1e-9      # 1 * (0.42 - 0.38)
    assert abs(newest["expected_pnl"] - 0.2) < 1e-9     # 1 * (0.42 - 0.40) * 10
    assert abs(newest["realized_pnl"] - 6.0) < 1e-9     # 1 * (1 - 0.40) * 10: bought a winner
    assert older["rfq_id"] == "R1"
    # R1: SELL 0.60 vs accepted trade 0.62, fair 0.58, settled YES at 1.0
    assert older["market_source"] == "accepted trade"
    assert abs(older["quote_edge"] - (-0.02)) < 1e-9   # sold below market
    assert abs(older["model_edge"] - 0.04) < 1e-9     # fair below market: selling is right
    assert abs(older["expected_pnl"] - 0.5) < 1e-9    # -1 * (0.58 - 0.60) * 25
    assert abs(older["realized_pnl"] - (-10.0)) < 1e-9  # -1 * (1 - 0.60) * 25: sold a winner
    assert older["settled_legs"] == 2 and older["total_legs"] == 2
    page2 = get_json(server, "/api/fills?page=2")
    assert page2["total"] == 2 and page2["rows"] == []


def test_exposure_endpoint(server):
    e = get_json(server, "/api/exposure")
    assert e["latest"]["equity"] == 50010.0
    assert e["latest"]["buying_power"] == 47900.0
    assert e["latest"]["total_wcl"] == 180.0
    assert len(e["series"]) == 2
    assert e["series"][0]["ts"] < e["series"][1]["ts"]  # oldest first


def test_latency_histogram_endpoint(server):
    h = get_json(server, "/api/latency/histogram?n=5")
    assert sum(b["n"] for b in h["wait"]) == 2
    assert sum(b["n"] for b in h["compute"]) == 2
    assert all(b["lo"] <= b["hi"] for b in h["wait"])
    full = get_json(server, "/api/latency/histogram")
    assert len(full["wait"]) <= 40


def test_risk_endpoint(server):
    r = get_json(server, "/api/risk")
    assert len(r["events"]) == 1
    assert r["events"][0]["action"] == "throttle"
    assert r["events"][0]["reason"] == "max_quotes_per_min"
    assert r["kill_switch"]["state"] == "tripped"
    assert r["kill_switch"]["trigger"] == "max_downswing"


def test_engine_endpoint(server):
    s = get_json(server, "/api/engine")
    assert s["health"]["messages_processed"] == 1000
    assert s["wait"]["p50"] == 250.0 and s["compute"]["p50"] == 45.0
    assert s["budget_ms"] == 400 and s["samples"] == 2
    assert [d["quote_id"] for d in s["drafts"]] == ["Q3", "Q1"]  # newest first
    assert s["drafts"][0]["buy_price"] == 0.38
    assert s["kill_switch"]["state"] == "tripped"


def test_inventory_endpoint(server):
    inv = get_json(server, "/api/inventory")
    # Q1 (sell 25 filled @0.60, buy 25 @0.55 still pending) + Q3 (10/10 pending).
    assert inv["equity"] == 50000.0
    # Q1: our offer 25 @0.55 filled (we sold 25 @0.60); buy 25 @0.55 still pending.
    # Q3: 10/10 pending. Pending WCL = 15.0 + 6.2; executed WCL = 10.0.
    assert inv["buying_power"] == pytest.approx(50000.0 - 15.0 - 10.0 - 6.2)
    assert inv["realized_pnl"] == 0.0
    assert inv["kill_switch"] is True
    assert inv["kill_switch_event"]["state"] == "tripped"
    assert set(inv["exposures"]) == set(inv["pending"]) | set(inv["executed"])
    assert inv["pending"] and inv["executed"]
    for game, total in inv["exposures"].items():
        assert total == pytest.approx(inv["pending"].get(game, 0)
                                      + inv["executed"].get(game, 0))
    assert len(inv["markets"]) == 3 and len(inv["teams"]) == 4
    assert inv["net_by_game"]  # signed positions tracked per game


def test_kill_switch_post_trips_and_resets(server):
    status, body = post_json(server, "/api/risk/kill-switch",
                             {"action": "trip", "reason": "test trip"})
    assert status == 200
    assert body["action"] == "trip"
    assert body["kill_switch"]["state"] == "tripped"
    assert get_json(server, "/api/risk")["kill_switch"]["state"] == "tripped"
    assert get_json(server, "/api/inventory")["kill_switch"] is True

    status, body = post_json(server, "/api/risk/kill-switch", {"action": "reset"})
    assert status == 200
    assert body["kill_switch"]["state"] == "reset"
    assert get_json(server, "/api/inventory")["kill_switch"] is False


def test_kill_switch_post_rejects_bad_input(server):
    status, body = post_json(server, "/api/risk/kill-switch", {"action": "nuke"})
    assert status == 400 and "action" in body["error"]
    status, body = post_json(server, "/api/risk/kill-switch", {})
    assert status == 400
    status, body = post_json(server, "/api/risk/kill-switch",
                             {"action": "trip", "reason": "   "})
    assert status == 400


def test_inventory_tab_present(server):
    status, body = get(server, "/")
    assert status == 200
    assert b'data-tab="inventory"' in body and b"/static/js/inventory.js" in body
    status, _ = get(server, "/static/js/inventory.js")
    assert status == 200


def test_nfl_meta_without_results(server):
    m = get_json(server, "/api/nfl/meta")
    assert m["has_results"] is False  # no results/nfl_backtest in this repo checkout


def _nfl_deps_ok():
    from dashboard import nfl_api
    return nfl_api.DEPS_OK


def test_nfl_view_without_results(server):
    if not _nfl_deps_ok():
        try:
            get(server, "/api/nfl/overview")
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        return
    v = get_json(server, "/api/nfl/overview")
    assert v["has_results"] is False and v["empty"] is True
    try:
        get(server, "/api/nfl/nope")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_nfl_games_without_pull(server):
    if not _nfl_deps_ok():
        try:
            get(server, "/api/nfl/games")
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        return
    g = get_json(server, "/api/nfl/games")
    assert g["games"] == []


def test_nfl_run_rejects_unknown_script(server):
    status, body = post_json(server, "/api/nfl/run", {"script": "rm -rf"})
    assert status == 400 and "allowed" in body["error"]
    status, _ = post_json(server, "/api/engine", {})
    assert status == 404  # no other POST surface


@pytest.mark.skipif(__import__("importlib").util.find_spec("altair") is None
                    or __import__("importlib").util.find_spec("scipy") is None,
                    reason="needs altair+scipy")
def test_nfl_explorer_price_hypothetical(server):
    status, body = post_json(server, "/api/nfl/explorer/price", {
        "home": "KC", "away": "BUF", "spread_home": -6.5, "total": 47.5, "team": "KC",
        "p_team_cover": 0.55, "p_over": 0.52, "use_ml": False, "corr_scale": 1.0,
        "legs": [{"kind": "ml", "side": "team"}, {"kind": "spread", "side": "team"}]})
    assert status == 200, body
    assert body["combo"]["model"] > body["combo"]["naive"]
    assert body["specs"][0]["spec"]["$schema"].startswith("https://vega.github.io/schema/vega-lite/")


def test_missing_database_reports_waiting(repo_path, tmp_path, monkeypatch):
    from dashboard import server as srv
    monkeypatch.setitem(srv._CONFIG, "data_dir", str(tmp_path / "empty"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload = get_json(f"http://127.0.0.1:{port}", "/api/engine")
        assert payload["waiting"] is True
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def post_json(base: str, path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture()
def serve_dir(repo_path, tmp_path, monkeypatch):
    from dashboard import server as srv
    monkeypatch.setitem(srv._CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setitem(srv._CONFIG, "active_db", "rfq_capture.db")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    thread.join(timeout=5)


def test_uninitialized_db_returns_waiting(serve_dir, tmp_path):
    (tmp_path / "rfq_capture.db").write_bytes(b"")  # exists but has no tables
    payload = get_json(serve_dir, "/api/rfqs")
    assert payload["waiting"] is True


def test_sources_list_and_switch(serve_dir, tmp_path):
    make_db(tmp_path / "rfq_capture.db")
    make_db(tmp_path / "week1_backtest.db")
    base = serve_dir

    src = get_json(base, "/api/sources")
    assert src["active"] == "rfq_capture.db"
    assert {s["id"] for s in src["sources"]} == {"rfq_capture.db", "week1_backtest.db"}
    assert all("label" in s for s in src["sources"])

    status, body = post_json(base, "/api/sources/active", {"id": "week1_backtest.db"})
    assert status == 200
    assert body["active"] == "week1_backtest.db"
    assert get_json(base, "/api/sources")["active"] == "week1_backtest.db"

    # unknown files and traversal are rejected
    for bad in ["nope.db", "../evil.db", "sub/dir.db", ""]:
        status, body = post_json(base, "/api/sources/active", {"id": bad})
        assert status == 400, bad


def test_nfl_run_rejects_unknown_script(server):
    status, body = post_json(server, "/api/nfl/run", {"script": "rm -rf"})
    assert status == 400
    assert "unknown script" in body["error"]


def test_build_argv_week_backtest(repo_path):
    from dashboard import nfl_api
    argv = nfl_api._build_argv("run_week_backtest", {}, data_dir="/tmp/x")
    assert argv == ["scripts/run_week_backtest.py", "--data-dir", "/tmp/x"]
    argv = nfl_api._build_argv("run_backtest", {"first_season": 2020})
    assert argv == ["scripts/nfl_backtest.py", "--first-season", "2020"]
    with pytest.raises(ValueError):
        nfl_api._build_argv("nope", {})
