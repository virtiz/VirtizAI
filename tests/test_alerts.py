import pytest
from virtizai_core.alerts import OperationalEventService
from virtizai_core.db import Database


@pytest.mark.asyncio
async def test_operational_events_suppress_initial_and_duplicates(tmp_path):
    db = Database(tmp_path / "state.db")
    db.open()
    events = OperationalEventService(db)
    seen = []
    async def listener(event):
        seen.append(event)
    events.subscribe(listener)
    assert await events.transition("provider", "p1", "Test", "healthy", initial=True) is not None
    assert await events.transition("provider", "p1", "Test", "healthy") is None
    event = await events.transition("provider", "p1", "Test", "unavailable", "timeout", "error")
    assert event["previous_state"] == "healthy"
    assert seen == [event]
    assert events.history()[0]["reason"] == "timeout"
