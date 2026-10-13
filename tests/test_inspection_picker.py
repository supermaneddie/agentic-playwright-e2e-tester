import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.browser import BrowserRunner, SelectorNeeded, Session
from app.config import Settings
from app.inspection import ElementResolver, VisualTarget
from app.models import BrowserAction, BrowserStep, InspectionQuery
from app.service import TestService


class InspectionPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_requires_active_question_and_rejects_forged_selection(self):
        case = {"status": "waiting_for_input", "run": {"id": "run", "questions": [
            {"key": "selector", "inspection": True}]}}
        store = SimpleNamespace(get=AsyncMock(return_value=case))
        service = TestService(store, Mock())
        with self.assertRaises(HTTPException) as error:
            await service.inspect_elements("case", InspectionQuery(
                run_id="old", key="selector", mode="css", query="button"))
        self.assertEqual(error.exception.status_code, 409)
        for value in ("forged-token", {"kind": "inspection_rectangle", "entry": {}}):
            with self.assertRaises(HTTPException) as error:
                await service.answer("case", {"selector": value})
            self.assertEqual(error.exception.status_code, 422)

    async def test_confirmed_rectangle_clicks_center_once_and_stale_selection_asks_again(self):
        settings = Settings(llm={"url": "https://llm.test/v1", "model": "test"})
        runner = BrowserRunner(settings.browser, Path('/tmp'))
        page = SimpleNamespace(mouse=SimpleNamespace(click=AsyncMock()))
        entry = {"box": {"x": 10, "y": 20, "width": 60, "height": 40}}
        context = {"target": {"kind": "inspection_rectangle", "entry": entry}}
        action = BrowserAction(action="click", selector="{{target}}")
        step = SimpleNamespace(title="Choose")
        result = {"actions_completed": 0, "pending_click": {"action": 1, "excluded_attempts": []}}
        with patch('app.browser.restore', AsyncMock(return_value=VisualTarget('button', 40, 40))):
            await runner._click_action(page, action, context, step, 1, "run", result, [])
        page.mouse.click.assert_awaited_once_with(40, 40)
        self.assertEqual(result['actions_completed'], 1)
        self.assertNotIn('pending_click', result)
        self.assertEqual(result['observations'][0]['rectangle'], entry['box'])
        with patch('app.browser.restore', AsyncMock(return_value=None)):
            with self.assertRaises(SelectorNeeded):
                await runner._selector(page, action, context, "Choose", "action-1", "run", [])
        page.mouse.click.assert_awaited_once()

    async def test_both_modes_send_screenshot_and_ignore_unobserved_ids(self):
        resolver = ElementResolver(Settings(llm={"url": "https://llm.test/v1", "model": "test"}))
        items = [{"id": str(i), "tag": "button", "name": "Button", "box": {
            "x": i * 100, "y": 20 + i * 1000, "width": 50, "height": 30}} for i in range(2)]
        for mode in ('prompt', 'css'):
            snapshot = {"elements": items, "viewport": {"width": 800, "height": 600}}
            page = SimpleNamespace(evaluate=AsyncMock(side_effect=[{"width": 800, "height": 1800}, snapshot, items]),
                screenshot=AsyncMock(return_value=b'png'), locator=Mock())
            resolver._request = AsyncMock(return_value='{"candidate_ids":["0","1","invented"]}')
            preview = await resolver.inspect_candidates(page, mode, 'button', [])
            self.assertEqual(preview['candidates'], items)
            self.assertEqual(preview['viewport'], {'width': 800, 'height': 1800})
            self.assertEqual(preview['coordinate_space'], 'document')
            self.assertTrue(page.screenshot.call_args.kwargs['full_page'])
            self.assertIn('mask', page.screenshot.call_args.kwargs)
            content = resolver._request.call_args.args[1]
            self.assertEqual(content[1]['image_url']['url'], 'data:image/png;base64,cG5n')
            self.assertEqual(json.loads(content[0]['text'])['mode'], mode)

    async def test_confirmed_answer_persists_rectangle_and_rejects_moved_target(self):
        case = {"status": "waiting_for_input", "run": {"id": "run", "questions": [
            {"key": "target", "inspection": True}]}}
        store = SimpleNamespace(get=AsyncMock(return_value=case), update_run=AsyncMock(), log=AsyncMock())
        service = TestService(store, Mock(), SimpleNamespace(sessions={"run": SimpleNamespace(page=Mock())}))
        service.launch = Mock()
        selection = {"kind": "inspection_rectangle", "entry": {"box": {
            "x": 10, "y": 20, "width": 60, "height": 40}, "point": {"x": 40, "y": 40}}}
        service.inspection_previews[("run", "target")] = {"token": selection}
        with patch('app.inspection_cache.restore', AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as error:
                await service.answer('case', {'target': 'token'})
            self.assertEqual(error.exception.status_code, 409)
        service.launch.assert_not_called()
        with patch('app.inspection_cache.restore', AsyncMock(return_value=VisualTarget('button', 40, 40))):
            await service.answer('case', {'target': 'token'})
        self.assertEqual(service.launch.call_args.args[2].resume, {'target': selection})
        self.assertFalse(service.inspection_previews)

    async def test_query_schema_rejects_empty_and_invalid_modes(self):
        for mode, query in (("css", "  "), ("javascript", "alert(1)")):
            with self.assertRaises(ValueError):
                InspectionQuery(run_id="run", key="selector", mode=mode, query=query)

    async def test_real_browser_lookup_confirm_click_and_stale_selection(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest("Chromium unavailable")
            browser = await driver.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('''<button onclick="window.clicks=(window.clicks||0)+1">First</button>
                    <button onclick="window.clicks=(window.clicks||0)+1">Second</button>''')
                settings = Settings(llm={"url": "https://llm.test/v1", "model": "test"})
                resolver = ElementResolver(settings)
                requests = []

                async def request(system, content):
                    requests.append(content)
                    payload = json.loads(content[0]["text"])
                    self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
                    return json.dumps({"candidate_ids": [item["id"] for item in payload["page"]["elements"]
                        if item["tag"] == "button"] + ["invented"]})

                resolver._request = request
                runner = BrowserRunner(settings.browser, Path('/tmp'), resolver=resolver)
                runner.sessions["run"] = Session(page=page)
                case = {"status": "waiting_for_input", "run": {"id": "run", "questions": [
                    {"key": "target", "inspection": True}]}}
                store = SimpleNamespace(get=AsyncMock(return_value=case), update_run=AsyncMock(), log=AsyncMock())
                graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={"context": {}})))
                service = TestService(store, graph, runner)
                service.launch = Mock()
                previews = []
                for mode, query in (("prompt", "the buttons"), ("css", "button")):
                    preview = await service.inspect_elements("case", InspectionQuery(
                        run_id="run", key="target", mode=mode, query=query))
                    self.assertEqual(len(preview["candidates"]), 2)
                    self.assertIsNone(await page.evaluate("window.clicks"))
                    previews.append(preview)
                self.assertEqual(len(requests), 2)
                with self.assertRaises(HTTPException):
                    await service.answer("case", {"target": previews[0]["candidates"][0]["id"]})
                chosen = previews[1]["candidates"][1]
                await service.answer("case", {"target": chosen["id"]})
                answer = service.launch.call_args.args[2].resume["target"]
                self.assertEqual(answer["entry"]["box"], chosen["box"])
                action = BrowserAction(action="click", selector="{{target}}")
                step = BrowserStep(title="Choose", actions=[action], assertions=[{"type": "title_contains", "expected": ""}])
                result = {"actions_completed": 0}
                await runner._click_action(page, action, {"target": answer}, step, 1, "run", result, [])
                self.assertEqual(await page.evaluate("window.clicks"), 1)
                self.assertEqual(result["actions_completed"], 1)
                self.assertEqual(result["observations"][0]["source"], "confirmed_rectangle")
                await page.locator('button').last.evaluate("el => el.style.marginLeft = '100px'")
                with self.assertRaises(SelectorNeeded):
                    await runner._selector(page, action, {"target": answer}, "Choose", "action-1", "run", [])
                self.assertEqual(await page.evaluate("window.clicks"), 1)
            finally:
                await browser.close()
