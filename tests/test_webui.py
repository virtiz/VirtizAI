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
