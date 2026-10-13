import unittest

import httpx

import test_api


class DependencyTests(unittest.TestCase):
    setUp = test_api.APITests.setUp
    tearDown = test_api.APITests.tearDown
    project = test_api.APITests.project
    wait_status = test_api.APITests.wait_status

    def case(self, project, name, dependencies=(), test_type="http", **data):
        step = {"title": name, "path": "/" + name, "assertions": [{"type": "status", "expected": 200}]}
        if test_type == "browser":
            step = {"kind": "browser", "title": name, "actions": [{"action": "goto", "path": "/" + name}],
                    "assertions": [{"type": "url_contains", "expected": "/" + name}]}
        payload = {"project_id": project, "name": name, "description": name, "test_type": test_type,
                   "depends_on": list(dependencies),
                   "test_data": {"base_url": "https://app.test", "steps": [step], **data}}
        response = self.client.post("/api/testcases", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["testcase_id"], payload

    def test_diamond_dependencies_share_cookies_and_retry_fresh(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/Login":
                self.assertNotIn("cookie", request.headers)
                return httpx.Response(200, headers={"set-cookie": "session=logged-in; Path=/"})
            self.assertEqual(request.headers.get("cookie"), "session=logged-in")
            return httpx.Response(200)

        self.runner.transport = httpx.MockTransport(handler)
        project = self.project()
        login, _ = self.case(project, "Login")
        left, _ = self.case(project, "Left", [login])
        right, _ = self.case(project, "Right", [login])
        export, _ = self.case(project, "Export", [left, right])
        base = f"/api/testcases/{export}"
        self.assertEqual(self.client.post(base + "/start").status_code, 202)
        result = self.wait_status(export, "passed")
        self.assertEqual(calls, ["/Login", "/Left", "/Right", "/Export"])
        self.assertEqual(len(result["run"]["results"]), 4)
        self.assertEqual(self.client.get(f"/api/testcases/{login}").json()["status"], "submitted")
        self.assertFalse(self.runner.sessions)
        self.assertEqual(self.client.post(base + "/retry?steps_only=true").status_code, 202)
        self.wait_status(export, "passed")
        self.assertEqual(calls, ["/Login", "/Left", "/Right", "/Export"] * 2)

    def test_dependency_failure_blocks_parent(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(500)
        self.runner.transport = httpx.MockTransport(handler)
        project = self.project()
        login, _ = self.case(project, "Login")
        export, _ = self.case(project, "Export", [login])
        self.client.post(f"/api/testcases/{export}/start")
        result = self.wait_status(export, "failed")
        self.assertEqual(calls, ["/Login"])
        self.assertIn("1 checklist steps were not executed", result["run"]["summary"])
        self.assertFalse(self.runner.sessions)

    def test_browser_dependencies_use_same_session_and_case_inputs(self):
        project = self.project()
        login, _ = self.case(project, "Login", test_type="browser", marker="login")
        export, _ = self.case(project, "Export", [login], test_type="browser", marker="export")
        self.client.post(f"/api/testcases/{export}/start")
        self.wait_status(export, "passed")
        self.assertEqual([call[0].title for call in self.browser.calls], ["Login", "Export"])
        self.assertEqual([call[1]["marker"] for call in self.browser.calls], ["login", "export"])
        self.assertEqual(len({call[2] for call in self.browser.calls}), 1)
        self.assertFalse(self.browser.sessions)

    def test_pause_resume_keeps_login_cookie_without_replaying_dependency(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/Login":
                return httpx.Response(200, headers={"set-cookie": "session=yes; Path=/"})
            self.assertEqual(request.headers.get("cookie"), "session=yes")
            return httpx.Response(200)
        self.runner.transport = httpx.MockTransport(handler)
        project = self.project()
        login, _ = self.case(project, "Login")
        export, _ = self.case(project, "Export", [login], requirements=[{"key": "filename", "question": "File?"}])
        base = f"/api/testcases/{export}"
        self.client.post(base + "/start")
        self.wait_status(export, "waiting_for_input")
        self.assertEqual(calls, ["/Login"])
        self.assertEqual(len(self.runner.sessions), 1)
        self.client.post(base + "/answers", json={"answers": {"filename": "report"}})
        self.wait_status(export, "passed")
        self.assertEqual(calls, ["/Login", "/Export"])
        self.assertFalse(self.runner.sessions)

    def test_validation_edit_and_delete(self):
        project = self.project()
        login, login_payload = self.case(project, "Login")
        export, payload = self.case(project, "Export", [login])
        self.assertIn(f'value="{login}" selected', self.client.get(f"/testcases/{export}/edit").text)
        self.assertIn('name="depends_on" multiple', self.client.get(f"/projects/{project}").text)
        update = {key: value for key, value in login_payload.items() if key != "project_id"}
        update.update(confirm_clear_results=True, depends_on=[export])
        self.assertEqual(self.client.put(f"/api/testcases/{login}", json=update).status_code, 422)
        update["depends_on"] = [login]
        self.assertEqual(self.client.put(f"/api/testcases/{login}", json=update).status_code, 422)
        for dependency in ("missing", self.case(self.project("Other"), "Other")[0],
                           self.case(project, "Browser", test_type="browser")[0]):
            self.assertEqual(self.client.post("/api/testcases", json={**payload, "depends_on": [dependency]}).status_code, 422)
        self.assertEqual(self.client.delete(f"/api/testcases/{login}").status_code, 409)
        update = {key: value for key, value in payload.items() if key != "project_id"}
        update.update(confirm_clear_results=True, depends_on=[])
        self.assertEqual(self.client.put(f"/api/testcases/{export}", json=update).status_code, 200)
        self.assertEqual(self.client.get(f"/api/testcases/{export}").json()["depends_on"], [])
        self.assertEqual(self.client.delete(f"/api/testcases/{login}").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/projects/{project}").status_code, 204)
