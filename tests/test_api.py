import importlib.util
import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if importlib.util.find_spec("aiosqlite") is None or importlib.util.find_spec("langgraph.checkpoint.sqlite") is None:
    raise unittest.SkipTest("Install project dependencies to run SQLite/API integration tests")

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.runner import HTTPRunner


class FakeBrowser:
    def __init__(self):
        self.sessions = set()
        self.calls = []
        self.outcome = "passed"

    async def execute(self, step, context, run_id, progress=None, secret_keys=()):
        self.sessions.add(run_id)
        self.calls.append((step, context, run_id))
        return {"title": step.title, "kind": "browser", "outcome": self.outcome,
                "actions_completed": len(step.actions), "assertions": [], "fatal": self.outcome != "passed"}

    async def close_run(self, run_id):
        self.sessions.discard(run_id)

    async def close(self):
        self.sessions.clear()


class APITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(llm={"url": "http://llm.test/v1", "model": "test"}, storage={"directory": Path(self.directory.name)})
        self.runner = HTTPRunner(self.settings.testing, httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})))
        self.browser = FakeBrowser()
        self.client = TestClient(create_app(self.settings, runner=self.runner, browser_runner=self.browser))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def project(self, name="Example", **fields):
        response = self.client.post("/api/projects", json={"name": name, **fields})
        self.assertEqual(response.status_code, 201)
        return response.json()["project_id"]

    def submit(self, base_url=None):
        data = {"steps": [{"title": "Health", "path": "/health", "assertions": [{"type": "status", "expected": 200}]}]}
        if base_url:
            data["base_url"] = base_url
        response = self.client.post("/api/testcases", json={"project_id": self.project(), "description": "Check health", "test_data": data})
        self.assertEqual(response.status_code, 201)
        return response.json()["testcase_id"]

    def wait_status(self, case_id, status):
        for _ in range(200):
            case = self.client.get(f"/api/testcases/{case_id}").json()
            if case["status"] == status:
                return case
            time.sleep(.01)
        self.fail(f"Expected {status}, got {case}")

    def test_case_name_create_edit_and_display(self):
        project_id = self.project()
        payload = {"project_id": project_id, "name": "  Login check  ", "description": "Check login"}
        response = self.client.post('/api/testcases', json=payload)
        self.assertEqual(response.status_code, 201)
        case_id = response.json()['testcase_id']
        base = f'/api/testcases/{case_id}'
        self.assertEqual(self.client.get(base).json()['name'], 'Login check')
        self.assertIn('Login check', self.client.get(f'/projects/{project_id}').text)
        self.assertIn('value="Login check"', self.client.get(f'/testcases/{case_id}/edit').text)
        versions = self.client.post(f'/api/portal/testcases/{case_id}/updates', json={}).json()['versions']
        update = {"name": "Renamed check", "description": "Check login", "confirm_clear_results": True}
        self.assertEqual(self.client.put(base, json=update).status_code, 200)
        self.assertEqual(self.client.get(base).json()['name'], 'Renamed check')
        self.assertIn('Renamed check', self.client.get(f'/testcases/{case_id}').text)
        self.assertEqual(self.client.get('/api/testcases').json()['testcases'][0]['name'], 'Renamed check')
        changes = self.client.post(f'/api/portal/testcases/{case_id}/updates', json={'versions': versions}).json()
        self.assertIn('Renamed check', changes['sections']['description'])
        for invalid in ('', '   ', 'x' * 201):
            self.assertEqual(self.client.post('/api/testcases', json={**payload, 'name': invalid}).status_code, 422)
            self.assertEqual(self.client.put(base, json={**update, 'name': invalid}).status_code, 422)
        del update['name']
        self.assertEqual(self.client.put(base, json=update).status_code, 200)
        self.assertEqual(self.client.get(base).json()['name'], 'Renamed check')

    def test_submit_start_answer_retry_and_templates(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.post(base + "/start").status_code, 202)
        case = self.wait_status(case_id, "waiting_for_input")
        self.assertEqual(case["run"]["questions"][0]["key"], "base_url")
        self.assertEqual(self.client.post(base + "/start").status_code, 409)
        self.assertEqual(self.client.post(base + "/answers", json={"answers": {"wrong": 1}}).status_code, 422)
        self.assertEqual(self.client.post(base + "/answers", json={"answers": {"base_url": "https://api.test"}}).status_code, 202)
        case = self.wait_status(case_id, "passed")
        self.assertTrue(any(log["kind"] == "graph" for log in case["logs"]))
        self.assertEqual(self.client.get(f"/testcases/{case_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/partials/testcases/{case_id}").status_code, 200)
        self.assertEqual(self.client.post(base + "/retry").status_code, 202)
        case = self.wait_status(case_id, "passed")
        self.assertTrue(case["has_saved_answers"])
        self.assertEqual(len(case["runs"]), 2)
        self.assertEqual(case["runs"][1]["status"], "passed")

    def test_reference_images_create_edit_and_planning(self):
        from app.models import HTTPPlan
        from unittest.mock import AsyncMock
        images = [{"name": name, "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1sAAAAASUVORK5CYII="}
                  for name in ("first.png", "second.png")]
        payload = {"project_id": self.project(), "description": "Use the references", "images": images}
        response = self.client.post("/api/testcases", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        base = "/api/testcases/" + response.json()["testcase_id"]
        self.assertEqual(self.client.get(base).json()["images"], images)
        html = self.client.get("/testcases/" + response.json()["testcase_id"] + "/edit").text
        self.assertIn('multiple', html)
        self.assertIn('second.png', html)
        plan = HTTPPlan.model_validate({"summary": "Check", "steps": [
            {"title": "Check", "path": "/", "assertions": [{"type": "status", "expected": 200}]}]})
        with patch("app.planner.Planner.create", new=AsyncMock(return_value=plan)) as planner:
            self.assertEqual(self.client.post(base + "/plan").status_code, 202)
            self.wait_status(response.json()["testcase_id"], "planned")
            self.assertEqual(planner.call_args.args[0]["images"], images)
        update = {"description": "Updated", "confirm_clear_results": True}
        self.assertEqual(self.client.put(base, json=update).status_code, 200)
        self.assertEqual(self.client.get(base).json()["images"], images)
        update["images"] = images[1:]
        self.assertEqual(self.client.put(base, json=update).status_code, 200)
        self.assertEqual(self.client.get(base).json()["images"], images[1:])
        update["images"] = []
        self.assertEqual(self.client.put(base, json=update).status_code, 200)
        self.assertEqual(self.client.get(base).json()["images"], [])
        payload["images"] = [{"name": "bad.png", "data_url": "data:image/png;base64,aGVsbG8="}]
        self.assertEqual(self.client.post("/api/testcases", json=payload).status_code, 422)

    def test_plan_prepares_checklist_and_start_reuses_it_after_restart(self):
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        html = self.client.get(f"/testcases/{case_id}").text
        self.assertLess(html.index('data-action="plan"'), html.index('data-action="start"'))
        with patch.object(self.runner, "execute", wraps=self.runner.execute) as execute:
            self.assertEqual(self.client.post(base + "/plan").status_code, 202)
            planned = self.wait_status(case_id, "planned")
            execute.assert_not_called()
        self.assertEqual(len(planned["run"]["checklist"]), 1)
        self.assertEqual(planned["run"]["results"], [])
        self.assertEqual(planned["run"]["questions"], [])
        html = self.client.get(f"/testcases/{case_id}").text
        self.assertRegex(html, r'<button data-action="plan"[^>]*>Re-plan</button>')
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.settings, runner=self.runner))
        self.client.__enter__()
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Planner called")) as planner:
            self.assertEqual(self.client.post(base + "/start").status_code, 202)
            finished = self.wait_status(case_id, "passed")
            planner.assert_not_called()
        self.assertEqual(finished["run"]["checklist"], planned["run"]["checklist"])
        self.assertEqual(finished["run"]["id"], planned["run"]["id"])
        self.assertEqual(len(finished["runs"]), 1)

    def test_replan_creates_fresh_checklist_without_execution(self):
        from app.models import HTTPPlan
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        self.assertEqual(self.client.post(base + "/plan").status_code, 202)
        original = self.wait_status(case_id, "planned")
        replacement = HTTPPlan.model_validate({"summary": "New plan", "steps": [{
            "title": "New check", "path": "/new", "assertions": [{"type": "status", "expected": 200}]}]})
        with patch("app.planner.Planner.create", return_value=replacement) as planner, patch.object(
                self.runner, "execute", wraps=self.runner.execute) as execute:
            self.assertEqual(self.client.post(base + "/plan").status_code, 202)
            replanned = self.wait_status(case_id, "planned")
            planner.assert_awaited_once()
            execute.assert_not_called()
        self.assertNotEqual(original["run"]["id"], replanned["run"]["id"])
        self.assertEqual(replanned["run"]["checklist"][0]["title"], "New check")
        self.assertEqual(replanned["runs"][1]["checklist"], original["run"]["checklist"])
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Should reuse plan")) as planner:
            self.assertEqual(self.client.post(base + "/start").status_code, 202)
            finished = self.wait_status(case_id, "passed")
            planner.assert_not_called()
        self.assertEqual(finished["run"]["id"], replanned["run"]["id"])

    def test_plan_defers_missing_input_questions_until_start(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.assertEqual(self.client.post(base + "/plan").status_code, 202)
        planned = self.wait_status(case_id, "planned")
        self.assertEqual(planned["run"]["questions"], [])
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Planner called")) as planner:
            self.assertEqual(self.client.post(base + "/start").status_code, 202)
            case = self.wait_status(case_id, "waiting_for_input")
            planner.assert_not_called()
        self.assertEqual(case["run"]["questions"][0]["key"], "base_url")

    def test_retry_steps_reuses_checkpoint_after_restart_without_planning(self):
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        original = self.wait_status(case_id, "passed")
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.settings, runner=self.runner))
        self.client.__enter__()
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Planner called")) as planner:
            self.assertEqual(self.client.post(base + "/retry?steps_only=true").status_code, 202)
            retried = self.wait_status(case_id, "passed")
            planner.assert_not_called()
            self.assertEqual(retried["run"]["checklist"], original["run"]["checklist"])
            self.assertEqual(len(retried["run"]["results"]), 1)
            self.assertEqual(retried["run"]["results"][0]["step"], 1)
            self.assertNotEqual(retried["run"]["id"], original["run"]["id"])
            self.assertEqual(self.client.post(base + "/retry").status_code, 202)
            self.wait_status(case_id, "error")
            planner.assert_awaited_once()

    def test_retry_steps_from_stopped_run_still_requests_missing_inputs(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.assertEqual(self.client.post(base + "/retry?steps_only=true").status_code, 409)
        self.client.post(base + "/stop")
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Planner called")) as planner:
            self.assertEqual(self.client.post(base + "/retry?steps_only=true").status_code, 202)
            case = self.wait_status(case_id, "waiting_for_input")
            self.assertEqual(case["run"]["questions"][0]["key"], "base_url")
            self.client.post(base + "/answers", json={"answers": {"base_url": "https://api.test"}})
            self.wait_status(case_id, "passed")
            planner.assert_not_called()

    def test_retry_steps_rejects_missing_checklist_without_creating_attempt(self):
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Planning failed")):
            self.client.post(base + "/start")
            self.wait_status(case_id, "error")
        response = self.client.post(base + "/retry?steps_only=true")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.client.get(base).json()["runs"]), 1)
        html = self.client.get(f"/testcases/{case_id}").text
        self.assertIn("Retry everything", html)
        self.assertIn('data-steps-only="true" disabled', html)

    def test_saved_data_editor_masks_secrets_and_updates_individual_entries(self):
        import json
        case_id = self.submit()
        base = f"/api/testcases/{case_id}/saved-data"
        saved = {"answers": {"base_url": "https://old.test", "password": "private-value",
                             "custom": "custom-private", "nested": {"token": "nested-private"},
                             "count": 3}, "secret_keys": ["custom"]}
        with sqlite3.connect(self.settings.storage.directory / "testcases.sqlite3") as db:
            db.execute("UPDATE cases SET saved_answers=?, saved_inspections=? WHERE id=?",
                       (json.dumps(saved), json.dumps({"one": {"path": "button"},
                                                      "two": {"path": "input"}}), case_id))
        response = self.client.get(base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        page = self.client.get(f"/testcases/{case_id}/saved-data")
        self.assertEqual(page.status_code, 200)
        for secret in ("private-value", "custom-private", "nested-private"):
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, page.text)
        data = response.json()
        self.assertEqual(self.client.patch(base, json={"revision": data["revision"],
            "answers": {"base_url": "https://api.test", "count": 4},
            "remove_inspections": ["one"]}).status_code, 200)
        self.assertEqual(self.client.patch(base, json={"revision": data["revision"],
            "remove_answers": ["password"]}).status_code, 409)
        with sqlite3.connect(self.settings.storage.directory / "testcases.sqlite3") as db:
            row = db.execute("SELECT saved_answers, saved_inspections FROM cases WHERE id=?", (case_id,)).fetchone()
        stored = json.loads(row[0])
        self.assertEqual(stored["answers"]["password"], "private-value")
        self.assertEqual(stored["answers"]["count"], 4)
        self.assertEqual(list(json.loads(row[1])), ["two"])
        data = self.client.get(base).json()
        self.assertEqual(self.client.patch(base, json={"revision": data["revision"],
            "answers": {"password": "replacement"}, "remove_answers": ["custom"]}).status_code, 200)
        self.client.post(f"/api/testcases/{case_id}/start")
        self.wait_status(case_id, "passed")
        self.assertEqual(self.client.get(f"/api/testcases/{case_id}").json()["run"]["attempt"], 1)

    def test_saved_data_editor_rejects_active_and_invalid_changes(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        revision = self.client.get(base + "/saved-data").json()["revision"]
        self.assertEqual(self.client.patch(base + "/saved-data", json={
            "revision": revision, "answers": {"unknown": "value"}}).status_code, 422)
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.assertFalse(self.client.get(base + "/saved-data").json()["editable"])
        self.assertEqual(self.client.patch(base + "/saved-data", json={"revision": revision}).status_code, 409)
        self.assertEqual(self.client.get("/api/testcases/missing/saved-data").status_code, 404)
        self.assertEqual(self.client.patch("/api/testcases/missing/saved-data", json={"revision": revision}).status_code, 404)

    def test_saved_answers_survive_restart_and_clear_updates_controls(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.assertEqual(self.client.post(base + "/clear-answers").status_code, 409)
        self.client.post(base + "/answers", json={"answers": {"base_url": "https://api.test"}})
        self.wait_status(case_id, "passed")
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.settings, runner=self.runner))
        self.client.__enter__()
        self.client.post(base + "/retry")
        self.wait_status(case_id, "passed")
        path = f"/api/portal/testcases/{case_id}/updates"
        before = self.client.post(path, json={}).json()
        self.assertIn("Saved answers &amp; inspections", before["sections"]["controls"])
        self.assertEqual(self.client.post(base + "/clear-answers").status_code, 200)
        self.assertFalse(self.client.get(base).json()["has_saved_answers"])
        after = self.client.post(path, json={"versions": before["versions"]}).json()
        self.assertIn("controls", after["sections"])
        self.assertNotIn("Answers from a passed run", after["sections"]["controls"])
        self.assertEqual(len(self.client.get(base).json()["runs"]), 2)
        self.client.post(base + "/retry")
        self.wait_status(case_id, "waiting_for_input")
        self.assertEqual(self.client.post("/api/testcases/missing/clear-answers").status_code, 404)

    def test_inspections_only_enable_clear_button_and_poll_updates(self):
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        with sqlite3.connect(self.settings.storage.directory / "testcases.sqlite3") as db:
            db.execute("UPDATE cases SET saved_inspections=? WHERE id=?", ('{"cached": {"path": "button"}}', case_id))
        case = self.client.get(base).json()
        self.assertTrue(case["has_saved_inspections"])
        self.assertFalse(case["has_saved_answers"])
        self.assertNotIn("saved_inspections", case)
        path = f"/api/portal/testcases/{case_id}/updates"
        before = self.client.post(path, json={}).json()
        controls = before["sections"]["controls"]
        button = controls.split('data-action="clear-answers"', 1)[1].split('</button>', 1)[0]
        self.assertNotIn('disabled', button)
        self.assertIn('>Clear', button)
        self.assertEqual(self.client.post(base + "/clear-answers").status_code, 200)
        self.assertFalse(self.client.get(base).json()["has_saved_inspections"])
        after = self.client.post(path, json={"versions": before["versions"]}).json()
        self.assertIn("controls", after["sections"])

    def test_failed_answers_are_not_saved(self):
        case_id = self.submit_browser()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.browser.outcome = "failed"
        self.client.post(base + "/answers", json={"answers": {"password": "unsuccessful"}})
        self.wait_status(case_id, "failed")
        self.assertFalse(self.client.get(base).json()["has_saved_answers"])
        self.browser.outcome = "passed"
        self.client.post(base + "/retry")
        self.wait_status(case_id, "waiting_for_input")

    def test_failed_corrections_keep_last_passed_answers(self):
        accepted = "good-token"
        def respond(request):
            status = 200 if request.headers.get("Authorization") == accepted else 401
            return httpx.Response(status)
        self.runner.transport = httpx.MockTransport(respond)
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.client.post(base + "/answers", json={"answers": {"auth_headers": {"Authorization": accepted}}})
        self.wait_status(case_id, "passed")
        accepted = "changed-token"
        self.client.post(base + "/retry")
        self.wait_status(case_id, "waiting_for_input")
        for _ in range(2):
            self.client.post(base + "/answers", json={"answers": {"auth_headers": {"Authorization": "bad-token"}}})
            self.wait_status(case_id, "waiting_for_input" if _ == 0 else "failed")
        accepted = "good-token"
        self.client.post(base + "/retry")
        self.wait_status(case_id, "passed")

    def test_saved_credentials_are_private_and_edits_clear_them(self):
        case_id = self.submit_browser()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.client.post(base + "/answers", json={"answers": {"password": "saved-password"}})
        self.wait_status(case_id, "passed")
        self.client.post(base + "/retry")
        case = self.wait_status(case_id, "passed")
        self.assertEqual(self.browser.calls[-1][1]["password"], "saved-password")
        self.assertNotEqual(self.browser.calls[0][2], self.browser.calls[-1][2])
        for response in (self.client.get(base), self.client.get(f"/testcases/{case_id}"),
                         self.client.post(f"/api/portal/testcases/{case_id}/updates", json={})):
            self.assertNotIn("saved-password", response.text)
            self.assertNotIn("collected_answers", response.text)
        self.assertEqual(self.client.put(base, json={**case["own_inputs"], "test_type": "browser",
            "confirm_clear_results": True}).status_code, 200)
        self.assertFalse(self.client.get(base).json()["has_saved_answers"])
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")

    def test_resume_after_server_restart(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.settings, runner=self.runner))
        self.client.__enter__()
        self.assertEqual(self.client.post(base + "/answers", json={"answers": {"base_url": "https://api.test"}}).status_code, 202)
        self.wait_status(case_id, "passed")

    def test_validation_and_missing_case(self):
        self.assertEqual(self.client.post("/api/testcases", json={"project": ""}).status_code, 422)
        for suffix in ("start", "stop", "retry"):
            self.assertEqual(self.client.post(f"/api/testcases/missing/{suffix}").status_code, 404)
        self.assertEqual(self.client.get("/api/testcases/missing").status_code, 404)

    def test_failed_assertions_are_not_passed(self):
        self.runner.transport = httpx.MockTransport(lambda request: httpx.Response(500))
        case_id = self.submit("https://api.test")
        self.client.post(f"/api/testcases/{case_id}/start")
        case = self.wait_status(case_id, "failed")
        self.assertEqual(case["run"]["results"][0]["outcome"], "failed")

    def submit_browser(self):
        response = self.client.post("/api/testcases", json={
            "project_id": self.project("UI"), "test_type": "browser", "description": "Login", "test_data": {
                "base_url": "https://ui.test", "steps": [
                    {"kind": "browser", "title": "Open login", "actions": [{"action": "goto", "path": "/login"}],
                     "assertions": [{"type": "visible", "selector": "#password"}]},
                    {"kind": "browser", "title": "Enter password", "actions": [{"action": "fill", "selector": "#password", "value": "{{password}}"}],
                     "assertions": [{"type": "visible", "selector": "button"}]},
                ],
            },
        })
        self.assertEqual(response.status_code, 201)
        return response.json()["testcase_id"]

    def test_browser_pauses_with_live_session_and_cleans_up_on_completion(self):
        case_id = self.submit_browser()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        case = self.wait_status(case_id, "waiting_for_input")
        self.assertIn(case["run"]["id"], self.browser.sessions)
        self.assertEqual(case["run"]["questions"][0]["key"], "password")
        self.assertIn("BROWSER", self.client.get(f"/testcases/{case_id}").text)
        self.client.post(base + "/answers", json={"answers": {"password": "private-password"}})
        case = self.wait_status(case_id, "passed")
        self.assertEqual(len(self.browser.calls), 2)
        self.assertEqual(self.browser.calls[0][2], self.browser.calls[1][2])
        self.assertNotIn("private-password", str(case))
        self.assertFalse(self.browser.sessions)

    def test_browser_waiting_stop_closes_session(self):
        case_id = self.submit_browser()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.assertTrue(self.browser.sessions)
        self.assertEqual(self.client.post(base + "/stop").status_code, 200)
        self.assertFalse(self.browser.sessions)

    def test_browser_restart_requires_fresh_attempt(self):
        case_id = self.submit_browser()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.settings, runner=self.runner, browser_runner=self.browser))
        self.client.__enter__()
        case = self.client.get(base).json()
        self.assertEqual(case["status"], "stopped")
        self.assertEqual(self.client.post(base + "/answers", json={"answers": {"password": "x"}}).status_code, 409)
        self.assertEqual(self.client.post(base + "/retry").status_code, 202)
        self.wait_status(case_id, "waiting_for_input")

    def test_browser_failure_does_not_execute_later_steps(self):
        self.browser.outcome = "failed"
        case_id = self.submit_browser()
        self.client.post(f"/api/testcases/{case_id}/start")
        case = self.wait_status(case_id, "failed")
        self.assertEqual(len(self.browser.calls), 1)
        self.assertIn("1 checklist steps were not executed", case["run"]["summary"])

    def test_screenshot_route_validation(self):
        from uuid import uuid4
        run_id, screenshot_id = uuid4(), uuid4()
        path = self.settings.storage.directory / "artifacts" / str(run_id)
        path.mkdir(parents=True)
        (path / f"{screenshot_id}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        response = self.client.get(f"/artifacts/{run_id}/{screenshot_id}.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(self.client.get(f"/artifacts/{run_id}/{uuid4()}.png").status_code, 404)
        self.assertEqual(self.client.get("/artifacts/invalid/invalid.png").status_code, 422)

    def test_selector_discovery_and_mid_step_resume_are_checkpointed(self):
        async def execute(step, context, run_id, progress=None, secret_keys=()):
            self.browser.sessions.add(run_id)
            self.browser.calls.append(progress)
            if len(self.browser.calls) <= 2:
                return {"title": step.title, "kind": "browser", "outcome": "waiting_for_input",
                        "actions_completed": 1, "assertions": [], "fatal": False,
                        "questions": [{"key": "email_selector", "question": "Which email field?", "secret": False}]}
            self.assertEqual(progress["actions_completed"], 1)
            self.assertEqual(context["email_selector"], "#email")
            return {"title": step.title, "kind": "browser", "outcome": "passed",
                    "actions_completed": 2, "assertions": [], "fatal": False}
        self.browser.execute = execute
        response = self.client.post("/api/testcases", json={
            "project_id": self.project("UI"), "test_type": "browser", "description": "Enter email", "test_data": {
                "base_url": "https://ui.test", "requirements": [{"key": "email_selector", "question": "CSS selector?"}],
                "steps": [{"kind": "browser", "title": "Email", "actions": [
                    {"action": "goto", "path": "/login"},
                    {"action": "fill", "selector": "{{email_selector}}", "value": "user@example.test"},
                ], "assertions": [{"type": "visible", "selector": "button"}]}],
            },
        })
        case_id = response.json()["testcase_id"]
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        case = self.wait_status(case_id, "waiting_for_input")
        # The legacy selector requirement did not block the runner before navigation.
        self.assertEqual(len(self.browser.calls), 1)
        self.assertTrue(case["run"]["results"][0]["superseded"])
        self.assertEqual(len(case["run"]["results"]), 1)
        self.assertEqual(self.client.post(base + "/answers", json={
            "answers": {"email_selector": "#email"}}).status_code, 202)
        case = self.wait_status(case_id, "waiting_for_input")
        self.assertEqual(len(self.browser.calls), 2)
        self.assertEqual(len(case["run"]["results"]), 1)
        self.assertEqual(case["run"]["results"][0]["outcome"], "waiting_for_input")
        self.assertEqual(self.client.post(base + "/answers", json={
            "answers": {"email_selector": "#email"}}).status_code, 202)
        case = self.wait_status(case_id, "passed")
        self.assertEqual(len(self.browser.calls), 3)
        self.assertEqual(len(case["run"]["results"]), 1)
        self.assertEqual(case["run"]["results"][0]["outcome"], "passed")
        page = self.client.get(f"/testcases/{case_id}").text
        self.assertEqual(page.count('data-key="result-'), 1)
        self.assertNotIn('badge waiting_for_input', page)
        self.assertIn("1/1 tests passed", case["run"]["summary"])

    def test_edit_requires_confirmation_and_valid_inputs(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        before = self.wait_status(case_id, "waiting_for_input")
        for payload in ({"description": "Changed"},
                        {"description": "Changed", "confirm_clear_results": False},
                        {"description": "", "confirm_clear_results": True}):
            self.assertEqual(self.client.put(base, json=payload).status_code, 422)
        after = self.client.get(base).json()
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["description"], before["description"])
        self.assertEqual(self.client.put('/api/testcases/missing', json={
            "description": "Changed", "confirm_clear_results": True}).status_code, 404)

    def test_edit_clears_history_and_preserves_case_identity(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.client.post(base + "/stop")
        self.client.post(base + "/retry")
        before = self.wait_status(case_id, "waiting_for_input")
        other = self.submit()
        for run in before["runs"]:
            path = self.settings.storage.directory / "artifacts" / run["id"]
            path.mkdir(parents=True)
            (path / "screen.png").write_bytes(b"image")
        payload = {**before["own_inputs"], "description": "Updated health", "confirm_clear_results": True}
        payload["test_data"] = {**payload["test_data"], "edited_value": "saved value"}
        payload["prompt"] = "Updated instructions"
        self.assertEqual(self.client.put(base, json=payload).status_code, 200)
        after = self.client.get(base).json()
        for key in ("testcase_id", "project_id", "created_at"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["description"], "Updated health")
        self.assertEqual(after["own_inputs"]["test_data"]["edited_value"], "saved value")
        self.assertEqual(after["own_inputs"]["prompt"], "Updated instructions")
        self.assertEqual(after["status"], "submitted")
        self.assertEqual(after["runs"], [])
        self.assertEqual(after["logs"], [])
        self.assertEqual(self.client.get(f"/api/testcases/{other}").status_code, 200)
        for run in before["runs"]:
            self.assertFalse((self.settings.storage.directory / "artifacts" / run["id"]).exists())
            with sqlite3.connect(self.settings.storage.directory / "testcases.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM logs WHERE run_id=?", (run["id"],)).fetchone()[0], 0)
            with sqlite3.connect(self.settings.storage.directory / "checkpoints.sqlite3") as db:
                for table in ("checkpoints", "writes"):
                    self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (run["id"],)).fetchone()[0], 0)
        self.assertEqual(self.client.post(base + "/start").status_code, 202)
        self.assertEqual(self.wait_status(case_id, "waiting_for_input")["run"]["attempt"], 1)

    def test_edit_page_uses_original_inputs_and_preserves_secrets(self):
        project_id = self.project(description="Inherited description", test_data={"shared": "project"})
        case_id = self.client.post('/api/testcases', json={
            "project_id": project_id, "description": "Own description", "test_type": "browser",
            "test_data": {"password": "original-secret"}, "prompt": "Own instructions",
        }).json()["testcase_id"]
        page = self.client.get(f'/testcases/{case_id}/edit')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers['cache-control'], 'no-store')
        self.assertIn('original-secret', page.text)
        self.assertNotIn('Inherited description', page.text)
        self.assertNotIn('&quot;shared&quot;', page.text)
        self.assertIn('value="browser" selected', page.text)
        self.assertIn('permanently clear all previous results', page.text)

    def test_edit_running_case_cancels_before_clearing_records(self):
        entered, cancelled = threading.Event(), threading.Event()
        async def slow(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.runner.transport = httpx.MockTransport(slow)
        case_id = self.submit("https://api.test")
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.client.put(base, json={
            "description": "Changed", "confirm_clear_results": True}).status_code, 200)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn(case_id, self.client.app.state.service.tasks)
        self.assertEqual(self.client.get(base).json()["runs"], [])

    def test_edit_waiting_browser_closes_session(self):
        case_id = self.submit_browser()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.assertTrue(self.browser.sessions)
        self.assertEqual(self.client.put(base, json={
            "description": "Changed", "test_type": "http", "confirm_clear_results": True}).status_code, 200)
        self.assertFalse(self.browser.sessions)
        self.assertEqual(self.client.get(base).json()["test_type"], "http")

    def test_delete_submitted_case_keeps_other_cases(self):
        case_id = self.submit()
        other = self.submit()
        project_id = self.client.get(f"/api/testcases/{case_id}").json()["project_id"]
        self.assertIn('data-action="delete"', self.client.get(f"/projects/{project_id}").text)
        self.assertIn('data-action="delete"', self.client.get(f"/testcases/{case_id}").text)
        response = self.client.delete(f"/api/testcases/{case_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(self.client.get(f"/api/testcases/{case_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/testcases/{case_id}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/testcases/{case_id}").status_code, 404)
        self.assertEqual([c["testcase_id"] for c in self.client.get("/api/testcases").json()["testcases"]], [other])

    def test_delete_removes_all_attempts_logs_checkpoints_and_screenshots(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        self.client.post(base + "/stop")
        self.client.post(base + "/retry")
        case = self.wait_status(case_id, "waiting_for_input")
        for run in case["runs"]:
            path = self.settings.storage.directory / "artifacts" / run["id"]
            path.mkdir(parents=True)
            (path / "screen.png").write_bytes(b"image")
        untouched = self.settings.storage.directory / "artifacts" / "unrelated"
        untouched.mkdir()
        (untouched / "keep.png").write_bytes(b"keep")
        self.assertEqual(self.client.delete(base).status_code, 204)
        for run in case["runs"]:
            self.assertFalse((self.settings.storage.directory / "artifacts" / run["id"]).exists())
            with sqlite3.connect(self.settings.storage.directory / "testcases.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM runs WHERE id=?", (run["id"],)).fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM logs WHERE run_id=?", (run["id"],)).fetchone()[0], 0)
            with sqlite3.connect(self.settings.storage.directory / "checkpoints.sqlite3") as db:
                for table in ("checkpoints", "writes"):
                    self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (run["id"],)).fetchone()[0], 0)
        self.assertTrue((untouched / "keep.png").exists())

    def test_delete_waiting_browser_closes_session(self):
        case_id = self.submit_browser()
        self.client.post(f"/api/testcases/{case_id}/start")
        self.wait_status(case_id, "waiting_for_input")
        self.assertTrue(self.browser.sessions)
        self.assertEqual(self.client.delete(f"/api/testcases/{case_id}").status_code, 204)
        self.assertFalse(self.browser.sessions)

    def test_delete_running_case_cancels_before_removing_records(self):
        entered, cancelled = threading.Event(), threading.Event()
        async def slow(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.runner.transport = httpx.MockTransport(slow)
        case_id = self.submit("https://api.test")
        self.client.post(f"/api/testcases/{case_id}/start")
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.client.delete(f"/api/testcases/{case_id}").status_code, 204)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn(case_id, self.client.app.state.service.tasks)
        self.assertEqual(self.client.get(f"/api/testcases/{case_id}").status_code, 404)

    def test_delete_cleanup_failure_keeps_retryable_case(self):
        case_id = self.submit()
        base = f"/api/testcases/{case_id}"
        self.client.post(base + "/start")
        self.wait_status(case_id, "waiting_for_input")
        with patch("app.service.shutil.rmtree", side_effect=PermissionError("test error")):
            self.assertEqual(self.client.delete(base).status_code, 503)
        self.assertEqual(self.client.get(base).json()["status"], "stopped")
        self.assertEqual(self.client.delete(base).status_code, 204)

    def test_index_ajax_sends_only_changed_projects(self):
        project = self.project()
        other = self.project("Unchanged")
        path = '/api/portal/projects/updates'
        first = self.client.post(path, json={}).json()
        self.assertIn(f'project:{project}', first['sections'])
        self.assertIn(f'project:{other}', first['sections'])
        unchanged = self.client.post(path, json={"versions": first['versions']}).json()
        self.assertEqual(unchanged['sections'], {})
        case = self.client.post('/api/testcases', json={"project_id": project, "description": "Private case title"}).json()["testcase_id"]
        changed = self.client.post(path, json={"versions": first['versions']}).json()
        self.assertEqual(set(changed['sections']), {f'project:{project}'})
        self.assertNotIn('Private case title', self.client.get('/').text)
        self.assertIn('1 test cases', self.client.get('/').text)
        self.assertIn(case, self.client.get(f'/projects/{project}').text)
        self.client.delete(f'/api/projects/{project}')
        deleted = self.client.post(path, json={"versions": changed['versions']}).json()
        self.assertNotIn(f'project:{project}', deleted['order'])
        self.assertIn(other, self.client.get('/').text)

    def test_details_ajax_only_sends_new_logs_and_changed_sections(self):
        case_id = self.submit()
        base = f'/api/testcases/{case_id}'
        self.client.post(base + '/start')
        self.wait_status(case_id, 'waiting_for_input')
        path = f'/api/portal/testcases/{case_id}/updates'
        first = self.client.post(path, json={}).json()
        self.assertTrue(first['logs']['reset'])
        self.assertTrue(first['logs']['items'])
        state = {"versions": first['versions'], "run_id": first['run_id'], "after_log_id": first['logs']['after_log_id']}
        unchanged = self.client.post(path, json=state).json()
        self.assertEqual(unchanged['sections'], {})
        self.assertEqual(unchanged['logs']['items'], [])
        self.assertFalse(unchanged['logs']['reset'])
        self.client.post(base + '/answers', json={"answers": {"base_url": "https://api.test"}})
        self.wait_status(case_id, 'passed')
        finished = self.client.post(path, json=state).json()
        self.assertNotIn('checklist', finished['sections'])
        self.assertIn('results', finished['sections'])
        self.assertTrue(all(item['id'] > state['after_log_id'] for item in finished['logs']['items']))
        self.client.post(base + '/retry')
        self.wait_status(case_id, 'passed')
        retry = self.client.post(path, json=state).json()
        self.assertTrue(retry['logs']['reset'])
        self.assertNotEqual(retry['run_id'], state['run_id'])
        self.assertIn('history', retry['sections'])

    def test_ajax_templates_escape_untrusted_data(self):
        project = self.project('<script>alert(1)</script>')
        updates = self.client.post('/api/portal/projects/updates', json={}).json()
        html = updates['sections'][f'project:{project}']
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_project_required_and_creation_page(self):
        self.assertEqual(self.client.post('/api/testcases', json={"description": "No project"}).status_code, 422)
        self.assertEqual(self.client.post('/api/testcases', json={"project_id": "missing", "description": "No project"}).status_code, 404)
        html = self.client.get('/projects/new').text
        self.assertIn('disabled', html)
        self.assertNotIn('id="submission-form"', html)
        self.assertEqual(self.client.get('/projects/missing').status_code, 404)
        self.assertEqual(self.client.post('/api/projects', json={"name": " "}).status_code, 422)

    def test_inherited_inputs_execute_and_project_edits_apply_to_retry(self):
        project = self.project(description="Shared description", prompt="Shared instructions", test_data={
            "base_url": "https://api.test", "headers": {"X-Shared": "yes", "X-Override": "old"},
            "steps": [{"title": "Health", "path": "/health", "assertions": [{"type": "status", "expected": 200}]}]})
        case_id = self.client.post('/api/testcases', json={"project_id": project, "description": "Case description",
            "prompt": "Case instructions", "test_data": {"headers": {"X-Override": "new"}}}).json()['testcase_id']
        base = f'/api/testcases/{case_id}'
        case = self.client.get(base).json()
        self.assertEqual(case['description'], 'Shared description\n\nCase description')
        self.assertEqual(case['prompt'], 'Shared instructions\n\nCase instructions')
        self.assertEqual(case['test_data']['headers'], {"X-Shared": "yes", "X-Override": "new"})
        self.client.post(base + '/start')
        self.wait_status(case_id, 'passed')
        self.assertEqual(self.client.put(f'/api/projects/{project}', json={"name": "Renamed", "test_data": case['test_data']}).status_code, 200)
        self.assertEqual(self.client.get(base).json()['project'], 'Renamed')
        self.client.post(base + '/retry')
        self.wait_status(case_id, 'passed')

    def test_delete_project_cleans_child_runs_and_preserves_other_projects(self):
        case_id = self.submit_browser()
        self.client.post(f'/api/testcases/{case_id}/start')
        case = self.wait_status(case_id, 'waiting_for_input')
        project = case['project_id']
        second = self.client.post('/api/testcases', json={"project_id": project, "description": "Second"}).json()['testcase_id']
        other = self.submit()
        run_id = case['run']['id']
        artifact = self.settings.storage.directory / 'artifacts' / run_id
        artifact.mkdir(parents=True)
        (artifact / 'screen.png').write_bytes(b'image')
        self.assertEqual(self.client.delete(f'/api/projects/{project}').status_code, 204)
        self.assertFalse(self.browser.sessions)
        self.assertFalse(artifact.exists())
        for deleted in (case_id, second):
            self.assertEqual(self.client.get(f'/api/testcases/{deleted}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/testcases/{other}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/projects/{project}').status_code, 404)
        with sqlite3.connect(self.settings.storage.directory / 'testcases.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs WHERE id=?', (run_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM logs WHERE run_id=?', (run_id,)).fetchone()[0], 0)
        with sqlite3.connect(self.settings.storage.directory / 'checkpoints.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints WHERE thread_id=?', (run_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
