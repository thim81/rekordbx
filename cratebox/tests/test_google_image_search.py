import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from playwright.sync_api import TimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import google_captcha, google_image_search


def test_chrome_startup_preserves_discovery_file_if_endpoint_is_temporarily_unavailable(monkeypatch, tmp_path):
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/existing\n", encoding="ascii")
    monkeypatch.setattr(google_image_search, "_CHROME_PROFILE", tmp_path)
    monkeypatch.setattr(google_image_search, "_DEVTOOLS_PORT_FILE", port_file)
    monkeypatch.setattr(google_image_search, "_CHROME_PROCESS", None)

    endpoint = "http://127.0.0.1:9222"
    discover = MagicMock(side_effect=[None, endpoint])
    monkeypatch.setattr(google_image_search, "_existing_chrome_endpoint", discover)

    def launch(*_args, **_kwargs):
        assert port_file.read_text(encoding="ascii") == "9222\n/devtools/browser/existing\n"
        process = MagicMock()
        process.poll.return_value = 0  # Chrome forwarded startup to the existing profile owner.
        return process

    monkeypatch.setattr(google_image_search.subprocess, "Popen", launch)

    assert google_image_search._ensure_chrome() == endpoint


def test_captcha_waits_for_checkbox_to_initialize():
    page = MagicMock()
    page.url = "https://www.google.com/sorry/index"
    challenge, anchor = MagicMock(), MagicMock()
    page.locator.side_effect = [challenge, anchor]
    challenge.count.return_value = 0
    checkbox = anchor.first.content_frame.locator.return_value
    clicked = False

    def click_when_ready(*, timeout):
        nonlocal clicked
        if timeout < 4000:
            raise TimeoutError("Checkbox is still initializing")
        clicked = True

    checkbox.click.side_effect = click_when_ready
    challenge.first.wait_for.side_effect = TimeoutError("Checkbox cleared without an image grid")

    assert google_captcha.submit_challenge(page)
    assert clicked


def test_captcha_classifies_a_fresh_grid_after_submitting(monkeypatch):
    page = MagicMock()
    page.url = "https://www.google.com/sorry/index"
    challenge = page.locator.return_value
    challenge.count.return_value = 1
    iframe = challenge.first.element_handle.return_value
    frame = iframe.content_frame.return_value
    challenge.first.content_frame = frame
    prompt, instructions, tiles, verify = [MagicMock() for _ in range(4)]
    frame.locator.side_effect = [prompt, instructions, tiles, verify] * 2
    prompt.locator.return_value.first.inner_text.side_effect = ["bicycles", "tractors"]
    instructions.inner_text.return_value = "If there are none, click skip"
    tiles.count.return_value = 16
    tiles.evaluate_all.side_effect = [["first"] * 16, ["second"] * 16, ["second"] * 16]
    iframe.is_visible.side_effect = [True, False]
    classifications = []

    def classify(_screenshots, target_classes):
        classifications.append(target_classes)
        return [3] if len(classifications) == 1 else [7]

    monkeypatch.setattr(google_captcha, "_matching_tiles", classify)

    assert google_captcha.submit_challenge(page)
    assert classifications == [(444, 671), (866,)]
    assert tiles.nth.call_args_list == [call(3), call(7)]
    assert verify.click.call_count == 2


def test_captcha_checks_replacement_tiles_before_verifying(monkeypatch):
    page = MagicMock()
    page.url = "https://www.google.com/sorry/index"
    challenge = page.locator.return_value
    challenge.count.return_value = 1
    iframe = challenge.first.element_handle.return_value
    frame = iframe.content_frame.return_value
    prompt, instructions, tiles, verify = [MagicMock() for _ in range(4)]
    frame.locator.side_effect = [prompt, instructions, tiles, verify]
    prompt.locator.return_value.first.inner_text.return_value = "bus"
    instructions.inner_text.return_value = "Click verify once there are none left."
    tiles.count.return_value = 9
    initial = ["original"] * 9
    replacement = initial.copy()
    replacement[4] = "replacement"
    waiting = replacement.copy()
    waiting[5] = None
    loaded = replacement.copy()
    loaded[5] = "later replacement"
    tiles.evaluate_all.side_effect = [initial, waiting, loaded]
    iframe.is_visible.side_effect = [True, True, False]
    screened = []

    def classify(screenshots, _target_classes):
        screened.append(len(list(screenshots)))
        return [4, 5] if len(screened) == 1 else []

    monkeypatch.setattr(google_captcha, "_matching_tiles", classify)

    assert google_captcha.submit_challenge(page)
    assert screened == [9, 1, 1]
    verify.click.assert_called_once()


