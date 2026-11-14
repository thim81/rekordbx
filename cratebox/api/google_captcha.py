"""One local MobileNetV2 attempt at a Google reCAPTCHA image challenge."""

# Adapted from noapi-google-search-mcp at fe5a37f05725200c4a718e0c3255d476772ef153.
# MIT License
#
# Copyright (c) 2025 Vincent Kaufmann
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import hashlib
import logging
import random
import re
import time
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import urlopen

from .config import STATE_DIRECTORY

logger = logging.getLogger(__name__)


class GoogleVerificationRequired(RuntimeError):
    """The Google challenge requires the user to complete verification."""


_MODEL_PATH = STATE_DIRECTORY / "models" / "mobilenetv2-12.onnx"
_MODEL_URL = (
    "https://media.githubusercontent.com/media/onnx/models/"
    "c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/classification/"
    "mobilenet/model/mobilenetv2-12.onnx"
)
_MODEL_SIZE = 13964571
_MODEL_SHA256 = "c0c3f76d93fa3fd6580652a45618618a220fced18babf65774ed169de0432ad5"
_WIDGET_TIMEOUT_MS = 8000
_MAX_IMAGE_ROUNDS = 30
_MAX_REPLACEMENTS_PER_TILE = 6
_NEXT_GRID_TIMEOUT_SECONDS = 4

_RECAPTCHA_IFRAME = (
    'iframe:is([src^="https://www.google.com/recaptcha/"], '
    '[src^="https://www.recaptcha.net/recaptcha/"])'
)
_TARGET_CLASSES = {
    "traffic light": (920,),
    "bus": (654, 779, 874),
    "bicycle": (444, 671),
    "motorcycle": (665, 670),
    "car": (436, 468, 511, 609, 656, 717, 751, 817),
    "taxi": (468,),
    "bridge": (839,),
    "boat": (472, 484, 554, 625, 814, 914),
    "airplane": (404, 405),
    "train": (466, 547, 820, 829),
    "truck": (555, 569, 656, 675, 717, 864, 867),
    "parking meter": (705,),
    "tractor": (866,),
}


def pause_before_interaction(page, *, minimum_ms: int = 200) -> None:
    page.wait_for_timeout(random.randint(minimum_ms, minimum_ms + 450))


@lru_cache(maxsize=1)
def _model_session():
    import onnxruntime

    if _MODEL_PATH.is_file() and _MODEL_PATH.stat().st_size == _MODEL_SIZE:
        model = _MODEL_PATH.read_bytes()
    else:
        model = b""
    if hashlib.sha256(model).hexdigest() != _MODEL_SHA256:
        with urlopen(_MODEL_URL, timeout=12) as response:
            model = response.read(_MODEL_SIZE + 1)
        if len(model) != _MODEL_SIZE or hashlib.sha256(model).hexdigest() != _MODEL_SHA256:
            raise ValueError("Unexpected MobileNetV2 model contents")
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=_MODEL_PATH.parent, delete=False) as staged:
            staged_path = Path(staged.name)
            try:
                staged.write(model)
                staged.close()
                staged_path.replace(_MODEL_PATH)
            finally:
                staged_path.unlink(missing_ok=True)
    return onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])


