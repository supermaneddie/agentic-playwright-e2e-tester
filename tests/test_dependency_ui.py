import unittest
from html.parser import HTMLParser

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.portal import running_step, test_groups as group_steps


class DependencyUITests(unittest.TestCase):
    def run_data(self, results=(), status='running'):
        dependency = {'case_id': 'login', 'name': 'Login <script>', 'start': 1, 'end': 2}
        checklist = [{'title': title, 'method': 'GET', 'path': '/', 'assertions': [], 'dependency': dependency}
                     for title in ('Open login', 'Submit credentials')]
        checklist.append({'title': 'Export file', 'method': 'GET', 'path': '/export', 'assertions': []})
        return {'id': 'run', 'status': status, 'checklist': checklist, 'results': list(results)}

    def result(self, step, outcome='passed', **fields):
        return {'step': step, 'title': f'Step {step}', 'outcome': outcome, 'assertions': [], **fields}

    def test_pass_requires_every_expected_step(self):
        for results, status, expected, passed in [
            ([], 'running', 'running', 0),
            ([self.result(1)], 'running', 'running', 1),
            ([self.result(1)], 'waiting_for_input', 'waiting_for_input', 1),
            ([self.result(1)], 'failed', 'incomplete', 1),
            ([self.result(1)], 'stopped', 'stopped', 1),
            ([self.result(1), self.result(1)], 'passed', 'incomplete', 1),
            ([self.result(1), self.result(2)], 'running', 'passed', 2),
            ([self.result(1), self.result(2, 'failed')], 'failed', 'failed', 1),
            ([self.result(1), self.result(2, 'error')], 'error', 'error', 1),
        ]:
            with self.subTest(status=status, expected=expected, results=results):
                group = group_steps(self.run_data(results, status))[0]
                self.assertEqual((group['outcome'], group['passed'], group['total']), (expected, passed, 2))

    def test_superseded_attempts_do_not_affect_group_outcome(self):
        results = [self.result(1, 'failed', superseded=True), self.result(1),
                   self.result(2, 'waiting_for_input', superseded=True)]
        group = group_steps(self.run_data(results, 'waiting_for_input'))[0]
        self.assertEqual(group['outcome'], 'waiting_for_input')
        self.assertEqual(group['passed'], 1)
        results.append(self.result(2))
        group = group_steps(self.run_data(results))[0]
        self.assertEqual(group['outcome'], 'passed')
        self.assertEqual(len(group['results']), 4)

    def test_grouping_preserves_order_and_main_steps(self):
        groups = group_steps(self.run_data([self.result(1), self.result(2), self.result(3)], 'passed'))
        self.assertEqual([group['key'] for group in groups], ['login', 'main'])
        self.assertEqual([item['index'] for item in groups[0]['steps']], [1, 2])
        self.assertEqual([result['step'] for result in groups[1]['results']], [3])

    def test_results_without_checklist_still_require_full_dependency(self):
        result = self.result(1, dependency={'case_id': 'login', 'name': 'Login', 'start': 1, 'end': 2})
        group = group_steps({'status': 'failed', 'results': [result]})[0]
        self.assertEqual((group['outcome'], group['total']), ('incomplete', 2))

    def test_templates_render_collapsed_accessible_groups_and_escape_names(self):
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.details = []
                self.summaries = 0
            def handle_starttag(self, tag, attributes):
                if tag == 'details':
                    self.details.append(dict(attributes))
                if tag == 'summary':
                    self.summaries += 1

        env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
        env.globals.update(test_groups=group_steps, running_step=running_step)
        run = self.run_data([self.result(1), self.result(2), self.result(3)], 'passed')
        for name in ('status_checklist.html', 'status_results.html'):
            with self.subTest(template=name):
                html = env.get_template(name).render(case={'run': run})
                parsed = Elements()
                parsed.feed(html)
                self.assertEqual(len(parsed.details), 1)
                self.assertNotIn('open', parsed.details[0])
                self.assertEqual(parsed.details[0]['data-key'], 'dependency:run:login')
                self.assertEqual(parsed.summaries, 1)
                self.assertIn('2/2 steps passed', html)
                self.assertIn('Login &lt;script&gt;', html)
                self.assertNotIn('Login <script>', html)
                self.assertIn('Export file' if name == 'status_checklist.html' else 'Step 3', html)

    def test_failed_dependency_does_not_mark_following_step_running(self):
        run = self.run_data([self.result(1, 'failed', dependency={'case_id': 'login'})])
        self.assertIsNone(running_step(run))
