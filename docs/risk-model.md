# Paper inventory and risk model

The inventory policy is enabled with `PipelineConfig(risk=RiskConfig(policy="inventory"))`.
The default remains `conservative` for existing replays. Both policies run only
inside the paper quoting engine. A manual halt can be latched or reset with
`python -m scripts.risk_halt --db PATH --trip|--reset --reason TEXT`.

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
`risk_events` preserve the history shown in the dashboard Risk tab.

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
reserved loss. The Risk tab labels it accordingly.
