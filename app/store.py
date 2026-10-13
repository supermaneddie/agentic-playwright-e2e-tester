import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.models import ProjectSubmission, Submission
from app.privacy import sensitive_question


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_data(defaults, overrides):
    result = dict(defaults)
    for key, value in overrides.items():
        result[key] = merge_data(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self.inspection_case_id = ContextVar("inspection_case_id", default=None)
        db.row_factory = aiosqlite.Row

    async def initialize(self):
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA recursive_triggers=ON;
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                test_data TEXT NOT NULL, prompt TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, description TEXT NOT NULL,
                test_data TEXT NOT NULL, prompt TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                checklist TEXT NOT NULL DEFAULT '[]', results TEXT NOT NULL DEFAULT '[]',
                questions TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS runs_case ON runs(case_id, attempt);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS logs_run ON logs(run_id, id);
            CREATE TRIGGER IF NOT EXISTS delete_case_children
            AFTER DELETE ON cases BEGIN
                DELETE FROM logs WHERE run_id IN (SELECT id FROM runs WHERE case_id=OLD.id);
                DELETE FROM runs WHERE case_id=OLD.id;
            END;
        """)
        async with self.db.execute("PRAGMA table_info(cases)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "images" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")
        if "saved_plan" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN saved_plan TEXT NOT NULL DEFAULT '{}'")
        if "depends_on" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]'")
        if "name" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        if "saved_inspections" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN saved_inspections TEXT NOT NULL DEFAULT '{}'")
        if "saved_answers" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN saved_answers TEXT NOT NULL DEFAULT '{}'")
        if "test_type" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN test_type TEXT NOT NULL DEFAULT 'http'")
        if "project_id" not in columns:
            await self.db.execute("ALTER TABLE cases ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE CASCADE")
        # Convert each legacy label once, preserving case IDs, inputs and run history.
        async with self.db.execute("SELECT DISTINCT project FROM cases WHERE project_id IS NULL") as cursor:
            labels = [row["project"] for row in await cursor.fetchall()]
        for label in labels:
            project_id = str(uuid4())
            await self.db.execute(
                "INSERT INTO projects VALUES (?, ?, '', '{}', NULL, ?)", (project_id, label, now()))
            await self.db.execute("UPDATE cases SET project_id=? WHERE project_id IS NULL AND project=?",
                                  (project_id, label))
        await self.db.executescript("""
            CREATE INDEX IF NOT EXISTS cases_project ON cases(project_id);
            CREATE TRIGGER IF NOT EXISTS require_case_project
            BEFORE INSERT ON cases WHEN NEW.project_id IS NULL BEGIN
                SELECT RAISE(ABORT, 'A saved project is required');
            END;
        """)
        # Never replay requests after a process crash: a remote side effect may have happened.
        await self.db.execute(
            "UPDATE runs SET status='stopped', summary=?, updated_at=? WHERE status='running'",
            ("Server restarted during execution. Review side effects before retrying.", now()),
        )
        # Live browser state and HTTP cookies cannot be restored from a graph checkpoint.
        await self.db.execute(
            "UPDATE runs SET status='stopped', questions='[]', summary=?, updated_at=? "
            "WHERE status='waiting_for_input' AND (json_array_length(results)>0 OR case_id IN (SELECT id FROM cases WHERE test_type='browser'))",
            ("Server restarted; client session was closed. Retry to start a fresh session.", now()),
        )
        await self.db.commit()

    async def create_project(self, project: ProjectSubmission) -> str:
        project_id = str(uuid4())
        await self.db.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)", (
            project_id, project.name, project.description, json.dumps(project.test_data), project.prompt, now()))
        await self.db.commit()
        return project_id

    async def get_project(self, project_id):
        async with self.db.execute("SELECT *, (SELECT COUNT(*) FROM cases WHERE project_id=projects.id) AS testcase_count FROM projects WHERE id=?", (project_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        project = dict(row)
        project["project_id"] = project.pop("id")
        project["test_data"] = json.loads(project["test_data"])
        return project

    async def list_projects(self):
        async with self.db.execute("""
            SELECT p.id AS project_id, p.name, p.description, p.created_at, COUNT(c.id) AS testcase_count
            FROM projects p LEFT JOIN cases c ON c.project_id=p.id
            GROUP BY p.id ORDER BY p.created_at DESC, p.rowid DESC
        """) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def update_project(self, project_id, project: ProjectSubmission):
        await self.db.execute("UPDATE projects SET name=?, description=?, test_data=?, prompt=? WHERE id=?",
                              (project.name, project.description, json.dumps(project.test_data), project.prompt, project_id))
        await self.db.commit()

    async def delete_project(self, project_id):
        await self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        await self.db.commit()

    async def create(self, submission: Submission) -> str:
        case_id = str(uuid4())
        await self.db.execute("INSERT INTO cases (id, project, project_id, name, description, test_data, prompt, created_at, test_type, depends_on, images) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            case_id, submission.project_id, submission.name, submission.description,
            json.dumps(submission.test_data), submission.prompt, now(), submission.test_type, json.dumps(submission.depends_on),
            json.dumps([image.model_dump() for image in submission.images]),
        ))
        await self.db.commit()
        return case_id

    async def list_cases(self, project_id=None):
        async with self.db.execute("""
            SELECT c.id AS testcase_id, c.project_id, p.name AS project, c.name, c.description, c.created_at, c.test_type,
                COALESCE(r.status, 'submitted') AS status, COALESCE(r.attempt, 0) AS attempt
            FROM cases c JOIN projects p ON p.id=c.project_id LEFT JOIN runs r ON r.id = (
                SELECT id FROM runs WHERE case_id=c.id ORDER BY attempt DESC LIMIT 1
            ) WHERE (? IS NULL OR c.project_id=?) ORDER BY c.created_at DESC, c.rowid DESC
        """, (project_id, project_id)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get(self, case_id: str):
        async with self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        case = dict(row)
        case.pop("saved_plan", None)
        case["has_saved_inspections"] = bool(json.loads(case.pop("saved_inspections")))
        case["has_saved_answers"] = bool(json.loads(case.pop("saved_answers")))
        case["testcase_id"] = case.pop("id")
        case["test_data"] = json.loads(case["test_data"])
        case["depends_on"] = json.loads(case["depends_on"])
        case["images"] = json.loads(case["images"])
        project = await self.get_project(case["project_id"])
        case["project"] = project["name"]
        case["own_inputs"] = {key: case[key] for key in ("description", "test_data", "prompt")}
        case["description"] = "\n\n".join(filter(None, [project["description"], case["description"]]))
        case["prompt"] = "\n\n".join(filter(None, [project["prompt"], case["prompt"]])) or None
        case["test_data"] = merge_data(project["test_data"], case["test_data"])
        async with self.db.execute("SELECT * FROM runs WHERE case_id=? ORDER BY attempt DESC", (case_id,)) as cursor:
            runs = [dict(row) for row in await cursor.fetchall()]
        for run in runs:
            for key in ("checklist", "results", "questions"):
                run[key] = json.loads(run[key])
            # Also mask questions saved before sensitivity was inferred automatically.
            run["questions"] = [{**question, "secret": sensitive_question(question)}
                                for question in run["questions"]]
        case["runs"] = runs
        case["run"] = runs[0] if runs else None
        case["status"] = runs[0]["status"] if runs else "submitted"
        return case

    async def saved_answers(self, case_id):
        async with self.db.execute("SELECT saved_answers FROM cases WHERE id=?", (case_id,)) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["saved_answers"])

    async def save_answers(self, case_id, answers):
        await self.db.execute("UPDATE cases SET saved_answers=? WHERE id=?", (json.dumps(answers), case_id))
        await self.db.commit()

    async def saved_plan(self, case_id):
        async with self.db.execute("SELECT saved_plan FROM cases WHERE id=?", (case_id,)) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["saved_plan"]) if row else {}

    async def save_plan(self, case_id, plan):
        await self.db.execute("UPDATE cases SET saved_plan=? WHERE id=?", (json.dumps(plan), case_id))
        await self.db.commit()

    @contextmanager
    def inspection_scope(self, case_id):
        # Share the run's session while using the source case's element cache.
        token = self.inspection_case_id.set(case_id)
        try:
            yield
        finally:
            self.inspection_case_id.reset(token)

    async def inspection(self, run_id, key):
        async with self.db.execute(
            "SELECT saved_inspections FROM cases WHERE id=COALESCE(?, (SELECT case_id FROM runs WHERE id=?))",
            (self.inspection_case_id.get(), run_id)
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["saved_inspections"]).get(key) if row else None

    async def save_inspection(self, run_id, key, entry):
        # JSON updates avoid overwriting other entries when runs execute concurrently.
        if entry is None:
            await self.db.execute(
                "UPDATE cases SET saved_inspections=json_remove(saved_inspections, ?) "
                "WHERE id=COALESCE(?, (SELECT case_id FROM runs WHERE id=?))",
                ('$.' + key, self.inspection_case_id.get(), run_id))
        else:
            await self.db.execute(
                "UPDATE cases SET saved_inspections=json_set(saved_inspections, ?, json(?)) "
                "WHERE id=COALESCE(?, (SELECT case_id FROM runs WHERE id=?))",
                ('$.' + key, json.dumps(entry), self.inspection_case_id.get(), run_id))
        await self.db.commit()

    async def saved_data(self, case_id):
        async with self.db.execute(
            "SELECT saved_answers, saved_inspections FROM cases WHERE id=?", (case_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return {key: json.loads(row[key]) for key in ("saved_answers", "saved_inspections")}

    async def update_saved_data(self, case_id, answers, inspections):
        await self.db.execute(
            "UPDATE cases SET saved_answers=?, saved_inspections=? WHERE id=?",
            (json.dumps(answers), json.dumps(inspections), case_id))
        await self.db.commit()

    async def clear_saved_data(self, case_id):
        await self.db.execute("UPDATE cases SET saved_answers='{}', saved_inspections='{}' WHERE id=?", (case_id,))
        await self.db.commit()

    async def new_run(self, case_id: str, attempt: int):
        run_id = str(uuid4())
        await self.db.execute(
            "INSERT INTO runs (id, case_id, attempt, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, case_id, attempt, "running", now(), now()),
        )
        await self.db.commit()
        return run_id

    async def update_case(self, case_id, submission):
        if "images" in submission.model_fields_set:
            await self.db.execute("UPDATE cases SET images=? WHERE id=?",
                                  (json.dumps([image.model_dump() for image in submission.images]), case_id))
        if "name" in submission.model_fields_set:
            await self.db.execute("UPDATE cases SET name=? WHERE id=?", (submission.name, case_id))
        await self.db.execute(
            "UPDATE cases SET description=?, test_data=?, prompt=?, test_type=?, depends_on=?, saved_plan='{}', saved_answers='{}', saved_inspections='{}' WHERE id=?",
            (submission.description, json.dumps(submission.test_data), submission.prompt, submission.test_type, json.dumps(submission.depends_on), case_id),
        )
        await self.db.execute("DELETE FROM logs WHERE run_id IN (SELECT id FROM runs WHERE case_id=?)", (case_id,))
        await self.db.execute("DELETE FROM runs WHERE case_id=?", (case_id,))
        await self.db.commit()

    async def delete_case(self, case_id: str):
        # The trigger removes child records atomically with the case, including old schemas.
        await self.db.execute("DELETE FROM cases WHERE id=?", (case_id,))
        await self.db.commit()

    async def update_run(self, run_id: str, **fields):
        allowed = {"status", "checklist", "results", "questions", "summary"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid run fields")
        values = [json.dumps(v) if k in {"checklist", "results", "questions"} else v for k, v in fields.items()]
        assignments = ", ".join(f"{key}=?" for key in fields)
        await self.db.execute(f"UPDATE runs SET {assignments}, updated_at=? WHERE id=?", (*values, now(), run_id))
        await self.db.commit()

    async def log(self, run_id: str, kind: str, message: str):
        await self.db.execute("INSERT INTO logs (run_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
                              (run_id, kind, message, now()))
        await self.db.commit()

    async def logs(self, run_id: str, after_log_id: int = 0):
        async with self.db.execute("SELECT * FROM (SELECT * FROM logs WHERE run_id=? AND id>? ORDER BY id DESC LIMIT 1000) ORDER BY id", (run_id, after_log_id)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
