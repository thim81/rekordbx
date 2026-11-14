"""Google Images adapter using a dedicated local Chrome CDP session."""

import atexit
import json
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen
from threading import Lock

from .google_captcha import GoogleVerificationRequired, submit_challenge as _submit_captcha
from .google_captcha import pause_before_interaction

_SEARCH_LOCK = Lock()
_MIN_SEARCH_INTERVAL_SECONDS = 2.0
_LAST_SEARCH_STARTED = 0.0
_RESULT_CACHE_TTL_SECONDS = 600.0
_RESULT_CACHE_MAX_ITEMS = 32
_RESULT_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, str]]]] = {}
_GOOGLE_COOLDOWN_SECONDS = 60.0
_GOOGLE_COOLDOWN_UNTIL = 0.0
_CHROME_PROFILE = Path("/tmp/cratebox-chrome")
_DEVTOOLS_PORT_FILE = _CHROME_PROFILE / "DevToolsActivePort"
_CHROME_PROCESS: subprocess.Popen | None = None

_GOOGLE_INIT_SCRIPT = """
(() => {
    if (!["www.google.com", "consent.google.com", "www.recaptcha.net"].includes(location.hostname)) {
        return;
    }

    if (navigator.webdriver) {
        Object.defineProperty(navigator, "webdriver", { configurable: true, get: () => false });
    }
    if (!navigator.plugins.length) {
        const pdfViewer = { name: "PDF Viewer", filename: "internal-pdf-viewer",
                            description: "Portable Document Format" };
        const plugins = [pdfViewer];
        plugins.item = index => plugins[index] ?? null;
        plugins.namedItem = name => name === pdfViewer.name ? pdfViewer : null;
        Object.defineProperty(navigator, "plugins", { configurable: true, get: () => plugins });
    }
    window.chrome ??= {};
    window.chrome.runtime ??= {
        connect() {},
        sendMessage() {},
        onMessage: { addListener() {} },
    };
    delete window.__playwright;
    delete window.__pw_manual;
})();
"""


def _stop_owned_chrome() -> None:
    if _CHROME_PROCESS and _CHROME_PROCESS.poll() is None:
        _CHROME_PROCESS.terminate()


atexit.register(_stop_owned_chrome)


def _web_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
    except ValueError:
        return False


def _wait_for_search_interval() -> None:
    """Keep consecutive Google searches at least two seconds apart."""
    global _LAST_SEARCH_STARTED
    remaining = _MIN_SEARCH_INTERVAL_SECONDS - (time.monotonic() - _LAST_SEARCH_STARTED)
    if remaining > 0:
        time.sleep(remaining)
    _LAST_SEARCH_STARTED = time.monotonic()


def _human_verification_required(page) -> bool:
    """Recognize Google's human-verification interstitials without interacting with them."""
    if "/sorry/" in page.url.lower():
        return True
    for selector in (
        'iframe[title="Captcha"]',
        'iframe[src*="recaptcha"]',
        'form[action*="sorry"]',
        "#captcha-form",
    ):
        try:
            if page.locator(selector).count():
                return True
        except Exception:
            continue
    try:
        page_text = page.locator("body").inner_text(timeout=1500).lower()
    except Exception:
        return False
    return any(
        phrase in page_text
        for phrase in (
            "unusual traffic",
            "automated queries",
            "verify you are human",
            "verify you're human",
            "not a robot",
        )
    )


def search_google_images(query: str, *, limit: int = 24) -> list[dict[str, str]]:
    """Read Google Images results from a dedicated, visible Chrome CDP session."""
    global _GOOGLE_COOLDOWN_UNTIL
    cache_key = (query, limit)
    with _SEARCH_LOCK:
        now = time.monotonic()
        cached = _RESULT_CACHE.get(cache_key)
        if cached:
            expires_at, results = cached
            if now < expires_at:
                return [dict(item) for item in results]
            _RESULT_CACHE.pop(cache_key, None)

        if now < _GOOGLE_COOLDOWN_UNTIL:
            seconds = int(_GOOGLE_COOLDOWN_UNTIL - now + 0.999)
            raise GoogleVerificationRequired(
                f"Google recently asked for human verification. Complete it in the "
                f"CrateBox Chrome window, then retry in about {seconds} seconds."
            )

        try:
            results = _search_google_images(query, limit=limit)
        except GoogleVerificationRequired as exc:
            _GOOGLE_COOLDOWN_UNTIL = time.monotonic() + _GOOGLE_COOLDOWN_SECONDS
            raise GoogleVerificationRequired(
                f"{exc}. Complete the challenge in the "
                "CrateBox Chrome window, then retry in about 60 seconds."
            ) from exc

        _RESULT_CACHE[cache_key] = (time.monotonic() + _RESULT_CACHE_TTL_SECONDS, results)
        if len(_RESULT_CACHE) > _RESULT_CACHE_MAX_ITEMS:
            _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
        return [dict(item) for item in results]


