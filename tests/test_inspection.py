import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.config import Settings
from app.inspection import ElementResolver


REF = "tb" + "a" * 32 + "-0"


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_transcription_includes_screen_context_and_source_hint(self):
        self.resolver._request = AsyncMock(return_value='{"text":"0338"}')
        result = await self.resolver.read_image(b"crop", page_screenshot=b"screen",
            target="Generated CAPTCHA on the left side")
        self.assertEqual(result, "0338")
        system, content = self.resolver._request.await_args.args
        self.assertIn("what is the generated captcha on the screen?", system)
        self.assertIn("login button", system)
        self.assertEqual(content[0]["image_url"], {
            "url": "data:image/png;base64,Y3JvcA==", "detail": "high"})
        self.assertEqual(content[1]["image_url"], {
            "url": "data:image/png;base64,c2NyZWVu", "detail": "high"})
        self.assertIn("left side", content[2]["text"])

    async def test_image_transcription_payload_and_validation(self):
        self.resolver._request = AsyncMock(return_value='{"text":"Ab3X"}')
        self.assertEqual(await self.resolver.read_image(b"png"), "Ab3X")
        system, content = self.resolver._request.await_args.args
        self.assertIn("untrusted", system)
        self.assertEqual(content[0]["image_url"]["url"], "data:image/png;base64,cG5n")
        for response in ('{"text":null}', '{"text":" "}', '{"text":123}',
                         '{"text":"Ab3X", "action":"click"}', 'not json'):
            self.resolver._request.return_value = response
            with self.assertRaises(ValueError):
                await self.resolver.read_image(b"png")

    def setUp(self):
        self.settings = Settings(browser={"vision": False}, llm={"url": "https://model.test/v1", "model": "test"})
        self.resolver = ElementResolver(self.settings)
        self.snapshot = {"elements": [{"id": REF, "tag": "input", "type": "email", "name": "Email"}]}

    async def test_model_only_selects_an_observed_id_and_secrets_are_redacted(self):
        real_client = httpx.AsyncClient
        def handler(request):
            body = json.loads(request.content)
            self.assertNotIn("private-value", request.content.decode())
            self.assertIn("untrusted", body["messages"][0]["content"])
            self.assertIn("placeholder", body["messages"][0]["content"])
            self.assertIn("wrapping-label", body["messages"][0]["content"])
            self.assertIn("form/group heading", body["messages"][0]["content"])
            self.assertEqual(body["model"], "test")
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"element_id": REF})}}]})
        with patch("app.inspection.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            selected = await self.resolver.choose("Email private-value", "fill", self.snapshot, None, ["private-value"])
        self.assertEqual(selected, REF)

    async def test_related_labels_and_form_context_reach_the_model(self):
        real_client = httpx.AsyncClient
        snapshot = {"step_context": "Sign in", "elements": [{"id": REF, "tag": "input", "type": "email", "name": "Email", "placeholder": "Your work email",
                                  "labels": [{"text": "Account email", "association": "label-for"}],
                                  "labelled_by": ["Email address"], "described_by": ["Use your work account"],
                                  "context": {"form_heading": "Sign in"},
                                  "nearby_labels": [{"text": "Email", "relation": "above", "distance": 8}]}]}
        def handler(request):
            data = json.loads(json.loads(request.content)['messages'][1]['content'])
            element = data['page']['elements'][0]
            self.assertEqual(data['step'], 'Sign in')
            self.assertEqual(element['placeholder'], 'Your work email')
            self.assertEqual(element['labels'][0]['association'], 'label-for')
            self.assertEqual(element['context']['form_heading'], 'Sign in')
            self.assertEqual(element['nearby_labels'][0]['relation'], 'above')
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"element_id": REF})}}]})
        with patch('app.inspection.httpx.AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            self.assertEqual(await self.resolver.choose('Login email field', 'fill', snapshot, None, []), REF)

    async def test_invented_id_and_arbitrary_selector_are_rejected(self):
        real_client = httpx.AsyncClient
        for choice in ("not-observed", "#email", None):
            def handler(request):
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"element_id": choice})}}]})
            with patch("app.inspection.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
                self.assertIsNone(await self.resolver.choose("email", "fill", self.snapshot, None, []))
                if choice is not None:
                    with self.assertRaises(ValueError):
                        await self.resolver.choose("email", "fill", self.snapshot, None, [], strict=True)

    async def test_optional_image_is_sent_as_multimodal_content(self):
        real_client = httpx.AsyncClient
        def handler(request):
            content = json.loads(request.content)["messages"][1]["content"]
            self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,cG5n")
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"element_id":null}'}}]})
        with patch("app.inspection.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            await self.resolver.choose("Email", "fill", self.snapshot, b"png", [])

    async def test_live_match_is_unique_visible_and_constructed_locally(self):
        page = MagicMock()
        locator = MagicMock()
        locator.first.wait_for = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.is_visible = AsyncMock(return_value=True)
        page.locator.return_value = locator
        page.evaluate = AsyncMock(return_value=self.snapshot)
        page.screenshot = AsyncMock(return_value=b"png")
        self.resolver.choose = AsyncMock(return_value=REF)
        selector = await self.resolver.resolve(page, "Email", "fill", [])
        self.assertEqual(selector, f'[data-testbench-ref="{REF}"]')
        page.screenshot.assert_not_awaited()
        locator.count.return_value = 2
        self.assertIsNone(await self.resolver.resolve(page, "Email", "fill", []))
        locator.count.return_value = 1
        locator.is_visible.return_value = False
        self.assertIsNone(await self.resolver.resolve(page, "Email", "fill", []))

    async def test_visual_first_does_not_extract_dom(self):
        from app.inspection import VisualTarget
        self.settings.browser.vision = True
        page = MagicMock()
        page.screenshot = AsyncMock(return_value=b'png')
        async def evaluate(script, arg=None):
            return {'width': 800, 'height': 600} if arg is None else arg['id']
        page.evaluate = AsyncMock(side_effect=evaluate)
        self.resolver._request = AsyncMock(return_value='{"candidates":[{"description":"Student tab","x":120,"y":80}]}')
        self.resolver.choose = AsyncMock()
        result = await self.resolver.resolve(page, 'Student', 'click', [])
        self.assertIsInstance(result, VisualTarget)
        self.assertEqual((result.x, result.y), (120, 80))
        self.resolver.choose.assert_not_awaited()
        self.assertNotIn('elements', str(self.resolver._request.call_args))

    async def test_non_click_visual_ambiguity_asks_for_clarification(self):
        from app.inspection import AmbiguousTarget
        self.settings.browser.vision = True
        page = MagicMock(screenshot=AsyncMock(return_value=b'png'),
                         evaluate=AsyncMock(return_value={'width': 800, 'height': 600}))
        self.resolver._request = AsyncMock(return_value='{"candidates":[{"description":"Student left","x":20,"y":40},{"description":"Student Login right","x":400,"y":40}]}')
        self.resolver.choose = AsyncMock()
        with self.assertRaises(AmbiguousTarget) as error:
            await self.resolver.resolve(page, 'Student', 'fill', [])
        self.assertEqual(len(error.exception.candidates), 2)
        self.resolver.choose.assert_not_awaited()

    async def test_missing_or_broken_vision_falls_back(self):
        self.settings.browser.vision = True
        page = MagicMock()
        page.locator.return_value.first.wait_for = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.locator.return_value.is_visible = AsyncMock(return_value=True)
        page.evaluate = AsyncMock(return_value=self.snapshot)
        self.resolver.choose = AsyncMock(return_value=REF)
        for outcome in (None, RuntimeError('unsupported images')):
            self.resolver.visual = AsyncMock(return_value=outcome if outcome is None else None,
                                             side_effect=outcome)
            self.assertEqual(await self.resolver.resolve(page, 'Email', 'fill', []),
                             f'[data-testbench-ref="{REF}"]')
        self.assertIsNone(self.resolver.choose.call_args.args[3])

    async def test_full_page_visual_uses_converted_viewport_coordinates(self):
        from app.inspection import FULL_PAGE_JS, HIT_TEST_JS, VisualTarget
        page = MagicMock(screenshot=AsyncMock(return_value=b'png'))

        async def evaluate(script, arg=None):
            if script == FULL_PAGE_JS:
                return {'width': 800, 'height': 2000}
            self.assertEqual(script, HIT_TEST_JS)
            self.assertTrue(arg['fullPage'])
            self.assertEqual((arg['x'], arg['y']), (100, 1800))
            return {'id': arg['id'], 'x': 100, 'y': 400}

        page.evaluate = AsyncMock(side_effect=evaluate)
        self.resolver._request = AsyncMock(return_value=json.dumps({'candidates': [
            {'description': 'Bottom', 'x': 100, 'y': 1800}]}))
        result = await self.resolver.visual(page, 'Bottom', 'click', [], None)
        self.assertIsInstance(result, VisualTarget)
        self.assertEqual((result.x, result.y), (100, 400))
        self.assertTrue(page.screenshot.call_args.kwargs['full_page'])
        self.assertEqual(page.screenshot.call_args.kwargs['scale'], 'css')
        self.assertIn('mask', page.screenshot.call_args.kwargs)

    async def test_out_of_view_coordinate_is_rejected(self):
        page = MagicMock(screenshot=AsyncMock(return_value=b'png'),
                         evaluate=AsyncMock(return_value={'width': 800, 'height': 600}))
        self.resolver._request = AsyncMock(return_value='{"candidates":[{"description":"Outside","x":900,"y":40}]}')
        self.assertIsNone(await self.resolver.visual(page, 'Student', 'click', [], None))
        self.assertEqual(page.evaluate.await_count, 1)

    async def test_multiple_visual_click_matches_are_returned_in_order(self):
        from app.inspection import VisualTargets
        self.settings.browser.vision = True
        page = MagicMock(screenshot=AsyncMock(return_value=b'png'))
        async def evaluate(script, arg=None):
            return {'width': 800, 'height': 600} if arg is None else arg['id']
        page.evaluate = AsyncMock(side_effect=evaluate)
        self.resolver._request = AsyncMock(return_value='{"candidates":[{"description":"Student left","x":20,"y":40},{"description":"Student Login right","x":400,"y":40}]}')
        self.resolver.choose = AsyncMock()
        result = await self.resolver.resolve(page, 'Student', 'click', [])
        self.assertIsInstance(result, VisualTargets)
        self.assertEqual([c.x for c in result.candidates], [20, 400])
        self.assertNotEqual(result.candidates[0].selector, result.candidates[1].selector)
        self.resolver.choose.assert_not_awaited()

    async def test_dom_fallback_excludes_already_clicked_elements(self):
        page = MagicMock(evaluate=AsyncMock(return_value={'elements': [
            {'id': REF, 'name': 'Already tried', 'click_attempt': 'attempt-one'},
            {'id': REF[:-1] + '1', 'name': 'Student', 'click_attempt': None},
        ]}))
        page.locator.return_value.first.wait_for = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.locator.return_value.is_visible = AsyncMock(return_value=True)
        self.resolver.choose = AsyncMock(return_value=REF[:-1] + '1')
        self.resolver.visual = AsyncMock()
        result = await self.resolver.resolve_dom(page, 'Student', 'click', [],
                                                excluded_attempts=['attempt-one'])
        self.assertEqual(result, f'[data-testbench-ref="{REF[:-1]}1"]')
        snapshot = self.resolver.choose.call_args.args[2]
        self.assertEqual([e['name'] for e in snapshot['elements']], ['Student'])
        self.resolver.visual.assert_not_awaited()
