"""Streamlit probe: render the live pricing view against a real priced quote.

Run by ``tests/test_dashboard_smoke.py`` through ``AppTest``. The dashboard's
own live view needs a running feed; this drives the same render functions with
a quote produced by the real pricer on fixture books, so the table, the
per-game calibration block and the spread components are all executed.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import dashboard.app as app  # noqa: E402
from combo_mm.combo_markets import ComboMarketCatalog, parse_catalog_page  # noqa: E402
from combo_mm.live_quoter import LiveQuoter  # noqa: E402
from combo_mm.nfl.live_pricer import LiveRfq, NflLivePricer  # noqa: E402
from combo_mm.nfl.params_provider import ParamsProvider  # noqa: E402
from combo_mm.quote_selections import QuoteSelectionStore  # noqa: E402
from tests.nfl_live_fixtures import GAME, StubBooks, catalog_payload, position  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

catalog = ComboMarketCatalog()
catalog.merge(parse_catalog_page(catalog_payload()))
store = QuoteSelectionStore()
pricer = NflLivePricer(catalog, StubBooks(now_ms=int(NOW.timestamp() * 1000)),
                       ParamsProvider(REPO / "params"))
quoter = LiveQuoter(pricer, store, start_worker=False, clock=lambda: NOW)
quoter.price_now(LiveRfq(rfq_id="R-QUOTED", qty_decimal="25", direction="BUY",
                         leg_position_ids=(position(GAME, 1),
                                           position(f"{GAME}-spread-home-4pt5", 0))), "auto")
quoter.price_now(LiveRfq(rfq_id="R-DECLINED", qty_decimal="25",
                         leg_position_ids=(position(GAME, 0),
                                           position(f"{GAME}-spread-home-4pt5", 0))), "auto")


class _Monitor:
    selections = store
    quoter = quoter


app._live_pricing_view(_Monitor(), {"pricing_note": "probe"})

# The list's detail selector defaults to the newest row (the decline), so render
# the quoted one too: its per-game calibration and spread components are the
# part a trader reads.
quoted = next(q for q in store.list_priced_quotes() if q["status"] == "QUOTED")
app._model_quote_detail(quoted)
