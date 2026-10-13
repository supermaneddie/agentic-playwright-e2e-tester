import base64
import unittest

from pydantic import ValidationError

from app.models import CaseImage, Submission


def image(data=b'\x89PNG\r\n\x1a\n', kind='png'):
    return {'name': 'reference.' + kind,
            'data_url': f'data:image/{kind};base64,' + base64.b64encode(data).decode()}


class CaseImageTests(unittest.TestCase):
    def test_supported_formats(self):
        for data, kind in [(b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpeg'),
                           (b'RIFF0000WEBP', 'webp')]:
            self.assertEqual(CaseImage.model_validate(image(data, kind)).name, 'reference.' + kind)

    def test_invalid_format_encoding_and_size(self):
        for entry in [image(b'not an image'), image(b'<svg/>', 'svg'),
                      {'name': 'remote.png', 'data_url': 'https://example.com/image.png'},
                      {'name': 'bad.png', 'data_url': 'data:image/png;base64,===='},
                      image(b'\x89PNG\r\n\x1a\n' + b'x' * (5 * 1024 * 1024))]:
            with self.subTest(name=entry['name']), self.assertRaises(ValidationError):
                CaseImage.model_validate(entry)

    def test_count_and_total_size_limits(self):
        payload = {'project_id': 'project', 'description': 'Check reference images'}
        with self.assertRaises(ValidationError):
            Submission(**payload, images=[image()] * 11)
        large = image(b'\x89PNG\r\n\x1a\n' + b'x' * (5 * 1024 * 1024 - 8))
        self.assertEqual(len(Submission(**payload, images=[large] * 4).images), 4)
        with self.assertRaises(ValidationError):
            Submission(**payload, images=[large] * 5)
