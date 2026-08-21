import os
from playwright.sync_api import sync_playwright
BASE = os.environ.get("VIRTIZAI_WEBUI_URL", "http://127.0.0.1:8766")
def open_view(page, name):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    if page.viewport_size["width"] <= 760:
        page.get_by_role("button", name="Open navigation").click()
    page.locator(f"button[data-view={name}]").click()
    page.wait_for_timeout(500)
def test_settings_persist_and_secret_safety():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        open_view(page, "settings")
        assert "Communication" in page.locator("body").inner_text()
        page.locator('[data-setting="response"] .segment[data-value="detailed"]').click()
        page.locator('[data-setting="updates"] .segment[data-value="full_trace"]').click()
        page.get_by_role("button", name="Save changes").click()
        page.locator("#settings-status").filter(has_text="Saved").wait_for(timeout=5000)
        page.reload(wait_until="domcontentloaded")
        page.locator("button[data-view=settings]").click()
        page.locator('[data-setting="response"] .segment[data-value="detailed"].selected').wait_for(timeout=5000)
        assert page.locator('[data-setting="updates"] .segment[data-value="full_trace"].selected').count() == 1
        page.locator('[data-setting="response"] .segment[data-value="normal"]').click()
        page.locator('[data-setting="updates"] .segment[data-value="important_milestones"]').click()
        page.get_by_role("button", name="Save changes").click()
        page.locator("#settings-status").filter(has_text="Saved").wait_for(timeout=5000)
        open_view(page, "integrations")
        assert page.locator("#discord-token").input_value() == ""
        assert "synthetic-token" not in page.locator("body").inner_text()
        assert "Gateway:" in page.locator("#discord-status").inner_text()
        browser.close()
def test_settings_integrations_responsive():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 844})
        for width in (1280, 768, 390):
            page.set_viewport_size({"width": width, "height": 844})
            open_view(page, "settings")
            assert page.evaluate("document.documentElement.scrollWidth") <= width + 2
            open_view(page, "integrations")
            assert page.evaluate("document.documentElement.scrollWidth") <= width + 2
        browser.close()