def _search_google_images(query: str, *, limit: int) -> list[dict[str, str]]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

    search_url = "https://www.google.com/search?" + urlencode({"udm": "2", "hl": "en", "q": query})
    results = []
    seen = set()
    with sync_playwright() as playwright:
        _wait_for_search_interval()
        endpoint = _ensure_chrome()
        browser = playwright.chromium.connect_over_cdp(
            endpoint, no_defaults=True, timeout=10000
        )
        with ExitStack() as cleanup:
            cleanup.callback(browser.close)
            context = browser.contexts[0]
            page = next(
                (tab for tab in context.pages if urlsplit(tab.url).hostname == "www.google.com"),
                None,
            ) or next(
                (tab for tab in context.pages if tab.url in {"about:blank", "chrome://newtab/"}),
                None,
            ) or context.new_page()
            script = page.add_init_script(_GOOGLE_INIT_SCRIPT)
            if script is not None:
                cleanup.enter_context(script)
            _set_chrome_window_state(page, "minimized")
            pause_before_interaction(page)
            page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            reject_cookies = page.get_by_role("button", name="Reject all", exact=True)
            if reject_cookies.count() and reject_cookies.is_visible():
                pause_before_interaction(page)
                reject_cookies.click(timeout=3000)

            cards = page.locator('[role="button"]:has(img[src*="encrypted-tbn"])')
            challenged = _human_verification_required(page)
            if not challenged:
                try:
                    cards.first.wait_for(state="visible", timeout=12000)
                except PlaywrightTimeout as exc:
                    challenged = _human_verification_required(page)
                    if not challenged:
                        title = page.title()
                        page_text = " ".join(page.locator("body").inner_text(timeout=2000).split())[:240]
                        raise RuntimeError(
                            f"Google Images cards unavailable (title={title!r}, cards={cards.count()}, "
                            f"images={page.locator('img').count()}, page_text={page_text!r})"
                        ) from exc

            if challenged:
                # Chrome pauses animation frames in hidden tabs, which blocks Playwright's stability checks.
                _set_chrome_window_state(page, "normal")
                page.bring_to_front()
                if not _submit_captcha(page):
                    raise GoogleVerificationRequired("Google still requires human verification")
                try:
                    cards.first.wait_for(state="visible", timeout=8000)
                except PlaywrightTimeout as exc:
                    raise GoogleVerificationRequired(
                        "Google Images did not resume after automatic verification"
                    ) from exc
                _set_chrome_window_state(page, "minimized")

            for _ in range(3):
                if cards.count() >= limit:
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pause_before_interaction(page, minimum_ms=400)

            for index in range(cards.count()):
                card = cards.nth(index)
                image = card.locator('img[src*="encrypted-tbn"]').first
                image_url = image.get_attribute("src") or ""
                title = (image.get_attribute("alt") or image.get_attribute("title") or "").strip()
                if not title or not _web_url(image_url) or urlsplit(image_url).scheme != "https":
                    continue
                if image_url in seen:
                    continue

                results.append({
                    "title": title[:300],
                    "image_url": image_url,
                    "thumbnail_url": image_url,
                    "source_url": search_url,
                    "source": "Google Images",
                })
                seen.add(image_url)
                if len(results) >= limit:
                    break
    return results


def _ensure_chrome() -> str:
    """Connect to the dedicated CDP profile, starting Chrome when needed."""
    global _CHROME_PROCESS

    endpoint = _existing_chrome_endpoint()
    if endpoint:
        return endpoint

    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        raise RuntimeError("Google Chrome is required for Google Images search")

    _CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    _CHROME_PROCESS = subprocess.Popen(
        [
            str(chrome),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={_CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-startup-window",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        # Chrome may forward the launch to an existing profile owner and exit.
        exit_code = _CHROME_PROCESS.poll()
        endpoint = _existing_chrome_endpoint()
        if endpoint:
            return endpoint
        if exit_code is not None:
            raise RuntimeError(f"Could not start the dedicated Google Chrome browser (exit code {exit_code})")
        time.sleep(0.25)
    raise RuntimeError("Timed out starting the dedicated Google Chrome browser")


def _existing_chrome_endpoint() -> str | None:
    try:
        port = int(_DEVTOOLS_PORT_FILE.read_text(encoding="ascii").splitlines()[0])
        endpoint = f"http://127.0.0.1:{port}"
        with urlopen(f"{endpoint}/json/version", timeout=1) as response:
            details = json.loads(response.read(4096))
        return endpoint if str(details.get("Browser", "")).startswith("Chrome/") else None
    except (URLError, TimeoutError, OSError, ValueError, IndexError, json.JSONDecodeError):
        return None


def _set_chrome_window_state(page, state: str) -> None:
    session = page.context.new_cdp_session(page)
    try:
        window = session.send("Browser.getWindowForTarget")
        session.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": state}},
        )
    finally:
        session.detach()
