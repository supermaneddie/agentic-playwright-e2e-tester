import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.screenshots import EXPANDED_PAGE_STYLE_JS, RESTORE_SCROLL_JS, save_page_screenshot


class SavedScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_expands_layout_masks_fields_and_restores_scroll(self):
        state = {'style': 'main {height:auto!important;overflow:visible!important}',
                 'scrolls': [{'selector': 'main', 'x': 0, 'y': 700}]}
        page = SimpleNamespace(evaluate=AsyncMock(return_value=state),
                               screenshot=AsyncMock(), locator=Mock())
        await save_page_screenshot(page, Path('/tmp/result.png'))
        self.assertEqual(page.evaluate.await_args_list[0].args, (EXPANDED_PAGE_STYLE_JS,))
        kwargs = page.screenshot.call_args.kwargs
        self.assertTrue(kwargs['full_page'])
        self.assertEqual(kwargs['style'], state['style'])
        self.assertEqual(kwargs['path'], '/tmp/result.png')
        self.assertIn('mask', kwargs)
        page.evaluate.assert_awaited_with(RESTORE_SCROLL_JS, state['scrolls'])

    async def test_failed_capture_still_restores_scroll(self):
        state = {'style': '', 'scrolls': [{'selector': 'main', 'x': 0, 'y': 700}]}
        page = SimpleNamespace(evaluate=AsyncMock(return_value=state),
                               screenshot=AsyncMock(side_effect=TimeoutError), locator=Mock())
        with self.assertRaises(TimeoutError):
            await save_page_screenshot(page, Path('/tmp/result.png'))
        page.evaluate.assert_awaited_with(RESTORE_SCROLL_JS, state['scrolls'])
