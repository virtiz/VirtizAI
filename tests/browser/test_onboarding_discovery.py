import os
from playwright.sync_api import sync_playwright
BASE=os.environ.get("VIRTIZAI_WEBUI_URL","http://127.0.0.1:8766")
def test_real_readiness_and_discord_discovery_limitation():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True); page=b.new_page(viewport={"width":1280,"height":900})
        page.goto(BASE+"/?wizard=1",wait_until="domcontentloaded")
        page.locator("#wizard").wait_for(state="visible")
        assert "Admin setup" in page.locator("#wizard").inner_text()
        page.get_by_role("button",name="Close setup").click()
        page.locator("button[data-view=integrations]").click(); page.wait_for_timeout(400)
        assert page.locator("#discord-guild-select").count()==1
        assert page.locator("#discord-status").inner_text().startswith("Gateway:")
        b.close()