@pytest.mark.parametrize("challenge", ["early", "late", "unsolved", "rejected", "crosswalks"])
def test_google_image_search_handles_verification(monkeypatch, challenge):
    playwright = MagicMock()
    browser = playwright.__enter__.return_value.chromium.connect_over_cdp.return_value
    page = browser.contexts[0].new_page.return_value
    browser.contexts[0].pages = []
    page.url = "https://www.google.com/sorry/" if challenge != "late" else "https://www.google.com/search"
    page.get_by_role.return_value.count.return_value = 0
    cards = page.locator.return_value
    cards.count.return_value = 1
    image = cards.nth.return_value.locator.return_value.first
    image.get_attribute.side_effect = {"src": "https://example.com/thumbnail", "alt": "Cover"}.get

    def wait_for_cards(**_kwargs):
        if page.url.endswith("/search") and challenge == "late":
            page.url = "https://www.google.com/sorry/"
        if page.url.endswith("/sorry/"):
            raise TimeoutError("Google displayed a challenge")

    cards.first.wait_for.side_effect = wait_for_cards
    attempts = []
    window_state = "normal"
    active = False
    startup_script_registered = False

    def send(method, params=None):
        nonlocal window_state
        if method == "Browser.getWindowForTarget":
            return {"windowId": 42}
        if method == "Browser.setWindowBounds":
            window_state = params["bounds"]["windowState"]

    def activate_tab():
        nonlocal active
        active = True

    page.context.new_cdp_session.return_value.send.side_effect = send
    page.bring_to_front.side_effect = activate_tab

    def register_startup_script(_source):
        nonlocal startup_script_registered
        startup_script_registered = True

    page.add_init_script.side_effect = register_startup_script

    def navigate(*_args, **_kwargs):
        assert startup_script_registered

    page.goto.side_effect = navigate

    def submit_challenge(challenge_page):
        attempts.append(challenge_page)
        if window_state != "normal" or not active:
            return False
        if challenge == "unsolved":
            return False
        if challenge == "crosswalks":
            frame = MagicMock()
            challenge_page.locator.return_value.count.return_value = 1
            challenge_page.locator.return_value.first.element_handle.return_value.content_frame.return_value = frame
            frame.locator.return_value.locator.return_value.first.inner_text.return_value = "crosswalks"
            return google_captcha.submit_challenge(challenge_page)
        if challenge != "rejected":
            page.url = "https://www.google.com/search?q=cover"
        return True

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: playwright)
    monkeypatch.setattr(google_image_search, "_ensure_chrome", lambda: "http://localhost:1234")
    monkeypatch.setattr(google_image_search, "_wait_for_search_interval", lambda: None)
    monkeypatch.setattr(google_image_search, "_human_verification_required", lambda tab: "/sorry/" in tab.url)
    monkeypatch.setattr(google_image_search, "_submit_captcha", submit_challenge)
    monkeypatch.setattr(google_image_search, "_RESULT_CACHE", {})
    monkeypatch.setattr(google_image_search, "_GOOGLE_COOLDOWN_UNTIL", 0)

    needs_manual_verification = challenge in {"unsolved", "rejected", "crosswalks"}
    if needs_manual_verification:
        with pytest.raises(google_image_search.GoogleVerificationRequired, match="Complete the challenge") as error:
            google_image_search.search_google_images("cover", limit=1)
        if challenge == "crosswalks":
            assert "crosswalks" in str(error.value)
    else:
        results = google_image_search.search_google_images("cover", limit=1)
        assert [item["title"] for item in results] == ["Cover"]
    assert attempts == [page]
    assert active
    assert window_state == ("normal" if needs_manual_verification else "minimized")
