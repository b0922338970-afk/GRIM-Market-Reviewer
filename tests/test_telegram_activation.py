import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from market_reviewer.telegram_activation import telegram_config, telegram_status, telegram_test_send
from market_reviewer.notification_delivery import deliver
from tests.test_notification_delivery import packet


class TelegramActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'delivery.json'
        self.env = {'GRIM_NOTIFY_TELEGRAM_ENABLED': 'true',
                    'GRIM_NOTIFY_TELEGRAM_TOKEN': '123:fake_test_token',
                    'GRIM_NOTIFY_TELEGRAM_CHAT_ID': '-123'}
        self.send = Mock(return_value=(200, b'{"ok":true}'))

    def test_missing_config_no_network_or_journal(self):
        result = telegram_test_send('test', self.path, env={}, transport=self.send)
        self.assertEqual(result['status'], 'BLOCKED_CONFIG')
        self.send.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_disabled_no_send(self):
        self.env['GRIM_NOTIFY_TELEGRAM_ENABLED'] = 'false'
        self.assertEqual(telegram_test_send('test', self.path, env=self.env, transport=self.send)['status'], 'BLOCKED_CONFIG')
        self.send.assert_not_called()

    def test_invalid_token_or_chat_blocked_without_disclosure(self):
        for key, value in [('TOKEN','123:secret/path'), ('CHAT_ID','chat\nsecret')]:
            env = dict(self.env, **{'GRIM_NOTIFY_TELEGRAM_'+key: value})
            result = telegram_test_send('test', self.path, env=env, transport=self.send)
            self.assertEqual(result['status'], 'BLOCKED_CONFIG')
            self.assertNotIn(value, json.dumps(result))
        self.send.assert_not_called()

    def test_other_channel_enabled_blocks_activation(self):
        self.env['GRIM_NOTIFY_DISCORD_ENABLED'] = 'true'
        result = telegram_test_send('test', self.path, env=self.env, transport=self.send)
        self.assertEqual(result['status'], 'BLOCKED_CONFIG')
        self.send.assert_not_called()

    def test_status_is_local_read_only_and_redacted(self):
        result = telegram_status(self.path, self.env)
        self.assertTrue(result['telegram']['telegram_only_ready'])
        self.assertFalse(result['telegram']['live_credentials_verified'])
        self.assertNotIn('fake_test_token', json.dumps(result))
        self.assertNotIn('-123', json.dumps(result))
        self.assertFalse(self.path.exists())

    def test_synthetic_non_actionable_telegram_only(self):
        result = telegram_test_send('activation-1', self.path, env=self.env, transport=self.send)
        self.assertEqual([r['channel'] for r in result['records']], ['TELEGRAM'])
        self.assertEqual(result['records'][0]['status'], 'DELIVERED')
        text = self.send.call_args.args[2]['text']
        self.assertIn('SYNTHETIC TEST / NON-ACTIONABLE', text)
        self.assertNotIn('WATCH', text)
        self.assertNotIn('ARMED', text)
        self.assertNotIn('opportunity_alert.v1', text)

    def test_same_test_id_duplicate_across_clock_changes(self):
        for now in (100, 99999):
            result = telegram_test_send('activation-1', self.path, env=self.env, transport=self.send, clock=lambda: now)
        self.assertEqual(result['records'][0]['status'], 'DUPLICATE_SUPPRESSED')
        self.send.assert_called_once()

    def test_new_explicit_id_can_send(self):
        for identity in ('test-1', 'test-2'):
            telegram_test_send(identity, self.path, env=self.env, transport=self.send)
        self.assertEqual(self.send.call_count, 2)

    def test_arbitrary_message_injection_rejected(self):
        result = telegram_test_send('BUY BTC NOW\n', self.path, env=self.env, transport=self.send)
        self.assertEqual(result['status'], 'INVALID_TEST_ID')
        self.send.assert_not_called()

    def test_no_secrets_or_message_in_journal(self):
        telegram_test_send('test', self.path, env=self.env, transport=self.send)
        contents = self.path.read_text()
        for secret in ('fake_test_token', '-123', 'api.telegram.org', 'SYNTHETIC TEST'):
            self.assertNotIn(secret, contents)
        self.assertEqual(json.loads(contents)['schema'], 'notification-delivery.v1')

    def test_timeout_no_retry(self):
        self.send.side_effect = TimeoutError('fake-secret')
        result = telegram_test_send('test', self.path, env=self.env, transport=self.send)
        self.assertEqual(result['records'][0]['error'], 'TIMEOUT')
        telegram_test_send('test', self.path, env=self.env, transport=self.send)
        self.send.assert_called_once()

    def test_http_4xx_5xx_normalized(self):
        for code in (400, 401, 403, 429, 500, 503):
            self.send.side_effect = HTTPError('secret-url', code, 'secret', {}, None)
            result = telegram_test_send('test-'+str(code), self.path, env=self.env, transport=self.send)
            row = result['records'][0]
            self.assertEqual(row['status'], 'DELIVERY_FAILED')
            self.assertEqual(row['response_code'], code)
            self.assertEqual(row['error'], 'HTTP_ERROR')

    def test_live_router_still_rejects_synthetic_payload(self):
        result = deliver({'schema': 'telegram-synthetic-test.v1'}, self.path, env=self.env, transport=self.send)
        self.assertEqual(result['status'], 'SKIPPED_INVALID_PACKET')
        self.send.assert_not_called()

    def test_live_router_preflight_rejects_invalid_credentials(self):
        self.env['GRIM_NOTIFY_TELEGRAM_TOKEN'] = 'invalid'
        result = deliver(packet(), self.path, env=self.env, transport=self.send)
        self.assertEqual(result['records'][0]['error'], 'INVALID_TELEGRAM_CONFIG')
        self.send.assert_not_called()

    def test_cli_exit_codes(self):
        from market_reviewer.cli import main
        with patch.dict('os.environ', {}, clear=True), patch('builtins.print'):
            self.assertEqual(main(['telegram-status','--path',str(self.path)]), 0)
            self.assertEqual(main(['telegram-test-send','--test-id','test','--path',str(self.path)]), 1)
        self.assertFalse(self.path.exists())

    def test_runner_survives_telegram_http_errors(self):
        from market_reviewer.observation_runner import RunnerConfig, _execute_ready_cycle
        cfg = RunnerConfig(output_dir=Path(self.tmp.name))
        with ExitStack() as stack:
            for name in ('_write_observation_intent','_write_production_head','_update_runner_state'):
                stack.enter_context(patch('market_reviewer.observation_runner.'+name))
            update = stack.enter_context(patch('market_reviewer.observation_runner._update_observation_intent'))
            stack.enter_context(patch('market_reviewer.observation_runner._sha256_or_none', return_value='unchanged'))
            stack.enter_context(patch('market_reviewer.observation_runner._previous_review_timestamps', return_value={}))
            stack.enter_context(patch.dict('os.environ', self.env, clear=True))
            network = stack.enter_context(patch('market_reviewer.notification_delivery.http_transport'))
            for code in (400, 500):
                p = dict(packet(), generated_at=code)
                def fail(*args):
                    self.assertEqual(update.call_args.kwargs['status'], 'COMPLETE')
                    raise HTTPError('secret-url', code, 'secret', {}, None)
                network.side_effect = fail
                result = _execute_ready_cycle(cfg, {}, code, lambda *args: {'reviews': {'BTC': {'opportunity_alert':p}}},
                    lambda *args: {'research_persistence':'PASS'}, lambda: 100, 'test')
                self.assertEqual(result['production_result'], 'PASS')
                self.assertEqual(result['research_result'], 'PASS')
            self.assertEqual(network.call_count, 2)
