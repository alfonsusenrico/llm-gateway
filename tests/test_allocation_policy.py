from __future__ import annotations

import random

import pytest

from llm_gateway.allocation.policy import (
    DIMENSIONS,
    Grant,
    PairRequest,
    Pool,
    Reason,
    allocate,
    water_fill,
)

# Large budgets keep budget out of the way when a test is about another dimension.
ROOMY = Pool(requests_per_minute=10_000, concurrency=1_000, monthly_budget_micro_usd=10**9)
NO_MINIMUM = Pool(0, 0, 0)


def pair(
    project_id: str,
    *,
    weight: int = 1,
    rpm: int = 10,
    concurrency: int = 1,
    budget: int = 1_000_000,
    ceiling: int = 10_000,
    environment: str = "production",
) -> PairRequest:
    return PairRequest(project_id, environment, weight, rpm, concurrency, budget, ceiling)


def by_project(grants: list[Grant]) -> dict[str, Grant]:
    return {grant.project_id: grant for grant in grants}


def test_undersubscribed_pool_grants_every_request() -> None:
    grants = allocate(
        [pair("alpha", concurrency=2), pair("beta", concurrency=3)], ROOMY, NO_MINIMUM
    )

    assert {(g.project_id, g.concurrency, g.state, g.reasons) for g in grants} == {
        ("alpha", 2, "approved", ()),
        ("beta", 3, "approved", ()),
    }


def test_weighted_sharing() -> None:
    capacity = Pool(10_000, 10, 10**9)
    minimum = Pool(0, 1, 0)

    grants = by_project(
        allocate(
            [pair("alpha", weight=3, concurrency=8), pair("beta", weight=1, concurrency=8)],
            capacity,
            minimum,
        )
    )

    assert (grants["alpha"].concurrency, grants["beta"].concurrency) == (7, 3)
    assert grants["alpha"].reasons == (Reason("concurrency", "capacity_shared"),)


def test_satisfied_project_releases_its_share() -> None:
    grants = by_project(
        allocate(
            [pair("alpha", concurrency=2), pair("beta", concurrency=10)],
            Pool(10_000, 8, 10**9),
            Pool(0, 1, 0),
        )
    )

    assert (grants["alpha"].concurrency, grants["beta"].concurrency) == (2, 6)
    assert grants["alpha"].state == "approved"
    assert grants["beta"].state == "reduced"


def test_minimums_exceeding_capacity_are_split_by_weight_with_a_reason() -> None:
    pairs = [pair(name, concurrency=3) for name in ("alpha", "beta", "gamma")]

    grants = by_project(allocate(pairs, Pool(10_000, 2, 10**9), Pool(0, 1, 0)))

    assert sorted(g.concurrency for g in grants.values()) == [0, 1, 1]
    starved = next(g for g in grants.values() if g.concurrency == 0)
    assert Reason("concurrency", "below_guaranteed_minimum") in starved.reasons


def test_global_budget_cap_is_respected() -> None:
    pairs = [pair(name, budget=2_000_000, ceiling=50_000) for name in ("a1", "b2", "c3", "d4")]

    grants = allocate(pairs, Pool(10_000, 1_000, 5_000_000), Pool(0, 0, 100_000))

    assert sum(g.monthly_budget_micro_usd for g in grants) <= 5_000_000
    assert all(g.reasons == (Reason("monthly_budget", "capacity_shared"),) for g in grants)


def test_ceiling_follows_a_reduced_budget() -> None:
    grants = allocate(
        [pair("alpha", budget=1_000_000, ceiling=500_000), pair("beta", budget=1_000_000)],
        Pool(10_000, 1_000, 600_000),
        NO_MINIMUM,
    )

    alpha = by_project(grants)["alpha"]
    assert alpha.monthly_budget_micro_usd == 300_000
    assert alpha.per_call_cost_ceiling_micro_usd == 300_000


def test_only_the_reduced_dimension_has_a_reason() -> None:
    grants = by_project(
        allocate(
            [pair("alpha", concurrency=4), pair("beta", concurrency=4)],
            Pool(10_000, 4, 10**9),
            NO_MINIMUM,
        )
    )

    assert grants["alpha"].reasons == (Reason("concurrency", "capacity_shared"),)


def test_water_fill_edge_cases() -> None:
    assert water_fill([], [], 5) == []
    assert water_fill([0, 0], [1, 1], 5) == [0, 0]
    assert water_fill([5, 5], [1, 1], 0) == [0, 0]
    assert water_fill([1, 1, 1], [1, 1, 1], 2) == [1, 1, 0]


def _random_pool(rng: random.Random) -> tuple[list[PairRequest], Pool, Pool]:
    pairs = [
        PairRequest(
            project_id=f"p{index:02d}",
            environment=rng.choice(["production", "development"]),
            weight=rng.randint(1, 10),
            requests_per_minute=rng.randint(1, 200),
            concurrency=rng.randint(1, 12),
            monthly_budget_micro_usd=rng.randint(0, 5_000_000),
            per_call_cost_ceiling_micro_usd=rng.randint(0, 100_000),
        )
        for index in range(rng.randint(1, 12))
    ]
    capacity = Pool(rng.randint(1, 600), rng.randint(1, 40), rng.randint(1, 10_000_000))
    minimum = Pool(
        rng.randint(0, capacity.requests_per_minute),
        rng.randint(0, capacity.concurrency),
        rng.randint(0, capacity.monthly_budget_micro_usd),
    )
    return pairs, capacity, minimum


@pytest.mark.parametrize("seed", range(300))
def test_allocation_properties(seed: int) -> None:
    rng = random.Random(seed)
    pairs, capacity, minimum = _random_pool(rng)

    grants = allocate(pairs, capacity, minimum)
    shuffled = list(pairs)
    rng.shuffle(shuffled)

    assert allocate(shuffled, capacity, minimum) == grants  # order independence
    requested = {pair.sort_key: pair for pair in pairs}
    for dimension in DIMENSIONS:
        field = "monthly_budget_micro_usd" if dimension == "monthly_budget" else dimension
        total_requested = sum(pair.requested(dimension) for pair in pairs)
        total_granted = sum(getattr(grant, field) for grant in grants)
        assert total_granted <= capacity.of(dimension)
        if total_requested <= capacity.of(dimension):
            assert total_granted == total_requested  # undersubscribed: exactly satisfied
        else:
            assert total_granted == capacity.of(dimension)  # no capacity left idle
    for grant in grants:
        asked = requested[(grant.project_id, grant.environment)]
        for dimension in DIMENSIONS:
            field = "monthly_budget_micro_usd" if dimension == "monthly_budget" else dimension
            assert getattr(grant, field) <= asked.requested(dimension)
        assert grant.per_call_cost_ceiling_micro_usd == min(
            asked.per_call_cost_ceiling_micro_usd, grant.monthly_budget_micro_usd
        )
        assert (grant.state == "approved") == (grant.reasons == ())