def _matching_tiles(screenshots, target_classes):
    import cv2
    import numpy as np

    session = _model_session()
    input_name = session.get_inputs()[0].name
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    matches = []
    for index, screenshot in enumerate(screenshots):
        image = cv2.imdecode(np.frombuffer(screenshot, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Could not decode a Google verification tile")
        image = cv2.resize(image, (224, 224))
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
        normalized = ((rgb - mean) / std).transpose(2, 0, 1)[None, ...]
        logits = session.run(None, {input_name: normalized})[0][0]
        probabilities = np.exp(logits - logits.max())
        probabilities /= probabilities.sum()
        if (
            set(probabilities.argsort()[-10:]).intersection(target_classes)
            or probabilities[list(target_classes)].max() > 0.05
        ):
            matches.append(index)
    return matches


def _loaded_grid_images(tiles):
    return tiles.evaluate_all("""tiles => tiles.map(tile => {
        const image = Array.from(tile.querySelectorAll('img')).at(-1);
        return image?.complete && image.naturalWidth ? image.currentSrc : null;
    })""")


def _wait_for_grid(page, iframe, tiles, is_ready):
    deadline = time.monotonic() + _NEXT_GRID_TIMEOUT_SECONDS
    while time.monotonic() < deadline and iframe.is_visible():
        images = _loaded_grid_images(tiles)
        if len(images) in {9, 16} and is_ready(images):
            return images
        page.wait_for_timeout(150)
    return None


def _select_matching_tiles(page, iframe, tiles, target_classes, replaces_tiles):
    positions = list(range(tiles.count()))
    images = _loaded_grid_images(tiles)
    if len(images) != len(positions) or not all(images):
        images = _wait_for_grid(page, iframe, tiles, lambda current: len(current) == len(positions) and all(current))
        if images is None:
            return None

    pending = {}
    replacement_counts = [0] * len(positions)
    while True:
        matches = _matching_tiles(
            (tiles.nth(index).screenshot(timeout=1500) for index in positions),
            target_classes,
        )
        if not matches and not any(replacement_counts):
            logger.warning("MobileNetV2 found no matching Google verification tiles")
            return None

        for match in matches:
            position = positions[match]
            if replaces_tiles:
                if replacement_counts[position] == _MAX_REPLACEMENTS_PER_TILE:
                    logger.warning("Automatic Google verification reached the replacement tile limit")
                    return None
                pending[position] = images[position]
                replacement_counts[position] += 1
            pause_before_interaction(page)
            tiles.nth(position).click(timeout=1500)
        if not pending:
            return images

        def ready_positions(current):
            if len(current) != len(replacement_counts):
                return []
            return [index for index, old in pending.items() if current[index] and current[index] != old]

        images = _wait_for_grid(page, iframe, tiles, ready_positions)
        if images is None:
            raise GoogleVerificationRequired("CrateBox stopped waiting for replacement tiles")
        positions = ready_positions(images)
        for position in positions:
            del pending[position]


def submit_challenge(page) -> bool:
    """Submit supported image grids. The caller checks whether results resume."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    if urlsplit(page.url).hostname != "www.google.com":
        return False
    step = "locating the verification checkbox"
    try:
        challenge = page.locator(f'{_RECAPTCHA_IFRAME}[src*="/bframe"]:visible')
        if not challenge.count():
            anchor = page.locator(f'{_RECAPTCHA_IFRAME}[src*="/anchor"]:visible')
            pause_before_interaction(page)
            anchor.first.content_frame.locator('#recaptcha-anchor[aria-checked="false"]').click(
                timeout=_WIDGET_TIMEOUT_MS,
            )
            try:
                challenge.first.wait_for(state="visible", timeout=_WIDGET_TIMEOUT_MS)
            except PlaywrightTimeout:
                # A checkbox can clear verification by itself. The caller waits for results.
                return True

        step = "opening the image challenge"
        iframe = challenge.first.element_handle(timeout=2000)
        frame = iframe.content_frame()
        if frame is None:
            return False

        for _ in range(_MAX_IMAGE_ROUNDS):
            step = "reading the image challenge"
            prompt = frame.locator(".rc-imageselect-desc, .rc-imageselect-desc-no-canonical, .rc-imageselect-desc-wrapper")
            category = " ".join(prompt.locator("strong").first.inner_text(timeout=_WIDGET_TIMEOUT_MS).lower().split())
            target_classes = next(
                (classes for name, classes in _TARGET_CLASSES.items() if re.fullmatch(f"{name}(?:s|es)?", category)),
                (),
            )
            if not target_classes:
                raise GoogleVerificationRequired(
                    f'CrateBox cannot automatically solve Google’s “{category[:80]}” challenge'
                )
            instructions = frame.locator("#rc-imageselect").inner_text(timeout=2000).lower()
            replaces_tiles = "none left" in instructions

            step = "reading the image grid"
            tiles = frame.locator("#rc-imageselect-target td.rc-imageselect-tile, .rc-imageselect-target td.rc-imageselect-tile")
            tiles.first.wait_for(state="visible", timeout=2000)
            if tiles.count() not in {9, 16}:
                logger.warning("Automatic Google verification encountered an unsupported grid size")
                return False
            step = "checking the image tiles"
            previous_images = _select_matching_tiles(
                page, iframe, tiles, target_classes, replaces_tiles,
            )
            if previous_images is None:
                return False

            step = "submitting the image challenge"
            pause_before_interaction(page)
            frame.locator("#recaptcha-verify-button").click(timeout=2000)
            step = "waiting for a fresh image grid"
            if not _wait_for_grid(page, iframe, tiles, lambda current: all(current) and current != previous_images):
                break
        return True
    except GoogleVerificationRequired:
        raise
    except PlaywrightTimeout:
        logger.warning("Automatic Google verification timed out while %s", step)
        return False
    except Exception:
        logger.warning("Could not run automatic Google verification", exc_info=True)
        return False
