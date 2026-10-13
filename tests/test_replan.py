import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

from app.service import TestService


def case_with_status(status):
    plan = {"summary": "Old", "steps": [{"title": "Old check"}], "requirements": []}
    run = {"id": "old-run", "attempt": 1, "status": status, "checklist": plan["steps"]}
    return {"testcase_id": "case", "name": "Case", "project": "Project", "description": "Description",
            "test_type": "http", "test_data": {}, "prompt": None, "images": [], "depends_on": [],
            "status": status, "run": run, "runs": [run]}, plan


class ReplanTests(unittest.IsolatedAsyncioTestCase):
    def service(self, status):
        case, plan = case_with_status(status)
        store = AsyncMock()
        store.get.return_value = case
        store.saved_answers.return_value = {"answers": {"base_url": "https://example.test"}}
        store.new_run.return_value = "new-run"
        graph = AsyncMock()
        graph.aget_state.return_value = SimpleNamespace(values={"plan": plan})
        service = TestService(store, graph)
        service.launch = Mock()
        return service, case, plan

    async def test_replanning_uses_new_attempt_without_reusing_plan(self):
        for status in ("submitted", "planned", "passed", "failed", "error", "stopped"):
            with self.subTest(status=status):
                service, case, _ = self.service(status)
                response = await service.start("case", plan_only=True)
                self.assertEqual(response["run_id"], "new-run")
                service.store.new_run.assert_awaited_once_with("case", 2)
                service.graph.aget_state.assert_not_awaited()
                initial = service.launch.call_args.args[2]
                self.assertTrue(initial["plan_only"])
                self.assertNotIn("plan", initial)
                self.assertEqual(initial["context"]["base_url"], "https://example.test")
                self.assertEqual(case["run"]["id"], "old-run")

    async def test_start_still_reuses_latest_prepared_plan(self):
        service, _, plan = self.service("planned")
        response = await service.start("case")
        self.assertEqual(response["run_id"], "old-run")
        service.store.new_run.assert_not_awaited()
        initial = service.launch.call_args.args[2]
        self.assertEqual(initial["plan"], plan)
        self.assertFalse(initial["plan_only"])

    async def test_active_run_cannot_be_replaced_by_planning(self):
        for status in ("running", "waiting_for_input"):
            service, _, _ = self.service(status)
            with self.assertRaises(HTTPException) as caught:
                await service.start("case", plan_only=True)
            self.assertEqual(caught.exception.status_code, 409)
            service.store.new_run.assert_not_awaited()
            service.launch.assert_not_called()

    def test_plan_button_always_enabled_and_label_tracks_previous_checklists(self):
        templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"))
        template = templates.get_template("status_controls.html")
        for status in ("submitted", "planned", "running", "waiting_for_input", "passed", "failed", "error", "stopped"):
            for has_plan in (False, True):
                with self.subTest(status=status, has_plan=has_plan):
                    case, _ = case_with_status(status)
                    if not has_plan:
                        case["run"]["checklist"] = []
                    html = template.render(case=case)
                    button = re.search(r'<button data-action="plan"([^>]*)>([^<]*)</button>', html)
                    self.assertNotIn("disabled", button[1])
                    self.assertEqual(button[2], "Re-plan" if has_plan else "Plan")
        case, _ = case_with_status("running")
        case["run"] = {"id": "new-run", "checklist": [], "attempt": 2}
        self.assertIn('>Re-plan</button>', template.render(case=case))
