"""NFL historical data pipeline feeding the combo correlation engine (issue #6).

Phase A: game scores + closing spreads/totals -> weekly covariance parameter
files for moneyline / spread / total / team-total legs. "History for shape,
market for location": historical residuals give the score covariance
``(sigma_home, sigma_away, rho)``; live leg prices pin the score means.

Offline only -- never in the RFQ hot path. Modules:

- :mod:`combo_mm.nfl.ingest` -- nflverse pull, raw cache + manifest, validation
  (stdlib).
- :mod:`combo_mm.nfl.estimate` -- walk-forward covariance estimator with
  shrinkage (numpy).
- :mod:`combo_mm.nfl.params_io` -- versioned params files: canonical writer,
  schema-validating loader, matchup covariance evaluation (stdlib; this is the
  part the pricer reads).
- :mod:`combo_mm.nfl.joint` -- canonical score legs, joint probability of a
  combo under the bivariate-normal score model, market-implied mean solver
  (numpy + scipy).
- :mod:`combo_mm.nfl.synthetic_backtest` -- walk-forward synthetic-combo
  backtest: model vs naive product vs realized, calibration, correlation
  structure, sensitivity to correlation (numpy).

``numpy``/``scipy`` are imported lazily by the modules that need them, so
``import combo_mm.nfl`` and the params loader stay stdlib-only.
"""

MODEL_VERSION = "nfl-phaseA-1"

__all__ = ["MODEL_VERSION"]
