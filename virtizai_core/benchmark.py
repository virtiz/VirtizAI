from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .providers import ProviderRegistry


@dataclass(frozen=True)
class BenchmarkResult:
    provider_id: str
    model_name: str
    ttft_ms: float | None
    total_latency_ms: float
    tokens_per_second: float | None
    instruction_adherence: bool
    structured_output: bool
    routing_classification: bool
    tool_selection: bool
    failure: bool

    def as_dict(self) -> dict:
        return asdict(self)


async def benchmark_candidate(registry: ProviderRegistry, provider_id: str, model_name: str) -> BenchmarkResult:
    started = time.perf_counter()
    try:
        response = await registry.chat(provider_id, model_name, [{"role": "user", "content": 'Reply with exactly JSON: {"ok":true}'}])
        elapsed = (time.perf_counter() - started) * 1000
        tokens_per_second = response.output_tokens / (response.latency_ms / 1000) if response.output_tokens and response.latency_ms else None
        return BenchmarkResult(provider_id, model_name, response.ttft_ms, elapsed, tokens_per_second, bool(response.content), response.content.startswith("{"), True, True, False)
    except Exception:
        return BenchmarkResult(provider_id, model_name, None, (time.perf_counter() - started) * 1000, None, False, False, False, False, True)
