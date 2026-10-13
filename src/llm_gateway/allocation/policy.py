"""Turning requests into grants: guaranteed minimums, then weighted water-filling.

Pure and deterministic: the same inputs always give the same grants, whatever
order the projects are listed in. Money is handled in integer micro-USD.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

Dimension = Literal["requests_per_minute", "concurrency", "monthly_budget"]
ReasonCode = Literal["capacity_shared", "below_guaranteed_minimum"]
GrantState = Literal["approved", "reduced"]
DIMENSIONS: tuple[Dimension, ...] = ("requests_per_minute", "concurrency", "monthly_budget")


@dataclass(frozen=True, slots=True)
class PairRequest:
    """One project environment's request and the project's weight."""

    project_id: str
    environment: str
    weight: int
    requests_per_minute: int
    concurrency: int
    monthly_budget_micro_usd: int
    per_call_cost_ceiling_micro_usd: int

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.project_id, self.environment)

    def requested(self, dimension: Dimension) -> int:
        if dimension == "monthly_budget":
            return self.monthly_budget_micro_usd
        return getattr(self, dimension)


@dataclass(frozen=True, slots=True)
class Pool:
    """Capacity or guaranteed minimum per pooled dimension."""

    requests_per_minute: int
    concurrency: int
    monthly_budget_micro_usd: int

    def of(self, dimension: Dimension) -> int:
        if dimension == "monthly_budget":
            return self.monthly_budget_micro_usd
        return getattr(self, dimension)


@dataclass(frozen=True, slots=True)
class Reason:
    dimension: Dimension
    code: ReasonCode


@dataclass(frozen=True, slots=True)
class Grant:
    project_id: str
    environment: str
    requests_per_minute: int
    concurrency: int
    monthly_budget_micro_usd: int
    per_call_cost_ceiling_micro_usd: int
    state: GrantState
    reasons: tuple[Reason, ...]


def water_fill(needs: list[int], weights: list[int], capacity: int) -> list[int]:
    """Share `capacity` in proportion to weight, never giving anyone more than they need.

    Callers pass the lists in a stable order; leftover single units go to the
    largest fractional share first, then to the earliest position.
    """
    granted = [0] * len(needs)
    remaining_need = list(needs)
    remaining = capacity
    active = [index for index, need in enumerate(needs) if need > 0]
    while remaining > 0 and active:
        total_weight = sum(weights[index] for index in active)
        shares = {index: Fraction(remaining * weights[index], total_weight) for index in active}
        satisfied = [index for index in active if shares[index] >= remaining_need[index]]
        if satisfied:
            # Fully satisfy whoever needs less than their share, then share again.
            for index in satisfied:
                granted[index] += remaining_need[index]
                remaining -= remaining_need[index]
                remaining_need[index] = 0
            active = [index for index in active if index not in satisfied]
            continue
        floors = {index: math.floor(shares[index]) for index in active}
        for index, amount in floors.items():
            granted[index] += amount
        leftover = remaining - sum(floors.values())
        by_remainder = sorted(active, key=lambda index: (-(shares[index] - floors[index]), index))
        for index in by_remainder[:leftover]:
            granted[index] += 1
        remaining = 0
    return granted


def _allocate_dimension(
    pairs: list[PairRequest], dimension: Dimension, capacity: int, minimum: int
) -> tuple[list[int], list[ReasonCode | None]]:
    requests = [pair.requested(dimension) for pair in pairs]
    weights = [pair.weight for pair in pairs]
    floors = [min(request, minimum) for request in requests]
    if sum(floors) > capacity:
        granted = water_fill(floors, weights, capacity)
    else:
        extra = water_fill(
            [request - floor for request, floor in zip(requests, floors, strict=True)],
            weights,
            capacity - sum(floors),
        )
        granted = [floor + more for floor, more in zip(floors, extra, strict=True)]
    reasons: list[ReasonCode | None] = []
    for request, floor, amount in zip(requests, floors, granted, strict=True):
        if amount < floor:
            reasons.append("below_guaranteed_minimum")
        elif amount < request:
            reasons.append("capacity_shared")
        else:
            reasons.append(None)
    return granted, reasons


def allocate(requests: list[PairRequest], capacity: Pool, minimum: Pool) -> list[Grant]:
    """Grant every pair its request where capacity allows; otherwise share it fairly."""
    pairs = sorted(requests, key=lambda pair: pair.sort_key)
    results = {
        dimension: _allocate_dimension(
            pairs, dimension, capacity.of(dimension), minimum.of(dimension)
        )
        for dimension in DIMENSIONS
    }
    grants = []
    for position, pair in enumerate(pairs):
        reasons = tuple(
            Reason(dimension, code)
            for dimension in DIMENSIONS
            if (code := results[dimension][1][position]) is not None
        )
        budget = results["monthly_budget"][0][position]
        grants.append(
            Grant(
                project_id=pair.project_id,
                environment=pair.environment,
                requests_per_minute=results["requests_per_minute"][0][position],
                concurrency=results["concurrency"][0][position],
                monthly_budget_micro_usd=budget,
                per_call_cost_ceiling_micro_usd=min(pair.per_call_cost_ceiling_micro_usd, budget),
                state="reduced" if reasons else "approved",
                reasons=reasons,
            )
        )
    return grants
