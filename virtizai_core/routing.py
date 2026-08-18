from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .db import Database


@dataclass(frozen=True)
class EligibleRoute:
    route_id: str
    provider_id: str
    model_id: str
    model_name: str
    provider_name: str
    priority: int
    ordinal: int
    expected_latency_ms: float | None
    first_token_latency_ms: float | None
    relative_cost: float | None
    locality: str | None
    same_provider_fallback: bool


class RoutingStrategy:
    name = "base"

    def order(self, routes: list[EligibleRoute]) -> list[EligibleRoute]:
        raise NotImplementedError


class PriorityStrategy(RoutingStrategy):
    name = "priority"

    def order(self, routes: list[EligibleRoute]) -> list[EligibleRoute]:
        return sorted(routes, key=lambda item: (item.priority, item.ordinal))


class LowestLatencyStrategy(RoutingStrategy):
    name = "lowest_latency"

    def order(self, routes: list[EligibleRoute]) -> list[EligibleRoute]:
        return sorted(routes, key=lambda item: (item.first_token_latency_ms or item.expected_latency_ms or 1e12, item.priority, item.ordinal))


class CheapestCapableStrategy(RoutingStrategy):
    name = "cheapest_capable"

    def order(self, routes: list[EligibleRoute]) -> list[EligibleRoute]:
        return sorted(routes, key=lambda item: (item.relative_cost if item.relative_cost is not None else 1e12, item.priority, item.ordinal))


class BalancedStrategy(RoutingStrategy):
    name = "balanced"

    def order(self, routes: list[EligibleRoute]) -> list[EligibleRoute]:
        def score(item: EligibleRoute) -> float:
            latency = item.first_token_latency_ms or item.expected_latency_ms or 1000
            cost = item.relative_cost if item.relative_cost is not None else 1
            return latency / 1000 + cost + item.priority / 1000
        return sorted(routes, key=score)


STRATEGIES: dict[str, RoutingStrategy] = {
    strategy.name: strategy
    for strategy in (PriorityStrategy(), LowestLatencyStrategy(), CheapestCapableStrategy(), BalancedStrategy())
}


class RoutingEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def eligible_routes(self, role_id: str, strategy: str = "priority") -> list[EligibleRoute]:
        rows = self.database.fetch_all(
            """
            SELECT r.id AS route_id, r.priority, rt.ordinal,
                   rt.provider_id, rt.model_id, rt.conditions_json,
                   p.name AS provider_name, p.health_status AS provider_status,
                   m.name AS model_name, m.status AS model_status,
                   m.expected_latency_ms, m.first_token_latency_ms,
                   m.relative_cost, m.locality
            FROM routes r
            JOIN route_targets rt ON rt.route_id = r.id AND rt.enabled = 1
            JOIN providers p ON p.id = rt.provider_id AND p.enabled = 1
            JOIN models m ON m.id = rt.model_id
            WHERE r.role_id = ? AND r.enabled = 1
              AND p.health_status IN ('healthy', 'degraded')
              AND m.status IN ('warm', 'cold', 'loading', 'available', 'unknown')
            ORDER BY r.priority, rt.ordinal
            """,
            (role_id,),
        )
        routes = []
        seen_providers: set[str] = set()
        for row in rows:
            same_provider = row["provider_id"] in seen_providers
            seen_providers.add(row["provider_id"])
            routes.append(EligibleRoute(
                route_id=row["route_id"], provider_id=row["provider_id"], model_id=row["model_id"],
                model_name=row["model_name"], provider_name=row["provider_name"], priority=row["priority"],
                ordinal=row["ordinal"], expected_latency_ms=row["expected_latency_ms"],
                first_token_latency_ms=row["first_token_latency_ms"], relative_cost=row["relative_cost"],
                locality=row["locality"], same_provider_fallback=same_provider,
            ))
        return STRATEGIES.get(strategy, STRATEGIES["priority"]).order(routes)

    def warnings(self, routes: list[EligibleRoute]) -> list[str]:
        providers = [route.provider_id for route in routes]
        if len(routes) > 1 and len(set(providers)) < len(providers):
            return ["This fallback chain is provider-correlated; same-provider fallback does not protect against provider outage."]
        return []

    def explain(self, role_id: str, strategy: str = "priority") -> dict:
        rows = self.database.fetch_all("""SELECT rt.provider_id, rt.model_id, p.name provider_name, p.health_status, p.enabled, m.name model_name, m.status model_status, r.id route_id FROM routes r JOIN route_targets rt ON rt.route_id=r.id JOIN providers p ON p.id=rt.provider_id JOIN models m ON m.id=rt.model_id WHERE r.role_id=? AND r.enabled=1 ORDER BY r.priority, rt.ordinal""", (role_id,))
        eligible = self.eligible_routes(role_id, strategy)
        eligible_keys = {(item.provider_id, item.model_id) for item in eligible}
        excluded = []
        for row in rows:
            key = (row["provider_id"], row["model_id"])
            if key not in eligible_keys:
                reason = "provider_disabled" if not row["enabled"] else (f"provider_{row['health_status']}" if row["health_status"] not in {"healthy", "degraded"} else f"model_{row['model_status']}")
                excluded.append({"provider": row["provider_name"], "model": row["model_name"], "reason": reason})
        return {"eligible": [item.__dict__ for item in eligible], "excluded": excluded, "selected": eligible[0].__dict__ if eligible else None, "strategy": strategy, "fallback_reason": "provider_health" if excluded and eligible else None}
