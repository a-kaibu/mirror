"""Opt-in live browser check: HF bytes, Unicode filename, Pages subpath, no-JS links.

uv run --isolated --locked --with playwright python -m playwright install chromium firefox
uv run --isolated --locked --with playwright python tests/browser_e2e.py
"""
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archiver as a
from catalog import build_site
from playwright.sync_api import sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


PUBLIC_SHA256 = "5cbb88fc63ccd583edf6ac52eda55d3df00025eced899ae64e584436f1d08b5d"
PUBLIC_SIZE = 93635


def run():
    # Use a real archived patch; the bucket intentionally has no test artifacts.
    key = "files/" + PUBLIC_SHA256
    url = a.public_url("vn-archive", "mirrors", key, True)
    with urlopen(url, timeout=30) as response:
        payload = response.read(PUBLIC_SIZE + 1)
    assert len(payload) == PUBLIC_SIZE, "public patch size changed"
    assert hashlib.sha256(payload).hexdigest() == PUBLIC_SHA256, "public patch checksum changed"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = a.Store(root / "archive")
        sha = hashlib.sha256(payload).hexdigest()
        filename = "日本語の体験版 (検証用).zip"
        store.add({"id": "test", "site": "lathercraft", "brand": "保存動作の検証", "title": "体験版の保存テスト",
            "filename": filename, "release_name": "体験版", "page_url": url, "mirrors": [url]})
        store.update("test", status="verified", sha256=sha, size=len(payload), remote_key="vn-archive/mirrors/"+key, remote_sha256=sha, remote_verified_at=a.now())
        build_site(store, root / "public" / "project")
        store.db.close()
        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args, **kw: QuietHandler(*args, directory=str(root / "public"), **kw))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/project/"
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get("BROWSERS", "chromium,firefox").split(","):
                    executable = os.environ.get(engine.upper() + "_EXECUTABLE")
                    # Firefox disables service workers in private contexts. Test
                    # normal browsing with a disposable persistent profile.
                    context = getattr(pw, engine).launch_persistent_context(str(root / engine), accept_downloads=True, **({"executable_path": executable} if executable else {}))
                    browser = context.browser
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("console", lambda msg: print(engine, "console:", msg.text) if msg.type == "error" else None)
                    page.goto(base)
                    page.get_by_role("link", name="保存動作の検証", exact=True).click()
                    try:
                        page.wait_for_function("document.querySelector('a.download').href.includes('/project/download/')", timeout=10000)
                    except Exception:
                        print(engine, "SW state:", page.evaluate("async () => ({secure: isSecureContext, support: 'serviceWorker' in navigator, controller: navigator.serviceWorker?.controller?.scriptURL, registrations: 'serviceWorker' in navigator ? (await navigator.serviceWorker.getRegistrations()).map(r => ({scope:r.scope, active:r.active?.state, waiting:r.waiting?.state, installing:r.installing?.state})) : []})"))
                        print("page errors:", errors)
                        raise
                    page.get_by_role("searchbox").fill("存在しない作品")
                    assert page.locator("[data-entry]:visible").count() == 0
                    page.get_by_role("searchbox").fill("体験版")
                    assert page.locator("[data-entry]:visible").count() == 1
                    with page.expect_download(timeout=60000) as pending:
                        page.get_by_role("link", name="ダウンロード", exact=True).click()
                    download = pending.value
                    assert download.suggested_filename == filename, download.suggested_filename
                    assert download.failure() is None, (download.failure(), download.url)
                    assert Path(download.path()).read_bytes() == payload
                    page.screenshot(path=f"/tmp/vn-catalog-{engine}.png", full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=f"/tmp/vn-catalog-{engine}-mobile.png", full_page=True)
                    assert not errors, errors
                    nojs = browser.new_context(java_script_enabled=False)
                    page = nojs.new_page()
                    page.goto(base)
                    page.get_by_role("link", name="保存動作の検証", exact=True).click()
                    assert page.get_by_role("link", name="ダウンロード", exact=True).get_attribute("href") == url
                    nojs.close()
                    context.close()
                    print(f"{engine}: Unicode filename, exact HF bytes, filters, 390px, no-JS links OK")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    run()
