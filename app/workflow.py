from contextlib import nullcontext
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import set_config_context
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.models import BrowserStep, Plan, Step
from app.privacy import SENSITIVE, redact, secret_values
from app.runner import missing_variables, resolve, target_url


class State(TypedDict, total=False):
    plan_only: bool
    dependency_cases: list[dict]
    scopes: list[dict]
    scope_index: int
    case: dict
    context: dict
    collected_answers: dict
    answer_secret_keys: list[str]
    plan: dict
    index: int
    results: list[dict]
    questions: list[dict]
    auth_retries: dict[str, int]
    done: bool
    browser_progress: dict


def build_workflow(planner, runner, checkpointer, max_steps: int, browser_runner=None, store=None):
    def validate_plan(planned, test_type, continuing=False):
        if len(planned.steps) > max_steps:
            raise ValueError("Plan exceeds configured maximum number of steps")
        if any(step.kind != test_type for step in planned.steps):
            raise ValueError("All steps must match the submitted test_type")
        if test_type == "browser" and not continuing and planned.steps[0].actions[0].action != "goto":
            raise ValueError("A browser plan must begin with goto")

    async def plan(state: State):
        scopes = state.get("scopes", [])
        if state.get("plan"):
            planned = state["plan"]
            groups = scopes or [{"start": 0, "end": len(planned["steps"]),
                                 "requirements": planned["requirements"]}]
            for scope in groups:
                item = Plan.model_validate({"summary": "Saved checklist", "requirements": scope["requirements"],
                    "steps": planned["steps"][scope["start"]:scope["end"]]})
                validate_plan(item, state["case"].get("test_type", "http"), continuing=scope["start"] > 0)
        else:
            steps, scopes, requirements, summaries = [], [], [], []
            completed_dependencies = []
            cases = [*state.get("dependency_cases", []), state["case"]]
            for case in cases:
                cached = case.get("saved_plan") if case is not state["case"] else None
                planning_case = {**case, "completed_dependencies": completed_dependencies} if completed_dependencies else case
                item = Plan.model_validate(cached) if cached else await planner.create(planning_case)
                validate_plan(item, case.get("test_type", "http"), continuing=bool(steps))
                if store and case.get("testcase_id"):
                    await store.save_plan(case["testcase_id"], item.model_dump())
                scopes.append({"start": len(steps), "end": len(steps) + len(item.steps),
                               "context": case.get("test_data", {}) if case is not state["case"] else state["context"],
                               "requirements": [r.model_dump() for r in item.requirements],
                               "case_id": case.get("testcase_id"),
                               "name": case.get("name") or case.get("description", "Test case")[:80],
                               "dependency": case is not state["case"]})
                steps.extend(item.steps)
                requirements.extend(item.requirements)
                summaries.append(item.summary)
                secrets = secret_values(case.get("test_data", {}))
                secrets.extend(case.get("test_data", {}).get(req.key) for req in item.requirements
                               if req.secret and isinstance(case.get("test_data", {}).get(req.key), str))
                completed_dependencies = [*completed_dependencies, redact({
                    "name": case.get("name") or case.get("description", "Test case")[:80],
                    "summary": item.summary, "steps": item.model_dump()["steps"],
                }, secrets)]
            # Each case is validated separately; the combined run can exceed a single plan's limits.
            planned = {"summary": "\n".join(summaries), "steps": [step.model_dump() for step in steps],
                       "requirements": [requirement.model_dump() for requirement in requirements]}
        return {"plan": planned, "scopes": scopes, "scope_index": -1,
                "index": 0, "results": [], "auth_retries": {}, "done": False, "browser_progress": {}}

    def prepare(state: State):
        context = state["context"]
        scope_index = next((i for i, scope in enumerate(state.get("scopes", []))
                            if scope["start"] <= state["index"] < scope["end"]), -1)
        scope = state["scopes"][scope_index] if scope_index >= 0 else None
        if scope and scope_index != state.get("scope_index"):
            context = {**context, **scope["context"]}
        questions = {}
        base_url = context.get("base_url")
        try:
            target_url(str(base_url or ""), "/")
        except ValueError:
            questions["base_url"] = {"key": "base_url", "question": "What is the HTTP(S) base URL of the application under test?", "secret": False}
        step = state["plan"]["steps"][state["index"]]
        is_browser = step.get("kind") == "browser"

        def inputs(browser_step):
            # Selectors are located from the actual page during execution, after navigation.
            return {key: [{k: v for k, v in item.items() if k not in {"selector", "target", "image_selector", "image_target"}} for item in browser_step[key]]
                    for key in ("actions", "assertions")}

        referenced = set(missing_variables([inputs(s) for s in state["plan"]["steps"]], context)) if is_browser else None
        for requirement in (scope["requirements"] if scope else state["plan"]["requirements"]):
            if is_browser and requirement["key"] not in referenced:
                continue
            if context.get(requirement["key"]) in (None, ""):
                questions[requirement["key"]] = requirement
        for key in missing_variables(inputs(step) if is_browser else step, context):
            questions.setdefault(key, {"key": key, "question": f"Provide {key} needed for: {step['title']}. Use a JSON object for nested values.", "secret": bool(SENSITIVE.search(key))})
        for key in ("headers", "auth_headers"):
            value = context.get(key, {})
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                questions[key] = {"key": key, "question": f"Provide {key} as a JSON object of header names and string values.", "secret": True}
        return {"questions": list(questions.values()), "context": context, "scope_index": scope_index}

    def ask(state: State, config: RunnableConfig):
        # No side effects before interrupt: this node is replayed on resume.
        # Explicit context also supports Python 3.10's async task propagation limitations.
        with set_config_context(config) as context:
            answer: dict[str, Any] = context.run(interrupt, {"questions": state["questions"]})
        permitted = {question["key"] for question in state["questions"]}
        supplied = {key: value for key, value in answer.items() if key in permitted}
        secret_keys = set(state.get("answer_secret_keys", []))
        secret_keys.update(q["key"] for q in state["questions"] if q.get("secret"))
        return {"context": {**state["context"], **supplied}, "questions": [],
                "collected_answers": {**state.get("collected_answers", {}), **supplied},
                "answer_secret_keys": sorted(secret_keys)}

    async def execute(state: State, config: RunnableConfig):
        raw_step = state["plan"]["steps"][state["index"]]
        is_browser = raw_step.get("kind") == "browser"
        step = BrowserStep.model_validate(raw_step) if is_browser else Step.model_validate(raw_step)
        if is_browser:
            if browser_runner is None:
                raise ValueError("Browser runner is not configured")
            scope = state.get("scopes", [])[state["scope_index"]] if state.get("scope_index", -1) >= 0 else None
            owner = scope.get("case_id") if scope else None
            with store.inspection_scope(owner) if store else nullcontext():
                result = await browser_runner.execute(
                    step, state["context"], config["configurable"]["thread_id"],
                    progress=state.get("browser_progress"),
                    secret_keys=sorted(set(state.get("answer_secret_keys", []))
                                       | {r["key"] for r in state["plan"]["requirements"] if r["secret"]}),
                )
        else:
            result = await runner.execute(step, state["context"], config["configurable"]["thread_id"])
        scope = state.get("scopes", [])[state["scope_index"]] if state.get("scope_index", -1) >= 0 else None
        if scope and scope["dependency"]:
            result["dependency"] = {"case_id": scope.get("case_id"), "name": scope["name"],
                                    "start": scope["start"] + 1, "end": scope["end"]}
        result["step"] = state["index"] + 1
        # A paused step is still one test; replace its interim result on resume.
        results = [previous for previous in state["results"]
                   if not (previous["step"] == result["step"]
                           and previous["outcome"] == "waiting_for_input")]
        results.append(result)
        if result.get("questions"):
            result["superseded"] = True
            return {"results": results, "questions": result["questions"], "browser_progress": {
                "actions_completed": result["actions_completed"], "assertions": result["assertions"],
                "observations": result.get("observations", []),
                "pending_click": result.get("pending_click"),
                "attempts": result.get("attempts", []),
            }}
        expected_auth_error = any(a.type == "status" and resolve(a.expected, state["context"]) in (401, 403) for a in step.assertions)
        retries = dict(state["auth_retries"])
        key = str(state["index"])
        if result.get("status_code") in {401, 403} and not expected_auth_error and retries.get(key, 0) < 2:
            retries[key] = retries.get(key, 0) + 1
            # Preserve the failed request as evidence, but distinguish it from the final assertion result.
            result["superseded"] = True
            return {"results": results, "auth_retries": retries, "questions": [{
                "key": "auth_headers", "secret": True,
                "question": f"The API returned HTTP {result['status_code']}. Provide corrected authentication headers as JSON (for example, Authorization: Bearer …). This request will be retried.",
            }]}
        index = state["index"] + 1
        dependency_failed = bool(scope and scope["dependency"] and result["outcome"] != "passed")
        return {"results": results, "index": index, "questions": [], "browser_progress": {},
                "done": result.get("fatal", False) or dependency_failed or index >= len(state["plan"]["steps"])}

    def after_prepare(state):
        if state["questions"]:
            return "ask_for_information"
        return "execute_browser_test" if state["plan"]["steps"][state["index"]].get("kind") == "browser" else "execute_http_test"

    def after_execute(state):
        return "ask_for_information" if state["questions"] else END if state["done"] else "prepare_test"

    graph = StateGraph(State)
    graph.add_node("summarize_checklist", plan)
    graph.add_node("prepare_test", prepare)
    graph.add_node("ask_for_information", ask)
    graph.add_node("execute_http_test", execute)
    graph.add_node("execute_browser_test", execute)
    graph.add_edge(START, "summarize_checklist")
    graph.add_conditional_edges("summarize_checklist", lambda state: END if state.get("plan_only") else "prepare_test")
    graph.add_conditional_edges("prepare_test", after_prepare)
    graph.add_edge("ask_for_information", "prepare_test")
    graph.add_conditional_edges("execute_http_test", after_execute)
    graph.add_conditional_edges("execute_browser_test", after_execute)
    return graph.compile(checkpointer=checkpointer)
