"""combo_mm: data pipeline for the Polymarket combo RFQ paper-trading bot.

Implements the RFQ ingestion pipeline against the Polymarket US gRPC
contract: RFQ stream ingestion (empty request, Bearer auth), normalization
with exact wire shapes, append-only event storage with idempotent
projections, reconnect + recovery in the contract-mandated order, Drop Copy
fill reconciliation, auth structure (RS256 Private Key JWT -> Auth0), the V1
independent-leg pricer, shadow quoting (paper only), paper metrics, and the
Streamlit dashboard.

Parked (not implemented here): correlation model, inventory/risk engine,
production quoting, formal backtest.
"""

from combo_mm.events import (
    ALL_EVENT_TYPES,
    POST_TRADE_EVENTS,
    QUOTE_EVENT_TYPES,
    RFQ_EVENT_TYPES,
    QUOTE_TERMINAL_STATUSES,
    RFQ_TERMINAL_STATUSES,
    NormalizedEvent,
    quote_allows,
    quote_target_status,
    rfq_allows,
)
from combo_mm.normalize import NormalizeError, normalize
from combo_mm.config import PipelineConfig
from combo_mm.store import EventStore
from combo_mm.stream import (
    GrpcTransport,
    RfqTransport,
    SimulatedTransport,
    StreamDisconnected,
)
from combo_mm.dropcopy import (
    DropCopyStub,
    DropCopyTransport,
    SimulatedDropCopyTransport,
    drain_drop_copy,
)
from combo_mm.auth import (
    AuthConfig,
    CredentialsNotConfigured,
    TokenProvider,
    map_grpc_error,
)
from combo_mm.consumer import ConsumerConfig, PollingConsumer, StreamConsumer
from combo_mm.sources import EventSource, SimulatedEventSource
from combo_mm.retail import RetailPollingSource
from combo_mm.recovery import RecoveryReport, recovery_sync
from combo_mm.books import LegBookCache
from combo_mm.reference import ReferenceCache
from combo_mm.quotes import QuoteTracker
from combo_mm.fills import FillsLedger
from combo_mm.pricing import (
    QUOTED_OK,
    LegMarkInput,
    QuoteDecision,
    price_combo,
)
from combo_mm.shadow import MODEL_VERSION as SHADOW_MODEL_VERSION, ShadowQuoter
from combo_mm import fixtures, paper_backtest, replay

__all__ = [
    "ALL_EVENT_TYPES",
    "POST_TRADE_EVENTS",
    "QUOTE_EVENT_TYPES",
    "RFQ_EVENT_TYPES",
    "QUOTE_TERMINAL_STATUSES",
    "RFQ_TERMINAL_STATUSES",
    "NormalizedEvent",
    "quote_allows",
    "quote_target_status",
    "rfq_allows",
    "NormalizeError",
    "normalize",
    "PipelineConfig",
    "EventStore",
    "GrpcTransport",
    "RfqTransport",
    "SimulatedTransport",
    "StreamDisconnected",
    "DropCopyStub",
    "DropCopyTransport",
    "SimulatedDropCopyTransport",
    "drain_drop_copy",
    "AuthConfig",
    "CredentialsNotConfigured",
    "TokenProvider",
    "map_grpc_error",
    "ConsumerConfig",
    "PollingConsumer",
    "StreamConsumer",
    "EventSource",
    "SimulatedEventSource",
    "RetailPollingSource",
    "RecoveryReport",
    "recovery_sync",
    "LegBookCache",
    "ReferenceCache",
    "QuoteTracker",
    "FillsLedger",
    "QUOTED_OK",
    "LegMarkInput",
    "QuoteDecision",
    "price_combo",
    "SHADOW_MODEL_VERSION",
    "ShadowQuoter",
    "fixtures",
    "paper_backtest",
    "replay",
]
