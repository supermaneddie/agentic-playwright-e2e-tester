import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite

from app.browser import BrowserRunner
from app.config import BrowserConfig
from app.inspection import VisualTarget, VisualTargets
from app.inspection_cache import capture, restore
from app.models import BrowserAction, ProjectSubmission, Submission
from app.store import Store


class InspectionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.driver = await async_playwright().start()
        self.addAsyncCleanup(self.driver.stop)
        if not Path(self.driver.chromium.executable_path).exists():
            self.skipTest('Chromium unavailable')
        self.browser = await self.driver.chromium.launch()
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.set_content('<button>Continue</button>')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = await aiosqlite.connect(Path(self.directory.name) / 'cache.sqlite3')
        self.addAsyncCleanup(self.db.close)
        self.store = Store(self.db)
        await self.store.initialize()
        project = await self.store.create_project(ProjectSubmission(name='Cache tests'))
        self.case = await self.store.create(Submission(project_id=project, description='Continue'))
        self.run = await self.store.new_run(self.case, 1)
        self.resolver = AsyncMock()
        self.resolver.resolve.return_value = 'button'
        self.runner = BrowserRunner(BrowserConfig(), Path(self.directory.name), self.resolver)
        self.runner.store = self.store
        self.item = BrowserAction(action='click', target='Continue')

    async def select(self, **kwargs):
        return await self.runner._selector(self.page, self.item, {}, 'Continue', 0, self.run, [], **kwargs)

    async def test_retry_after_restart_skips_inspection_and_stale_label_refreshes(self):
        self.assertEqual(await self.select(), 'button')
        # Reconnect to the database to exercise persistence across server restarts.
        await self.db.close()
        self.db = await aiosqlite.connect(Path(self.directory.name) / 'cache.sqlite3')
        self.addAsyncCleanup(self.db.close)
        self.store = Store(self.db)
        await self.store.initialize()
        self.runner.store = self.store
        self.run = await self.store.new_run(self.case, 2)
        # New page has none of the previous session's temporary element references.
        self.page = await self.browser.new_page()
        await self.page.set_content('<button>Continue</button>')
        self.assertIn('nth-of-type', await self.select())
        self.assertEqual(self.resolver.resolve.await_count, 1)
        await self.page.set_content('<button>Cancel</button>')
        self.assertEqual(await self.select(), 'button')
        self.assertEqual(self.resolver.resolve.await_count, 2)

    async def test_clear_removes_both_and_preserves_history(self):
        await self.select()
        await self.store.save_answers(self.case, {'answers': {'foo': 'bar'}})
        case = await self.store.get(self.case)
        self.assertTrue(case['has_saved_inspections'])
        self.assertNotIn('saved_inspections', case)
        await self.store.clear_saved_data(self.case)
        case = await self.store.get(self.case)
        self.assertFalse(case['has_saved_inspections'])
        self.assertFalse(case['has_saved_answers'])
        self.assertEqual(len(case['runs']), 1)
        await self.select()
        self.assertEqual(self.resolver.resolve.await_count, 2)

    async def test_case_isolation_and_dom_fallback_bypasses_cache(self):
        await self.select()
        self.resolver.resolve_dom.return_value = 'button'
        await self.select(dom_only=True, excluded_attempts=['failed-click'])
        self.resolver.resolve_dom.assert_awaited_once()
        case = await self.store.get(self.case)
        other = await self.store.create(Submission(project_id=case['project_id'], description='Continue'))
        self.run = await self.store.new_run(other, 1)
        await self.select()
        self.assertEqual(self.resolver.resolve.await_count, 2)

    async def test_visual_geometry_hit_test_and_ambiguity(self):
        box = await self.page.locator('button').bounding_box()
        target = VisualTarget('button', box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        entry = await capture(self.page, target)
        self.assertIsInstance(await restore(self.page, entry, None), VisualTarget)
        self.assertIsNone(await capture(self.page, VisualTargets([target, target])))
        await self.page.locator('button').evaluate("el => el.style.marginLeft = '80px'")
        self.assertIsNone(await restore(self.page, entry, None))
        await self.page.locator('button').evaluate("el => el.style.marginLeft = ''")
        await self.page.evaluate("""() => {
            const overlay = document.createElement('div');
            overlay.style = 'position:fixed;inset:0;z-index:100'; document.body.append(overlay);
        }""")
        self.assertIsNone(await restore(self.page, entry, None))

    async def test_edit_invalidates_cache(self):
        await self.select()
        case = await self.store.get(self.case)
        await self.store.update_case(self.case, Submission(project_id=case['project_id'], description='Changed'))
        self.assertFalse((await self.store.get(self.case))['has_saved_inspections'])


class CacheWithoutBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_visual_restore_rejects_moved_or_missing_target(self):
        from unittest.mock import MagicMock
        page = MagicMock()
        locator = page.locator.return_value
        locator.count = AsyncMock(return_value=1)
        locator.is_visible = AsyncMock(return_value=True)
        state = {'path': 'html > body > button', 'identity': {'disabled': False, 'label': 'Continue'},
                 'box': {'x': 0, 'y': 0, 'width': 30, 'height': 20},
                 'viewport': {'width': 800, 'height': 600}, 'scroll': {'x': 0, 'y': 0}}
        import copy
        locator.evaluate = AsyncMock(side_effect=lambda *args: copy.deepcopy(state) if len(args) == 1 else True)
        entry = await capture(page, VisualTarget('button', 15, 10))
        result = await restore(page, entry, 'Continue')
        self.assertIsInstance(result, VisualTarget)
        self.assertEqual((result.x, result.y), (15, 10))
        state['box']['x'] = 100
        self.assertIsNone(await restore(page, entry, 'Continue'))
        locator.count.return_value = 0
        self.assertIsNone(await restore(page, entry, 'Continue'))

    async def test_document_rectangle_scrolls_and_revalidates_before_reuse(self):
        import copy
        from unittest.mock import MagicMock
        from app.inspection_cache import DESCRIBE_JS, fingerprint

        page = MagicMock()
        locator = page.locator.return_value
        locator.count = AsyncMock(return_value=1)
        locator.is_visible = AsyncMock(return_value=True)
        state = {'path': 'button', 'identity': {'disabled': False, 'label': 'Continue'},
                 'box': {'x': 10, 'y': 1500, 'width': 100, 'height': 40},
                 'viewport': {'width': 800, 'height': 600}, 'scroll': {'x': 0, 'y': 0}}
        entry = copy.deepcopy(state)
        entry.update(identity=fingerprint(state['identity']), coordinate_space='document',
                     point={'x': 60, 'y': 1520})
        covered = False

        async def scroll(**kwargs):
            state['box']['y'] = 200
            state['scroll']['y'] = 1300

        locator.scroll_into_view_if_needed = AsyncMock(side_effect=scroll)
        locator.evaluate = AsyncMock(side_effect=lambda script, *args:
            copy.deepcopy(state) if script == DESCRIBE_JS else not covered)
        result = await restore(page, entry, None)
        self.assertEqual((result.x, result.y), (60, 220))
        self.assertIsInstance(await restore(page, entry, None), VisualTarget)
        covered = True
        self.assertIsNone(await restore(page, entry, None))
        covered = False
        state['box']['x'] += 50
        locator.scroll_into_view_if_needed.reset_mock()
        self.assertIsNone(await restore(page, entry, None))
        locator.scroll_into_view_if_needed.assert_not_awaited()

    async def test_persist_retry_clear_and_stale_fallback(self):
        from unittest.mock import MagicMock
        from app.inspection_cache import DESCRIBE_JS

        async with aiosqlite.connect(':memory:') as db:
            store = Store(db)
            await store.initialize()
            project = await store.create_project(ProjectSubmission(name='Cache'))
            case_id = await store.create(Submission(project_id=project, description='Continue'))
            run_id = await store.new_run(case_id, 1)
            page = MagicMock(url='https://example.test/login')
            locator = page.locator.return_value
            locator.count = AsyncMock(return_value=1)
            locator.is_visible = AsyncMock(return_value=True)
            identity = {'tag': 'BUTTON', 'label': 'Continue', 'disabled': False}
            def describe(*args):
                self.assertEqual(args[0], DESCRIBE_JS)
                return {'path': 'html > body > button', 'identity': dict(identity),
                        'box': {'x': 0, 'y': 0, 'width': 30, 'height': 20},
                        'viewport': {'width': 800, 'height': 600}, 'scroll': {'x': 0, 'y': 0}}
            locator.evaluate = AsyncMock(side_effect=describe)
            resolver = AsyncMock()
            resolver.resolve.return_value = 'button'
            runner = BrowserRunner(BrowserConfig(), Path('/tmp'), resolver)
            runner.store = store
            item = BrowserAction(action='click', target='Continue')
            async def select():
                return await runner._selector(page, item, {}, 'Continue', 0, run_id, [])
            self.assertEqual(await select(), 'button')
            run_id = await store.new_run(case_id, 2)
            self.assertEqual(await select(), 'html > body > button')
            self.assertEqual(resolver.resolve.await_count, 1)
            identity['label'] = 'Cancel'
            await select()
            self.assertEqual(resolver.resolve.await_count, 2)
            await store.save_answers(case_id, {'answers': {'a': 'b'}})
            await store.clear_saved_data(case_id)
            case = await store.get(case_id)
            self.assertFalse(case['has_saved_answers'])
            self.assertFalse(case['has_saved_inspections'])
            self.assertEqual(len(case['runs']), 2)
            await select()
            self.assertEqual(resolver.resolve.await_count, 3)
