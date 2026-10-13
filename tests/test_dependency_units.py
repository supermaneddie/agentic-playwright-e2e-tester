import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import HTTPException

from app.config import TestingConfig
from app.models import Step
from app.runner import HTTPRunner
from app.service import TestService


class DependencyUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_client_cookies_persist_and_runs_are_isolated(self):
        requests = []
        def handler(request):
            requests.append((request.url.path, request.headers.get('cookie')))
            return httpx.Response(200, headers={'set-cookie': 'session=authenticated; Path=/'})
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(handler))
        step = Step(title='Login', path='/login', assertions=[{'type': 'status', 'expected': 200}])
        context = {'base_url': 'https://example.test'}
        try:
            await runner.execute(step, context, 'export')
            original = runner.sessions['export']
            await runner.execute(step.model_copy(update={'path': '/export'}), context, 'export')
            self.assertIs(original, runner.sessions['export'])
            await runner.execute(step, context, 'independent')
            self.assertEqual(requests, [('/login', None), ('/export', 'session=authenticated'), ('/login', None)])
            await runner.close_run('export')
            self.assertTrue(original.is_closed)
            await runner.execute(step, context, 'export')
            self.assertEqual(requests[-1], ('/login', None))
        finally:
            await runner.close()
        self.assertFalse(runner.sessions)

    async def test_dependency_order_deduplication_and_cycles(self):
        cases = {name: {'testcase_id': name, 'project_id': 'project', 'test_type': 'http', 'depends_on': dependencies}
                 for name, dependencies in [('login', []), ('left', ['login']), ('right', ['login']),
                                             ('export', ['left', 'right'])]}
        service = TestService(AsyncMock(get=AsyncMock(side_effect=lambda key: cases.get(key))), None)
        self.assertEqual([case['testcase_id'] for case in await service.dependency_cases(cases['export'])],
                         ['login', 'left', 'right'])
        cases['login']['depends_on'] = ['export']
        with self.assertRaises(HTTPException) as caught:
            await service.dependency_cases(cases['export'])
        self.assertEqual(caught.exception.status_code, 422)
        cases['login']['depends_on'] = ['missing']
        with self.assertRaises(HTTPException):
            await service.dependency_cases(cases['export'])

    async def test_workflow_executes_dependencies_before_parent_and_stops_on_failure(self):
        from unittest.mock import Mock, patch
        from app.models import Plan
        from app.workflow import build_workflow

        for login_status in (200, 500):
            with self.subTest(login_status=login_status):
                nodes = {}
                graph_builder = Mock()
                graph_builder.add_node.side_effect = lambda name, fn: nodes.update({name: fn})
                calls = []
                def handler(request):
                    calls.append((request.url.path, request.headers.get('cookie')))
                    return httpx.Response(login_status if request.url.path == '/login' else 200,
                                          headers={'set-cookie': 'session=yes; Path=/'})
                runner = HTTPRunner(TestingConfig(), httpx.MockTransport(handler))
                planner = AsyncMock()
                planner.create.side_effect = lambda case: Plan(
                    summary=case['name'], steps=[Step(title=case['name'], path='/' + case['name'],
                                                      assertions=[{'type': 'status', 'expected': 200}])])
                with patch('app.workflow.StateGraph', return_value=graph_builder):
                    build_workflow(planner, runner, None, 30)
                state = {'case': {'name': 'export', 'test_data': {}},
                         'dependency_cases': [{'name': 'login', 'test_data': {}}],
                         'context': {'base_url': 'https://example.test'}}
                config = {'configurable': {'thread_id': 'run'}}
                try:
                    state.update(await nodes['summarize_checklist'](state))
                    while not state['done']:
                        state.update(nodes['prepare_test'](state))
                        self.assertEqual(state['questions'], [])
                        state.update(await nodes['execute_http_test'](state, config))
                    expected = [('/login', None)]
                    if login_status == 200:
                        expected.append(('/export', 'session=yes'))
                    self.assertEqual(calls, expected)
                finally:
                    await runner.close()

    async def test_cached_dependency_skips_planner_and_uses_its_element_cache(self):
        from unittest.mock import Mock, patch
        from app.models import BrowserPlan
        from app.store import Store
        from app.workflow import build_workflow

        def plan(name):
            return BrowserPlan(summary=name, steps=[{
                'kind': 'browser', 'title': name,
                'actions': [{'action': 'goto', 'path': '/' + name}],
                'assertions': [{'type': 'url_contains', 'expected': '/' + name}],
            }])

        nodes = {}
        graph_builder = Mock()
        graph_builder.add_node.side_effect = lambda name, fn: nodes.update({name: fn})
        store = Store(Mock())
        store.save_plan = AsyncMock()
        planner = AsyncMock()
        planner.create.return_value = plan('export')
        observed = []
        async def execute(step, context, run_id, **kwargs):
            observed.append((step.title, run_id, store.inspection_case_id.get()))
            return {'title': step.title, 'outcome': 'passed', 'assertions': []}
        browser = AsyncMock(execute=AsyncMock(side_effect=execute))
        with patch('app.workflow.StateGraph', return_value=graph_builder):
            build_workflow(planner, None, None, 30, browser, store)
        root = {'testcase_id': 'export', 'name': 'export', 'test_type': 'browser', 'test_data': {}}
        state = {'case': root, 'dependency_cases': [{
            'testcase_id': 'login', 'name': 'login', 'test_type': 'browser', 'test_data': {},
            'saved_plan': plan('login').model_dump(),
        }], 'context': {'base_url': 'https://example.test'}}
        state.update(await nodes['summarize_checklist'](state))
        planner.create.assert_awaited_once()
        planning_case = planner.create.await_args.args[0]
        self.assertEqual(planning_case['testcase_id'], root['testcase_id'])
        self.assertEqual(planning_case['completed_dependencies'][0]['name'], 'login')
        while not state['done']:
            state.update(nodes['prepare_test'](state))
            state.update(await nodes['execute_browser_test'](state, {'configurable': {'thread_id': 'shared'}}))
            self.assertIsNone(store.inspection_case_id.get())
        self.assertEqual(observed, [('login', 'shared', 'login'), ('export', 'shared', 'export')])
        self.assertEqual(store.save_plan.await_count, 2)
        self.assertEqual(store.save_plan.await_args_list[0].args[1]['steps'][0]['title'], 'login')

    async def test_legacy_dependency_plan_extracts_only_its_own_steps(self):
        from types import SimpleNamespace
        store = AsyncMock()
        store.saved_plan.return_value = {}
        graph = AsyncMock()
        graph.aget_state.return_value = SimpleNamespace(values={
            'plan': {'summary': 'Combined', 'requirements': [], 'steps': ['setup', 'login']},
            'scopes': [{'case_id': 'setup', 'start': 0, 'end': 1, 'requirements': []},
                       {'case_id': 'login', 'start': 1, 'end': 2, 'requirements': []}],
        })
        service = TestService(store, graph)
        case = {'testcase_id': 'login', 'description': 'Login', 'depends_on': ['setup'],
                'runs': [{'id': 'old-run', 'checklist': ['redacted']} ]}
        cached = await service.dependency_plan(case)
        self.assertEqual(cached['steps'], ['login'])
        store.saved_plan.return_value = cached
        graph.aget_state.reset_mock()
        self.assertEqual(await service.dependency_plan(case), cached)
        graph.aget_state.assert_not_awaited()

    async def test_dashboard_plan_continues_after_login_without_repeating_it(self):
        import json
        from unittest.mock import Mock, patch
        from app.config import Settings
        from app.planner import Planner
        from app.workflow import build_workflow

        login_steps = [
            {'kind': 'browser', 'title': 'Fill email and password', 'actions': [
                {'action': 'goto', 'path': '/login'},
                {'action': 'fill', 'target': 'Email', 'value': '{{email}}'},
                {'action': 'fill', 'target': 'Password', 'value': '{{password}}'},
            ], 'assertions': [{'type': 'visible', 'target': 'Login'}]},
            {'kind': 'browser', 'title': 'Click Login', 'actions': [{'action': 'click', 'target': 'Login'}],
             'assertions': [{'type': 'visible', 'target': 'Dashboard'}]},
        ]
        dashboard_step = {'kind': 'browser', 'title': 'Click Dashboard',
                          'actions': [{'action': 'click', 'target': 'Dashboard'}],
                          'assertions': [{'type': 'visible', 'target': 'Dashboard page'}]}
        requests = []
        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            system = payload['messages'][0]['content']
            data = json.loads(payload['messages'][1]['content'])
            self.assertIn('MUST NOT be repeated', system)
            self.assertIn('goto is not required', system)
            self.assertNotIn('first action of the first step must be goto', system)
            self.assertEqual(len(data['completed_dependencies']), 1)
            self.assertEqual([s['title'] for s in data['completed_dependencies'][0]['steps']],
                             ['Fill email and password', 'Click Login'])
            self.assertNotIn('secret-password', json.dumps(payload))
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
                'summary': 'Open Dashboard after Login', 'steps': [dashboard_step],
            })}}]})

        nodes = {}
        builder = Mock()
        builder.add_node.side_effect = lambda name, fn: nodes.update({name: fn})
        browser = AsyncMock()
        browser.execute.side_effect = lambda step, *args, **kwargs: {
            'title': step.title, 'outcome': 'passed', 'assertions': []}
        settings = Settings(llm={'url': 'https://model.test', 'model': 'test'})
        with patch('app.workflow.StateGraph', return_value=builder):
            build_workflow(Planner(settings), None, None, 30, browser)
        root = {'testcase_id': 'dashboard', 'project': 'UI', 'name': 'Dashboard',
                'description': 'Login instructions inherited from project. Click Dashboard.',
                'prompt': None, 'test_type': 'browser', 'test_data': {}}
        state = {'case': root, 'dependency_cases': [{
            'testcase_id': 'login', 'name': 'Login', 'test_type': 'browser',
            'test_data': {'password': 'secret-password', 'email': 'user@example.test'},
            'saved_plan': {'summary': 'Login', 'steps': login_steps},
        }], 'context': {'base_url': 'https://app.test'}}
        real_client = httpx.AsyncClient
        with patch('app.planner.httpx.AsyncClient', side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(handler), **kw)):
            state.update(await nodes['summarize_checklist'](state))
        self.assertEqual(len(requests), 1)  # Login's saved plan is reused.
        self.assertEqual([s['title'] for s in state['plan']['steps']],
                         ['Fill email and password', 'Click Login', 'Click Dashboard'])
        self.assertEqual([(s['start'], s['end']) for s in state['scopes']], [(0, 2), (2, 3)])
        while not state['done']:
            state.update(nodes['prepare_test'](state))
            self.assertEqual(state['questions'], [])
            state.update(await nodes['execute_browser_test'](state, {'configurable': {'thread_id': 'shared'}}))
        self.assertEqual([call.args[0].title for call in browser.execute.await_args_list],
                         ['Fill email and password', 'Click Login', 'Click Dashboard'])
        self.assertEqual({call.args[2] for call in browser.execute.await_args_list}, {'shared'})
        # Retry steps only accepts a continuation step and does not call the planner.
        retried = await nodes['summarize_checklist'](state)
        self.assertEqual(retried['plan'], state['plan'])
        # A standalone browser case must still establish its initial page.
        with self.assertRaisesRegex(ValueError, 'begin with goto'):
            await nodes['summarize_checklist']({'case': root, 'plan': {
                'summary': 'Dashboard', 'requirements': [], 'steps': [dashboard_step]}})
