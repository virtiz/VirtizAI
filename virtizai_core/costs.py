from __future__ import annotations

from dataclasses import dataclass

from .db import Database


@dataclass(frozen=True)
class CostResult:
    amount: float | None
    currency: str | None
    source: str
    local: bool = False


class CostService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def calculate(self, provider_id: str, model_name: str, input_tokens: int | None, output_tokens: int | None, provider_reported: float | None = None) -> CostResult:
        provider = self.database.fetch_one("SELECT adapter_type FROM providers WHERE id = ?", (provider_id,))
        if provider and provider["adapter_type"] in {"ollama", "mock"}:
            return CostResult(None, None, "local", local=True)
        if provider_reported is not None:
            return CostResult(provider_reported, "USD", "provider_reported")
        profile = self.database.fetch_one("SELECT input_cost_per_million, output_cost_per_million, currency FROM cost_profiles WHERE provider_id = ? AND model_name = ?", (provider_id, model_name))
        if not profile or input_tokens is None or output_tokens is None or profile["input_cost_per_million"] is None or profile["output_cost_per_million"] is None:
            return CostResult(None, None, "unknown")
        amount = input_tokens / 1_000_000 * profile["input_cost_per_million"] + output_tokens / 1_000_000 * profile["output_cost_per_million"]
        return CostResult(amount, profile["currency"], "configured")
