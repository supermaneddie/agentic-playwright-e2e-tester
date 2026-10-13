import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from app.browser import BrowserRunner
from app.config import Settings
from app.inspection import ElementResolver
from app.models import BrowserStep


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is not installed")
class ChromiumTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_terms_and_bounded_login_retry(self):
        from playwright.async_api import async_playwright, expect
        from app.browser import Session

        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
            browser = await driver.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content("""<input id="email"><button id="login"
                    onclick="window.tries=(window.tries||0)+1;
                    if(window.tries===2) document.querySelector('#status').textContent='Home'">Login Now</button>
                    <p id="status">Login</p>""")
                settings = Settings(browser={"screenshots": False, "action_timeout": 1},
                                    llm={"url": "https://model.test", "model": "test"})
                with tempfile.TemporaryDirectory() as directory:
                    runner = BrowserRunner(settings.browser, Path(directory))
                    runner.expect = expect
                    runner.sessions["retry"] = Session(page=page)
                    setup = BrowserStep(title="Setup", actions=[
                        {"action": "fill", "selector": "#email", "value": "student"},
                        {"action": "check", "selector": "#terms", "if_present": True}],
                        assertions=[{"type": "value_equals", "selector": "#email", "expected": "student"}])
                    context = {"base_url": "https://ui.test"}
                    self.assertEqual((await runner.execute(setup, context, "retry"))["outcome"], "passed")
                    login = BrowserStep(title="Login", max_retries=2,
                        actions=[{"action": "click", "selector": "#login"}],
                        assertions=[{"type": "text_equals", "selector": "#status", "expected": "Home"}])
                    result = await runner.execute(login, context, "retry")
                    self.assertEqual(result["outcome"], "passed", result)
                    self.assertEqual(result["attempt"], 2)
                    self.assertEqual(await page.evaluate("window.tries"), 2)
                    self.assertEqual(await page.locator("#email").input_value(), "student")
            finally:
                await browser.close()

    async def test_click_hidden_checkbox_toggles_through_label(self):
        from playwright.async_api import async_playwright, expect
        from app.browser import Session

        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
            browser = await driver.chromium.launch()
            try:
                page = await browser.new_page()
                page.set_default_timeout(500)
                await page.set_content('''<label>
                    <input id="terms" type="checkbox" style="display:none">
                    <span style="display:inline-block;width:16px;height:16px"></span>
                    我已閱讀並同意 <a onclick="window.policyClicked=true">Privacy Policy</a>
                    </label>''')
                settings = Settings(browser={"screenshots": False, "action_timeout": 1},
                                    llm={"url": "https://model.test", "model": "test"})
                with tempfile.TemporaryDirectory() as directory:
                    runner = BrowserRunner(settings.browser, Path(directory))
                    runner.expect = expect
                    runner.sessions["terms"] = Session(page=page)
                    step = BrowserStep(title="Agree to terms", actions=[{
                        "action": "click", "selector": "#terms"}])
                    for expected in (True, False):
                        result = await runner.execute(step, {"base_url": "https://ui.test"}, "terms")
                        self.assertEqual(result["outcome"], "passed", result)
                        self.assertEqual(await page.locator("#terms").is_checked(), expected)
                        self.assertIsNone(await page.evaluate("window.policyClicked"))
                    await page.locator("#terms").evaluate("el => el.disabled = true")
                    result = await runner.execute(step, {"base_url": "https://ui.test"}, "terms")
                    self.assertEqual(result["outcome"], "error")
                    self.assertFalse(await page.locator("#terms").is_checked())
            finally:
                await browser.close()

    async def test_hidden_terms_checkbox_uses_associated_label(self):
        from playwright.async_api import async_playwright, expect
        from app.browser import Session
        from app.inspection import HIT_TEST_JS

        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
            browser = await driver.chromium.launch()
            try:
                page = await browser.new_page()
                page.set_default_timeout(500)
                await page.set_content('''<input id="terms" type="checkbox" style="display:none">
                    <label for="terms"><span>我已閱讀並同意</span></label><p>Unrelated text</p>''')
                settings = Settings(browser={"vision": False, "screenshots": False, "action_timeout": 1},
                                    llm={"url": "https://model.test", "model": "test"})
                resolver = ElementResolver(settings)

                async def choose(target, operation, snapshot, screenshot, secrets):
                    return next(e["id"] for e in snapshot["elements"] if e["tag"] == "label")

                resolver.choose = choose
                with tempfile.TemporaryDirectory() as directory:
                    runner = BrowserRunner(settings.browser, Path(directory), resolver)
                    runner.expect = expect
                    runner.sessions["terms"] = Session(page=page)
                    for operation, expected in (("check", True), ("check", True), ("uncheck", False)):
                        step = BrowserStep(title="Agree to terms", actions=[{
                            "action": operation, "target": '"我已閱讀並同意" checkbox'}])
                        result = await runner.execute(step, {"base_url": "https://ui.test"}, "terms")
                        self.assertEqual(result["outcome"], "passed", result)
                        self.assertEqual(await page.locator("#terms").is_checked(), expected)
                    box = await page.locator("label span").bounding_box()
                    hit = await page.evaluate(HIT_TEST_JS, {
                        "x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2,
                        "id": "terms-hit", "operation": "check", "exact": "我已閱讀並同意"})
                    self.assertEqual(hit, "terms-hit")
                    with self.assertRaises(ValueError):
                        await runner._set_checked(page, page.locator("p"), True)
                    await page.locator("#terms").evaluate("el => el.disabled = true")
                    result = await runner.execute(step.model_copy(update={"actions": [
                        step.actions[0].model_copy(update={"action": "check"})]}),
                        {"base_url": "https://ui.test"}, "terms")
                    self.assertEqual(result["outcome"], "error")
                    self.assertFalse(await page.locator("#terms").is_checked())
                    await page.set_content('''<label class="t-checkbox login-checkbox" tabindex="0">
                        <input type="checkbox" tabindex="-1" class="t-checkbox__former" style="display:none">
                        <span class="t-checkbox__input" style="display:inline-block;width:16px;height:16px"></span>
                        <span class="t-checkbox__label"><span>I have read and agree to the
                        <a onclick="window.policyClicked=true">Privacy Policy</a></span></span></label>''')
                    # An exact Chinese label must not silently match English text.
                    selector = await resolver.resolve_dom(page, '"我已閱讀並同意" checkbox', "check", [],
                                                          allow_translation=False)
                    self.assertIsNone(selector)
                    step = BrowserStep(title="Agree to terms", actions=[{
                        "action": "check", "target": '"I have read and agree to the Privacy Policy" checkbox'}],
                        assertions=[{"type": "checked", "selector": "input"}])
                    result = await runner.execute(step, {"base_url": "https://ui.test"}, "terms")
                    self.assertEqual(result["outcome"], "passed", result)
                    self.assertIsNone(await page.evaluate("window.policyClicked"))
            finally:
                await browser.close()

    async def test_image_fill_survives_dom_reference_reassignment(self):
        from playwright.async_api import async_playwright
        from unittest.mock import AsyncMock
        from app.browser import Session

        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
            browser = await driver.chromium.launch()
            try:
                page = await browser.new_page()
                page.set_default_timeout(1000)
                await page.set_content('<input placeholder="Verification code"><canvas width="90" height="30"></canvas>')
                settings = Settings(browser={"vision": True, "screenshots": False, "action_timeout": 1},
                                    llm={"url": "https://model.test", "model": "test"})
                resolver = ElementResolver(settings)
                resolver.visual = AsyncMock(return_value=None)
                resolver.read_image = AsyncMock(return_value="0338")
                refs = []

                async def choose(target, operation, snapshot, screenshot, secrets):
                    field = next(e for e in snapshot["elements"] if e["tag"] == "input")
                    refs.append(field["id"])
                    return next(e["id"] for e in snapshot["elements"]
                                if e["tag"] == ("input" if operation == "fill" else "canvas"))

                resolver.choose = choose
                with tempfile.TemporaryDirectory() as directory:
                    runner = BrowserRunner(settings.browser, Path(directory), resolver)
                    runner.sessions["image-test"] = Session(page=page)
                    step = BrowserStep(title="Fill verification code", actions=[{
                        "action": "fill_from_image", "target": "Verification code field",
                        "image_target": "Generated CAPTCHA on the left side"}],
                        assertions=[{"type": "value_equals", "selector": "input", "expected_from_action": 1}])
                    from playwright.async_api import expect
                    runner.expect = expect
                    result = await runner.execute(step, {"base_url": "https://ui.test"}, "image-test")
                    self.assertEqual(result["outcome"], "passed", result)
                    self.assertNotEqual(refs[0], refs[1])
                    self.assertEqual(await page.locator("input").input_value(), "0338")
                    self.assertTrue(resolver.read_image.await_args.kwargs["page_screenshot"])
            finally:
                await browser.close()

    async def test_image_source_rejects_login_button_and_accepts_captcha(self):
        from playwright.async_api import async_playwright
        from app.inspection import HIT_TEST_JS, IMAGE_SOURCE_JS
        from unittest.mock import AsyncMock

        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
            browser = await driver.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('''<form id="form"><input id="answer">
                    <canvas id="captcha" width="90" height="30"></canvas>
                    <button id="login"><span id="button-text">立即登入</span></button>
                    <div id="text-captcha">Ab3X</div></form>''')
                resolver = ElementResolver(Settings(browser={"vision": False},
                    llm={"url": "https://model.test", "model": "test"}))
                for selector, valid in (("#login", False), ("#button-text", False),
                                        ("#answer", False), ("#form", False),
                                        ("#captcha", True), ("#text-captcha", True)):
                    with self.subTest(selector=selector):
                        locator = page.locator(selector)
                        self.assertEqual(await locator.evaluate(IMAGE_SOURCE_JS), valid)
                        box = await locator.bounding_box()
                        if selector != "#form":
                            hit = await page.evaluate(HIT_TEST_JS, {
                                "x": box["x"] + box["width"] / 2,
                                "y": box["y"] + box["height"] / 2,
                                "id": "test-ref", "operation": "read_image", "exact": None})
                            self.assertEqual(hit, "test-ref" if valid else None)

                        async def choose(target, operation, snapshot, screenshot, secrets):
                            return await locator.get_attribute("data-testbench-ref")

                        resolver.choose = choose
                        result = await resolver.resolve_dom(page, "CAPTCHA", "read_image", [])
                        self.assertEqual(result is not None, valid)

                # A bad visual coordinate must fall back to DOM inspection.
                resolver.settings.browser.vision = True
                box = await page.locator("#button-text").bounding_box()
                resolver._request = AsyncMock(return_value=json.dumps({"candidates": [{
                    "description": "CAPTCHA", "x": box["x"] + box["width"] / 2,
                    "y": box["y"] + box["height"] / 2}]}))
                resolver.resolve_dom = AsyncMock(return_value="#captcha")
                self.assertEqual(await resolver.resolve(page, "CAPTCHA", "read_image", []), "#captcha")
                resolver.resolve_dom.assert_awaited_once()
            finally:
                await browser.close()

    async def test_assertion_failure_evidence_in_real_chromium(self):
        from playwright.async_api import async_playwright
        from app.inspection import HIT_TEST_JS
        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(browser={"vision": False, "screenshots": False, "action_timeout": 0.1},
                                llm={"url": "https://model.test", "model": "test"})
            runner = BrowserRunner(settings.browser, Path(directory))
            run_id = str(uuid4())
            try:
                session = await runner._session(run_id, "https://ui.test")
                await session.page.set_content('<label id="label">Email address field</label>'
                    '<input id="email" type="email"><input id="other"><input id="private" type="password">')
                step = BrowserStep(title="Fill email", actions=[
                    {"action": "fill", "selector": "#email", "value": "alice@example.test"}],
                    assertions=[{"type": "value_equals", "selector": selector, "expected": "alice@example.test"}
                                for selector in ("#email", "#other", "#missing", "input", "#label")])
                result = await runner.execute(step, {"base_url": "https://ui.test"}, run_id)
                self.assertEqual(result["outcome"], "failed", result)
                checks = result["assertions"]
                self.assertTrue(checks[0]["passed"])
                self.assertEqual(checks[1]["details"]["actual"], "")
                self.assertEqual(checks[1]["details"]["expected"], "alice@example.test")
                self.assertEqual(checks[2]["details"]["matched_count"], 0)
                self.assertEqual(checks[3]["details"]["matched_count"], 3)
                self.assertIn("not an input", checks[4]["error"])
                box = await session.page.locator('#label').bounding_box()
                hit = await session.page.evaluate(HIT_TEST_JS, {
                    "x": box['x'] + box['width'] / 2, "y": box['y'] + box['height'] / 2,
                    "id": "ref", "prefix": "tb", "operation": "value_equals", "exact": None})
                self.assertIsNone(hit)
                private = BrowserStep(title="Private field", actions=[
                    {"action": "fill", "selector": "#private", "value": "literal-private-value"}],
                    assertions=[{"type": "value_equals", "selector": "#private", "expected": "other-private-value"}])
                result = await runner.execute(private, {"base_url": "https://ui.test"}, run_id)
                self.assertEqual(result["assertions"][0]["details"]["actual"], "[redacted]")
                self.assertNotIn("private-value", str(result))
            finally:
                await runner.close()

    async def test_login_flow_in_real_chromium(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Install Chromium with python -m playwright install chromium")
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(browser={"vision": False}, llm={"url": "https://model.test", "model": "test"})
            resolver = ElementResolver(settings)
            snapshots = []

            async def choose(target, operation, snapshot, screenshot, secrets):
                snapshots.append(snapshot)
                if operation == "fill":
                    kind = "password" if "Password" in target else "email"
                    field = next(e for e in snapshot["elements"] if e["type"] == kind and e['context']['form_name'] == 'Login')
                    self.assertTrue(field['labels'])
                    if kind == 'email':
                        self.assertEqual(field['placeholder'], 'Your work email')
                        self.assertEqual(field['labels'][0]['association'], 'label-for')
                    self.assertEqual(snapshot['step_context'], 'Login')
                    return field['id']
                if operation == "click":
                    return next(e["id"] for e in snapshot["elements"] if e["tag"] == "button")
                return next(e["id"] for e in snapshot["elements"] if e["tag"] == "p" and e["name"].startswith("Welcome"))

            resolver.choose = choose
            runner = BrowserRunner(settings.browser, Path(directory), resolver)
            run_id = str(uuid4())
            try:
                session = await runner._session(run_id, "https://ui.test")
                html = '''<title>Login demo</title><form aria-label="Login"><h2>Sign in</h2><label for="username">Email</label><input id="username" type="email" placeholder="Your work email"><label for="password">Password</label><input id="password" type="password">
                <button type="button" onclick="document.querySelector('#welcome').textContent='Welcome '+document.querySelector('#username').value">Sign in</button>
                <p id="welcome"></p></form><form aria-label="Newsletter"><h2>Subscribe</h2><label>Email<input type="email" placeholder="Your work email"></label></form>'''
                await session.page.route("https://ui.test/**", lambda route: route.fulfill(content_type="text/html", body=html))
                step = BrowserStep(title="Login", actions=[
                    {"action": "goto", "path": "/login"},
                    {"action": "fill", "target": "Email field", "value": "alice"},
                    {"action": "fill", "target": "Password field", "value": "{{password}}"},
                    {"action": "click", "target": "Sign in button"},
                ], assertions=[{"type": "text_equals", "target": "Welcome message", "expected": "Welcome alice"}])
                result = await runner.execute(step, {"base_url": "https://ui.test", "password": "secret"}, run_id)
                self.assertEqual(result["outcome"], "passed", result)
                self.assertTrue(list((Path(directory) / run_id).glob("*.png")))
                self.assertEqual(await session.page.locator("#username").input_value(), "alice")
                self.assertNotIn("secret", str(snapshots))
                self.assertEqual(len(snapshots), 4)
            finally:
                await runner.close()

    async def test_visual_delegated_click_and_tab_change(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest('Chromium unavailable')
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(llm={'url': 'https://model.test', 'model': 'test'})
            resolver = ElementResolver(settings)
            runner = BrowserRunner(settings.browser, Path(directory), resolver)
            run_id = str(uuid4())
            try:
                session = await runner._session(run_id, 'https://ui.test')
                await session.page.set_content('''<ul onclick="document.querySelector('#panel').textContent='Student panel'"><li>Student</li></ul>
                    <div id="panel">Original panel</div>''')
                box = await session.page.locator('li').bounding_box()
                async def request(system, content):
                    import json
                    return json.dumps({'candidates': [{'description': 'Student item',
                        'x': box['x'] + box['width'] / 2, 'y': box['y'] + box['height'] / 2}]})
                resolver._request = request
                step = BrowserStep(title='Open Student', actions=[{'action': 'click', 'target': 'Student'}],
                    assertions=[{'type': 'text_equals', 'selector': '#panel', 'expected': 'Student panel'}])
                result = await runner.execute(step, {'base_url': 'https://ui.test'}, run_id)
                self.assertEqual(result['outcome'], 'passed', result)
                self.assertTrue(result['observations'][0]['ui_changed'])
            finally:
                await runner.close()
