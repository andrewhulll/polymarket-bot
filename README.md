# polymarket-bot

## Quick start

```bash
git clone https://github.com/andrewhulll/polymarket-bot.git
cd polymarket-bot
python -m pip install -r requirements-nfl.txt websockets
```

For the live RFQ logger, set `POLYMARKET_API_KEY`, `POLYMARKET_SECRET`, `POLYMARKET_PASSPHRASE`, and `POLYMARKET_ADDRESS` in your environment or a repo-root `.env` file. Keep `.env` private. Then run these in separate terminals from the repository root:

```bash
# Open the dashboard at http://localhost:8000. It starts capture if needed.
python -m dashboard.server --data-dir data/live --port 8000

# Optional: manage capture separately and keep dashboard startup read-only.
python scripts/capture_live_rfqs.py --data-dir data/live
python -m dashboard.server --data-dir data/live --port 8000 --no-start-capture
```

A paper-trading bot for Totalis that listens to Polymarket combo RFQs, prices them, manages paper exposure, and evaluates decisions through replay. It stores draft quotes but never submits quotes or orders.

## Totalis brief and progress

| Step | Requested substeps | What is implemented |
|---|---|---|
| **1. RFQ listener and storage** | Receive RFQs and lifecycle updates; reconnect safely; save raw messages, normalized RFQs, legs, timestamps, quotes, cancellations, expirations, executions, and settlements; process duplicates safely. | The consumer, recovery flow, and SQLite event store handle quoted RFQs with idempotent application. The live screener also shows rejected RFQs in memory while capture runs; those requests are never saved. The international gateway is receive-only; the Exchange gRPC adapter remains a stub. |
| **2. Pricing and correlation** | Estimate each leg's fair probability; model dependence between legs; explain correlation estimates; produce a combo fair price, bid/ask, size, expected edge, and adjustment reasons. | The live NFL pricer uses market leg prices and a historical score model for same-game dependence. It stores model inputs, confidence, adjustments, and decline reasons. Unsupported legs are declined; offline replay also supports the guarded independent-leg baseline. |
| **3. Inventory and risk ($50k starting capital)** | Track pending and executed exposure by market, game, and portfolio; widen, skew, reduce, or reject quotes as exposure grows. | The live capture path uses the inventory policy: pending quotes reserve capacity, fills update positions, and risk limits adjust or reject drafts. The default engine policy remains the simpler conservative check. See [risk model](docs/risk-model.md). |
| **4. Shadow quoting** | Generate and save a draft for each eligible RFQ without submitting it. | The engine screens RFQs, prices eligible ones, applies risk checks, and stores reproducible paper quotes and decision reasons. No live submission path is enabled. |
| **5. Backtest** | Replay RFQs without future information; report counts, rates, expected and realized P&L, swings, exposure, market and combo breakdowns, and correlation sensitivity. | The chronological runner and report cover these measures using simulated NFL RFQs built from historical games. It checks for future-data leakage and supports a correlation sweep. Validation against recorded live RFQ outcomes remains to be done. See [backtest](docs/backtest.md). |

## What the bot does

The bot screens every incoming RFQ, prices eligible NFL combos, applies paper inventory limits, and saves successful draft quotes with the reasons behind each decision. The dashboard shows both saved quotes and session-only rejected RFQs. Replay runs the paper decision path over a dataset and scores the results after settlement.

## Architecture

```text
RFQ feed + leg books → screening
                       ├→ rejected: memory-only dashboard feed
                       └→ eligible: NFL pricing → inventory risk
                                                  ↓
                                     quoted: SQLite event store
                                                  ↓
                                         dashboard / backtest
```

The capture process writes quoted RFQs to the event store and serves rejected screens from memory on localhost. Rejected rows disappear when capture restarts. The backtest replays saved events in timestamp order through the paper quoting engine. Detailed notes live in [docs](docs/): [data](docs/data.md), [RFQ simulation](docs/rfq-simulation.md), [risk](docs/risk-model.md), [backtest](docs/backtest.md), [settlement tracking](docs/settlement-tracking.md), and [capture supervision](docs/always-on.md).

## Correlation

For NFL same-game combos, the model estimates how team scores move together from historical games. Live market prices anchor each leg; the model supplies a joint-probability adjustment to the product of those leg prices. That adjustment matters because outcomes such as winning, covering the spread, and hitting the game total can depend on the same score.

The current live feed has sent mainly moneyline × total and spread × total pairs. On those deployed families, the corrected training analysis finds only a small improvement over independent-leg pricing at the selected correlation scale (`0.2`); it does not establish a large live trading edge. The model, estimation method, results, and limitations are documented in [correlation model](docs/correlation-model.md).
