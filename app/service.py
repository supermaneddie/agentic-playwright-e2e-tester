import asyncio
import shutil
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from langgraph.types import Command
from pydantic import ValidationError

from app.privacy import redact, secret_values, sensitive_question


class TestService:
    def __init__(self, store, graph, browser_runner=None, artifacts: Path | None = None, http_runner=None):
        self.http_runner = http_runner
        self.store = store
        self.graph = graph
        self.browser_runner = browser_runner
        self.artifacts = artifacts
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.inspection_previews = {}

    def clear_inspection_previews(self, run_id):
        self.inspection_previews = {key: value for key, value in self.inspection_previews.items()
                                    if key[0] != run_id}

    async def require_case(self, case_id):
        case = await self.store.get(case_id)
        if case is None:
            raise HTTPException(404, "Test case not found")
        return case

    async def require_project(self, project_id):
        project = await self.store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        return project

    async def create_case(self, submission):
        async with self.lock:
            await self.require_project(submission.project_id)
            await self.dependency_cases(submission.model_dump())
            return await self.store.create(submission)

    async def dependency_cases(self, root):
        ordered, visited, visiting = [], set(), set()

        async def visit(case):
            key = case.get("testcase_id", "new")
            if key in visiting:
                raise HTTPException(422, "Test case dependencies must not contain cycles")
            if key in visited:
                return
            visiting.add(key)
            for dependency_id in case.get("depends_on", []):
                dependency = root if dependency_id == root.get("testcase_id") else await self.store.get(dependency_id)
                if dependency is None:
                    raise HTTPException(422, "A dependency test case no longer exists")
                if dependency["project_id"] != root["project_id"] or dependency["test_type"] != root["test_type"]:
                    raise HTTPException(422, "Dependencies must belong to the same project and test type")
                await visit(dependency)
            visiting.remove(key)
            visited.add(key)
            ordered.append(case)

        await visit(root)
        return ordered[:-1]

    async def delete_project(self, project_id):
        async with self.lock:
            await self.require_project(project_id)
            for case in await self.store.list_cases(project_id):
                await self._delete_case(case["testcase_id"])
            await self.store.delete_project(project_id)

    @staticmethod
    def config(run_id):
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100000}

    async def dependency_plan(self, case):
        cached = await self.store.saved_plan(case["testcase_id"])
        if cached:
            return cached
        # Older cases use the original checkpoint, just as Retry steps only does.
        for run in case["runs"]:
            if not run["checklist"]:
                continue
            state = await self.graph.aget_state(self.config(run["id"]))
            plan = state.values.get("plan")
            if not plan:
                continue
            scopes = state.values.get("scopes", [])
            if scopes:
                scope = next((scope for scope in scopes if scope.get("case_id") == case["testcase_id"]), None)
                if scope is None:
                    continue
                plan = {"summary": case["description"][:4000], "requirements": scope["requirements"],
                        "steps": plan["steps"][scope["start"]:scope["end"]]}
            elif case["depends_on"]:
                continue
            return plan
        return None

    async def start(self, case_id, retry=False, steps_only=False, plan_only=False):
        async with self.lock:
            case = await self.require_case(case_id)
            if retry and case["status"] not in {"passed", "failed", "error", "stopped"}:
                raise HTTPException(409, "Retry requires a finished or stopped run")
            if plan_only and case["status"] in {"running", "waiting_for_input"}:
                raise HTTPException(409, "Stop the active run before planning again.")
            if not retry and not plan_only and case["status"] not in {"submitted", "planned"}:
                raise HTTPException(409, "This test has already started. Use retry for a new attempt.")
            dependencies = await self.dependency_cases(case)
            reused_plan = None
            starting_planned = not retry and not plan_only and case["status"] == "planned"
            if steps_only or starting_planned:
                run = case.get("run") or {}
                if (not retry and not starting_planned) or not run.get("checklist"):
                    raise HTTPException(409, "No checklist is available. Use Retry everything.")
                # The displayed checklist is redacted; execute the original checkpointed plan.
                state = await self.graph.aget_state(self.config(run["id"]))
                reused_plan = state.values.get("plan")
                if not reused_plan or not reused_plan.get("steps"):
                    raise HTTPException(409, "The saved checklist is unavailable. Use Retry everything.")
                scopes = state.values.get("scopes", [])
                if scopes and [scope.get("case_id") for scope in scopes] != [
                        item["testcase_id"] for item in [*dependencies, case]]:
                    raise HTTPException(409, "Dependencies have changed. Use Retry everything.")
            saved = await self.store.saved_answers(case_id)
            answers = saved.get("answers", {})
            if starting_planned:
                run_id = case["run"]["id"]
                task = self.tasks.get(case_id)
                if task:
                    await task
                await self.store.update_run(run_id, status="running")
            else:
                run_id = await self.store.new_run(case_id, len(case["runs"]) + 1)
            initial = {"plan_only": plan_only,
                       "case": {k: case[k] for k in ("testcase_id", "name", "project", "description", "test_data", "prompt", "test_type", "images")},
                       "context": {**case["test_data"], **answers},
                       "collected_answers": answers, "answer_secret_keys": saved.get("secret_keys", [])}
            initial["dependency_cases"] = []
            for dependency in dependencies:
                saved_dependency = await self.store.saved_answers(dependency["testcase_id"])
                initial["dependency_cases"].append({
                    **{key: dependency[key] for key in ("testcase_id", "name", "project", "description", "prompt", "test_type", "images")},
                    "test_data": {**dependency["test_data"], **saved_dependency.get("answers", {})},
                    "saved_plan": await self.dependency_plan(dependency) if reused_plan is None else None})
                initial["answer_secret_keys"].extend(saved_dependency.get("secret_keys", []))
            if reused_plan is not None:
                initial["plan"] = reused_plan
                initial["scopes"] = [{**scope, "context": item["test_data"] if scope["dependency"] else initial["context"]}
                                     for scope, item in zip(scopes, [*initial["dependency_cases"], case])]
            self.launch(case_id, run_id, initial)
            return {"testcase_id": case_id, "run_id": run_id, "status": "running"}

    def launch(self, case_id, run_id, value):
        self.tasks[case_id] = asyncio.create_task(self.drive(case_id, run_id, value))

    async def inspect_elements(self, case_id, query):
        from app.inspection import VisualTarget
        from app.inspection_cache import capture

        async with self.lock:
            case = await self.require_case(case_id)
            run = case.get("run") or {}
            question = next((q for q in run.get("questions", []) if q["key"] == query.key), None)
            if (case["status"] != "waiting_for_input" or run.get("id") != query.run_id
                    or not question or not question.get("inspection") or question.get("options")):
                raise HTTPException(409, "This inspection question is no longer active")
            runner = self.browser_runner
            session = runner.sessions.get(run["id"]) if runner else None
            if not session or not runner.resolver:
                raise HTTPException(409, "The browser session is unavailable. Retry the test.")
            preview_key = (run["id"], query.key)
            if query.action in {"scroll", "preview"}:
                # Every question shares this browser: scrolling invalidates all old selections.
                self.clear_inspection_previews(run["id"])
                try:
                    return await runner.resolver.inspection_view(session.page,
                        direction=query.direction if query.action == "scroll" else None, x=query.x, y=query.y)
                except Exception as exc:
                    raise HTTPException(422, "Could not refresh the browser view. Try again.") from exc
            self.inspection_previews.pop(preview_key, None)
            state = await self.graph.aget_state(self.config(run["id"]))
            context = state.values.get("context", {})
            secrets = secret_values(context)
            secrets.extend(context[key] for key in state.values.get("answer_secret_keys", [])
                           if isinstance(context.get(key), str))
            try:
                preview = await runner.resolver.inspect_candidates(session.page, query.mode, query.query, secrets,
                    **({"full_page": False} if not query.full_page else {}))
                selections, candidates = {}, []
                for item in preview["candidates"]:
                    box = item["box"]
                    target = VisualTarget(f'[data-testbench-ref="{item["id"]}"]',
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    entry = await capture(session.page, target)
                    if entry and preview.get("coordinate_space") == "document":
                        entry["coordinate_space"] = "document"
                        entry["box"] = {**entry["box"],
                            "x": entry["box"]["x"] + entry["scroll"]["x"],
                            "y": entry["box"]["y"] + entry["scroll"]["y"]}
                    if not entry or entry["box"] != box:
                        continue
                    entry["page_digest"] = hashlib.sha256(session.page.url.encode()).hexdigest()
                    token = uuid4().hex
                    selections[token] = {"kind": "inspection_rectangle", "entry": entry}
                    candidates.append({"id": token, "box": box,
                        "label": redact(item.get("name") or item["tag"], secrets)})
                self.inspection_previews[preview_key] = selections
                return {**preview, "candidates": candidates}
            except Exception as exc:
                raise HTTPException(422, "Could not locate elements. Check the query and LLM configuration, then try again.") from exc

    async def answer(self, case_id, answers):
        answers = dict(answers)
        async with self.lock:
            case = await self.require_case(case_id)
            if case["status"] != "waiting_for_input":
                raise HTTPException(409, "This test is not waiting for input")
            questions = case["run"]["questions"]
            required = {q["key"] for q in questions}
            if set(answers) != required or any(answers[key] is None or answers[key] == "" for key in required):
                raise HTTPException(422, "Provide a nonempty answer for each requested key, and no additional keys")
            for question in questions:
                key = question["key"]
                if question.get("inspection") and not question.get("options"):
                    from app.inspection_cache import restore
                    token = answers[key]
                    selection = self.inspection_previews.get((case["run"]["id"], key), {}).get(token) if isinstance(token, str) else None
                    if not selection:
                        raise HTTPException(422, "Locate an element and click a highlighted result to confirm it")
                    session = self.browser_runner.sessions.get(case["run"]["id"])
                    try:
                        valid = session and await restore(session.page, selection["entry"], None)
                    except Exception:
                        valid = False
                    if not valid:
                        raise HTTPException(409, "The selected element has moved. Locate it again before resuming.")
                    answers[key] = selection
                if key in {"headers", "auth_headers"}:
                    if not isinstance(answers[key], dict) or not all(isinstance(v, str) for v in answers[key].values()):
                        raise HTTPException(422, f"{key} must be a JSON object of header names and string values")
            run_id = case["run"]["id"]
            await self.store.update_run(run_id, status="running", questions=[])
            await self.store.log(run_id, "progress", "Information received; resuming the saved workflow.")
            for key in required:
                self.inspection_previews.pop((run_id, key), None)
            self.launch(case_id, run_id, Command(resume=answers))
            return {"testcase_id": case_id, "run_id": run_id, "status": "running"}

    @staticmethod
    def saved_revision(data):
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    async def saved_data(self, case_id):
        case = await self.require_case(case_id)
        data = await self.store.saved_data(case_id)
        saved = data["saved_answers"]
        answers = saved.get("answers", {})
        secrets = secret_values(case["test_data"]) + secret_values(answers)
        secret_keys = set(saved.get("secret_keys", []))
        for key, value in answers.items():
            if key in secret_keys and isinstance(value, str):
                secrets.append(value)
        rows = []
        for key, value in answers.items():
            hidden = (key in secret_keys or sensitive_question({"key": key})
                      or redact(value, secrets) != value)
            inspection = isinstance(value, dict) and value.get("kind") == "inspection_rectangle"
            rows.append({"key": key, "secret": hidden, "inspection": inspection,
                         "value": None if hidden else value,
                         "format": "text" if isinstance(value, str) else "json"})
        return {"revision": self.saved_revision(data), "answers": rows,
                "inspections": [{"key": key, "value": redact(value, secrets)}
                                for key, value in data["saved_inspections"].items()],
                "editable": case["status"] not in {"running", "waiting_for_input"}}

    async def update_saved_data(self, case_id, update):
        async with self.lock:
            case = await self.require_case(case_id)
            if case["status"] in {"running", "waiting_for_input"}:
                raise HTTPException(409, "Stop the active test before editing saved answers and inspections")
            data = await self.store.saved_data(case_id)
            if update.revision != self.saved_revision(data):
                raise HTTPException(409, "Saved data has changed. Reload the page before editing.")
            saved = data["saved_answers"]
            answers = saved.get("answers", {})
            inspections = data["saved_inspections"]
            if (not set(update.answers).issubset(answers)
                    or not set(update.remove_answers).issubset(answers)
                    or not set(update.remove_inspections).issubset(inspections)
                    or set(update.answers) & set(update.remove_answers)):
                raise HTTPException(422, "Choose existing entries and either update or remove each answer")
            for key, value in update.answers.items():
                if isinstance(answers[key], dict) and answers[key].get("kind") == "inspection_rectangle":
                    raise HTTPException(422, "Remove an inspection selection to locate it again on retry")
                if value is None or value == "":
                    raise HTTPException(422, "Answers must be nonempty; use Remove to forget an answer")
                if key in {"headers", "auth_headers"} and (
                        not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values())):
                    raise HTTPException(422, f"{key} must be a JSON object of header names and string values")
            answers.update(update.answers)
            for key in update.remove_answers:
                del answers[key]
            for key in update.remove_inspections:
                del inspections[key]
            secret_keys = [key for key in saved.get("secret_keys", []) if key in answers]
            await self.store.update_saved_data(case_id,
                {"answers": answers, "secret_keys": secret_keys} if answers else {}, inspections)
            return {"testcase_id": case_id}

    async def clear_answers(self, case_id):
        async with self.lock:
            case = await self.require_case(case_id)
            if case["status"] in {"running", "waiting_for_input"}:
                raise HTTPException(409, "Stop the active test before clearing saved answers and inspections")
            await self.store.clear_saved_data(case_id)
            return {"testcase_id": case_id, "has_saved_answers": False, "has_saved_inspections": False}

    async def stop(self, case_id):
        async with self.lock:
            case = await self.require_case(case_id)
            if case["status"] not in {"running", "waiting_for_input"}:
                raise HTTPException(409, "Only an active or waiting test can be stopped")
            task = self.tasks.get(case_id)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self.http_runner:
                await self.http_runner.close_run(case["run"]["id"])
            if self.browser_runner:
                await self.browser_runner.close_run(case["run"]["id"])
            self.clear_inspection_previews(case["run"]["id"])
            await self.store.update_run(case["run"]["id"], status="stopped", questions=[], summary="Stopped by user.")
            await self.store.log(case["run"]["id"], "progress", "Stopped. Requests already received by the target cannot be undone.")
            return {"testcase_id": case_id, "status": "stopped"}

    async def delete(self, case_id):
        async with self.lock:
            case = await self.require_case(case_id)
            for other in await self.store.list_cases(case["project_id"]):
                dependent = await self.store.get(other["testcase_id"])
                if case_id in dependent["depends_on"]:
                    raise HTTPException(409, "Remove this case from dependent cases before deleting it")
            await self._delete_case(case_id)

    async def _delete_case(self, case_id):
        case = await self.require_case(case_id)
        await self._clear_run_resources(case)
        await self.store.delete_case(case_id)

    async def update(self, case_id, submission):
        async with self.lock:
            case = await self.require_case(case_id)
            await self.dependency_cases({**case, **submission.model_dump()})
            for other in await self.store.list_cases(case["project_id"]):
                dependent = await self.store.get(other["testcase_id"])
                if case_id in dependent["depends_on"] and dependent["test_type"] != submission.test_type:
                    raise HTTPException(422, "Change or remove dependent cases before changing test type")
            await self._clear_run_resources(case)
            await self.store.update_case(case_id, submission)
            return {"testcase_id": case_id, "status": "submitted"}

    async def _clear_run_resources(self, case):
        case_id = case["testcase_id"]
        task = self.tasks.get(case_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # If external cleanup fails, keep a stopped, retryable record instead of a stale active run.
        if case["status"] in {"running", "waiting_for_input"}:
            self.clear_inspection_previews(case["run"]["id"])
            await self.store.update_run(case["run"]["id"], status="stopped", questions=[], summary="Stopped to clear run history.")
        for run in case["runs"]:
            run_id = str(UUID(run["id"]))
            self.clear_inspection_previews(run_id)
            if self.http_runner:
                await self.http_runner.close_run(run_id)
            if self.browser_runner:
                await self.browser_runner.close_run(run_id)
            if self.artifacts:
                try:
                    await asyncio.to_thread(shutil.rmtree, self.artifacts / run_id)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise HTTPException(503, "Could not remove screenshots. The case was retained; retry the operation.") from exc
            await self.graph.checkpointer.adelete_thread(run_id)

    async def drive(self, case_id, run_id, value):
        config = self.config(run_id)
        keep_browser = False
        secrets = []
        try:
            state = await self.graph.aget_state(config)
            context = state.values.get("context", {}) if state.values else {}
            context = {**context, **(value.get("context", {}) if isinstance(value, dict) else value.resume)}
            secrets = secret_values(context)
            inputs = value if isinstance(value, dict) else state.values
            for dependency in inputs.get("dependency_cases", []):
                secrets.extend(secret_values(dependency["test_data"]))
                secrets.extend(dependency["test_data"][key] for key in inputs.get("answer_secret_keys", [])
                               if isinstance(dependency["test_data"].get(key), str))
            for scope in inputs.get("scopes", []):
                secrets.extend(secret_values(scope["context"]))
                secrets.extend(scope["context"][requirement["key"]] for requirement in scope["requirements"]
                               if requirement["secret"] and isinstance(scope["context"].get(requirement["key"]), str))
            for key in inputs.get("answer_secret_keys", []):
                item = context.get(key)
                if isinstance(item, str):
                    secrets.append(item)
            if state.values:
                for question in state.values.get("questions", []):
                    if question.get("secret") and isinstance(context.get(question["key"]), str):
                        secrets.append(context[question["key"]])
            await self.store.log(run_id, "progress", "Workflow running.")
            async for update in self.graph.astream(value, config, stream_mode="updates"):
                for node, changed in update.items():
                    if node == "__interrupt__":
                        continue
                    await self.store.log(run_id, "graph", f"{node} completed")
                    if "context" in changed:
                        context = changed["context"]
                        secrets.extend(secret_values(context))
                    if "plan" in changed:
                        plan = changed["plan"]
                        for scope in changed.get("scopes", []):
                            secrets.extend(secret_values(scope["context"]))
                            for requirement in scope["requirements"]:
                                secret = scope["context"].get(requirement["key"])
                                if requirement["secret"] and isinstance(secret, str):
                                    secrets.append(secret)
                        # Treat explicitly marked requirement values as secrets even with non-secret names.
                        for req in plan["requirements"]:
                            if req["secret"] and isinstance(context.get(req["key"]), str):
                                secrets.append(context[req["key"]])
                        checklist = [dict(step) for step in plan["steps"]]
                        for scope in changed.get("scopes", []):
                            if scope["dependency"]:
                                dependency = {"case_id": scope["case_id"], "name": scope["name"],
                                              "start": scope["start"] + 1, "end": scope["end"]}
                                for step in checklist[scope["start"]:scope["end"]]:
                                    step["dependency"] = dependency
                        await self.store.update_run(run_id, checklist=redact(checklist, secrets),
                                                    summary=redact(plan["summary"], secrets))
                        await self.store.log(run_id, "progress", f"Checklist ready: {len(plan['steps'])} tests.")
                    if "results" in changed and changed["results"]:
                        results = redact(changed["results"], secrets)
                        await self.store.update_run(run_id, results=results)
                        last = results[-1]
                        await self.store.log(run_id, "progress", f"Step {last['step']}: {last['outcome']}" +
                                             (f" (HTTP {last['status_code']})" if "status_code" in last else ""))
                    if changed.get("questions"):
                        await self.store.log(run_id, "progress", "Additional information is needed to continue.")
                    elif node == "prepare_test":
                        await self.store.log(run_id, "progress", "Inputs ready; executing the next test.")
            state = await self.graph.aget_state(config)
            if state.values.get("plan_only"):
                await self.store.update_run(run_id, status="planned", questions=[])
                await self.store.log(run_id, "progress", "Planning complete. Ready to start.")
                return
            questions = state.values.get("questions", [])
            if state.next and questions:
                keep_browser = True
                await self.store.update_run(run_id, status="waiting_for_input", questions=redact(questions, secrets))
                await self.store.log(run_id, "graph", "ask_for_information interrupted; checkpoint saved")
                return
            results = [r for r in state.values.get("results", []) if not r.get("superseded")]
            passed = sum(r["outcome"] == "passed" for r in results)
            status = "passed" if results and passed == len(results) else "failed"
            summary = f"{passed}/{len(results)} tests passed. {len(results) - passed} failed or errored."
            remaining = len(state.values.get("plan", {}).get("steps", [])) - len(results)
            if remaining > 0:
                summary += f" {remaining} checklist steps were not executed."
            if status == "passed" and remaining <= 0:
                answers = state.values.get("collected_answers", {})
                await self.store.save_answers(case_id, {
                    "answers": answers, "secret_keys": state.values.get("answer_secret_keys", [])
                } if answers else {})
            await self.store.update_run(run_id, status=status, questions=[], summary=summary)
            await self.store.log(run_id, "progress", summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"LLM endpoint returned HTTP {exc.response.status_code}. Check config.yml."
            elif isinstance(exc, ValidationError):
                errors = exc.errors(include_input=False, include_context=False, include_url=False)
                locations = [".".join(map(str, error["loc"])) + ": " + error["msg"] for error in errors]
                detail = redact("Invalid test plan. Check fields: " + "; ".join(locations[:8]), secrets)
            else:
                detail = f"Execution error ({type(exc).__name__}). Check model configuration, test data and request paths."
            await self.store.update_run(run_id, status="error", questions=[], summary=detail)
            await self.store.log(run_id, "progress", detail)
        finally:
            if not keep_browser:
                self.clear_inspection_previews(run_id)
            if self.http_runner and not keep_browser:
                await self.http_runner.close_run(run_id)
            if self.browser_runner and not keep_browser:
                await self.browser_runner.close_run(run_id)
            if self.tasks.get(case_id) is asyncio.current_task():
                self.tasks.pop(case_id, None)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.http_runner:
            await self.http_runner.close()
        if self.browser_runner:
            await self.browser_runner.close()
