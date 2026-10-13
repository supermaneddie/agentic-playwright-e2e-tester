import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from app.config import Settings
from app.inspection import ElementResolver, VIEWPORT_SNAPSHOT_JS
from app.models import InspectionQuery
from app.service import TestService


class InspectionScrollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))

    async def test_wheel_uses_selected_panel_and_returns_masked_view_without_llm(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={'width': 800, 'height': 600}),
            mouse=SimpleNamespace(move=AsyncMock(), wheel=AsyncMock()),
            screenshot=AsyncMock(return_value=b'png'), locator=Mock())
        self.resolver._request = AsyncMock()
        with patch('app.inspection.asyncio.sleep', AsyncMock()):
            preview = await self.resolver.inspection_view(page, direction='down', x=.25, y=.75)
        page.mouse.move.assert_awaited_once_with(200, 450)
        page.mouse.wheel.assert_awaited_once_with(0, 420)
        self.assertFalse(page.screenshot.call_args.kwargs['full_page'])
        self.assertIn('mask', page.screenshot.call_args.kwargs)
        self.assertEqual(preview['coordinate_space'], 'viewport')
        self.resolver._request.assert_not_awaited()
        page.mouse.wheel.reset_mock()
        await self.resolver.inspection_view(page)
        page.mouse.wheel.assert_not_awaited()

    async def test_viewport_lookup_preserves_scroll_and_filters_offscreen_rectangles(self):
        items = [{'id': str(i), 'tag': 'button', 'box': {
            'x': 20, 'y': y, 'width': 100, 'height': 30}} for i, y in enumerate((-100, 100, 1000))]
        page = SimpleNamespace(evaluate=AsyncMock(return_value={
            'elements': items, 'viewport': {'width': 800, 'height': 600}}),
            screenshot=AsyncMock(return_value=b'png'), locator=Mock())
        self.resolver._request = AsyncMock(return_value='{"candidate_ids": ["0", "1", "2"]}')
        preview = await self.resolver.inspect_candidates(page, 'prompt', 'Button', [], full_page=False)
        self.assertEqual([item['id'] for item in preview['candidates']], ['1'])
        self.assertEqual(page.evaluate.await_count, 1)
        self.assertEqual(page.evaluate.call_args.args[0], VIEWPORT_SNAPSHOT_JS)
        self.assertEqual(preview['coordinate_space'], 'viewport')
        self.assertFalse(page.screenshot.call_args.kwargs['full_page'])

    async def test_scroll_requires_active_question_and_invalidates_all_run_selections(self):
        case = {'status': 'waiting_for_input', 'run': {'id': 'run', 'questions': [
            {'key': 'target', 'inspection': True}]}}
        resolver = SimpleNamespace(inspection_view=AsyncMock(return_value={'candidates': []}))
        runner = SimpleNamespace(sessions={'run': SimpleNamespace(page=Mock())}, resolver=resolver)
        service = TestService(SimpleNamespace(get=AsyncMock(return_value=case)), Mock(), runner)
        service.inspection_previews = {('run', 'target'): {'old': {}}, ('run', 'second'): {'old': {}},
                                      ('other', 'target'): {'keep': {}}}
        query = InspectionQuery(run_id='run', key='target', mode='prompt', action='scroll', direction='up')
        await service.inspect_elements('case', query)
        self.assertEqual(list(service.inspection_previews), [('other', 'target')])
        self.assertEqual(resolver.inspection_view.call_args.kwargs['direction'], 'up')
        case['status'] = 'running'
        with self.assertRaises(HTTPException) as error:
            await service.inspect_elements('case', query)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(resolver.inspection_view.await_count, 1)

    async def test_scroll_schema_bounds_and_locate_query(self):
        base = dict(run_id='run', key='target', mode='prompt')
        InspectionQuery(**base, action='preview')
        for values in ({'action': 'locate'}, {'action': 'scroll', 'x': -1},
                       {'action': 'scroll', 'y': float('nan')}, {'action': 'scroll', 'direction': 'invalid'}):
            with self.assertRaises(ValueError):
                InspectionQuery(**base, **values)
