"""Forward estimates: interface and schema only.

No source in this pipeline creates them. They are supplied by the user, and must carry one of three statuses:
COMPANY_GUIDANCE, ANALYST_ESTIMATE, USER_ASSUMPTION. Nothing here ever generates an analyst consensus.
A licensed provider can implement ForwardEstimateProvider later without any change to the valuation code.
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..contracts.models import ForwardEstimate


class ForwardEstimateProvider(Protocol):
    def estimates(self, ticker: str, period: str) -> list[ForwardEstimate]: ...


class UserSuppliedForwardEstimates:
    """The only implementation in this phase: whatever the user typed in, kept with its status and note."""

    def __init__(self) -> None:
        self._items: list[ForwardEstimate] = []

    def add(self, est: ForwardEstimate) -> None:
        self._items.append(est)

    def estimates(self, ticker: str, period: str) -> list[ForwardEstimate]:
        return [e for e in self._items if e.ticker == ticker and e.period == period]


class NoForwardEstimates:
    """Default: no automatic source exists."""

    def estimates(self, ticker: str, period: str) -> list[ForwardEstimate]:
        return []


def best_label(items: list[ForwardEstimate]) -> Optional[str]:
    return items[0].status if items else None
