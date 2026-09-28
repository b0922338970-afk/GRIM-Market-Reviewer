"""Execute the same browser presentation mappings without network access."""
import shutil
import subprocess
import unittest
from pathlib import Path


class WebsitePresentationTests(unittest.TestCase):
    def test_frontend_presentation_contract(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js unavailable; frontend mapping test not executed')
        script = Path(__file__).with_name('website_presentation.test.cjs')
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_two_line_story_css(self):
        css = (Path(__file__).resolve().parents[1]/'web/styles.css').read_text(encoding='utf-8')
        self.assertIn('-webkit-line-clamp:2', css)
        self.assertIn('white-space:pre-line', css)
