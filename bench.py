from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = AppConfig(root / "data", root / "workspace", root / "logs", root / "data" / "state.db")
        app = create_app(config)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = (await client.post("/v1/sessions", json={"user_id": "bench"})).json()["session_id"]
            samples = []
            for _ in range(100):
                started = time.perf_counter()
                response = await client.post(
                    f"/v1/sessions/{session_id}/messages",
                    json={"user_id": "bench", "content": "hello"},
                )
                response.raise_for_status()
                samples.append((time.perf_counter() - started) * 1000)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        print(f"samples={len(samples)} median_ms={statistics.median(samples):.3f} p95_ms={p95:.3f} min_ms={samples[0]:.3f} max_ms={samples[-1]:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
