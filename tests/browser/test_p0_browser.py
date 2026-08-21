import os
import pytest
from playwright.sync_api import sync_playwright

BASE = os.environ.get("VIRTIZAI_WEBUI_URL", "http://127.0.0.1:8766")

@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        yield pg
        browser.close()

def test_live_registry_and_navigation(page):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    assert "VirtizAI" in page.title()
    assert "Provider A" not in page.locator("body").inner_text()
    page.locator("button").filter(has_text="Providers").click()
    page.wait_for_timeout(250)
    assert "Homelab Ollama" in page.locator("body").inner_text()
    page.locator("button").filter(has_text="Models").click()
    page.wait_for_timeout(250)
    assert "phi4-mini:latest" in page.locator("body").inner_text()
    page.locator("button").filter(has_text="Routing").click()
    page.wait_for_timeout(250)
    assert "Secretary" in page.locator("body").inner_text()

def test_provider_validation_and_responsive_layout(page):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.locator("button").filter(has_text="Providers").click()
    page.get_by_role("button", name="Add provider").click()
    page.get_by_role("button", name="Save and discover models").click()
    assert "required" in page.locator("body").inner_text().lower()
    for width in (768, 390):
        page.set_viewport_size({"width": width, "height": 844})
        page.reload(wait_until="domcontentloaded")
        assert page.evaluate("document.documentElement.scrollWidth") <= width + 2
