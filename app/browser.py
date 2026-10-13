import asyncio
import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from app.config import BrowserConfig
from app.screenshots import save_page_screenshot
from app.inspection_cache import cache_key, capture, restore
from app.models import BrowserStep
from app.inspection import TargetAbsent, AmbiguousTarget, CHECK_CONTROL_JS, EXACT_LABEL_JS, IMAGE_SOURCE_JS, TranslationNeeded, VisualTarget, VisualTargets
from app.targeting import exact_target, QUOTED_TARGET
from app.privacy import SENSITIVE, redact, secret_values
from app.runner import missing_variables, resolve, target_url


class BrowserUnavailable(RuntimeError):
    """A browser session could not be created."""


class TargetUnavailable(RuntimeError):
    """A visual target could not be prepared; no mouse event was dispatched."""


class ImageReadUnavailable(RuntimeError):
    """An image could not be transcribed for a fill action."""


class InvalidNavigation(ValueError):
    """A navigation URL failed application-owned validation."""


def browser_target_url(base_url: str, path: str) -> str:
    try:
        target_url(base_url, "/")
        if not isinstance(path, str) or not path or any(c.isspace() or ord(c) < 32 for c in path):
            raise ValueError("goto requires a URL or path without whitespace or control characters")
        base, destination = urlsplit(base_url), urlsplit(path)
        if destination.scheme:
            if (destination.scheme, destination.netloc) != (base.scheme, base.netloc):
                raise ValueError("goto URL must use the same origin as base_url")
            if destination.username or destination.password or "\\" in path:
                raise ValueError("goto URL must not contain embedded credentials or backslashes")
            return path
        return target_url(base_url, path)
    except ValueError as exc:
        # Only application-owned messages are exposed, never the URL or parser exception.
        raise InvalidNavigation(
            "Invalid browser navigation. Use a path starting with / or a full HTTP(S) URL on the "
            "same origin as base_url, without embedded credentials, whitespace or backslashes. "
            "base_url must not contain query parameters or a fragment."
        ) from exc


@dataclass
class ConfirmedTarget(VisualTarget):
    entry: dict | None = None


class SelectorNeeded(Exception):
    def __init__(self, key: str, target: str, candidates=()):
        question = f"I could not uniquely identify {target}. Describe its visible label, location, or nearby text (for example, the button next to XXX in the middle of the screen)."
        if candidates:
            question = "Which target do you mean? " + "; ".join(candidates) + ". Reply with its label and location, or describe it in your own words."
        self.question = {"key": key, "question": question, "secret": False, "inspection": True}
        if candidates:
            self.question["choices"] = list(candidates)


@dataclass
class Session:
    driver: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    image_values: dict[int, str] = field(default_factory=dict)

    async def close(self):
        for resource, method in ((self.context, "close"), (self.browser, "close"), (self.driver, "stop")):
            if resource is not None:
                with suppress(Exception):
                    await getattr(resource, method)()


