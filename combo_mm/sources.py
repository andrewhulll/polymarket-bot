"""Swappable event sources: ``poll(now) -> list[raw event dicts]``.

An :class:`EventSource` exposes the same item envelope the rest of the
pipeline already speaks (see :mod:`combo_mm.stream`):

* ``{"kind": "event", "raw": {...}}`` -- ``raw`` is exactly what
  :func:`combo_mm.normalize.normalize` accepts (RFQ/quote event dicts).
  An optional ``"client_derived": True`` on the item marks the event as a
  local inference (e.g. an RFQ that disappeared from a poll listing) rather
  than an exchange fact; the consumer copies it onto the normalized event.
* ``{"kind": "book", "symbol": ..., "bid": ..., "ask": ...,
  "bid_size": ..., "ask_size": ..., "seq": ..., "ts": ...}`` -- a book
  snapshot for the leg book cache.

:class:`SimulatedEventSource` replays the scripted session feed on a
logical clock. :class:`combo_mm.retail.RetailPollingSource` polls the
Polymarket US Retail REST API.
:class:`combo_mm.intl_gateway.InternationalQuoterGatewayAdapter` streams the
live international (polymarket.com) quoter-gateway websocket -- receive-only.
A future Exchange gRPC adapter would add a streaming source here too --
``PollingConsumer`` accepts any ``EventSource``.
"""
from __future__ import annotations

import abc
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from combo_mm.fixtures import BASE_TS
from combo_mm.stream import StreamDisconnected

log = logging.getLogger("combo_mm.sources")


class EventSource(abc.ABC):
    """Pollable source of pipeline items.

    Implementations must be side-effect free with respect to the pipeline:
    they only *read* upstream state. ``poll`` may raise
    :class:`StreamDisconnected` to signal a transport drop (the consumer
    treats it like a reconnect, preserving the pacing/backoff contract).
    """

    @abc.abstractmethod
    def poll(self, now: datetime) -> List[Dict[str, Any]]:
        """Return all items available as of ``now`` (aware datetime)."""


class SimulatedEventSource(EventSource):
    """Replay a scripted session feed on a logical clock.

    ``session`` is a list of stream items with ``"t"`` (ms offset from
    ``base_ts``) and ``"kind"``. Items with ``"kind": "disconnect"`` raise
    :class:`StreamDisconnected` when their time arrives; items with
    ``"stream": False`` are never emitted (invisible to a stream reader).
    """

    def __init__(self, session: List[Dict[str, Any]],
                 base_ts: datetime | None = None) -> None:
        self._items = sorted(session, key=lambda item: item.get("t", 0))
        self._base = base_ts or BASE_TS
        self._pos = 0

    def poll(self, now: datetime) -> List[Dict[str, Any]]:
        due: List[Dict[str, Any]] = []
        while self._pos < len(self._items):
            item = self._items[self._pos]
            due_at = self._base + timedelta(milliseconds=item.get("t", 0))
            if due_at > now:
                break
            self._pos += 1
            if item.get("kind") == "disconnect":
                raise StreamDisconnected(
                    f"simulated disconnect at t={item.get('t')}ms")
            if item.get("stream", True) is False:
                continue
            due.append(item)
        return due

    @property
    def exhausted(self) -> bool:
        """True once every session item has been released."""
        return self._pos >= len(self._items)


__all__ = ["EventSource", "SimulatedEventSource", "StreamDisconnected"]
