import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("VIRTIZAI_WEBUI_URL", "http://127.0.0.1:8766")

def test_agents_workers_jobs_environment_navigation_and_responsive():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1280,"height":900})
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.get_by_role("button", name="Agents / Roles").click()
        page.wait_for_timeout(250)
        assert "Agents" in page.locator("body").inner_text()
        page.get_by_role("button", name="Workers").click(); page.wait_for_timeout(250)
        assert "Codex CLI" in page.locator("body").inner_text()
        page.get_by_role("button", name="Jobs").click(); page.wait_for_timeout(250)
        assert "Jobs" in page.locator("body").inner_text()
        page.get_by_role("button", name="Environment").click(); page.wait_for_timeout(250)
        assert "Environment" in page.locator("body").inner_text()
        for width in (768,390):
            page.set_viewport_size({"width":width,"height":844})
            page.reload(wait_until="domcontentloaded")
            assert page.evaluate("document.documentElement.scrollWidth") <= width + 2
        browser.close()

def test_projects_customer_flow(page=None):
    # Kept as a lightweight browser smoke test; project association API is covered separately.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width":1280,"height":900})
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.get_by_role("button", name="Projects").click(); pg.wait_for_timeout(300)
        assert "Projects" in pg.locator("body").inner_text()
        pg.get_by_role("button", name="Add project").click()
        pg.locator("#data-first").fill("Browser QA Disposable")
        pg.locator("#data-second").fill("Customer journey")
        pg.locator("#data-third").fill("")
        pg.get_by_role("button", name="Save").click(); pg.wait_for_timeout(350)
        assert "Browser QA Disposable" in pg.locator("body").inner_text()
        pg.get_by_role("button", name="Open →").click(); pg.wait_for_timeout(250)
        assert "Chats:" in pg.locator("body").inner_text()
        pg.evaluate("async()=>{const p=await fetch('/v1/projects').then(r=>r.json()); const x=p.find(i=>i.name==='Browser QA Disposable'); if(x) await fetch('/v1/projects/'+x.id,{method:'DELETE'})}")
        browser.close()
