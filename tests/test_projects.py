import sqlite3

import aiosqlite
import tempfile
import unittest
from pathlib import Path

from app.models import Submission
from app.store import Store


def verify_migration(tmp_path):
    import asyncio

    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE cases (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, description TEXT NOT NULL,
                test_data TEXT NOT NULL, prompt TEXT, created_at TEXT NOT NULL
            );
            INSERT INTO cases VALUES ('one', 'Legacy', 'First', '{"shared": 1}', 'Instructions', '2020');
            INSERT INTO cases VALUES ('two', 'Legacy', 'Second', '{}', NULL, '2020');
        """)

    async def verify():
        async with aiosqlite.connect(path) as db:
            store = Store(db)
            await store.initialize()
            projects = await store.list_projects()
            assert len(projects) == 1
            assert projects[0]['testcase_count'] == 2
            project_id = projects[0]['project_id']
            run_id = await store.new_run('one', 1)
            await store.update_run(run_id, status='passed')
            await store.log(run_id, 'progress', 'Preserved')
            await store.initialize()
            assert await store.list_projects() == projects
            case = await store.get('one')
            assert case['project_id'] == project_id
            assert case['name'] == ''
            assert case['test_data'] == {'shared': 1}
            assert case['prompt'] == 'Instructions'
            assert case['status'] == 'passed'
            assert len(await store.logs(run_id)) == 1
            with unittest.TestCase().assertRaises(sqlite3.IntegrityError):
                await store.create(Submission(project_id='missing', description='Invalid'))
            await db.rollback()
            await store.delete_project(project_id)
            assert await store.list_cases() == []
            assert await store.logs(run_id) == []
            async with db.execute('SELECT COUNT(*) FROM runs') as cursor:
                assert (await cursor.fetchone())[0] == 0

    asyncio.run(verify())


class MigrationTests(unittest.TestCase):
    def test_legacy_migration_is_repeatable_and_preserves_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            verify_migration(Path(directory))


class InheritanceTests(unittest.TestCase):
    def test_nested_case_values_override_without_mutating_project_defaults(self):
        from app.store import merge_data

        defaults = {'headers': {'Shared': 'yes', 'Override': 'old'}, 'steps': [1], 'optional': 'default'}
        overrides = {'headers': {'Override': 'new'}, 'steps': [2], 'optional': None}
        self.assertEqual(merge_data(defaults, overrides), {
            'headers': {'Shared': 'yes', 'Override': 'new'}, 'steps': [2], 'optional': None})
        self.assertEqual(defaults['headers']['Override'], 'old')
        self.assertEqual(defaults['steps'], [1])
