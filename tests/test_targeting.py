import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.inspection import ElementResolver
from app.planner import Planner
from app.portal import description_segments, section_updates
from app.targeting import TARGET_MATCHING_GUIDANCE, exact_target, preserve_target_quotes
from app.models import BrowserPlan


class DescriptionTests(unittest.TestCase):
    def test_quote_delimiters_enable_full_matching(self):
        for target in ('"Student Role"', '“Student Role”', "'Student Role'", '‘Student Role’',
                       'the "Student Role" option next to Teacher'):
            self.assertEqual(exact_target(target), 'Student Role')
        self.assertIsNone(exact_target('Student role'))
        self.assertIsNone(exact_target("Student's role"))

    def test_generated_targets_retain_source_quotes_and_case(self):
        plan = BrowserPlan.model_validate({'summary': 'Choose role', 'steps': [{
            'title': 'Choose role', 'actions': [{'action': 'click', 'target': 'student role option'}],
            'assertions': [{'type': 'visible', 'target': 'Student Role'}],
        }]})
        preserve_target_quotes(plan, 'Click “Student Role”')
        self.assertEqual(plan.steps[0].actions[0].target, '“Student Role” option')
        self.assertEqual(plan.steps[0].assertions[0].target, '“Student Role”')

    def test_longest_whole_phrase_and_original_capitalization(self):
        steps = [{'kind': 'browser', 'actions': [{'target': 'Student'}, {'target': 'Student role'}]}]
        text = 'Click STUDENT role, then Student. Do not match Students.'
        segments = description_segments(text, steps)
        self.assertEqual(''.join(part['text'] for part in segments), text)
        self.assertEqual([part['text'] for part in segments if part['matched']], ['STUDENT role', 'Student'])

    def test_description_highlight_updates_after_planning_and_escapes_html(self):
        env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
        env.globals['description_segments'] = description_segments
        case = {'project': 'Demo', 'testcase_id': 'abc12345',
                'description': 'Click Student role <script>alert(1)</script>', 'run': {'checklist': []}}
        templates = SimpleNamespace(env=env)

        def sections():
            return {'description': ([case['description'], case['run']['checklist']],
                                    'status_description.html', {'case': case})}

        before = section_updates(templates, sections(), {})
        self.assertNotIn('<strong>Student role</strong>', before['sections']['description'])
        case['run']['checklist'] = [{'kind': 'browser', 'actions': [{'target': 'Student role'}]}]
        after = section_updates(templates, sections(), before['versions'])
        html = after['sections']['description']
        self.assertIn('Click <strong>Student role</strong>', html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertEqual(section_updates(templates, sections(), after['versions'])['sections'], {})

    def test_literal_regex_characters_and_html_in_targets(self):
        text = 'Click <b>Student</b> and C++ (advanced).'
        steps = [{'kind': 'browser', 'actions': [{'target': '<b>Student</b>'}, {'target': 'C++ (advanced)'}]}]
        segments = description_segments(text, steps)
        self.assertEqual([part['text'] for part in segments if part['matched']],
                         ['<b>Student</b>', 'C++ (advanced)'])


class SemanticTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_quoted_dom_target_accepts_different_case_but_rejects_partial_matches(self):
        resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))
        snapshot = {'elements': [{'id': 'exact', 'name': 'Student Role'},
                                 {'id': 'short', 'name': 'Student'},
                                 {'id': 'long', 'name': 'Student Role Login'},
                                 {'id': 'case', 'name': 'student role'}]}
        for selected in ('short', 'long', 'case', 'exact'):
            resolver._request = AsyncMock(return_value=json.dumps({'element_id': selected}))
            result = await resolver.choose('"Student Role"', 'click', snapshot, None, [])
            self.assertEqual(result, selected if selected in ('exact', 'case') else None)
            payload = json.loads(resolver._request.call_args.args[1])
            self.assertEqual(payload['exact_label'], 'Student Role')
            self.assertEqual([e['id'] for e in payload['page']['elements']], ['exact', 'case'])

    async def test_quoted_dom_target_missing_does_not_relax_to_semantic_match(self):
        resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))
        resolver._request = AsyncMock()
        self.assertIsNone(await resolver.choose('"Student Role"', 'click',
            {'elements': [{'id': 'short', 'name': 'Student'}]}, None, []))
        resolver._request.assert_not_awaited()

    async def test_visual_hit_testing_receives_exact_label_and_discards_mismatch(self):
        resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))
        resolver._request = AsyncMock(return_value=json.dumps({'candidates': [
            {'description': 'Student', 'x': 10, 'y': 20},
            {'description': 'Student Role', 'x': 100, 'y': 20}]}))
        page = MagicMock(screenshot=AsyncMock(return_value=b'png'))

        async def evaluate(script, args=None):
            if args is None:
                return {'width': 800, 'height': 600}
            self.assertEqual(args['exact'], 'Student Role')
            return None if args['x'] == 10 else args['id']

        page.evaluate = AsyncMock(side_effect=evaluate)
        result = await resolver.visual(page, '"Student Role"', 'click', [], None)
        self.assertEqual(result.x, 100)
        content = resolver._request.call_args.args[1]
        self.assertEqual(json.loads(content[0]['text'])['exact_label'], 'Student Role')

    async def test_dom_live_label_is_rechecked_after_model_selection(self):
        resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))
        ref = 'tb' + 'a' * 32 + '-0'
        page = MagicMock(evaluate=AsyncMock(return_value={'elements': [{'id': ref, 'name': 'Student Role'}]}))
        page.locator.return_value.first.wait_for = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.locator.return_value.is_visible = AsyncMock(return_value=True)
        page.locator.return_value.evaluate = AsyncMock(return_value=False)
        resolver.choose = AsyncMock(return_value=ref)
        self.assertIsNone(await resolver.resolve_dom(page, '"Student Role"', 'click', []))
        self.assertEqual(page.locator.return_value.evaluate.call_args.args[1], 'Student Role')

    async def test_dom_and_visual_requests_share_semantic_matching_guidance(self):
        resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'}))
        ref = 'tb' + 'a' * 32 + '-0'
        resolver._request = AsyncMock(return_value=json.dumps({'element_id': ref}))
        target = 'Student role'
        snapshot = {'elements': [{'id': ref, 'name': 'Student', 'role': 'tab'}]}
        self.assertEqual(await resolver.choose(target, 'click', snapshot, None, []), ref)
        self.assertIn(TARGET_MATCHING_GUIDANCE, resolver._request.call_args.args[0])
        self.assertEqual(json.loads(resolver._request.call_args.args[1])['target'], target)

        resolver._request.return_value = '{"candidates": []}'
        page = MagicMock(screenshot=AsyncMock(return_value=b'png'),
                         evaluate=AsyncMock(return_value={'width': 800, 'height': 600}))
        await resolver.visual(page, target, 'click', [], 'Choose a role')
        self.assertIn(TARGET_MATCHING_GUIDANCE, resolver._request.call_args.args[0])

    async def test_planner_separates_target_phrase_from_literal_expected_text(self):
        real_client = httpx.AsyncClient
        plan = {'summary': 'Choose Student', 'steps': [{'kind': 'browser', 'title': 'Click Student role',
            'actions': [{'action': 'goto', 'path': '/'}, {'action': 'click', 'target': 'Student role'}],
            'assertions': [{'type': 'visible', 'target': 'Student role'}]}]}

        def handler(request):
            prompt = json.loads(request.content)['messages'][0]['content']
            self.assertIn(TARGET_MATCHING_GUIDANCE, prompt)
            self.assertIn('preserve the user\'s target phrase verbatim', prompt)
            self.assertIn('Do not turn "click Student role" into', prompt)
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(plan)}}]})

        settings = Settings(llm={'url': 'https://model.test', 'model': 'test'})
        with patch('app.planner.httpx.AsyncClient',
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            result = await Planner(settings).create({'project': 'Demo', 'description': 'Click Student role',
                'test_type': 'browser', 'test_data': {}, 'prompt': None})
        self.assertEqual(result.steps[0].actions[1].target, 'Student role')
        self.assertIsNone(result.steps[0].assertions[0].expected)

    async def test_explicit_text_expectations_are_not_relaxed(self):
        settings = Settings(llm={'url': 'https://model.test', 'model': 'test'})
        result = await Planner(settings).create({'description': 'Check literal text', 'test_type': 'browser',
            'test_data': {'steps': [{'title': 'Check role label', 'actions': [{'action': 'goto', 'path': '/'}],
                'assertions': [{'type': 'text_equals', 'target': 'Role label', 'expected': 'Student role'}]}]}})
        self.assertEqual(result.steps[0].assertions[0].expected, 'Student role')
