import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from market_reviewer import website_supabase_publisher as module
from market_reviewer.website_cli import main
from market_reviewer.website_public_snapshot import build_public_snapshot, publish_public_snapshot
from tests.test_website_public_snapshot import fixture

FAKE_KEY = "test-only-service-role-key-not-a-credential"
ENV = {module.URL_ENV: "https://example.supabase.co", module.KEY_ENV: FAKE_KEY}


class SupabasePublisherTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch.object(module, "build_opener")
        self.opener_factory = self.network.start()
        self.addCleanup(self.network.stop)
        self.opener = self.opener_factory.return_value
        self.response = self.opener.open.return_value.__enter__.return_value
        self.response.status = 201
        self.snapshot = build_public_snapshot(fixture(), now=1100)

    def test_status_is_safe_and_offline(self):
        status = module.supabase_status()
        self.assertEqual(status, {"url_configured": True, "credential_configured": True,
                                  "url_valid_https": True, "ready": True})
        self.assertNotIn(FAKE_KEY, json.dumps(status))
        self.assertNotIn("example.supabase", json.dumps(status))
        self.opener_factory.assert_not_called()

    def test_missing_credentials_no_build_or_request(self):
        with patch.dict("os.environ", {module.KEY_ENV: ""}):
            builder = MagicMock()
            result = module.publish_supabase(builder=builder)
        self.assertEqual(result["status"], "PUBLISH_FAILED")
        builder.assert_not_called()
        self.opener_factory.assert_not_called()

    def test_invalid_urls_rejected(self):
        for url in ("http://example.supabase.co", "https://user:secret@example.supabase.co",
                    "https://example.supabase.co/path", "https://example.supabase.co?key=secret",
                    "https://example.supabase.co#secret", "https://example.supabase.co:80",
                    "https://example.supabase.co\n", "file:///C:/private", "https://", ""):
            with self.subTest(url=url), patch.dict("os.environ", {module.URL_ENV: url}):
                self.assertFalse(module.supabase_status()["ready"])
                self.assertEqual(module.publish_supabase()["status"], "PUBLISH_FAILED")
        self.opener_factory.assert_not_called()

    def test_key_header_injection_rejected(self):
        with patch.dict("os.environ", {module.KEY_ENV: "secret\nHeader: value"}):
            self.assertFalse(module.supabase_status()["ready"])
            self.assertEqual(module.publish_supabase()["status"], "PUBLISH_FAILED")
        self.opener_factory.assert_not_called()

    def test_correct_upsert_and_fixed_identity(self):
        before = copy.deepcopy(self.snapshot)
        module.SupabasePublisher().publish(self.snapshot)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.supabase.co/rest/v1/grim_website_snapshot?on_conflict=id")
        self.assertEqual(request.method, "POST")
        payload = json.loads(request.data)
        self.assertEqual(set(payload), {"id", "schema", "generated_at", "source_observation",
                                       "stale_after_seconds", "snapshot"})
        self.assertEqual(payload["id"], "latest")
        self.assertEqual(payload["snapshot"], self.snapshot)
        for field in ("schema", "generated_at", "source_observation", "stale_after_seconds"):
            self.assertEqual(payload[field], self.snapshot[field])
        self.assertEqual(request.get_header("Prefer"), "resolution=merge-duplicates,return=minimal")
        self.assertEqual(request.get_header("Apikey"), FAKE_KEY)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 10)
        self.assertNotIn(FAKE_KEY, request.data.decode())
        self.assertNotIn("private", request.data.decode())
        self.assertNotIn("C:", request.data.decode())
        self.assertEqual(before, self.snapshot)
        self.response.read.assert_not_called()
        self.opener.open.assert_called_once()

    def test_secret_key_is_not_bearer_jwt(self):
        with patch.dict("os.environ", {module.KEY_ENV: "sb_secret_test_only"}):
            module.SupabasePublisher().publish(self.snapshot)
        request = self.opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_header("Apikey"), "sb_secret_test_only")

    def test_http_failures_normalized_without_body_or_retry(self):
        for code in (301, 400, 401, 403, 429, 500, 503):
            with self.subTest(code=code):
                self.opener.open.reset_mock()
                body = io.BytesIO(FAKE_KEY.encode())
                self.opener.open.side_effect = HTTPError("https://secret", code, FAKE_KEY, {}, body)
                result = module.publish_supabase(builder=fixture, now=1100)
                self.assertEqual(result, {"status": "PUBLISH_FAILED", "error": "HTTP_ERROR", "http_status": code})
                self.assertNotIn(FAKE_KEY, json.dumps(result))
                self.assertTrue(body.closed)
                self.opener.open.assert_called_once()

    def test_timeout_network_and_unknown_failures_contained(self):
        for exc, error in ((TimeoutError(FAKE_KEY), "TIMEOUT"),
                           (URLError(FAKE_KEY), "NETWORK_ERROR"),
                           (RuntimeError("C:/private/" + FAKE_KEY), "TRANSPORT_ERROR")):
            with self.subTest(error=error):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = exc
                result = module.publish_supabase(builder=fixture, now=1100)
                self.assertEqual(result["error"], error)
                self.assertNotIn(FAKE_KEY, json.dumps(result))
                self.opener.open.assert_called_once()

    def test_returned_non_success_status_rejected(self):
        self.response.status = 500
        result = module.publish_supabase(builder=fixture, now=1100)
        self.assertEqual(result["status"], "PUBLISH_FAILED")
        self.assertEqual(result["http_status"], 500)

    def test_invalid_public_payload_never_sent(self):
        self.snapshot["service_role_key"] = FAKE_KEY
        result = publish_public_snapshot(self.snapshot, module.SupabasePublisher())
        self.assertEqual(result["status"], "PUBLISH_FAILED")
        self.opener_factory.assert_not_called()

    def test_redirect_not_followed_and_proxy_disabled(self):
        handler = module._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://attacker.example"))
        module.SupabasePublisher().publish(self.snapshot)
        handlers = self.opener_factory.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], module._NoRedirect)

    def test_remote_read_contract(self):
        url = module.remote_read_url(ENV[module.URL_ENV] + "/")
        self.assertEqual(url, ENV[module.URL_ENV] + module.READ_PATH)
        self.assertIn("id=eq.latest&select=snapshot,generated_at,source_observation,stale_after_seconds,updated_at", url)
        self.assertNotIn(FAKE_KEY, url)
        with self.assertRaisesRegex(ValueError, "^INVALID_HTTPS_PROJECT_URL$"):
            module.remote_read_url("http://secret")
        self.opener_factory.assert_not_called()

    def test_cli_safe_status_and_publish_output(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["supabase-status"]), 0)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.opener_factory.assert_not_called()
        with patch.object(module, "build_website_read_model", return_value=fixture()), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["publish-supabase"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "PUBLISHED")
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_cli_failure_safe_exit(self):
        self.opener.open.side_effect = TimeoutError(FAKE_KEY)
        with patch.object(module, "build_website_read_model", return_value=fixture()), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["publish-supabase"]), 1)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"], "TIMEOUT")

    def test_failed_publication_leaves_caller_and_files_unchanged(self):
        model = fixture()
        before = copy.deepcopy(model)
        with tempfile.TemporaryDirectory() as tmp:
            files = [Path(tmp) / name for name in ("runner.json", "production.json", "research.json")]
            for path in files:
                path.write_text('{"status":"COMPLETE"}')
            content = {p: p.read_bytes() for p in files}
            self.opener.open.side_effect = TimeoutError(FAKE_KEY)
            result = module.publish_supabase(builder=lambda: model, now=1100)
            self.assertEqual(result["status"], "PUBLISH_FAILED")
            self.assertEqual(content, {p: p.read_bytes() for p in files})
            self.assertEqual(len(list(Path(tmp).iterdir())), 3)
        self.assertEqual(before, model)

    def test_builder_failure_sanitized(self):
        builder = MagicMock(side_effect=ValueError(FAKE_KEY))
        self.assertEqual(module.publish_supabase(builder=builder)["error"], "SNAPSHOT_BUILD_FAILED")
        self.opener_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
