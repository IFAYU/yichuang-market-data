"""MarketDataProvider: what the domain layer is allowed to know about a data source.

The domain layer calls these methods and receives domain objects. It never sees an endpoint URL, a Chinese field name or an
HTTP detail; those live in providers/endpoints.py and in the concrete providers.
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..contracts.models import Company, FinancialMetric, MarketPrice, OfficialRatio
from ..providers.endpoints import ENDPOINTS, Endpoint
from ..providers.http import RunContext, fetch_endpoint
from ..raw.store import RawRef, RawStore


class PhaseNotImplemented(NotImplementedError):
    """Declared by the interface, deliberately not connected in this phase."""


class MarketDataProvider(Protocol):
    market: str

    def companies(self, raw: RawRef) -> list[Company]: ...
    def prices(self, raw: RawRef) -> list[MarketPrice]: ...
    def official_ratios(self, raw: RawRef) -> list[OfficialRatio]: ...
    def industry_eps(self, raw: RawRef) -> list[FinancialMetric]: ...
    def monthly_revenue(self, raw: RawRef) -> list[FinancialMetric]: ...
    def financial_forms(self, raws: dict) -> dict: ...
    def income_statements(self, raw: RawRef) -> list[FinancialMetric]: ...
    def balance_sheets(self, raw: RawRef) -> list[FinancialMetric]: ...


class OfficialProviderBase:
    market: str = ""

    def endpoint(self, dataset: str) -> Endpoint:
        return ENDPOINTS[f"{self.market.lower()}.{dataset}"]

    def fetch(self, dataset: str, store: RawStore, ctx: RunContext, **kw) -> RawRef:
        return fetch_endpoint(self.endpoint(dataset), store, ctx, **kw)

    # Not connected in this phase (declared so the interface is complete).
    def income_statements(self, raw: RawRef) -> list[FinancialMetric]:
        raise PhaseNotImplemented("full income statements are not ingested in Phase 3A-3C; industry_eps carries EPS/revenue/net income")

    def balance_sheets(self, raw: RawRef) -> list[FinancialMetric]:
        raise PhaseNotImplemented("balance sheets are not ingested in Phase 3A-3C")


def fetched_at(raw: RawRef) -> str:
    return raw.meta.fetchedAt


def only(items: list, ticker: str) -> Optional[object]:
    return next((i for i in items if i.ticker == ticker), None)
