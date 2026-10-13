import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from app.config import Settings, TestingConfig, load_config
from app.models import Requirement, Step, Submission
from app.planner import Planner
from app.privacy import redact, secret_values
from app.runner import HTTPRunner, missing_variables, resolve, target_url
from app.workflow import build_workflow


def step(**overrides):
    return {"title": "Check health", "path": "/health", "assertions": [{"type": "status", "expected": 200}], **overrides}


class ConfigAndModelTests(unittest.TestCase):
    def test_sensitive_requirements_are_masked_even_without_secret_flag(self):
        for key in ('password', 'login_password', 'api_key', 'access_token', 'auth_headers'):
            requirement = Requirement(key=key, question='Provide the value', secret=False)
            self.assertTrue(requirement.secret, key)
        self.assertFalse(Requirement(key='username', question='User name').secret)
        self.assertFalse(Requirement(key='password_selector', question='Which field?').secret)
        self.assertTrue(Requirement(key='custom_value', question='Value', secret=True).secret)

    def test_config_defaults_and_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text('llm:\n  url: http://localhost:11434/v1\n  model: local\n  api_key: ${TEST_LLM_KEY}\n')
            with patch.dict(os.environ, {"TEST_LLM_KEY": "secret: value"}):
                config = load_config(path)
            self.assertEqual(config.server.port, 8091)
            self.assertEqual(config.llm.api_key, "secret: value")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                load_config(path)

    def test_submission_alias_and_validation(self):
        case = Submission(project_id=" API ", descrition="Check health", test_data={})
        self.assertEqual(case.project_id, "API")
        self.assertEqual(case.description, "Check health")
        with self.assertRaises(ValidationError):
            Submission(project_id=" ", description="a", test_data={})
        with self.assertRaises(ValidationError):
            Submission(project_id="p", description="a", test_data=[])

    def test_assertions_are_required(self):
        with self.assertRaises(ValidationError):
            Step.model_validate(step(assertions=[]))
        with self.assertRaises(ValidationError):
            Step.model_validate(step(assertions=[{"type": "status", "expected": "OK"}]))

    def test_target_cannot_escape_origin(self):
        self.assertEqual(target_url("https://example.test/api", "/health"), "https://example.test/api/health")
        for path in ("//evil.test", "https://evil.test", "/\\evil.test"):
            with self.assertRaises(ValueError):
                target_url("https://example.test", path)
        for base in ("file:///etc", "https://user:pass@example.test", "https://example.test?token=a"):
            with self.assertRaises(ValueError):
                target_url(base, "/")

    def test_placeholders_preserve_json_types(self):
        context = {"user": {"id": 42}, "enabled": False}
        self.assertEqual(resolve({"id": "{{user.id}}", "path": "/u/{{user.id}}", "flag": "{{enabled}}"}, context),
                         {"id": 42, "path": "/u/42", "flag": False})
        self.assertEqual(missing_variables(["{{user.name}}", "{{token}}", "{{enabled}}"], context), ["token", "user"])

    def test_redaction_retains_question_metadata(self):
        data = {"token": "sensitive-value", "headers": {"Authorization": "Bearer abcdef"}}
        result = redact({"token": "sensitive-value", "message": "Got abcdef", "secret": False}, secret_values(data))
        self.assertEqual(result, {"token": "[redacted]", "message": "Got [redacted]", "secret": False})


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_all_assertions(self):
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.content), {"id": 7})
            self.assertEqual(request.headers["Authorization"], "Bearer private")
            return httpx.Response(201, json={"users": [{"id": 7}]}, headers={"X-Result": "created"})
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(handler))
        item = Step.model_validate(step(method="POST", path="/users", body={"id": "{{id}}"}, assertions=[
            {"type": "status", "expected": 201}, {"type": "json_equals", "path": "users.0.id", "expected": 7},
            {"type": "body_contains", "expected": "users"}, {"type": "header_contains", "path": "x-result", "expected": "created"},
        ]))
        result = await runner.execute(item, {"base_url": "https://api.test", "id": 7, "headers": {"Authorization": "Bearer private"}})
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(len(result["assertions"]), 4)
        self.assertNotIn("private", json.dumps(result))

    async def test_failed_assertion_and_bad_json(self):
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(lambda request: httpx.Response(200, text="no json")))
        result = await runner.execute(Step.model_validate(step(assertions=[{"type": "json_equals", "path": "id", "expected": 1}])), {"base_url": "https://api.test"})
        self.assertEqual(result["outcome"], "failed")
        self.assertFalse(result["assertions"][0]["passed"])

    async def test_auth_header_corrections_are_case_insensitive(self):
        def handler(request):
            self.assertEqual(request.headers.get_list("authorization"), ["Bearer corrected"])
            return httpx.Response(200)
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(handler))
        context = {"base_url": "https://api.test", "headers": {"authorization": "Bearer old"},
                   "auth_headers": {"Authorization": "Bearer corrected"}}
        result = await runner.execute(Step.model_validate(step()), context)
        self.assertEqual(result["outcome"], "passed")

    async def test_limits_redirects_and_network_failure(self):
        context = {"base_url": "https://api.test"}
        item = Step.model_validate(step())
        runner = HTTPRunner(TestingConfig(max_response_bytes=1024), httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 2000)))
        self.assertEqual((await runner.execute(item, context))["outcome"], "error")
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": "https://evil.test"})))
        self.assertEqual((await runner.execute(item, context))["status_code"], 302)
        def failure(request):
            raise httpx.ConnectError("secret in URL", request=request)
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(failure))
        result = await runner.execute(item, context)
        self.assertEqual(result["outcome"], "error")
        self.assertNotIn("secret in URL", json.dumps(result))

    async def test_cancellation(self):
        entered = asyncio.Event()
        async def slow(request):
            entered.set()
            await asyncio.Event().wait()
        runner = HTTPRunner(TestingConfig(), httpx.MockTransport(slow))
        task = asyncio.create_task(runner.execute(Step.model_validate(step()), {"base_url": "https://api.test"}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_image_assertion_binds_to_runtime_text(self):
        real_client = httpx.AsyncClient
        generated = {"summary": "Read CAPTCHA", "steps": [{"title": "Read CAPTCHA", "actions": [
            {"action": "goto", "path": "/"},
            {"action": "fill_from_image", "target": "CAPTCHA field", "image_target": "CAPTCHA image"}],
            "assertions": [{"type": "value_equals", "target": "CAPTCHA field", "expected": "CAPTCHA text"}]}]}
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(generated)}}]})
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        case = {"project": "p", "description": "Read CAPTCHA", "test_type": "browser", "prompt": None,
                "test_data": {}}
        config = Settings(llm={"url": "https://model.test", "model": "test"})
        with patch("app.planner.httpx.AsyncClient", side_effect=client_factory):
            plan = await Planner(config).create(case)
        self.assertIsNone(plan.steps[0].assertions[0].expected)
        self.assertEqual(plan.steps[0].assertions[0].expected_from_action, 2)
        case["test_data"]["steps"] = generated["steps"]
        explicit = await Planner(config).create(case)
        self.assertEqual(explicit.steps[0].assertions[0].expected, "CAPTCHA text")

    async def test_reference_images_are_vision_parts_and_survive_plan_repair(self):
        real_client = httpx.AsyncClient
        requests = []
        images = [{"name": f"screen-{index}.png", "data_url": "data:image/png;base64,iVBORw0KGgo="}
                  for index in range(2)]
        def handler(request):
            requests.append(json.loads(request.content))
            plan = {"summary": "Check", "steps": [{"title": "Check", "path": "/",
                    "assertions": [{"type": "status", "expected": 200}]}]} if len(requests) > 1 else {}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        config = Settings(llm={"url": "https://model.test", "model": "vision"})
        case = {"project": "p", "description": "Check the screenshots", "prompt": None,
                "test_data": {"password": "sensitive-value"}, "images": images}
        with patch("app.planner.httpx.AsyncClient", side_effect=client_factory):
            await Planner(config).create(case)
        self.assertEqual(len(requests), 2)
        for request in requests:
            parts = request["messages"][1]["content"]
            self.assertEqual([part["image_url"]["url"] for part in parts if part["type"] == "image_url"],
                             [image["data_url"] for image in images])
            self.assertNotIn("sensitive-value", parts[0]["text"])
            self.assertIn("screen-1.png", str(parts))

    async def test_generated_email_fill_and_assertion_reference_json_data(self):
        real_client = httpx.AsyncClient
        generated = {"summary": "Enter email", "steps": [{
            "kind": "browser", "title": "Enter email", "actions": [
                {"action": "goto", "path": "/"},
                {"action": "fill", "target": "Email address field", "value": "old@example.test"}],
            "assertions": [{"type": "value_equals", "target": "Email address field",
                            "expected": "old@example.test"}]}]}

        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(generated)}}]})

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        config = Settings(llm={"url": "https://model.test", "model": "test"})
        case = {"project": "p", "description": "Enter email", "test_type": "browser", "prompt": None,
                "test_data": {"account": {"email": "old@example.test"}}}
        with patch("app.planner.httpx.AsyncClient", side_effect=client_factory):
            plan = await Planner(config).create(case)
        self.assertEqual(plan.steps[0].actions[1].value, "{{account.email}}")
        self.assertEqual(plan.steps[0].assertions[0].expected, "{{account.email}}")
        context = {"account": {"email": "new@example.test"}}
        self.assertEqual(resolve(plan.steps[0].actions[1].value, context), "new@example.test")
        self.assertEqual(resolve(plan.steps[0].assertions[0].expected, context), "new@example.test")
        # User-authored explicit plans keep their literal expectations.
        case["test_data"]["steps"] = generated["steps"]
        explicit = await Planner(config).create(case)
        self.assertEqual(explicit.steps[0].assertions[0].expected, "old@example.test")

    def test_browser_data_binding_preserves_distinct_expectations_and_ambiguous_values(self):
        from app.models import BrowserPlan
        from app.planner import bind_browser_test_data
        plan = BrowserPlan(summary="Fill", steps=[{
            "title": "Email", "actions": [{"action": "fill", "target": "Email", "value": "Alice@example.test"}],
            "assertions": [{"type": "value_equals", "target": "Email", "expected": "alice@example.test"}]}])
        bind_browser_test_data(plan, {"users": [{"email": "Alice@example.test"}]})
        self.assertEqual(plan.steps[0].actions[0].value, "{{users.0.email}}")
        self.assertEqual(plan.steps[0].assertions[0].expected, "alice@example.test")
        plan.steps[0].actions[0].value = "duplicate"
        bind_browser_test_data(plan, {"first": "duplicate", "second": "duplicate"})
        self.assertEqual(plan.steps[0].actions[0].value, "duplicate")

    async def test_browser_assertion_repaired_after_validation_feedback(self):
        real_client = httpx.AsyncClient
        requests = []
        invalid = {"summary": "Sign in", "steps": [{
            "kind": "browser", "title": "Open login", "actions": [{"action": "goto", "path": "/login"}],
            "assertions": [{"type": "visible"}],
        }]}

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertNotIn('"$ref": "#/$defs/Step"', payload["messages"][0]["content"])
            if len(requests) == 2:
                feedback = payload["messages"][-1]["content"]
                self.assertIn("steps.0.assertions.0", feedback)
                self.assertIn("Element assertions require a selector or a target", feedback)
                invalid["steps"][0]["assertions"][0]["target"] = "Sign in button"
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(invalid)}}]})

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        config = Settings(llm={"url": "https://model.test", "model": "test"})
        with patch("app.planner.httpx.AsyncClient", side_effect=client_factory):
            plan = await Planner(config).create({"project": "p", "description": "Sign in", "test_type": "browser",
                                                 "test_data": {}, "prompt": None})
        self.assertEqual(len(requests), 2)
        self.assertEqual(plan.steps[0].assertions[0].target, "Sign in button")

    async def test_invalid_explicit_browser_plan_does_not_call_llm(self):
        config = Settings(llm={"url": "https://model.test", "model": "test"})
        with patch("app.planner.httpx.AsyncClient") as client:
            with self.assertRaises(ValidationError) as caught:
                await Planner(config).create({"description": "Sign in", "test_type": "browser", "test_data": {
                    "steps": [{"title": "Open login", "actions": [{"action": "goto", "path": "/login"}],
                               "assertions": [{"type": "text_contains", "target": "Heading"}]}],
                }})
            client.assert_not_called()
        self.assertEqual(caught.exception.errors()[0]["loc"], ("steps", 0, "assertions", 0))
        self.assertIn("requires expected text", str(caught.exception))

    async def test_configured_endpoint_and_optional_key(self):
        real_client = httpx.AsyncClient
        for key in (None, "provider-key"):
            def handler(request):
                self.assertEqual(str(request.url), "https://model.test/v1/chat/completions")
                self.assertEqual(request.headers.get("Authorization"), "Bearer provider-key" if key else None)
                payload = json.loads(request.content)
                self.assertEqual(payload["model"], "custom-model")
                self.assertNotIn("private-token", request.content.decode())
                content = json.dumps({"summary": "Health check", "steps": [step()]})
                return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]})
            def client_factory(**kwargs):
                return real_client(transport=httpx.MockTransport(handler), **kwargs)
            config = Settings(llm={"url": "https://model.test/v1", "model": "custom-model", "api_key": key})
            with patch("app.planner.httpx.AsyncClient", side_effect=client_factory):
                plan = await Planner(config).create({"project": "p", "description": "Health", "prompt": None,
                                                     "test_data": {"token": "private-token"}})
            self.assertEqual(plan.steps[0].path, "/health")

    async def test_invalid_model_plan_is_rejected(self):
        real_client = httpx.AsyncClient
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"summary":"Done","steps":[]}'}}],
            })
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        config = Settings(llm={"url": "https://model.test", "model": "test"})
        with patch("app.planner.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(ValidationError):
                await Planner(config).create({"project": "p", "description": "Health", "test_data": {}, "prompt": None})
        self.assertEqual(len(requests), 2)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def make_graph(self, handler, steps=None, requirements=None):
        settings = Settings(llm={"url": "http://llm.test/v1", "model": "test"})
        graph = build_workflow(Planner(settings), HTTPRunner(settings.testing, httpx.MockTransport(handler)), InMemorySaver(), 30)
        case = {"project": "p", "description": "Verify health", "prompt": None,
                "test_data": {"steps": steps or [step()], "requirements": requirements or []}}
        return graph, {"case": case, "context": dict(case["test_data"])}

    async def test_plan_only_stops_before_inputs_and_execution_then_reuses_plan(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200)
        graph, initial = self.make_graph(handler)
        config = {"configurable": {"thread_id": "planned-case"}}
        planned = await graph.ainvoke({**initial, "plan_only": True}, config)
        self.assertTrue(planned["plan"]["steps"])
        self.assertEqual(planned["results"], [])
        self.assertFalse(planned.get("questions"))
        self.assertEqual(calls, [])
        context = {**initial["context"], "base_url": "https://api.test"}
        with patch("app.planner.Planner.create", side_effect=RuntimeError("Planner called")) as planner:
            finished = await graph.ainvoke({**initial, "plan_only": False,
                "plan": planned["plan"],
                "scopes": [{**scope, "context": context} for scope in planned["scopes"]],
                "context": context}, config)
            planner.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(finished["results"][0]["outcome"], "passed")

    async def test_missing_url_interrupt_and_resume(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200)
        graph, initial = self.make_graph(handler)
        config = {"configurable": {"thread_id": "case-1"}}
        state = await graph.ainvoke(initial, config)
        self.assertEqual(state["questions"][0]["key"], "base_url")
        self.assertEqual(calls, [])
        state = await graph.ainvoke(Command(resume={"base_url": "https://api.test"}), config)
        self.assertTrue(state["done"])
        self.assertEqual(state["results"][0]["outcome"], "passed")
        self.assertEqual(len(calls), 1)

    async def test_authentication_during_test(self):
        def handler(request):
            return httpx.Response(200 if request.headers.get("Authorization") == "Bearer valid" else 401)
        graph, initial = self.make_graph(handler)
        initial["context"]["base_url"] = "https://api.test"
        config = {"configurable": {"thread_id": "auth-case"}}
        state = await graph.ainvoke(initial, config)
        self.assertEqual(state["questions"][0]["key"], "auth_headers")
        self.assertTrue(state["results"][0]["superseded"])
        state = await graph.ainvoke(Command(resume={"auth_headers": {"Authorization": "Bearer valid"}}), config)
        self.assertTrue(state["done"])
        self.assertEqual(state["results"][-1]["outcome"], "passed")

    async def test_auth_retry_limit(self):
        graph, initial = self.make_graph(lambda request: httpx.Response(401))
        initial["context"]["base_url"] = "https://api.test"
        config = {"configurable": {"thread_id": "auth-limit"}}
        state = await graph.ainvoke(initial, config)
        for _ in range(2):
            state = await graph.ainvoke(Command(resume={"auth_headers": {}}), config)
        self.assertTrue(state["done"])
        self.assertEqual(len(state["results"]), 3)
        self.assertNotIn("superseded", state["results"][-1])

    async def test_expected_401_does_not_ask_for_credentials(self):
        graph, initial = self.make_graph(lambda request: httpx.Response(401), steps=[step(assertions=[{"type": "status", "expected": "{{expected_status}}"}])])
        initial["context"].update(base_url="https://api.test", expected_status=401)
        state = await graph.ainvoke(initial, {"configurable": {"thread_id": "negative-auth"}})
        self.assertTrue(state["done"])
        self.assertEqual(state["results"][0]["outcome"], "passed")

    async def test_information_can_be_requested_between_steps(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200)
        graph, initial = self.make_graph(handler, steps=[step(), step(path="/users/{{user_id}}")])
        initial["context"]["base_url"] = "https://api.test"
        config = {"configurable": {"thread_id": "mid-test"}}
        state = await graph.ainvoke(initial, config)
        self.assertEqual(calls, ["/health"])
        self.assertEqual(state["questions"][0]["key"], "user_id")
        await graph.ainvoke(Command(resume={"user_id": 7}), config)
        self.assertEqual(calls, ["/health", "/users/7"])

    async def test_invalid_url_reprompts(self):
        graph, initial = self.make_graph(lambda request: httpx.Response(200))
        config = {"configurable": {"thread_id": "bad-url"}}
        await graph.ainvoke(initial, config)
        state = await graph.ainvoke(Command(resume={"base_url": "not-a-url"}), config)
        self.assertEqual(state["questions"][0]["key"], "base_url")
        self.assertEqual(state["results"], [])


class TemplateTests(unittest.TestCase):
    def test_sensitive_answers_use_password_inputs_including_headers(self):
        from html.parser import HTMLParser

        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.fields = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'data-answer-key' in attrs:
                    self.fields[attrs['data-answer-key']] = (tag, attrs)

        questions = [Requirement(key=key, question='Provide value').model_dump()
                     for key in ('password', 'api_key', 'username', 'auth_headers')]
        env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
        html = env.get_template('status_questions.html').render(
            case={'status': 'waiting_for_input', 'run': {'id': 'run', 'questions': questions}})
        parser = Inputs()
        parser.feed(html)
        for key in ('password', 'api_key', 'auth_headers'):
            tag, attrs = parser.fields[key]
            self.assertEqual((tag, attrs['type']), ('input', 'password'))
            self.assertNotIn('value', attrs)
        self.assertEqual(parser.fields['username'][1]['type'], 'text')
        self.assertEqual(parser.fields['auth_headers'][1]['data-answer-format'], 'json')

    def test_templates_compile_and_escape_data(self):
        env = Environment(loader=FileSystemLoader("app/templates"), autoescape=select_autoescape())
        for name in env.list_templates():
            env.get_template(name)
        html = env.get_template("case_list.html").render(projects=[{'project_id': 'p', 'name': '<script>alert(1)</script>', 'testcase_count': 0}])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
