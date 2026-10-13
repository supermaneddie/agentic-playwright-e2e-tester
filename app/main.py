from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import Settings, load_config
from app.browser import BrowserRunner
from app.inspection import ElementResolver
from app.models import SavedDataUpdate, InspectionQuery, Answer, CaseUpdate, PortalPoll, ProjectSubmission, Submission
from app.portal import description_segments, running_step, section_updates, test_groups
from app.planner import Planner
from app.privacy import redact, secret_values
from app.runner import HTTPRunner
from app.service import TestService
from app.store import Store
from app.workflow import build_workflow

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals["description_segments"] = description_segments
templates.env.globals["running_step"] = running_step
templates.env.globals["test_groups"] = test_groups


def asset_version(filename):
    return sha256((ROOT / "static" / filename).read_bytes()).hexdigest()[:16]


templates.env.globals["asset_version"] = asset_version


def create_app(settings: Settings | None = None, planner=None, runner=None, browser_runner=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        config = settings or load_config()
        config.storage.directory.mkdir(parents=True, exist_ok=True)
        app.state.artifacts = config.storage.directory / "artifacts"
        browser = browser_runner or BrowserRunner(config.browser, app.state.artifacts, ElementResolver(config))
        async with aiosqlite.connect(config.storage.directory / "testcases.sqlite3") as db:
            store = Store(db)
            await store.initialize()
            if isinstance(browser, BrowserRunner):
                browser.log = store.log
                browser.store = store
            async with AsyncSqliteSaver.from_conn_string(str(config.storage.directory / "checkpoints.sqlite3")) as saver:
                await saver.setup()
                http_runner = runner or HTTPRunner(config.testing)
                graph = build_workflow(planner or Planner(config), http_runner, saver, config.testing.max_steps, browser, store)
                app.state.service = TestService(store, graph, browser, app.state.artifacts, http_runner)
                try:
                    yield
                finally:
                    await app.state.service.close()

    app = FastAPI(title="Agentic Playwright E2E Tester", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-cache" if request.url.path.startswith("/static/") else "no-store"
        return response

    async def dependency_tags(case):
        if not case.get("depends_on"):
            return []
        candidates = {item["testcase_id"]: item
                      for item in await app.state.service.store.list_cases(case["project_id"])}
        return [{"testcase_id": case_id,
                 "name": candidates[case_id]["name"] or candidates[case_id]["description"][:80]}
                for case_id in case["depends_on"] if case_id in candidates]

    async def detail(case_id):
        service = app.state.service
        case = await service.require_case(case_id)
        case["dependencies"] = await dependency_tags(case)
        images = case.pop("images", [])
        case = redact(case, secret_values(case["test_data"]))
        case["images"] = images
        case["logs"] = await service.store.logs(case["run"]["id"]) if case["run"] else []
        return case

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/artifacts/{run_id}/{screenshot_id}.png")
    async def screenshot(run_id: UUID, screenshot_id: UUID):
        path = app.state.artifacts / str(run_id) / f"{screenshot_id}.png"
        if not path.is_file():
            raise HTTPException(404, "Screenshot not found")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/projects", status_code=201)
    async def create_project(project: ProjectSubmission):
        project_id = await app.state.service.store.create_project(project)
        return {"project_id": project_id}

    @app.get("/api/projects")
    async def list_projects():
        return {"projects": await app.state.service.store.list_projects()}

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        project = await app.state.service.require_project(project_id)
        return {**project, "testcases": await app.state.service.store.list_cases(project_id)}

    @app.put("/api/projects/{project_id}")
    async def update_project(project_id: str, project: ProjectSubmission):
        async with app.state.service.lock:
            await app.state.service.require_project(project_id)
            await app.state.service.store.update_project(project_id, project)
        return {"project_id": project_id}

    @app.delete("/api/projects/{project_id}", status_code=204)
    async def delete_project(project_id: str):
        await app.state.service.delete_project(project_id)
        return Response(status_code=204)

    @app.post("/api/testcases", status_code=201)
    async def submit(submission: Submission):
        case_id = await app.state.service.create_case(submission)
        return {"testcase_id": case_id, "status": "submitted"}

    @app.get("/api/testcases")
    async def list_cases():
        return {"testcases": await app.state.service.store.list_cases()}

    @app.get("/api/testcases/{case_id}")
    async def get_case(case_id: str):
        return await detail(case_id)

    @app.put("/api/testcases/{case_id}")
    async def update_case(case_id: str, submission: CaseUpdate):
        return await app.state.service.update(case_id, submission)

    @app.delete("/api/testcases/{case_id}", status_code=204)
    async def delete_case(case_id: str):
        await app.state.service.delete(case_id)
        return Response(status_code=204)

    @app.post("/api/testcases/{case_id}/plan", status_code=202)
    async def plan(case_id: str):
        return await app.state.service.start(case_id, plan_only=True)

    @app.post("/api/testcases/{case_id}/start", status_code=202)
    async def start(case_id: str):
        return await app.state.service.start(case_id)

    @app.post("/api/testcases/{case_id}/retry", status_code=202)
    async def retry(case_id: str, steps_only: bool = False):
        return await app.state.service.start(case_id, retry=True, steps_only=steps_only)

    @app.post("/api/testcases/{case_id}/stop")
    async def stop(case_id: str):
        return await app.state.service.stop(case_id)

    @app.get("/api/testcases/{case_id}/saved-data")
    async def saved_data(case_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await app.state.service.saved_data(case_id)

    @app.patch("/api/testcases/{case_id}/saved-data")
    async def update_saved_data(case_id: str, body: SavedDataUpdate):
        return await app.state.service.update_saved_data(case_id, body)

    @app.post("/api/testcases/{case_id}/clear-answers")
    async def clear_answers(case_id: str):
        return await app.state.service.clear_answers(case_id)

    @app.post("/api/testcases/{case_id}/inspection")
    async def inspect_elements(case_id: str, body: InspectionQuery):
        return await app.state.service.inspect_elements(case_id, body)

    @app.post("/api/testcases/{case_id}/answers", status_code=202)
    async def answer(case_id: str, body: Answer):
        return await app.state.service.answer(case_id, body.answers)

    async def index_context():
        return {"projects": await app.state.service.store.list_projects()}

    @app.post("/api/portal/projects/updates")
    async def list_updates(poll: PortalPoll):
        context = await index_context()
        sections = {f"project:{project['project_id']}": (project, "list_project.html", {"project": project})
                    for project in context["projects"]}
        if not sections:
            sections["empty"] = (True, "list_empty.html", {})
        return {**section_updates(templates, sections, poll.versions), "order": list(sections)}

    @app.post("/api/portal/projects/{project_id}/updates")
    async def project_updates(project_id: str, poll: PortalPoll):
        await app.state.service.require_project(project_id)
        cases = await app.state.service.store.list_cases(project_id)
        return section_updates(templates, {"cases": (cases, "project_cases.html", {"cases": cases})}, poll.versions)

    @app.post("/api/portal/testcases/{case_id}/updates")
    async def detail_updates(case_id: str, poll: PortalPoll):
        case = await app.state.service.require_case(case_id)
        case["dependencies"] = await dependency_tags(case)
        images = case.pop("images", [])
        case = redact(case, secret_values(case["test_data"]))
        case["images"] = images
        run = case["run"] or {}
        run_id = run.get("id")
        reset_logs = run_id != poll.run_id
        cursor = 0 if reset_logs else poll.after_log_id
        logs = await app.state.service.store.logs(run_id, cursor) if run_id else []
        dependency_progress = ([run.get("results", []), case["status"]]
                               if any(step.get("dependency") for step in run.get("checklist", [])) else None)
        data = {
            "description": [case["name"], case["description"], case["dependencies"], run.get("checklist", [])],
            "controls": [run_id, case["status"], run.get("attempt"), case["has_saved_answers"], case["has_saved_inspections"]],
            "questions": [run_id, case["status"], run.get("questions", [])],
            "summary": run.get("summary", ""),
            "checklist": [run_id, run.get("checklist", []), running_step(run), dependency_progress],
            "results": [run_id, run.get("results", []), dependency_progress,
                        run.get("checklist", []) if dependency_progress else None],
            "history": case["runs"][1:],
        }
        sections = {key: (value, f"status_{key}.html", {"case": case}) for key, value in data.items()}
        render_log = templates.env.get_template("macros.html").module.log_line
        return {
            **section_updates(templates, sections, poll.versions), "run_id": run_id,
            "logs": {"reset": reset_logs, "after_log_id": logs[-1]["id"] if logs else cursor,
                     "items": [{"id": log["id"], "kind": log["kind"], "html": str(render_log(log))} for log in logs]},
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context=await index_context())

    @app.get("/partials/testcases", response_class=HTMLResponse)
    async def index_partial(request: Request):
        return templates.TemplateResponse(request=request, name="case_list.html", context=await index_context())

    @app.get("/projects/new", response_class=HTMLResponse)
    async def new_project_page(request: Request):
        return templates.TemplateResponse(request=request, name="project.html", context={"project": None, "cases": []})

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    async def project_page(request: Request, project_id: str):
        project = await app.state.service.require_project(project_id)
        return templates.TemplateResponse(request=request, name="project.html", context={
            "project": project, "cases": await app.state.service.store.list_cases(project_id)})

    @app.get("/testcases/{case_id}/saved-data", response_class=HTMLResponse)
    async def saved_data_page(request: Request, case_id: str):
        case = await app.state.service.require_case(case_id)
        return templates.TemplateResponse(request=request, name="saved_data.html", context={
            "case": case, "saved": await app.state.service.saved_data(case_id)},
            headers={"Cache-Control": "no-store"})

    @app.get("/testcases/{case_id}/edit", response_class=HTMLResponse)
    async def edit_case_page(request: Request, case_id: str):
        case = await app.state.service.require_case(case_id)
        return templates.TemplateResponse(request=request, name="edit_case.html", context={"case": case, "cases": await app.state.service.store.list_cases(case["project_id"])})

    @app.get("/testcases/{case_id}", response_class=HTMLResponse)
    async def case_page(request: Request, case_id: str):
        return templates.TemplateResponse(request=request, name="detail.html", context={"case": await detail(case_id)})

    @app.get("/partials/testcases/{case_id}", response_class=HTMLResponse)
    async def case_partial(request: Request, case_id: str):
        return templates.TemplateResponse(request=request, name="case_status.html", context={"case": await detail(case_id)})

    return app


app = create_app()
