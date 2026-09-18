"""No-leakage instrumentation for backtest runs (issue #5 item C).

The sidecar carries future information (closing-model fair, competitor
quotes, requester valuations). Only :mod:`combo_mm.backtest.fill_model` may
read it — the pricer, risk and engine paths must never see it. This module
gives tests two mechanisms:

1. :func:`instrumented_run` — wraps an arbitrary mapping of "forbidden"
   sources (e.g. ``open`` on the sidecar file, a sidecar loader) so any call
   outside the fill model raises :class:`SidecarLeak`.
2. :func:`assert_no_leak` — verifies the guard stayed armed.

The fill model imports a single marked helper
(:func:`fill_model.load_sidecar_attrs`) which is allow-listed via the
:class:`LeakGuard` ``allow`` flag.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

__all__ = ["SidecarLeak", "LeakGuard", "instrumented_run", "assert_no_leak"]

_FILL_MODEL_MODULE = "combo_mm.backtest.fill_model"


class SidecarLeak(AssertionError):
    """Raised when sidecar data is touched outside the fill model."""


class LeakGuard:
    """Re-entrant guard: only the fill model may touch the sidecar."""

    def __init__(self) -> None:
        self._local = threading.local()
        self.violations: List[str] = []
        self.armed = False

    @contextlib.contextmanager
    def allow(self) -> Iterator[None]:
        """Mark the dynamic scope as the fill model (allow-listed)."""
        prev = getattr(self._local, "in_fill_model", False)
        self._local.in_fill_model = True
        try:
            yield
        finally:
            self._local.in_fill_model = prev

    def check(self, what: str) -> None:
        """Raise :class:`SidecarLeak` unless inside the fill-model scope.

        Violations are recorded only while the guard is armed (inside
        :func:`instrumented_run`); outside a run the raise is still loud but
        stateless, so unit tests of the guard itself do not contaminate run
        assertions.
        """
        if getattr(self._local, "in_fill_model", False):
            return
        if self.armed:
            self.violations.append(what)
        raise SidecarLeak(
            f"sidecar access outside the fill model is forbidden: {what}"
        )


_GUARD = LeakGuard()


def guard() -> LeakGuard:
    return _GUARD


@contextlib.contextmanager
def instrumented_run() -> Iterator[LeakGuard]:
    """Yield the process-wide guard for the duration of a backtest.

    The sidecar loader in :mod:`combo_mm.backtest.fill_model` calls
    :meth:`LeakGuard.check` unless wrapped in :meth:`LeakGuard.allow`, so a
    run that touches the sidecar anywhere else fails loudly with a
    :class:`SidecarLeak`. Arms violation recording for the duration.
    """
    _GUARD.violations.clear()
    prev, _GUARD.armed = _GUARD.armed, True
    try:
        yield _GUARD
    finally:
        _GUARD.armed = prev


def assert_no_leak() -> None:
    """Assert no sidecar violation was recorded; reset the log."""
    violations = list(_GUARD.violations)
    _GUARD.violations.clear()
    assert not violations, (
        "sidecar leakage detected: " + "; ".join(violations)
    )
