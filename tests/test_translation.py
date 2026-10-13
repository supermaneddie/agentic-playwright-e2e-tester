import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.inspection import ElementResolver, TranslationNeeded


REF = 'tb' + 'a' * 32 + '-0'


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.resolver = ElementResolver(Settings(llm={'url': 'https://model.test', 'model': 'test'},
                                                 browser={'vision': False}))
        self.page = MagicMock()
        self.page.evaluate = AsyncMock(return_value={'elements': [
            {'id': REF, 'tag': 'span', 'name': '學生', 'visible_text': '學生'}]})
        self.locator = self.page.locator.return_value
        self.locator.first.wait_for = AsyncMock()
        self.locator.count = AsyncMock(return_value=1)
        self.locator.is_visible = AsyncMock(return_value=True)
        self.locator.evaluate = AsyncMock(return_value='學生')
        self.resolver._request = AsyncMock(return_value=json.dumps({'candidate_ids': [REF]}))

    async def test_missing_exact_label_proposes_observed_translation_for_confirmation(self):
        with self.assertRaises(TranslationNeeded) as caught:
            await self.resolver.resolve(self.page, '"Student" role', 'click', [])
        self.assertEqual(caught.exception.source, 'Student')
        self.assertEqual(caught.exception.labels, ['學生'])
        self.resolver._request.assert_awaited_once()
        self.assertIn('USER CONFIRMATION ONLY', self.resolver._request.call_args.args[0])

    async def test_rejected_translation_does_not_get_proposed_again(self):
        result = await self.resolver.resolve(self.page, '"Student" role', 'click', [], allow_translation=False)
        self.assertIsNone(result)
        self.resolver._request.assert_not_awaited()

    async def test_existing_exact_match_never_prompts_for_translation(self):
        self.page.evaluate.return_value['elements'].append({'id': REF[:-1] + '1', 'tag': 'span', 'name': 'Student'})
        self.resolver._request.return_value = json.dumps({'element_id': REF[:-1] + '1'})
        self.locator.evaluate.return_value = True
        result = await self.resolver.resolve(self.page, '"Student" role', 'click', [])
        self.assertEqual(result, f'[data-testbench-ref="{REF[:-1]}1"]')
        self.resolver._request.assert_awaited_once()
        self.assertNotIn('USER CONFIRMATION ONLY', self.resolver._request.call_args.args[0])

    async def test_invented_id_or_changed_label_cannot_be_proposed(self):
        self.resolver._request.return_value = '{"candidate_ids": ["invented"]}'
        self.assertIsNone(await self.resolver.resolve(self.page, '"Student" role', 'click', []))
        self.locator.evaluate.assert_not_awaited()
        self.resolver._request.return_value = json.dumps({'candidate_ids': [REF]})
        self.locator.evaluate.return_value = 'Teacher'
        self.assertIsNone(await self.resolver.resolve(self.page, '"Student" role', 'click', []))

    def test_confirmation_requires_explicit_choice_and_escapes_labels(self):
        env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
        question = {'key': 'translation_123', 'secret': False, 'question': 'Use 學生?', 'options': [
            {'label': 'Yes, use 學生', 'value': json.dumps({'source': 'Student', 'translation': '學生'})},
            {'label': 'No, keep original', 'value': 'reject'}]}
        html = env.get_template('status_questions.html').render(
            case={'status': 'waiting_for_input', 'run': {'id': 'run', 'questions': [question]}})
        self.assertIn('<select', html)
        self.assertIn('<option value="" disabled selected>', html)
        self.assertIn('Yes, use 學生', html)
        self.assertIn('value="reject"', html)
