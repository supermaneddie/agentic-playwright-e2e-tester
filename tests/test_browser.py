import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError

from app.browser import BrowserRunner, BrowserUnavailable, Session, browser_target_url, InvalidNavigation
from app.config import BrowserConfig
from app.screenshots import EXPANDED_PAGE_STYLE_JS, RESTORE_SCROLL_JS
from app.models import BrowserAction, BrowserAssertion, BrowserStep, Plan, Submission


def browser_step(**overrides):
    return BrowserStep.model_validate({
        "kind": "browser", "title": "Sign in",
        "actions": [{"action": "goto", "path": "/login"},
                    {"action": "fill", "selector": "#password", "value": "{{password}}"},
                    {"action": "click", "selector": "button"}],
        "assertions": [{"type": "visible", "selector": "#welcome"}], **overrides,
    })


class BrowserModelTests(unittest.TestCase):
    def test_image_expectation_requires_valid_action_reference(self):
        for assertion in (
            {"type": "visible", "expected_from_action": 1},
            {"type": "value_equals", "expected_from_action": 1, "expected": "CAPTCHA text"},
            {"type": "value_equals", "expected_from_action": 0},
            {"type": "value_equals", "expected_from_action": True},
            {"type": "value_equals", "expected_from_action": 4},
            {"type": "value_equals", "expected_from_action": 1},
        ):
            with self.subTest(assertion=assertion), self.assertRaises(ValidationError):
                browser_step(assertions=[{"selector": "#answer", **assertion}])

    def test_control_flow_bounds_and_invalid_conditions(self):
        for value in (-1, 6, True, 1.5, "2"):
            with self.assertRaises(ValidationError):
                browser_step(max_retries=value)
        with self.assertRaises(ValidationError):
            BrowserAction(action="goto", path="/", if_present=True)
        with self.assertRaises(ValidationError):
            BrowserAssertion(type="url_contains", expected="/home", if_present=True)

    def test_browser_navigation_allows_same_origin_urls(self):
        self.assertEqual(browser_target_url("https://ui.test/app", "/login"), "https://ui.test/app/login")
        self.assertEqual(browser_target_url("https://ui.test/app", "https://ui.test/login?next=%2Fhome#form"),
                         "https://ui.test/login?next=%2Fhome#form")
        for path in ("https://other.test/login", "http://ui.test/login", "https://ui.test:123/login",
                     "https://user:secret@ui.test/login", "//other.test", "/\\other.test",
                     "javascript:alert(1)", "https://ui.test/\nlogin", 123):
            with self.subTest(path=path), self.assertRaises(InvalidNavigation):
                browser_target_url("https://ui.test", path)

    def test_image_action_requires_source_and_destination(self):
        BrowserAction(action="fill_from_image", target="Answer", image_target="CAPTCHA")
        for fields in ({"target": "Answer"}, {"image_target": "CAPTCHA"},
                       {"target": "Answer", "image_target": "CAPTCHA", "value": "invented"}):
            with self.assertRaises(ValidationError):
                BrowserAction(action="fill_from_image", **fields)

    def test_browser_and_legacy_http_plans(self):
        self.assertEqual(Submission(project_id="p", description="d").test_type, "http")
        plan = Plan(summary="Login", steps=[browser_step().model_dump()])
        self.assertIsInstance(plan.steps[0], BrowserStep)
        with self.assertRaises(ValidationError):
            BrowserAction(action="evaluate", value="document.cookie")
        with self.assertRaises(ValidationError):
            BrowserAction(action="fill", selector="input")
        with self.assertRaises(ValidationError):
            BrowserAssertion(type="visible")
        with self.assertRaises(ValidationError):
            browser_step(assertions=[])


class BrowserRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_value_assertion_uses_fresh_transcription_on_retry(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(side_effect=["0228", "2287"]))
        self.locator.screenshot = AsyncMock(return_value=b"image")
        self.expectation.to_have_value.side_effect = [AssertionError(), None]
        step = browser_step(max_retries=1, actions=[
            {"action": "fill_from_image", "selector": "#answer", "image_selector": "#captcha"}],
            assertions=[{"type": "value_equals", "selector": "#answer", "expected_from_action": 1}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual([c.args[0] for c in self.expectation.to_have_value.await_args_list], ["0228", "2287"])

    async def test_image_value_survives_assertion_clarification_without_refilling(self):
        from app.browser import SelectorNeeded
        self.runner.resolver = MagicMock(read_image=AsyncMock(return_value="2287"))
        self.locator.screenshot = AsyncMock(return_value=b"image")
        step = browser_step(actions=[
            {"action": "fill_from_image", "selector": "#answer", "image_selector": "#captcha"}],
            assertions=[{"type": "value_equals", "target": "Answer", "expected_from_action": 1}])
        original = self.runner._selector
        async def selector(page, item, *args, **kwargs):
            if isinstance(item, BrowserAssertion):
                raise SelectorNeeded("answer", "Answer")
            return await original(page, item, *args, **kwargs)
        with patch.object(self.runner, "_selector", side_effect=selector):
            progress = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(progress["outcome"], "waiting_for_input")
        self.assertNotIn("2287", str(progress))
        with patch.object(self.runner, "_selector", return_value="#answer"):
            result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id, progress)
        self.assertEqual(result["outcome"], "passed")
        self.locator.fill.assert_awaited_once_with("2287")
        self.expectation.to_have_value.assert_awaited_once_with("2287", timeout=self.runner.settings.action_timeout * 1000)

    async def test_optional_absence_skips_action_and_assertion(self):
        self.locator.count = AsyncMock(return_value=0)
        step = browser_step(actions=[{"action": "check", "selector": "#terms", "if_present": True}],
            assertions=[{"type": "checked", "selector": "#terms", "if_present": True}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["assertions"][0]["skipped"])
        self.assertIn("Skipped", result["observations"][0]["note"])
        self.locator.check.assert_not_awaited()

    async def test_optional_present_action_failure_is_not_skipped(self):
        self.locator.count = AsyncMock(return_value=1)
        self.locator.fill.side_effect = RuntimeError("disabled")
        result = await self.runner.execute(browser_step(actions=[
            {"action": "fill", "selector": "#field", "value": "x", "if_present": True}]),
            {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["actions_completed"], 0)

    async def test_optional_target_inspection_absence_and_error_differ(self):
        from app.inspection import TargetAbsent
        self.runner.resolver = MagicMock(resolve_dom=AsyncMock(side_effect=TargetAbsent()))
        step = browser_step(actions=[{"action": "check", "target": "Terms", "if_present": True}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.runner.resolver.resolve_dom.side_effect = RuntimeError("provider failed")
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "waiting_for_input")
        self.assertEqual(result["actions_completed"], 0)

    async def test_retry_reads_fresh_image_and_stops_on_success(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(side_effect=["first", "second"]))
        self.locator.screenshot = AsyncMock(return_value=b"image")
        self.expectation.to_have_url.side_effect = [AssertionError(), None]
        step = browser_step(max_retries=2, actions=[
            {"action": "fill_from_image", "selector": "#code", "image_selector": "#captcha"}],
            assertions=[{"type": "url_contains", "expected": "/student/activity/home"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["attempts"][0]["outcome"], "failed")
        self.assertEqual(self.runner.resolver.read_image.await_count, 2)
        self.assertEqual([c.args[0] for c in self.locator.fill.await_args_list], ["first", "second"])
        self.runner._start_session.assert_awaited_once()

    async def test_retry_exhaustion_and_default_no_retry(self):
        self.locator.fill.side_effect = RuntimeError()
        for retries in (0, 2):
            self.locator.fill.reset_mock()
            result = await self.runner.execute(browser_step(max_retries=retries, actions=[
                {"action": "fill", "selector": "#code", "value": "x"}]),
                {"base_url": "https://ui.test"}, self.run_id)
            self.assertEqual(result["outcome"], "error")
            self.assertTrue(result["fatal"])
            self.assertEqual(self.locator.fill.await_count, retries + 1)
            self.assertEqual(len(result["attempts"]), retries)

    async def test_retry_pause_preserves_history_and_completed_actions(self):
        from app.browser import SelectorNeeded
        calls = []
        async def execute_attempt(step, context, run_id, result, secrets):
            calls.append(result["actions_completed"])
            if len(calls) == 1:
                result["outcome"] = "failed"
            elif len(calls) == 2:
                result["actions_completed"] = 1
                raise SelectorNeeded("selector", "Submit")
            else:
                result["outcome"] = "passed"
        self.runner._execute = execute_attempt
        self.runner.sessions[self.run_id] = self.session
        step = browser_step(max_retries=2)
        result = await self.runner.execute(step, {}, self.run_id)
        self.assertEqual(result["outcome"], "waiting_for_input")
        self.assertEqual(result["attempt"], 2)
        result = await self.runner.execute(step, {}, self.run_id, progress=result)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(calls, [0, 0, 1])
        self.assertEqual(len(result["attempts"]), 1)

    async def test_goto_base_url_placeholder(self):
        step = browser_step(actions=[{"action": "goto", "path": "{{base_url}}"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test/"}, self.run_id)
        self.assertEqual(result["outcome"], "passed", result)
        self.page.goto.assert_awaited_once_with("https://ui.test/", wait_until="domcontentloaded")

    async def test_invalid_navigation_explains_error_without_exposing_url(self):
        step = browser_step(actions=[{"action": "goto", "path": "https://other.test/private-token"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["actions_completed"], 0)
        self.assertIn("same origin", result["message"])
        self.assertNotIn("private-token", str(result))
        self.page.goto.assert_not_awaited()

    async def test_image_is_captured_and_transcribed_before_filling(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(return_value="Ab3X"))
        self.locator.screenshot = AsyncMock(return_value=b"png-image")
        step = browser_step(actions=[{"action": "fill_from_image", "selector": "#answer",
                                     "image_selector": "#captcha"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.runner.resolver.read_image.assert_awaited_once_with(
            b"png-image", page_screenshot=b"page-image", target=None)
        self.locator.fill.assert_awaited_once_with("Ab3X")
        self.assertIn("mask", self.locator.screenshot.call_args.kwargs)
        self.assertNotIn("Ab3X", str(result))

    async def test_image_failure_stops_before_fill_and_submission(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(side_effect=ValueError("private response")))
        self.locator.screenshot = AsyncMock(return_value=b"png-image")
        step = browser_step(actions=[{"action": "fill_from_image", "selector": "#answer",
                                     "image_selector": "#captcha"},
                                    {"action": "click", "selector": "#submit"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["actions_completed"], 0)
        self.assertIn("Could not read the image", result["message"])
        self.assertNotIn("private response", str(result))
        self.locator.fill.assert_not_awaited()
        self.locator.click.assert_not_awaited()

    async def test_invalid_image_source_stops_before_transcription_and_fill(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(return_value="立即登入"))
        self.locator.evaluate.return_value = False
        self.locator.screenshot = AsyncMock()
        step = browser_step(actions=[{"action": "fill_from_image", "selector": "#answer",
                                     "image_selector": "#submit"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["actions_completed"], 0)
        self.locator.screenshot.assert_not_awaited()
        self.runner.resolver.read_image.assert_not_awaited()
        self.locator.fill.assert_not_awaited()

    async def test_image_action_requires_vision(self):
        self.runner.settings.vision = False
        step = browser_step(actions=[{"action": "fill_from_image", "selector": "#answer",
                                     "image_selector": "#captcha"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertIn("browser.vision", result["message"])
        self.locator.fill.assert_not_awaited()

    async def test_image_targets_resolve_with_separate_operations(self):
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=["#answer", "#captcha"]),
                                         read_image=AsyncMock(return_value="Ab3X"))
        self.locator.screenshot = AsyncMock(return_value=b"png-image")
        step = browser_step(actions=[{"action": "fill_from_image", "target": "Answer field",
                                     "image_target": "CAPTCHA image"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        calls = self.runner.resolver.resolve.await_args_list
        self.assertEqual(calls[0].args[1:3], ("Answer field", "fill"))
        self.assertEqual(calls[1].args[1:3], ("CAPTCHA image", "read_image"))

    async def test_image_inspection_does_not_invalidate_destination_reference(self):
        original_field = MagicMock(evaluate=AsyncMock(), fill=AsyncMock())
        image = MagicMock(evaluate=AsyncMock(return_value=True),
                          screenshot=AsyncMock(return_value=b"crop"))
        stable_field = MagicMock(fill=AsyncMock())
        pinned = {}

        async def pin(script, token, **kwargs):
            pinned["selector"] = f'[data-testbench-fill-target="{token}"]'

        original_field.evaluate.side_effect = pin

        async def resolve_target(page, target, operation, secrets, **kwargs):
            if operation == "fill":
                return '[data-testbench-ref="old-field"]'
            # A second DOM snapshot invalidates the original locator.
            original_field.fill.side_effect = TimeoutError("stale reference")
            return "#captcha"

        def locate(selector):
            if selector == '[data-testbench-ref="old-field"]':
                return original_field
            if selector == pinned.get("selector"):
                return stable_field
            return image if selector == "#captcha" else self.locator

        self.page.locator.side_effect = locate
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=resolve_target),
                                         read_image=AsyncMock(return_value="3383"))
        step = browser_step(actions=[{"action": "fill_from_image", "target": "Verification code field",
                                     "image_target": "Generated captcha on the left side"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed", result)
        stable_field.fill.assert_awaited_once_with("3383")
        original_field.fill.assert_not_awaited()

    async def test_image_fill_failure_identifies_destination_stage(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(return_value="3383"))
        self.locator.screenshot = AsyncMock(return_value=b"crop")
        self.locator.fill.side_effect = TimeoutError("private page contents")
        step = browser_step(actions=[{"action": "fill_from_image", "selector": "#answer",
                                     "image_selector": "#captcha"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertIn("image was transcribed", result["message"])
        self.assertIn("TimeoutError", result["message"])
        self.assertNotIn("private page contents", str(result))
        self.assertEqual(result["actions_completed"], 0)

    async def test_image_crop_can_be_read_if_context_capture_fails(self):
        self.runner.resolver = MagicMock(read_image=AsyncMock(return_value="3383"))
        self.locator.screenshot = AsyncMock(return_value=b"crop")
        self.page.screenshot.side_effect = TimeoutError()
        step = browser_step(actions=[{"action": "fill_from_image", "selector": "#answer",
                                     "image_selector": "#captcha"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed", result)
        self.runner.resolver.read_image.assert_awaited_once_with(b"crop", page_screenshot=None, target=None)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = BrowserRunner(BrowserConfig(), Path(self.directory.name))
        self.run_id = str(uuid4())
        self.page = MagicMock()
        self.page.goto = AsyncMock()
        self.page.screenshot = AsyncMock(return_value=b"page-image")
        async def screenshot_state(script, *args):
            if script == EXPANDED_PAGE_STYLE_JS:
                return {"style": "html { overflow: visible !important; }", "scrolls": []}
            if script == RESTORE_SCROLL_JS:
                return None
            raise NotImplementedError("Page observation is not mocked for this test")
        self.page.evaluate = AsyncMock(side_effect=screenshot_state)
        self.locator = MagicMock()
        self.locator.evaluate = AsyncMock(return_value=True, side_effect=lambda script, *args, **kwargs:
            False if script == "el => el.matches('input[type=checkbox],input[type=radio]')" else DEFAULT)
        for method in ("fill", "click", "press", "check", "uncheck", "select_option", "wait_for"):
            setattr(self.locator, method, AsyncMock())
        self.page.locator.return_value = self.locator
        self.expectation = MagicMock()
        for method in ("to_be_visible", "to_be_hidden", "to_be_checked", "to_contain_text", "to_have_text", "to_have_url", "to_have_title", "to_have_value"):
            setattr(self.expectation, method, AsyncMock())
        self.runner.expect = MagicMock(return_value=self.expectation)
        self.session = Session(page=self.page, context=MagicMock(close=AsyncMock()))
        self.runner._start_session = AsyncMock(return_value=self.session)

    async def test_actions_screenshot_and_session_reuse(self):
        context = {"base_url": "https://ui.test", "password": "private"}
        result = await self.runner.execute(browser_step(), context, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.page.goto.assert_awaited_once_with("https://ui.test/login", wait_until="domcontentloaded")
        self.locator.fill.assert_awaited_once_with("private")
        self.assertNotIn("private", str(result))
        self.assertIn(f"/artifacts/{self.run_id}/", result["screenshot_url"])
        self.assertIn("mask", self.page.screenshot.call_args.kwargs)
        await self.runner.execute(browser_step(actions=[{"action": "click", "selector": "button"}]), context, self.run_id)
        self.runner._start_session.assert_awaited_once()
        await self.runner.close_run(self.run_id)
        self.session.context.close.assert_awaited_once()
        self.assertFalse(self.runner.sessions)

    async def test_failed_assertion_stops_dependent_steps(self):
        self.expectation.to_be_visible.side_effect = AssertionError("private DOM text")
        result = await self.runner.execute(browser_step(), {"base_url": "https://ui.test", "password": "x"}, self.run_id)
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(result["fatal"])
        self.assertNotIn("private DOM text", str(result))
        self.assertIn("screenshot_url", result)

    async def test_actions_fail_without_fabricating_assertions(self):
        self.locator.click.side_effect = RuntimeError("password in error")
        result = await self.runner.execute(browser_step(), {"base_url": "https://ui.test", "password": "x"}, self.run_id)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["actions_completed"], 2)
        self.assertEqual(result["assertions"], [])
        self.assertNotIn("password in error", str(result))

    async def test_value_failure_explains_expected_actual_and_target(self):
        self.expectation.to_have_value.side_effect = AssertionError("unrelated private DOM")
        self.locator.evaluate_all = AsyncMock(return_value={"count": 1, "element": {
            "tag": "input", "type": "email", "id": "email", "name": "email",
            "value": "wrong@example.test"}})
        step = browser_step(actions=[{"action": "fill", "selector": "#email", "value": "alice@example.test"}],
                            assertions=[{"type": "value_equals", "selector": "#email",
                                         "target": "Email address field", "expected": "alice@example.test"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test"}, self.run_id)
        check = result["assertions"][0]
        self.assertEqual(result["actions_completed"], 1)
        self.assertEqual(check["details"]["actual"], "wrong@example.test")
        self.assertEqual(check["details"]["expected"], "alice@example.test")
        self.assertEqual(check["details"]["selector"], "#email")
        self.assertEqual(check["details"]["matched_count"], 1)
        self.assertNotIn("unrelated private DOM", str(result))
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(loader=FileSystemLoader("app/templates"), autoescape=select_autoescape())
        html = env.get_template("macros.html").module.results([result])
        self.assertIn("Target:", html)
        self.assertIn("Expected", html)
        self.assertIn("wrong@example.test", html)

    async def test_failure_diagnostics_missing_ambiguous_wrong_element_and_unavailable(self):
        assertion = BrowserAssertion(type="value_equals", selector="#field", expected="email")
        for count, element, message in [(0, None, "No element"), (2, None, "matched 2"),
            (1, {"tag": "label", "type": None, "id": "", "name": None, "value": None}, "not an input")]:
            with self.subTest(count=count):
                self.locator.evaluate_all = AsyncMock(return_value={"count": count, "element": element})
                check = await self.runner._assertion_failure(self.page, assertion, [], AssertionError())
                self.assertIn(message, check["error"])
        self.locator.evaluate_all.side_effect = RuntimeError("private exception")
        check = await self.runner._assertion_failure(self.page, assertion, [], RuntimeError())
        self.assertIn("Could not read", check["details"]["observation"])
        self.assertNotIn("private exception", str(check))

    async def test_failure_diagnostics_redact_password_fields_and_known_secrets(self):
        for field_type, secrets in [("password", []), ("text", ["private-value"])]:
            with self.subTest(field_type=field_type):
                self.locator.evaluate_all = AsyncMock(return_value={"count": 1, "element": {
                    "tag": "input", "type": field_type, "id": "field", "name": None,
                    "value": "private-value"}})
                check = await self.runner._assertion_failure(self.page,
                    BrowserAssertion(type="value_equals", selector="#field", expected="private-value"),
                    secrets, AssertionError("private-value"))
                self.assertNotIn("private-value", str(check))
                self.assertEqual(check["details"]["actual"], "[redacted]")
                self.assertEqual(check["details"]["expected"], "[redacted]")

    async def test_missing_browser_has_actionable_error(self):
        self.runner._start_session.side_effect = BrowserUnavailable("Install Chromium")
        result = await self.runner.execute(browser_step(), {"base_url": "https://ui.test", "password": "x"}, self.run_id)
        self.assertEqual(result["message"], "Install Chromium")
        self.assertTrue(result["fatal"])

    async def test_email_fill_and_assertion_resolve_same_json_field(self):
        step = browser_step(actions=[{"action": "fill", "selector": "#email", "value": "{{account.email}}"}],
                            assertions=[{"type": "value_equals", "selector": "#email",
                                         "expected": "{{account.email}}"}])
        result = await self.runner.execute(step, {"base_url": "https://ui.test",
            "account": {"email": "provided@example.test"}}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.locator.fill.assert_awaited_once_with("provided@example.test")
        self.expectation.to_have_value.assert_awaited_once_with("provided@example.test",
            timeout=self.runner.settings.action_timeout * 1000)

    async def test_cancellation_propagates(self):
        entered = asyncio.Event()
        async def wait_for_click():
            entered.set()
            await asyncio.Event().wait()
        self.locator.click.side_effect = wait_for_click
        task = asyncio.create_task(self.runner.execute(browser_step(), {"base_url": "https://ui.test", "password": "x"}, self.run_id))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.runner.close_run(self.run_id)
        self.session.context.close.assert_awaited_once()

    async def test_no_screenshot_when_disabled(self):
        self.runner.settings.screenshots = False
        result = await self.runner.execute(browser_step(), {"base_url": "https://ui.test", "password": 1234}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.locator.fill.assert_awaited_once_with("1234")
        self.page.screenshot.assert_not_awaited()

    async def test_session_limit(self):
        self.runner.settings.max_sessions = 1
        context = {"base_url": "https://ui.test", "password": "x"}
        await self.runner.execute(browser_step(), context, self.run_id)
        result = await self.runner.execute(browser_step(), context, str(uuid4()))
        self.assertIn("session limit", result["message"])

    async def test_action_dispatch_and_assertion_dispatch(self):
        self.runner._set_checked = AsyncMock()
        actions = [{"action": action, "selector": "#field", "value": "a"} for action in
                   ("fill", "select_option", "press", "check", "uncheck", "wait_for")]
        assertions = [{"type": kind, "selector": "#field", "expected": "a"} for kind in
                      ("visible", "hidden", "checked", "text_contains", "text_equals", "url_contains", "title_contains", "value_equals")]
        result = await self.runner.execute(browser_step(actions=actions, assertions=assertions), {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["actions_completed"], 6)
        self.assertEqual(len(result["assertions"]), 8)

    async def test_check_associated_label_verifies_state_and_preserves_existing_state(self):
        self.locator.evaluate.return_value = {"label": True}
        self.locator.is_checked = AsyncMock(return_value=False)
        self.expectation.to_be_enabled = AsyncMock()
        await self.runner._set_checked(self.page, self.locator, True)
        self.locator.click.assert_awaited_once()
        self.expectation.to_be_checked.assert_awaited_once_with(
            checked=True, timeout=self.runner.settings.action_timeout * 1000)
        self.locator.click.reset_mock()
        self.locator.is_checked.return_value = True
        await self.runner._set_checked(self.page, self.locator, True)
        self.locator.click.assert_not_awaited()

    async def test_check_rejects_unassociated_text(self):
        self.locator.evaluate.return_value = None
        with self.assertRaises(ValueError):
            await self.runner._set_checked(self.page, self.locator, True)
        self.locator.click.assert_not_awaited()

    async def test_check_without_label_uses_native_control(self):
        self.locator.evaluate.return_value = {"label": False}
        self.locator.is_checked = AsyncMock(return_value=False)
        self.locator.set_checked = AsyncMock()
        await self.runner._set_checked(self.page, self.locator, True)
        self.locator.set_checked.assert_awaited_once_with(True)

    async def test_semantic_target_uses_live_page_after_navigation(self):
        async def resolve_element(page, target, operation, secrets, step_title=None):
            self.page.goto.assert_awaited_once()
            self.assertEqual(target, "Password field")
            self.assertEqual(operation, "fill")
            self.assertEqual(step_title, "Sign in")
            self.assertIn("private", secrets)
            return "#observed-password"
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=resolve_element))
        item = browser_step(actions=[{"action": "goto", "path": "/login"},
                                     {"action": "fill", "target": "Password field", "value": "{{password}}"}])
        result = await self.runner.execute(item, {"base_url": "https://ui.test", "password": "private"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.page.locator.assert_any_call("#observed-password")

    async def test_ambiguous_element_pauses_and_resumes_without_replaying_actions(self):
        self.runner.resolver = MagicMock(resolve=AsyncMock(return_value=None))
        item = browser_step(actions=[{"action": "goto", "path": "/login"},
                                     {"action": "click", "target": "Sign in button"}])
        context = {"base_url": "https://ui.test"}
        result = await self.runner.execute(item, context, self.run_id)
        self.assertEqual(result["outcome"], "waiting_for_input")
        self.assertEqual(result["actions_completed"], 1)
        self.locator.click.assert_not_awaited()
        question = result["questions"][0]
        resumed = await self.runner.execute(item, {**context, question["key"]: "#sign-in"}, self.run_id,
                                            progress={"actions_completed": 1, "assertions": []})
        self.assertEqual(resumed["outcome"], "passed")
        self.page.goto.assert_awaited_once()
        self.locator.click.assert_awaited_once()
        self.runner.resolver.resolve.assert_awaited_once()

    async def test_legacy_selector_placeholder_is_discovered(self):
        self.runner.resolver = MagicMock(resolve=AsyncMock(return_value="#observed"))
        item = browser_step(actions=[{"action": "goto", "path": "/login"},
                                     {"action": "fill", "selector": "{{email_selector}}", "value": "user@example.test"}])
        result = await self.runner.execute(item, {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(self.runner.resolver.resolve.call_args.args[1], "email selector")

    async def test_assertion_discovery_pause_does_not_repeat_click(self):
        self.runner.resolver = MagicMock(resolve=AsyncMock(return_value=None))
        item = browser_step(actions=[{"action": "click", "selector": "#submit"}],
                            assertions=[{"type": "visible", "target": "Success message"}])
        context = {"base_url": "https://ui.test"}
        result = await self.runner.execute(item, context, self.run_id)
        key = result["questions"][0]["key"]
        resumed = await self.runner.execute(item, {**context, key: "#success"}, self.run_id,
                                            progress={"actions_completed": 1, "assertions": []})
        self.assertEqual(resumed["outcome"], "passed")
        self.locator.click.assert_awaited_once()

    async def test_resolver_failure_prompts_instead_of_guessing(self):
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=RuntimeError("private provider text")))
        result = await self.runner.execute(browser_step(actions=[{"action": "click", "target": "Submit button"}]),
                                           {"base_url": "https://ui.test"}, self.run_id)
        self.assertEqual(result["outcome"], "waiting_for_input")
        self.assertNotIn("private provider text", str(result))
        self.locator.click.assert_not_awaited()

    async def test_plain_language_clarification_reinspects_without_replaying(self):
        from app.inspection import AmbiguousTarget
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=[
            AmbiguousTarget(["Student at left", "Student Login at right"]), "#student"]))
        item = browser_step(actions=[{"action": "goto", "path": "/"},
                                     {"action": "click", "target": "Student"}])
        context = {"base_url": "https://ui.test"}
        result = await self.runner.execute(item, context, self.run_id)
        question = result["questions"][0]
        self.assertIn("Student Login", question["question"])
        resumed = await self.runner.execute(item, {**context, question["key"]: "The tab on the left"},
            self.run_id, progress={"actions_completed": 1, "assertions": []})
        self.assertEqual(resumed["outcome"], "passed")
        self.assertIn("The tab on the left", self.runner.resolver.resolve.call_args.args[1])
        self.page.goto.assert_awaited_once()
        self.locator.click.assert_awaited_once()

    async def test_visual_click_uses_current_element_center_not_model_point(self):
        from app.inspection import VisualTarget
        self.locator.bounding_box = AsyncMock(return_value={'x': 100, 'y': 200, 'width': 80, 'height': 40})
        self.locator.evaluate = AsyncMock(return_value=True)
        self.page.mouse.click = AsyncMock()
        self.runner._observe = AsyncMock(side_effect=['before', 'after'])
        result = {'actions_completed': 0}
        with patch('app.browser.asyncio.sleep', new_callable=AsyncMock):
            changed = await self.runner._click_and_observe(self.page, VisualTarget('#student', 3, 7), 1, result)
        self.assertTrue(changed)
        self.locator.click.assert_awaited_once_with(trial=True)
        self.page.mouse.click.assert_awaited_once_with(140, 220)
        self.assertEqual(self.locator.evaluate.call_args.args[1], {'x': 140, 'y': 220})

    async def test_covered_center_does_not_dispatch_mouse_click(self):
        from app.browser import TargetUnavailable
        from app.inspection import VisualTarget
        self.locator.bounding_box = AsyncMock(return_value={'x': 10, 'y': 20, 'width': 80, 'height': 40})
        self.locator.evaluate = AsyncMock(return_value=False)
        self.page.mouse.click = AsyncMock()
        with self.assertRaises(TargetUnavailable):
            await self.runner._click_and_observe(self.page, VisualTarget('#student', 3, 7), 1, {})
        self.page.mouse.click.assert_not_awaited()

    async def test_quoted_label_is_rechecked_before_mouse_click(self):
        from app.browser import TargetUnavailable
        from app.inspection import VisualTarget
        self.locator.evaluate = AsyncMock(return_value=False)
        self.page.mouse.click = AsyncMock()
        self.runner._observe = AsyncMock(return_value='same UI')
        with self.assertRaises(TargetUnavailable):
            await self.runner._click_and_observe(self.page,
                VisualTarget('#student', 10, 20, exact_label='Student Role'), 1, {})
        self.assertEqual(self.locator.evaluate.call_args.args[1], 'Student Role')
        self.page.mouse.click.assert_not_awaited()

    async def test_translation_requires_confirmation_then_resolves_approved_label(self):
        from app.inspection import TranslationNeeded
        import json
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=[
            TranslationNeeded('Student', ['學生']), '#student']))
        item = browser_step(actions=[{'action': 'goto', 'path': '/'},
                                     {'action': 'click', 'target': '"Student" role'}])
        context = {'base_url': 'https://ui.test'}
        result = await self.runner.execute(item, context, self.run_id)
        self.assertEqual(result['outcome'], 'waiting_for_input')
        self.assertEqual(result['actions_completed'], 1)
        self.locator.click.assert_not_awaited()
        question = result['questions'][0]
        self.assertIn('學生', question['question'])
        answer = json.loads(question['options'][0]['value'])
        resumed = await self.runner.execute(item, {**context, question['key']: answer}, self.run_id,
            progress={'actions_completed': 1, 'assertions': []})
        self.assertEqual(resumed['outcome'], 'passed')
        self.assertEqual(self.runner.resolver.resolve.call_args.args[1], '"學生" role')
        self.assertFalse(self.runner.resolver.resolve.call_args.kwargs['allow_translation'])
        self.page.goto.assert_awaited_once()
        self.locator.click.assert_awaited_once()

    async def test_declining_translation_never_clicks_the_translated_target(self):
        from app.inspection import TranslationNeeded
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=[
            TranslationNeeded('Student', ['學生']), None]))
        item = browser_step(actions=[{'action': 'click', 'target': '"Student" role'}])
        context = {'base_url': 'https://ui.test'}
        result = await self.runner.execute(item, context, self.run_id)
        key = result['questions'][0]['key']
        resumed = await self.runner.execute(item, {**context, key: 'reject'}, self.run_id,
            progress={'actions_completed': 0, 'assertions': []})
        self.assertEqual(resumed['outcome'], 'waiting_for_input')
        self.assertEqual(self.runner.resolver.resolve.call_args.args[1], '"Student" role')
        self.assertFalse(self.runner.resolver.resolve.call_args.kwargs['allow_translation'])
        self.locator.click.assert_not_awaited()

    async def test_translation_approval_is_reused_for_same_target_assertion(self):
        from app.inspection import TranslationNeeded
        import json
        self.runner.resolver = MagicMock(resolve=AsyncMock(side_effect=[
            TranslationNeeded('Student', ['學生']), '#student', '#student']))
        item = browser_step(actions=[{'action': 'click', 'target': '"Student" role'}],
                            assertions=[{'type': 'visible', 'target': '"Student" role'}])
        context = {'base_url': 'https://ui.test'}
        result = await self.runner.execute(item, context, self.run_id)
        question = result['questions'][0]
        resumed = await self.runner.execute(item,
            {**context, question['key']: json.loads(question['options'][0]['value'])}, self.run_id,
            progress={'actions_completed': 0, 'assertions': []})
        self.assertEqual(resumed['outcome'], 'passed')
        calls = self.runner.resolver.resolve.call_args_list
        self.assertEqual([call.args[1] for call in calls[1:]], ['"學生" role', '"學生" role'])
        self.locator.click.assert_awaited_once()

    def setup_visual_attempts(self, outcomes):
        from app.inspection import VisualTarget, VisualTargets
        candidates = [VisualTarget('#student', 10, 10), VisualTarget('#student-login', 30, 10)]
        self.runner.resolver = MagicMock(resolve=AsyncMock(return_value=VisualTargets(candidates)),
                                        resolve_dom=AsyncMock(return_value='#dom-target'))
        self.runner._click_and_observe = AsyncMock(side_effect=outcomes)
        return browser_step(actions=[{'action': 'click', 'target': 'Student'}])

    async def test_visual_attempts_stop_when_second_match_changes_ui(self):
        item = self.setup_visual_attempts([False, True])
        result = await self.runner.execute(item, {'base_url': 'https://ui.test'}, self.run_id)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(self.runner._click_and_observe.await_count, 2)
        self.assertEqual([c.args[1].selector for c in self.runner._click_and_observe.call_args_list],
                         ['#student', '#student-login'])
        self.runner.resolver.resolve_dom.assert_not_awaited()

    async def test_dom_inspection_only_after_all_visual_matches_fail(self):
        item = self.setup_visual_attempts([False, False, True])
        result = await self.runner.execute(item, {'base_url': 'https://ui.test'}, self.run_id)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(self.runner._click_and_observe.await_count, 3)
        self.assertEqual(self.runner._click_and_observe.call_args.args[1], '#dom-target')
        excluded = self.runner.resolver.resolve_dom.call_args.kwargs['excluded_attempts']
        self.assertEqual(len(set(excluded)), 2)
        self.assertNotIn('pending_click', result)

    async def test_first_changed_or_unknown_observation_stops_attempts(self):
        for outcome in (True, None):
            item = self.setup_visual_attempts([outcome])
            result = await self.runner.execute(item, {'base_url': 'https://ui.test'}, self.run_id)
            self.assertEqual(result['outcome'], 'passed')
            self.runner._click_and_observe.assert_awaited_once()
            self.runner.resolver.resolve_dom.assert_not_awaited()

    async def test_dom_clarification_resume_does_not_repeat_visual_attempts(self):
        from app.inspection import AmbiguousTarget
        item = self.setup_visual_attempts([False, False, True])
        self.runner.resolver.resolve_dom.side_effect = [AmbiguousTarget(['Student tab', 'Student link']), '#dom-target']
        context = {'base_url': 'https://ui.test'}
        result = await self.runner.execute(item, context, self.run_id)
        self.assertEqual(result['outcome'], 'waiting_for_input')
        self.assertEqual(result['actions_completed'], 0)
        self.assertEqual(result['pending_click']['action'], 1)
        key = result['questions'][0]['key']
        resumed = await self.runner.execute(item, {**context, key: 'Student tab in the middle'}, self.run_id,
            progress={k: result[k] for k in ('actions_completed', 'assertions', 'pending_click')})
        self.assertEqual(resumed['outcome'], 'passed')
        self.assertEqual(self.runner._click_and_observe.await_count, 3)
        self.runner.resolver.resolve.assert_awaited_once()
        self.assertIn('Student tab in the middle', self.runner.resolver.resolve_dom.call_args.args[1])
        self.assertEqual(self.runner.resolver.resolve_dom.call_args.kwargs['excluded_attempts'],
                         result['pending_click']['excluded_attempts'])

    async def test_unavailable_visual_centers_are_exhausted_before_dom(self):
        from app.browser import TargetUnavailable
        item = self.setup_visual_attempts([TargetUnavailable(), TargetUnavailable(), True])
        result = await self.runner.execute(item, {'base_url': 'https://ui.test'}, self.run_id)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(self.runner._click_and_observe.await_count, 3)
        self.runner.resolver.resolve_dom.assert_awaited_once()
        self.assertEqual(self.runner.resolver.resolve_dom.call_args.kwargs['excluded_attempts'], [])

    async def test_unchanged_visual_observation_records_false(self):
        from app.inspection import VisualTarget
        self.locator.bounding_box = AsyncMock(return_value={'x': 100, 'y': 200, 'width': 80, 'height': 40})
        self.locator.evaluate = AsyncMock(return_value=True)
        self.page.mouse.click = AsyncMock()
        self.runner._observe = AsyncMock(return_value='same UI')
        result = {'actions_completed': 0}
        with patch('app.browser.asyncio.sleep', new_callable=AsyncMock):
            changed = await self.runner._click_and_observe(self.page, VisualTarget('#student', 3, 7), 1, result)
        self.assertIs(changed, False)
        self.assertEqual(self.runner._observe.await_count, 4)
        self.assertIs(result['observations'][0]['ui_changed'], False)
        self.page.mouse.click.assert_awaited_once_with(140, 220)


if __name__ == "__main__":
    unittest.main()
