# Paper inventory and risk model

The inventory policy is enabled with `PipelineConfig(risk=RiskConfig(policy="inventory"))`.
The default remains `conservative` for existing replays. Live RFQ capture checks
paper quotes before saving them: it reduces each RFQ to at most $1,000 of quoted notional,
rejects a quote that would take open quoted notional for its game above $5,000,
and applies the inventory policy's default $10,000 per-game maximum-loss limit.
The inventory check can reduce either side before rejecting a quote. Open paper
quotes reserve capacity until the RFQ closes or expires; decisions from multiple
pricing workers are checked under the capture lock. Capture requires every leg
to resolve to the same game for these limits. A manual halt can be latched or reset with
`python -m scripts.risk_halt --db PATH --trip|--reset --reason TEXT`.
Live capture shows RFQs rejected by risk in the session-only RFQ screener.
Their identifiers and reasons are not written to `risk_events` or the raw RFQ log.

## Accounting

The provider rebuilds inventory from the SQLite RFQ, quote, and fill read
models. One open shadow draft per RFQ is pending. Its reserved loss is the
larger of the two possible one-sided fills: `buy_qty * (1 - buy_price)` for
our offer, or `sell_qty * sell_price` for our bid. A terminal RFQ releases
the pending draft. Fills move quantity into executed inventory, and
opposing fills offset by combo symbol. Closing fills realize P&L using the
average entry price. Duplicate fill IDs have no effect.

For an open long position, maximum loss is `quantity * average_price`.
For an open short, it is `quantity * (1 - average_price)`. Game, team, leg
market, and portfolio exposure are conservative sums of these losses and
pending reservations. Equity starts at $50,000 plus realized closing P&L;
buying power is equity less reserved loss. `exposure_snapshots` and
`risk_events` preserve the history shown in the dashboard Inventory tab.

NFL game IDs come from a supplied resolver or the pricer when available.
Otherwise canonical NFL leg symbols group by their season, week, away, and
home prefix. Unknown legs fall back to the combo symbol. The risk event
records which source the engine used.

## Quote policy

The inventory policy checks each possible fill side against configurable
per RFQ, leg market, game, team, portfolio, and minimum buying power caps.
It reduces size to the largest whole quantity within every cap, disabling a
side below `min_qty`. It rejects the draft if neither side remains.
Utilization above the soft threshold widens the spread. Net executed
inventory skews both prices to favor the offsetting side. Prices are rounded
to ticks and kept at least `min_edge_bps` from fair. A latched manual kill
switch rejects all drafts, including under the conservative policy.

The additive loss model is intentionally conservative for overlapping NFL
legs. It does not calculate score scenario WCL, CVaR, VaR, settlement P&L,
or automatic drawdown triggers. Those measures require a calibrated joint
game model and settlement lifecycle; dashboard exposure here is additive
reserved loss. The Inventory tab labels it accordingly.

## Dashboard

The live dashboard's **Inventory** tab (`GET /api/inventory`) shows the
current paper inventory rebuilt from the event store: equity, buying power,
and realized P&L; worst-case-loss exposure by game (pending vs executed
split), by leg market, and by team; and the recent risk-event feed. The tab
also shows recent paper quotes and the net notional inferred from shadow fills.
Shadow fills are limited to the displayed equity in chronological order; a
partially allocated fill is marked **CAPPED** in Performance. Displayed buying
power is the smaller of recorded inventory buying power and equity less the
absolute paper net notional. An expired RFQ releases its pending reservation.
The shadow-fill estimate is dashboard accounting, not a recorded exchange fill.
Game, leg market, and team exposure tables include the estimated maximum loss
from those paper fills. Each leg market and each team in a combo receives its
full loss, so those breakdowns overlap. The event table includes paper quote
decisions and capital caps alongside recorded risk events, with distinct action
labels.
The tab
also carries the manual kill-switch control (`POST /api/risk/kill-switch`
with `{"action": "trip"|"reset"}`), which appends one row to
`kill_switch_events` -- the same table the engine reads on every RFQ, so a
trip halts paper quoting immediately. The switch latches until an explicit
reset, exactly like `python -m scripts.risk_halt`.
