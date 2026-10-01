"""Open a URL in the sandbox's Chromium (through the egress proxy) and print JSON {title, url, text}; save a screenshot.
Usage: python /opt/jarvis/browse.py <url> <screenshot_path>"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

url, shot = sys.argv[1], sys.argv[2]
proxy = os.environ.get("HTTPS_PROXY")
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
    page = b.new_page(viewport={"width": 1280, "height": 800})
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    page.screenshot(path=shot)
    text = page.evaluate("() => document.body ? document.body.innerText : ''")
    print(json.dumps({"title": page.title(), "url": page.url, "text": text[:20000]}))
    b.close()
