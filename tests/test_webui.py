from pathlib import Path
INDEX = Path(__file__).parents[1] / "webui" / "index.html"
APP = Path(__file__).parents[1] / "webui" / "app.js"

def test_webui_uses_shared_live_app_and_no_demo_registry():
    index = INDEX.read_text()
    app = APP.read_text()
    assert "/static/app.js" in index
    assert "Provider A" not in app
    assert "Model A" not in app
    assert "/v1/providers" in app
    assert "/v1/models" in app
    assert "/v1/routes" in app
    assert "/v1/interfaces/message" in app

def test_webui_exposes_real_setup_actions():
    app = APP.read_text()
    assert "Save and discover models" in app or "Save, test, discover" in app
    assert "/discover" in app
    assert "route-target-select" in app or "data-route-model" in app
    assert "No demo providers are created" in app


def test_webui_chat_uses_persistent_shared_sessions():
    app = APP.read_text()
    assert "New Chat" in app
    assert "/v1/sessions" in app
    assert "include_archived" in app
    assert "archive-session" in app
    assert "rename-session" in app
    assert "/v1/interfaces/identity" in app
    assert "localStorage.getItem('virtizai.webui.session')" in app

def test_webui_selectedRouteId_state_exists():
    """selectedRouteId state variable exists for explicit route selection."""
    app = APP.read_text()
    assert 'selectedRouteId' in app

def test_webui_routing_view_has_selectable_route_list():
    """Routing view renders all routes as selectable items via data-select-route."""
    app = APP.read_text()
    assert 'data-select-route' in app

def test_webui_selectRoute_assigns_selectedRouteId():
    """selectRoute(routeId) assigns selectedRouteId=routeId."""
    app = APP.read_text()
    assert 'selectRoute(routeId)' in app
    assert 'selectedRouteId=routeId' in app

def test_webui_routing_no_routes_zero_fallback():
    """Routing view does NOT auto-select routes[0] when no Secretary exists."""
    app = APP.read_text()
    # The routing section in refreshLiveData must not contain routes[0] fallback
    assert 'secRoute ? secRoute : routes[0]' not in app
    assert 'secRoute?secRoute:routes[0]' not in app

def test_webui_routing_show_select_state_when_no_secretary():
    """When no selectedRouteId and no Secretary route, routing shows 'Select a route' state."""
    app = APP.read_text()
    assert 'Select a route above to edit it' in app

def test_webui_saveRoute_PUT_uses_selectedRouteId():
    """saveRoute PUT URL uses selectedRouteId, not liveRouteId."""
    app = APP.read_text()
    assert '/v1/routes/${selectedRouteId}' in app or "/v1/routes/'+selectedRouteId" in app

def test_webui_deleteRoute_clears_selectedRouteId():
    """deleteRoute calls DELETE /v1/routes/{selectedRouteId} and clears selectedRouteId on success."""
    app = APP.read_text()
    assert "method:'DELETE'" in app or 'method: \'DELETE\'' in app
    # deleteRoute must clear selectedRouteId on success
    assert 'selectedRouteId=null' in app

def test_webui_routing_editor_displays_selected_route_identity():
    """Route editor displays selected route's role_name or role_id."""
    app = APP.read_text()
    assert 'selectedRoute.role_name' in app or 'selectedRoute.role_id' in app

def test_webui_create_route_role_selector_rendered():
    """New-route role select is rendered in the routing/create UI."""
    app = APP.read_text()
    assert 'id="new-route-role-select"' in app



def test_webui_create_route_sends_selected_role_id():
    """POST /v1/routes sends the role selected by its canonical ID."""
    app = APP.read_text()
    assert "const roleId=document.querySelector('#new-route-role-select')?.value||'role-secretary'" in app
    assert "role=roles.find(r=>r.id===roleId)" in app

def test_webui_create_route_no_roles_zero_fallback():
    """createRoute does not use roles[0] as fallback."""
    app = APP.read_text()
    # The createRoute function must not reference roles[0].id
    assert 'roles[0].id' not in app

def test_webui_onboarding_uses_role_secretary():
    """Onboarding canonical Secretary path uses role-secretary."""
    app = APP.read_text()
    # wizardAction must use secretary lookup with role-secretary fallback, not roles[0]
    assert "role_id:roles.find(r=>r.name==='secretary')?.id||'role-secretary'" in app


def test_webui_edit_route_sends_selected_role_and_existing_targets():
    app = APP.read_text()
    assert "#edit-route-role-select" in app
    assert "targetSelect?.selectedOptions" in app
    assert "role_id:document.querySelector('#edit-route-role-select').value" in app

def test_webui_chat_sidebar_uses_role_secretary_only():
    """Chat sidebar: if showing Secretary state, uses role-secretary lookup."""
    app = APP.read_text()
    assert 'role_secretary' in app or "r=>r.role_id==='role-secretary'" in app