class BrowserRunner:
    def __init__(self, settings: BrowserConfig, artifacts: Path, resolver=None, log=None):
        self.settings = settings
        self.artifacts = artifacts
        self.sessions: dict[str, Session] = {}
        self.lock = asyncio.Lock()
        self.expect = None
        self.resolver = resolver
        self.log = log
        self.store = None

    async def _selector(self, page, item, context, step_title, position, run_id, secrets,
                        dom_only=False, excluded_attempts=(), optional=False):
        selector = item.selector
        missing = missing_variables(selector, context) if selector else []
        if selector and not missing and not re.fullmatch(r"{{\s*[^{}]+\s*}}", selector):
            return str(resolve(selector, context))
        placeholder = re.fullmatch(r"{{\s*([^{}]+?)\s*}}", selector or "")
        if placeholder:
            missing = [placeholder.group(1)]
        target = item.target or (missing[0].replace("_", " ") if missing else step_title)
        operation = getattr(item, "action", None) or item.type
        suffix = hashlib.sha256(f"{step_title}:{position}:{target}".encode()).hexdigest()[:12]
        key = missing[0] if missing else f"selector_{suffix}"
        if isinstance(context.get(key), dict) and context[key].get("kind") == "inspection_rectangle":
            entry = context[key].get("entry", {})
            try:
                restored = await restore(page, entry, None)
            except Exception:
                restored = None
            if not isinstance(restored, VisualTarget):
                raise SelectorNeeded(key, target + " (the saved selection has moved; locate it again)")
            if operation == "click":
                return ConfirmedTarget(restored.selector, restored.x, restored.y, entry=entry)
            return restored.selector
        if context.get(key):
            answer = str(context[key]).strip()
            # Keep explicitly supplied selectors compatible; ordinary replies are target descriptions.
            if answer.startswith(("#", ".", "css=", "xpath=", "text=", "[")):
                return answer
            target += ". User clarification: " + answer
        translation_scope = hashlib.sha256(f"{step_title}:{target}".encode()).hexdigest()[:12]
        translation_key = f"translation_{translation_scope}"
        translation = context.get(translation_key)
        source = exact_target(target)
        translation_decided = translation == "reject"
        if (isinstance(translation, dict) and translation.get("source") == source
                and isinstance(translation.get("translation"), str) and translation["translation"].strip()):
            label = translation["translation"]
            # Replace just the quoted target; preserve role/location context and all literal assertions.
            # Labels containing quote delimiters need a direct selector or further clarification.
            if not any(char in label for char in '\"“”‘’'):
                target = QUOTED_TARGET.sub(lambda _: f'"{label}"', target, count=1)
                translation_decided = True
        if self.settings.auto_selectors and self.resolver:
            inspection_key = None
            if self.store:
                inspection_key = cache_key(page, target, operation, step_title, position, self.settings.vision)
                entry = await self.store.inspection(run_id, inspection_key)
                if entry and not dom_only:
                    try:
                        cached = await restore(page, entry, exact_target(target))
                    except Exception:
                        cached = None
                    if cached:
                        if self.log:
                            await self.log(run_id, "progress", "Reusing a saved element inspection.")
                        return cached
                if entry:
                    await self.store.save_inspection(run_id, inspection_key, None)
            if self.log:
                await self.log(run_id, "progress", f"Inspecting the current page to locate the target for {operation}.")
            try:
                options = {"allow_translation": False} if translation_decided else {}
                if optional:
                    selector = await self.resolver.resolve_dom(page, target, operation, secrets,
                        step_title=step_title, allow_translation=False, optional=True)
                elif dom_only:
                    selector = await self.resolver.resolve_dom(page, target, operation, secrets,
                        step_title=step_title, excluded_attempts=excluded_attempts, **options)
                else:
                    selector = await self.resolver.resolve(page, target, operation, secrets, step_title=step_title, **options)
            except TargetAbsent:
                if optional:
                    return None
                raise
            except TranslationNeeded as exc:
                question = SelectorNeeded(translation_key, target)
                found = ", ".join(f'“{label}”' for label in exc.labels)
                question.question.update({
                    "question": f'I could not find “{exc.source}”, but found {found} as a possible translation. Do you want to use it?',
                    "options": [{"label": f'Yes, use “{label}”',
                                 "value": json.dumps({"source": source, "translation": label}, ensure_ascii=False)}
                                for label in exc.labels] + [{"label": "No, keep the original label", "value": "reject"}],
                })
                raise question from exc
            except AmbiguousTarget as exc:
                raise SelectorNeeded(key, target, exc.candidates) from exc
            except Exception as exc:
                # Invalid provider output, stale DOM, or vision incompatibility must not guess a target.
                selector = None
                if self.log:
                    await self.log(run_id, "progress", f"Page inspection could not complete ({type(exc).__name__}). Check the LLM configuration and browser.vision setting.")
            if selector:
                if inspection_key:
                    try:
                        entry = await capture(page, selector)
                    except Exception:
                        entry = None
                    if entry:
                        await self.store.save_inspection(run_id, inspection_key, entry)
                if self.log:
                    await self.log(run_id, "progress", "LLM matched the target to an observed page element.")
                return selector
        raise SelectorNeeded(key, target)

    async def _start_session(self, base_url: str) -> Session:
        try:
            from playwright.async_api import async_playwright, expect
        except ImportError as exc:
            raise BrowserUnavailable("Install dependencies and run: python -m playwright install chromium") from exc
        self.expect = expect
        session = Session()
        try:
            session.driver = await async_playwright().start()
            session.browser = await session.driver.chromium.launch(headless=self.settings.headless)
            session.context = await session.browser.new_context(accept_downloads=False, service_workers="block")
            session.context.set_default_timeout(self.settings.action_timeout * 1000)
            origin = urlsplit(target_url(base_url, "/"))

            async def route_request(route):
                request = route.request
                url = urlsplit(request.url)
                # External static assets are allowed; document navigation stays on the test origin.
                if url.scheme not in {"http", "https"} or (
                    request.is_navigation_request() and (url.scheme, url.netloc) != (origin.scheme, origin.netloc)
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await session.context.route("**/*", route_request)
            session.page = await session.context.new_page()
            return session
        except BaseException as exc:
            await session.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BrowserUnavailable(
                "Chromium could not start. Run python -m playwright install --with-deps chromium; "
                "use browser.headless: true on servers without a display."
            ) from exc

    async def _session(self, run_id: str, base_url: str):
        async with self.lock:
            if run_id not in self.sessions:
                if len(self.sessions) >= self.settings.max_sessions:
                    raise BrowserUnavailable("Browser session limit reached. Stop another browser run and retry.")
                self.sessions[run_id] = await self._start_session(base_url)
            return self.sessions[run_id]

    async def _assert(self, page, assertion):
        timeout = self.settings.action_timeout * 1000
        if assertion.type == "url_contains":
            await self.expect(page).to_have_url(re.compile(re.escape(assertion.expected)), timeout=timeout)
        elif assertion.type == "title_contains":
            await self.expect(page).to_have_title(re.compile(re.escape(assertion.expected)), timeout=timeout)
        else:
            locator = self.expect(page.locator(assertion.selector))
            if assertion.type == "visible":
                await locator.to_be_visible(timeout=timeout)
            elif assertion.type == "hidden":
                await locator.to_be_hidden(timeout=timeout)
            elif assertion.type == "checked":
                await locator.to_be_checked(timeout=timeout)
            elif assertion.type == "text_contains":
                await locator.to_contain_text(assertion.expected, timeout=timeout)
            elif assertion.type == "text_equals":
                await locator.to_have_text(assertion.expected, timeout=timeout)
            elif assertion.type == "value_equals":
                await locator.to_have_value(assertion.expected, timeout=timeout)

    async def _assertion_failure(self, page, assertion, secrets, exc):
        # Never store Playwright exceptions: they can include unrelated DOM text and credentials.
        details = {"selector": assertion.selector, "timeout_ms": self.settings.action_timeout * 1000,
                   "exception_type": type(exc).__name__, "observation": "Snapshot after assertion failure"}
        sensitive = bool(SENSITIVE.search(f"{assertion.target or ''} {assertion.selector or ''}"))
        expected = assertion.expected
        if assertion.type in {"visible", "hidden", "checked"}:
            expected = assertion.type != "hidden"
        reason = ("Browser assertion did not match before the timeout." if isinstance(exc, AssertionError)
                  else "Browser assertion could not be evaluated. See the matched element and exception type.")
        actual = None
        try:
            if assertion.type == "url_contains":
                actual = page.url
            elif assertion.type == "title_contains":
                actual = await asyncio.wait_for(page.title(), timeout=2)
            else:
                snapshot = await asyncio.wait_for(page.locator(assertion.selector).evaluate_all("""els => {
                    const el = els.length === 1 ? els[0] : null;
                    return {count: els.length, element: el ? {
                        tag: el.tagName.toLowerCase(), type: el.getAttribute('type'),
                        id: el.id, name: el.getAttribute('name'), autocomplete: el.getAttribute('autocomplete'),
                        value: 'value' in el ? el.value : null,
                        text: el.textContent, checked: 'checked' in el ? el.checked : null,
                        visible: !!(el.getBoundingClientRect().width && el.getBoundingClientRect().height)
                            && getComputedStyle(el).visibility !== 'hidden'
                    } : null};
                }"""), timeout=2)
                details["matched_count"] = snapshot["count"]
                element = snapshot["element"]
                if snapshot["count"] == 0:
                    reason = "No element matched the assertion selector after the failure."
                elif snapshot["count"] > 1:
                    reason = f"Assertion selector matched {snapshot['count']} elements; expected one."
                else:
                    sensitive = sensitive or bool(SENSITIVE.search(" ".join(
                        str(element.get(key) or '') for key in ('type', 'id', 'name', 'autocomplete'))))
                    details["element"] = {key: element[key] for key in ("tag", "type", "id", "name")}
                    if assertion.type == "value_equals":
                        actual = element["value"]
                        reason = ("Matched element is not an input, textarea, or select; its value cannot be checked."
                                  if element["tag"] not in {"input", "textarea", "select"}
                                  else "Field value did not equal the expected value before the timeout.")
                    elif assertion.type in {"text_contains", "text_equals"}:
                        actual = element["text"]
                    elif assertion.type == "checked":
                        actual, expected = element["checked"], True
                    else:
                        actual, expected = element["visible"], assertion.type == "visible"
        except Exception:
            details["observation"] = "Could not read the page after the assertion failure."
        if sensitive:
            details["expected"] = details["actual"] = "[redacted]"
        else:
            details.update(expected=expected, actual=actual)
        # Redact before truncating so a long secret cannot leak as an unrecognized prefix.
        details = redact(details, secrets)
        for key in ("expected", "actual"):
            if isinstance(details[key], str) and len(details[key]) > 1000:
                details[key] = details[key][:1000] + "… [truncated]"
        return {"error": reason, "details": details}

    async def _observe(self, page):
        try:
            # Compare locally; no page text or field values are persisted in observations.
            state = await page.evaluate("""() => JSON.stringify({url: location.href,
                text: document.body.innerText,
                states: [...document.querySelectorAll('[aria-selected],[aria-expanded],input')]
                    .map(el => [el.getAttribute('aria-selected'), el.getAttribute('aria-expanded'), el.checked])})""")
            screenshot = None
            if self.settings.vision:
                screenshot = await page.screenshot(full_page=False, timeout=2000,
                    mask=[page.locator("input, textarea, [contenteditable]")])
            return state, screenshot
        except Exception:
            return None

    async def _visual_center(self, locator):
        box = await locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise RuntimeError("The target no longer has a visible box")
        point = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
        hit = await locator.evaluate("""(el, p) => {
            const hit = document.elementFromPoint(p.x, p.y);
            return p.x >= 0 && p.y >= 0 && p.x < innerWidth && p.y < innerHeight
                && hit !== null && (hit === el || el.contains(hit));
        }""", point)
        if not hit:
            raise RuntimeError("The target center is covered or outside the viewport")
        return point

    async def _click_and_observe(self, page, selector, index, result, attempt=None):
        visual = isinstance(selector, VisualTarget)
        locator = page.locator(selector.selector if visual else selector)
        if visual:
            try:
                # Scroll and wait before capturing the baseline, without dispatching a mouse event.
                await locator.click(trial=True)
            except Exception as exc:
                raise TargetUnavailable("The visual target is unavailable") from exc
        if attempt:
            await locator.evaluate("(el, token) => el.setAttribute('data-testbench-click-attempt', token)", attempt)
        before = await self._observe(page)
        if visual:
            try:
                if selector.exact_label is not None and not await locator.evaluate(EXACT_LABEL_JS, selector.exact_label):
                    raise TargetUnavailable("The quoted target label no longer matches")
                # Read the live center after observation, immediately before dispatching the click.
                point = await self._visual_center(locator)
            except Exception as exc:
                raise TargetUnavailable("The visual target center is unavailable") from exc
            await page.mouse.click(point["x"], point["y"])
        else:
            # Styled native controls may be hidden behind an associated label.
            # Preserve click's toggle semantics while using the visible label.
            native_check = await locator.evaluate(
                "el => el.matches('input[type=checkbox],input[type=radio]')")
            if native_check is True:
                is_radio = await locator.evaluate("el => el.type === 'radio'")
                await self._set_checked(page, locator, True if is_radio else not await locator.is_checked())
            else:
                await locator.click()
        # A dispatched click must not be replayed if observation fails or times out.
        result["actions_completed"] = index
        changed = None
        for delay in (0.1, 0.3, 0.6):
            await asyncio.sleep(delay)
            after = await self._observe(page)
            if before is not None and after is not None:
                changed = before != after
                if changed:
                    break
            else:
                changed = None
        note = ("UI changed after click." if changed else
                "No UI change observed." if changed is False else "UI observation unavailable.")
        result.setdefault("observations", []).append({"action": index, "ui_changed": changed,
            "source": "visual" if visual else "dom", "note": note})
        return changed

    async def _click_action(self, page, action, context, step, index, run_id, result, secrets, selected=None):
        pending = result.get("pending_click")
        dom_only = bool(pending and pending["action"] == index)
        excluded = pending["excluded_attempts"] if dom_only else []
        selector = selected or await self._selector(page, action, context, step.title, f"action-{index}",
                                        run_id, secrets, dom_only=dom_only, excluded_attempts=excluded)
        if isinstance(selector, ConfirmedTarget):
            # A user-confirmed target is clicked once, even when the UI does not visibly change.
            await page.mouse.click(selector.x, selector.y)
            result["actions_completed"] = index
            result.pop("pending_click", None)
            result.setdefault("observations", []).append({"action": index,
                "source": "confirmed_rectangle", "rectangle": selector.entry["box"],
                "note": "Clicked the center of the user-confirmed rectangle."})
            return
        if not isinstance(selector, (VisualTarget, VisualTargets)):
            await self._click_and_observe(page, selector, index, result)
            result.pop("pending_click", None)
            return
        candidates = selector.candidates if isinstance(selector, VisualTargets) else [selector]
        attempted = []
        for candidate in candidates:
            attempt = uuid4().hex
            try:
                changed = await self._click_and_observe(page, candidate, index, result, attempt=attempt)
            except TargetUnavailable:
                result.setdefault("observations", []).append({"action": index, "ui_changed": None,
                    "source": "visual", "note": "Visual target center unavailable; trying the next match."})
                continue
            attempted.append(attempt)
            if changed is not False:
                # Unknown observation is not evidence that clicking another target is necessary.
                result.pop("pending_click", None)
                return
        # All visual candidates are exhausted. Persist the DOM phase across clarification pauses.
        result["pending_click"] = {"action": index, "excluded_attempts": attempted}
        result["actions_completed"] = index - 1
        if self.log:
            await self.log(run_id, "progress", "No visual match changed the UI. Falling back to DOM inspection.")
        selector = await self._selector(page, action, context, step.title, f"action-{index}",
            run_id, secrets, dom_only=True, excluded_attempts=attempted)
        await self._click_and_observe(page, selector, index, result)
        result.pop("pending_click", None)

    async def _set_checked(self, page, locator, checked):
        token = uuid4().hex
        target = await locator.evaluate("""(el, token) => {
          const control = (""" + CHECK_CONTROL_JS + """)(el);
          if (!control) return null;
          control.setAttribute('data-testbench-check-control', token);
          const labels = [...(control.labels || [])];
          const label = labels.find(node => {
            const r = node.getBoundingClientRect(), s = getComputedStyle(node);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
              && s.display !== 'none' && s.opacity !== '0';
          });
          if (label) {
            // Prefer the decorative checkbox box, keeping embedded policy links out of the click.
            const box = [...label.querySelectorAll('span')].find(node => {
              const r = node.getBoundingClientRect(), s = getComputedStyle(node);
              return !node.textContent.trim() && !node.querySelector('input,a,button,[role]')
                && !node.closest('a,button,[role=link],[role=button]')
                && r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            });
            (box || label).setAttribute('data-testbench-check-label', token);
          }
          return {label: !!label};
        }""", token)
        if not target:
            raise ValueError("The check target is not a checkbox, radio, or associated label.")
        control = page.locator(f'[data-testbench-check-control="{token}"]')
        if await control.is_checked() == checked:
            return
        if target["label"]:
            await self.expect(control).to_be_enabled(timeout=self.settings.action_timeout * 1000)
            await page.locator(f'[data-testbench-check-label="{token}"]').click()
            await self.expect(control).to_be_checked(checked=checked, timeout=self.settings.action_timeout * 1000)
        else:
            await control.set_checked(checked)

    async def _optional_target(self, page, item, context, title, position, run_id, secrets):
        selector = await self._selector(page, item, context, title, position, run_id, secrets, optional=True)
        if selector is None:
            return None
        raw = selector.selector if isinstance(selector, VisualTarget) else selector
        if await page.locator(raw).count() == 0:
            return None
        return selector

    async def _execute(self, step: BrowserStep, context: dict, run_id: str, result: dict, secrets):
        session = await self._session(run_id, str(context["base_url"]))
        page = session.page
        # Resume at the first unexecuted action; never replay previous clicks or submits.
        completed = result["actions_completed"]
        if completed == 0:
            session.image_values.clear()
        for index, original in enumerate(step.actions[completed:], completed + 1):
            fields = resolve(original.model_dump(exclude={"selector", "target", "image_selector", "image_target"}), context)
            if isinstance(fields.get("value"), (int, float, bool)):
                fields["value"] = str(fields["value"])
            action = original.model_copy(update=fields)
            result["current_action"] = index
            selected = None
            if action.if_present:
                selector = await self._optional_target(page, action, context, step.title,
                    f"action-{index}", run_id, secrets)
                if selector is None:
                    result.setdefault("observations", []).append({"action": index,
                        "note": "Skipped: optional target is absent."})
                    result["actions_completed"] = index
                    continue
                selected = selector
                action = action.model_copy(update={"selector": selector.selector
                    if isinstance(selector, VisualTarget) else selector})
            if action.action == "goto":
                await page.goto(browser_target_url(str(context["base_url"]), action.path), wait_until="domcontentloaded")
            elif action.action == "click":
                await self._click_action(page, action, context, step, index, run_id, result, secrets, selected=selected)
            else:
                target_action = action.model_copy(update={"action": "fill"}) if action.action == "fill_from_image" else action
                selector = await self._selector(page, target_action, context, step.title, f"action-{index}", run_id, secrets)
                visual = selector if isinstance(selector, VisualTarget) else None
                locator = page.locator(visual.selector if visual else selector)
                if action.action == "fill":
                    await locator.fill(action.value)
                elif action.action == "fill_from_image":
                    if not self.settings.vision or self.resolver is None:
                        raise ImageReadUnavailable("fill_from_image requires browser.vision: true and an image-capable LLM.")
                    # DOM inspection rewrites data-testbench-ref on every snapshot. Keep
                    # the answer field addressable while resolving the separate image source.
                    fill_token = uuid4().hex
                    try:
                        await locator.evaluate(
                            "(el, token) => el.setAttribute('data-testbench-fill-target', token)", fill_token,
                            timeout=self.settings.action_timeout * 1000)
                    except Exception as exc:
                        raise ImageReadUnavailable(
                            "Could not prepare the verification code field before reading the image "
                            f"({type(exc).__name__}). Check the destination target.") from exc
                    locator = page.locator(f'[data-testbench-fill-target="{fill_token}"]')
                    image_action = action.model_copy(update={"action": "read_image",
                        "selector": action.image_selector, "target": action.image_target})
                    image_selector = await self._selector(page, image_action, context, step.title,
                        f"image-{index}", run_id, secrets)
                    if self.log:
                        await self.log(run_id, "progress", "Reading the selected image with the LLM.")
                    stage = "validate the image source"
                    try:
                        image_locator = page.locator(image_selector)
                        if not await image_locator.evaluate(IMAGE_SOURCE_JS,
                                timeout=self.settings.action_timeout * 1000):
                            raise ImageReadUnavailable("The selected element is not an image source.")
                        stage = "capture the image source"
                        screenshot = await image_locator.screenshot(type="png",
                            timeout=self.settings.action_timeout * 1000,
                            mask=[page.locator("input, textarea, [contenteditable]")])
                        # Optional context must not prevent reading an otherwise valid crop.
                        page_screenshot = None
                        with suppress(Exception):
                            page_screenshot = await page.screenshot(full_page=False, type="png",
                                timeout=self.settings.action_timeout * 1000,
                                mask=[page.locator("input, textarea, [contenteditable]")])
                        stage = "transcribe the image"
                        value = await self.resolver.read_image(screenshot, page_screenshot=page_screenshot,
                            target=redact(action.image_target, secrets))
                    except Exception as exc:
                        raise ImageReadUnavailable(f"Could not read the image: failed to {stage} "
                            f"({type(exc).__name__}). Check the image target and image-capable LLM, then retry.") from exc
                    try:
                        await locator.fill(value)
                        session.image_values[index] = value
                    except Exception as exc:
                        raise ImageReadUnavailable("The image was transcribed, but the verification code field "
                            f"could not be filled ({type(exc).__name__}). "
                            "Check that the destination is an editable field and the page has not changed.") from exc
                elif action.action == "select_option":
                    await locator.select_option(value=action.value)
                elif action.action == "press":
                    await locator.press(action.value)
                elif action.action == "wait_for":
                    await locator.wait_for(state="visible")
                elif action.action in {"check", "uncheck"}:
                    await self._set_checked(page, locator, action.action == "check")
                else:
                    await getattr(locator, action.action)()
            result["actions_completed"] = index
        checked = len(result["assertions"])
        for index, original in enumerate(step.assertions[checked:], checked + 1):
            expected = resolve(original.expected, context)
            if original.expected_from_action is not None:
                if original.expected_from_action not in session.image_values:
                    raise ImageReadUnavailable("The referenced image action did not produce a value in this attempt.")
                expected = session.image_values[original.expected_from_action]
            if isinstance(expected, (int, float, bool)):
                expected = str(expected)
            assertion = original.model_copy(update={"expected": expected, "expected_from_action": None})
            if assertion.if_present:
                selector = await self._optional_target(page, assertion, context, step.title,
                    f"assertion-{index}", run_id, secrets)
                if selector is None:
                    result["assertions"].append({"type": assertion.type,
                        "path": original.target or original.selector or "", "passed": True,
                        "skipped": True})
                    continue
                assertion = assertion.model_copy(update={"selector": selector})
            if assertion.type not in {"url_contains", "title_contains"}:
                selector = await self._selector(page, assertion, context, step.title, f"assertion-{index}", run_id, secrets)
                assertion = assertion.model_copy(update={"selector": selector})
            check = {"type": assertion.type, "path": original.target or assertion.selector or "", "passed": False}
            try:
                await self._assert(page, assertion)
                check["passed"] = True
            except Exception as exc:
                check.update(await self._assertion_failure(page, assertion, secrets, exc))
            result["assertions"].append(check)
        result["outcome"] = "passed" if all(c["passed"] for c in result["assertions"]) else "failed"

    async def execute(self, step: BrowserStep, context: dict, run_id: str, progress=None, secret_keys=()):
        attempts = list((progress or {}).get("attempts", []))
        while True:
            result = await self._execute_attempt(step, context, run_id, progress, secret_keys)
            result["attempt"] = len(attempts) + 1
            result["attempts"] = list(attempts)
            if (result["outcome"] not in {"failed", "error"}
                    or result.get("retryable") is False or len(attempts) >= step.max_retries):
                return result
            attempts.append({key: value for key, value in result.items() if key != "attempts"})
            progress = None
            if self.log:
                await self.log(run_id, "progress",
                    f"Retrying browser step: attempt {len(attempts) + 1} of {step.max_retries + 1}.")

    async def _execute_attempt(self, step: BrowserStep, context: dict, run_id: str, progress=None, secret_keys=()):
        started = monotonic()
        result = {"title": step.title, "kind": "browser", "outcome": "error", "assertions": [], "actions_completed": 0}
        if progress:
            if run_id not in self.sessions:
                result.update(message="The browser session was lost. Retry the test with a fresh browser.", fatal=True, retryable=False)
                return result
            result["actions_completed"] = progress["actions_completed"]
            result["assertions"] = list(progress["assertions"])
            result["observations"] = list(progress.get("observations", []))
            if progress.get("pending_click"):
                result["pending_click"] = dict(progress["pending_click"])
        secrets = secret_values(context)
        secrets.extend(context[key] for key in secret_keys if isinstance(context.get(key), str))
        try:
            await asyncio.wait_for(self._execute(step, context, run_id, result, secrets), timeout=self.settings.step_timeout)
        except SelectorNeeded as exc:
            result["outcome"] = "waiting_for_input"
            result["questions"] = [exc.question]
            result["message"] = "Page inspection needs clarification. Completed actions will not be repeated."
        except asyncio.CancelledError:
            raise
        except BrowserUnavailable as exc:
            result["retryable"] = False
            result["message"] = str(exc)
        except ImageReadUnavailable as exc:
            result["message"] = str(exc)
        except InvalidNavigation as exc:
            result["retryable"] = False
            result["message"] = str(exc)
        except asyncio.TimeoutError:
            result["message"] = "Browser step exceeded browser.step_timeout. Remaining actions were not executed."
        except Exception as exc:
            result["message"] = f"Browser action failed ({type(exc).__name__}). Check the selector, page state and test inputs."
        result["duration_ms"] = round((monotonic() - started) * 1000)
        # Stop dependent UI steps after a failure rather than clicking in an unknown page state.
        result["fatal"] = result["outcome"] not in {"passed", "waiting_for_input"}
        session = self.sessions.get(run_id)
        if self.settings.screenshots and session is not None:
            filename = f"{uuid4()}.png"
            directory = self.artifacts / run_id
            directory.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.wait_for(save_page_screenshot(session.page, directory / filename), timeout=6)
                result["screenshot_url"] = f"/artifacts/{run_id}/{filename}"
            except Exception:
                result["screenshot_note"] = "Screenshot unavailable."
        return result

    async def close_run(self, run_id: str):
        session = self.sessions.pop(run_id, None)
        if session:
            await session.close()

    async def close(self):
        for run_id in list(self.sessions):
            await self.close_run(run_id)
