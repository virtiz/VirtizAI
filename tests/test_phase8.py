from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from virtizai_core.api import create_app
from virtizai_core.config import AppConfig
from virtizai_core.policy import CommunicationPolicy


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path / 'data', tmp_path / 'workspace', tmp_path / 'logs', tmp_path / 'data' / 'state.db')


def test_policy_controls_output_budget_and_events() -> None:
    assert CommunicationPolicy('minimal').output_token_budget() < CommunicationPolicy('detailed').output_token_budget()
    assert CommunicationPolicy(execution_updates='important_milestones').should_surface('tool_call') is False
    assert CommunicationPolicy(execution_updates='full_trace').should_surface('tool_call') is True


@pytest.mark.asyncio
async def test_preferences_inherit_and_override_without_llm_call(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        await client.put('/v1/preferences', json={'user_id':'u','response_verbosity':'concise','execution_updates':'silent','tool_details':'hidden'})
        await client.put('/v1/preferences', json={'user_id':'u','interface_type':'discord','response_verbosity':'minimal'})
        prefs = (await client.get('/v1/preferences/u', params={'interface_type':'discord'})).json()
        assert prefs['response_verbosity'] == 'minimal'
        assert (await client.get('/v1/dashboard')).json()['request_count'] == 0
