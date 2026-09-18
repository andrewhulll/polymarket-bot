"""The live dashboard server: read-only JSON API over the capture database."""
import json
import sqlite3
import threading
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
        CREATE TABLE rfq (rfq_id TEXT PRIMARY KEY, created_time TEXT, status TEXT,
                          qty_decimal TEXT, cash_order_qty TEXT);
        CREATE TABLE rfq_screen (rfq_id TEXT PRIMARY KEY, screen TEXT, n_legs INTEGER,
                          n_resolved INTEGER, n_nfl_legs INTEGER, checks_json TEXT,
                          direction TEXT, side TEXT, submission_deadline TEXT);
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
        CREATE TABLE quotes (quote_id TEXT, rfq_id TEXT, buy_price REAL, sell_price REAL,
                          buy_qty_decimal TEXT, sell_qty_decimal TEXT,
                          created_time TEXT, status TEXT);
        CREATE TABLE rfq_legs (rfq_id TEXT, settlement_price REAL);
    """)
    conn.execute("INSERT INTO rfq VALUES ('R1','2026-09-17T12:00:00Z','open','25',NULL)");
    conn.execute("INSERT INTO rfq VALUES ('R2','2026-09-17T12:01:00Z','open','10',NULL)");
    conn.execute("INSERT INTO rfq_screen VALUES ('R1','QUOTABLE',2,2,2,"
                 "'{\"known legs\": true}', 'BUY','YES',NULL)");
    conn.execute("INSERT INTO rfq_screen VALUES ('R2','NON_NFL',2,2,0,"
                 "'{\"known legs\": true}', 'BUY','YES',NULL)");
    detail = json.dumps({"games": [{"game": "KC@BUF"}],
                         "legs": [{"canonical": "ML"}, {"canonical": "SPREAD"}]})
    conn.execute("INSERT INTO priced_quotes VALUES ('R1','auto','QUOTED','OK','fine',"
                 "0.60,'SELL','25','shares',0.58,0.65,?,"
                 "'2026-09-17T12:00:01Z','YES')", (detail,))
    conn.execute("INSERT INTO quotes VALUES ('Q1','R1',0.55,0.60,'25','25',"
                 "'2026-09-17T12:00:01Z','shadow')");
    conn.execute("INSERT INTO quote_latency (rfq_id,wait_ms,compute_ms,source) "
                 "VALUES ('R1',120.5,30.2,'live_capture')");
    conn.execute("INSERT INTO live_engine_health VALUES (1,'2026-09-17T11:00:00Z',"
                 "1000,2,1,'2026-09-17T12:05:00Z',0)");
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


def test_serves_the_frontend(server):
    status, body = get(server, "/")
    assert status == 200 and b"Combo RFQ bot" in body
    status, _ = get(server, "/static/app.js")
    assert status == 200
    status, _ = get(server, "/static/style.css")
    assert status == 200


def test_health_reports_the_database(server):
    health = get_json(server, "/api/health")
    assert health["ok"] and health["waiting"] is False
    assert health["db"].endswith("rfq_capture.db")


def test_rfqs_endpoint_filters_and_paginates(server):
    all_rfqs = get_json(server, "/api/rfqs")
    assert all_rfqs["total"] == 2 and len(all_rfqs["rows"]) == 2
    quotable = get_json(server, "/api/rfqs?only_quotable=1")
    assert quotable["total"] == 1
    assert quotable["rows"][0]["rfq_id"] == "R1"
    assert quotable["rows"][0]["quotable"] is True


def test_pricing_endpoint_and_single_rfq(server):
    pricing = get_json(server, "/api/pricing")
    assert pricing["total"] == 1
    row = pricing["rows"][0]
    assert row["status"] == "QUOTED" and row["response_price"] == 0.60
    one = get_json(server, "/api/pricing/R1")
    assert one["rfq_id"] == "R1" and one["edge_vs_market"] is not None
    try:
        get(server, "/api/pricing/NOPE")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_performance_endpoint(server):
    p = get_json(server, "/api/performance")
    assert p["quoted"] == 1 and p["shadow_fills"] == 1
    assert p["win_rate"] == 1.0
    assert p["by_family"][0]["family"] == "Moneyline + Spread"
    assert len(p["curve"]) == 1


def test_engine_endpoint(server):
    s = get_json(server, "/api/engine")
    assert s["health"]["messages_processed"] == 1000
    assert s["wait"]["p50"] == 120.5 and s["compute"]["p50"] == 30.2
    assert s["budget_ms"] == 400 and s["samples"] == 1
    assert s["drafts"][0]["quote_id"] == "Q1"


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
