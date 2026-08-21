import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("VIRTIZAI_WEBUI_URL", "http://127.0.0.1:8766")


def send(page, text):
    page.locator("textarea[aria-label=Message]").fill(text)
    page.get_by_role("button", name="Send").click()
    page.locator(".message.pending").wait_for(state="detached", timeout=15000)


def test_chat_lifecycle():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.locator("button[data-view=chat]").click()
        page.get_by_role("button", name="New Chat +").click()
        send(page, "What is your current routing setup?")
        first = page.locator(".message:not(.user):not(.pending)").last.inner_text()
        assert "routing" in first.lower()
        sid = page.locator(".session-item").first.get_attribute("data-session-id")
        assert sid
        page.reload(wait_until="domcontentloaded")
        page.locator(".message:not(.user):not(.pending)").last.wait_for(timeout=10000)
        assert first.split("\n")[0] in page.locator(".chat-messages").inner_text()
        send(page, "Which model answered that last message?")
        assert "introspection" in page.locator(".chat-messages").inner_text().lower()
        title = "Browser acceptance unique " + str(__import__("time").time_ns())
        page.once("dialog", lambda dialog: dialog.accept(title))
        page.get_by_role("button", name="Rename").click()
        page.wait_for_timeout(300)
        assert title in page.locator("#session-list").inner_text()
        page.locator("#session-search").fill(title)
        page.wait_for_timeout(500)
        assert title in page.locator("#session-list").inner_text()
        page.get_by_role("button", name="Archive", exact=True).click()
        page.wait_for_timeout(500)
        assert title not in page.locator("#session-list").inner_text()
        page.get_by_role("button", name="Archived").click()
        page.wait_for_timeout(500)
        assert title in page.locator("#session-list").inner_text()
        page.get_by_role("button", name="New Chat +").click()
        send(page, "What is your current routing setup?")
        assert page.locator(".session-item").count() >= 1
        browser.close()
