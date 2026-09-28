import json
from pathlib import Path
import shutil
import subprocess
import unittest

from market_reviewer.website_public_snapshot import build_public_snapshot
from tests.test_website_public_snapshot import fixture

ROOT = Path(__file__).resolve().parents[1]


class HostedWebsiteTests(unittest.TestCase):
    def node(self, args, **kwargs):
        binary = shutil.which("node")
        if not binary:
            self.skipTest("Node.js unavailable")
        result = subprocess.run([binary, *args], cwd=ROOT, text=True, encoding="utf-8",
                                capture_output=True, timeout=30, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_hosted_api_and_frontend(self):
        self.node(["--test", "tests/website_hosted.test.cjs"])

    def test_python_public_snapshot_matches_hosted_wire_contract(self):
        self.node(["-e",
            "const {validSnapshot}=require('./server/public-snapshot');"
            "let s='';process.stdin.on('data',d=>s+=d);"
            "process.stdin.on('end',()=>{if(!validSnapshot(JSON.parse(s)))process.exitCode=1;});"],
            input=json.dumps(build_public_snapshot(fixture(), now=1100)))


if __name__ == "__main__":
    unittest.main()
