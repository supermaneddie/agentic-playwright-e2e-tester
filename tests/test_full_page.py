"""Full-page coordinates must still resolve to a live, uncovered element."""
import importlib.util
import base64
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.inspection import ElementResolver, VisualTarget
from app.inspection_cache import capture, restore


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is not installed")
class FullPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.driver = await async_playwright().start()
        self.addAsyncCleanup(self.driver.stop)
        if not Path(self.driver.chromium.executable_path).exists():
            self.skipTest('Chromium unavailable')
        self.browser = await self.driver.chromium.launch()
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page(viewport={'width': 800, 'height': 600})
        await self.page.set_content('''<style>body {margin:0} button {width:120px;height:40px}</style>
            <button id="top">Top</button><div style="height:1800px"></div>
            <button id="bottom" onclick="window.clicked=true">Bottom</button>''')
        self.resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))

    async def test_visual_bottom_target_scrolls_and_full_image_is_sent(self):
        self.resolver._request = AsyncMock(return_value=json.dumps({'candidates': [
            {'description': 'Bottom', 'x': 60, 'y': 1860}]}))
        # A previous step may have left the page scrolled.
        await self.page.evaluate('scrollTo(0, 700)')
        target = await self.resolver.visual(self.page, 'Bottom', 'click', [], None)
        self.assertIsInstance(target, VisualTarget)
        self.assertEqual(await self.page.locator(target.selector).get_attribute('id'), 'bottom')
        self.assertLess(target.y, 600)
        self.assertGreater(await self.page.evaluate('scrollY'), 0)
        image = self.resolver._request.call_args.args[1][1]['image_url']['url']
        width, height = struct.unpack('>II', base64.b64decode(image.split(',')[1])[16:24])
        self.assertEqual(width, 800)
        self.assertGreater(height, 1800)
        await self.page.mouse.click(target.x, target.y)
        self.assertTrue(await self.page.evaluate('window.clicked'))

    async def test_picker_offscreen_rectangle_restores_after_scroll_and_rejects_movement(self):
        async def request(system, content):
            page = json.loads(content[0]['text'])['page']
            return json.dumps({'candidate_ids': [el['id'] for el in page['elements'] if el['tag'] == 'button']})
        self.resolver._request = request
        for mode in ('prompt', 'css'):
            preview = await self.resolver.inspect_candidates(self.page, mode, 'button', [])
            self.assertGreater(preview['viewport']['height'], 1800)
            self.assertEqual(len(preview['candidates']), 2)
            item = preview['candidates'][-1]
            self.assertGreater(item['box']['y'], 600)
            target = VisualTarget(f'[data-testbench-ref="{item["id"]}"]', 60, 1860)
            entry = await capture(self.page, target)
            entry['coordinate_space'] = 'document'
            await self.page.evaluate('scrollTo(0, 300)')
            restored = await restore(self.page, entry, None)
            self.assertIsInstance(restored, VisualTarget)
            self.assertLess(restored.y, 600)
            # Revalidation after the confirmation scroll must also work.
            self.assertIsInstance(await restore(self.page, entry, None), VisualTarget)
        await self.page.locator('#bottom').evaluate("el => el.style.marginLeft = '50px'")
        self.assertIsNone(await restore(self.page, entry, None))

    async def test_scrolling_does_not_select_a_fixed_overlay(self):
        await self.page.evaluate('''() => {
            const overlay = document.createElement('button'); overlay.id = 'overlay';
            overlay.textContent = 'Wrong'; overlay.style = 'position:fixed;inset:0;width:100vw;height:100vh';
            document.body.append(overlay);
        }''')
        self.resolver._request = AsyncMock(return_value=json.dumps({'candidates': [
            {'description': 'Bottom', 'x': 60, 'y': 1860}]}))
        self.assertIsNone(await self.resolver.visual(self.page, 'Bottom', 'click', [], None))
        self.assertIsNone(await self.page.evaluate('window.clicked'))

    async def test_user_scroll_reveals_nested_panel_target_for_viewport_picker(self):
        await self.page.set_content('''<style>html,body {margin:0;height:100%;overflow:hidden}
            main {height:500px;overflow:auto}</style>
            <main><div style="height:1500px"></div><button>Bottom</button></main>''')
        for _ in range(4):
            await self.resolver.inspection_view(self.page, direction='down', x=.5, y=.5)
        self.assertEqual(await self.page.evaluate('scrollY'), 0)
        self.assertGreater(await self.page.locator('main').evaluate('el => el.scrollTop'), 0)

        async def request(system, content):
            snapshot = json.loads(content[0]['text'])['page']
            return json.dumps({'candidate_ids': [el['id'] for el in snapshot['elements'] if el['tag'] == 'button']})

        self.resolver._request = request
        preview = await self.resolver.inspect_candidates(self.page, 'prompt', 'Bottom', [], full_page=False)
        self.assertEqual(len(preview['candidates']), 1)
        self.assertEqual(preview['coordinate_space'], 'viewport')
        self.assertLess(preview['candidates'][0]['box']['y'], 500)

    async def test_saved_image_expands_clipped_main_and_restores_live_layout(self):
        import tempfile
        from app.screenshots import save_page_screenshot

        await self.page.set_content('''<style>html,body {margin:0;height:100%;overflow:hidden}
            main {height:500px;overflow:auto}</style>
            <main><div style="height:1900px"></div><button>Bottom</button></main>''')
        await self.page.locator('main').evaluate('el => el.scrollTop = 700')
        describe = "el => ({height:el.clientHeight, top:el.scrollTop, overflow:getComputedStyle(el).overflow})"
        before = await self.page.locator('main').evaluate(describe)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'expanded.png'
            await save_page_screenshot(self.page, path)
            width, height = struct.unpack('>II', path.read_bytes()[16:24])
            self.assertEqual(width, 800)
            self.assertGreaterEqual(height, 1900)
        self.assertEqual(await self.page.locator('main').evaluate(describe), before)
