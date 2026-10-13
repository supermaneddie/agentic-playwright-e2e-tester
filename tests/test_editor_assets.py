import unittest
from hashlib import sha256
from unittest.mock import patch

from starlette.requests import Request

from app.main import ROOT, asset_version, create_app, templates


class EditorAssetTests(unittest.TestCase):
    def test_script_url_tracks_file_contents(self):
        expected = sha256((ROOT / 'static' / 'app.js').read_bytes()).hexdigest()[:16]
        self.assertEqual(asset_version('app.js'), expected)
        with patch('pathlib.Path.read_bytes', return_value=b'updated script'):
            self.assertNotEqual(asset_version('app.js'), expected)

    def test_editor_loads_versioned_scripts_and_starts_with_save_disabled(self):
        app = create_app()
        request = Request({'type': 'http', 'scheme': 'http', 'server': ('testserver', 80),
                           'root_path': '', 'path': '/', 'headers': [], 'router': app.router})
        html = templates.get_template('edit_case.html').render(request=request, case={
            'testcase_id': 'example', 'test_type': 'http',
            'own_inputs': {'description': 'Original', 'test_data': {'key': 'old'}, 'prompt': None},
        })
        self.assertIn(f'/static/app.js?v={asset_version("app.js")}', html)
        self.assertIn('type="submit" disabled', html)
        self.assertIn('id="edit-loading"', html)

