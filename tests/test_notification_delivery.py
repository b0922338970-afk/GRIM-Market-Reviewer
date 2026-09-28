import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from market_reviewer.notification_delivery import (
    deliver, render_message, notification_status, alert_identity,
    dispatch_completed_reviews, adapter_request, NoRedirect)


def packet(state="WATCH"):
    return {"schema": "opportunity_alert.v1", "symbol": "BTC", "timeframe": "M5",
            "direction": "LONG", "review_state": state,
            "alert_level": {"WATCH": "NORMAL_ALERT", "ARMED": "HIGH_PRIORITY_ALERT"}.get(state, "SILENT"),
            "emit_alert": state in {"WATCH", "ARMED"}, "generated_at": 1787974800,
            "quality_context": {}, "market_story": "Closed-candle context",
            "entry_zone": {"availability": "UNAVAILABLE"}, "fragility": []}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "delivery.json"
        self.env = {"GRIM_NOTIFY_WEBHOOK_ENABLED": "true", "GRIM_NOTIFY_WEBHOOK_URL": "https://example.invalid/hook",
                    "GRIM_NOTIFY_WEBHOOK_TOKEN": "test-secret"}
        self.transport = Mock(return_value=(200, b'{"ok":true}'))

    def run_delivery(self, p=None, **kwargs):
        return deliver(p or packet(), self.path, env=self.env, transport=self.transport, clock=lambda: 123, **kwargs)

    def test_no_trade_silent(self):
        self.assertEqual(self.run_delivery(packet("NO_TRADE"))["status"], "SILENT")
        self.transport.assert_not_called()

    def test_wait_silent_even_forged_priority(self):
        p = packet("WAIT")
        p.update(alert_level="HIGH_PRIORITY_ALERT", emit_alert=True)
        self.assertEqual(self.run_delivery(p)["status"], "SILENT")
        self.transport.assert_not_called()

    def test_watch_normal(self):
        self.run_delivery()
        self.assertIn("Priority: NORMAL_ALERT", self.transport.call_args.args[2]["message"])

    def test_armed_high(self):
        self.run_delivery(packet("ARMED"))
        self.assertIn("HIGH PRIORITY", self.transport.call_args.args[2]["message"])
        self.assertIn("No order has been placed", self.transport.call_args.args[2]["message"])

    def test_disabled_default(self):
        self.env = {}
        self.assertTrue(all(r['status'] == 'SKIPPED_DISABLED' for r in self.run_delivery()['records']))
        self.transport.assert_not_called()

    def test_missing_credentials(self):
        del self.env['GRIM_NOTIFY_WEBHOOK_URL']
        self.assertEqual(self.run_delivery()['records'][-1]['status'], 'SKIPPED_NOT_CONFIGURED')
        self.transport.assert_not_called()

    def test_timeout_fail_open(self):
        self.transport.side_effect = TimeoutError('test-secret')
        self.assertEqual(self.run_delivery()['records'][-1]['error'], 'TIMEOUT')

    def test_http_errors_fail_open(self):
        for code in (400, 401, 429, 500, 503):
            p = packet(); p['generated_at'] += code
            self.transport.return_value = (code, b'secret-body')
            self.assertEqual(self.run_delivery(p)['records'][-1]['status'], 'DELIVERY_FAILED')

    def test_duplicate_suppressed_across_invocations(self):
        self.run_delivery(); self.run_delivery()
        self.transport.assert_called_once()
        self.assertEqual(notification_status(self.path, {})['duplicate_suppressed_count'], 1)

    def test_failed_attempt_not_retried(self):
        self.transport.side_effect = TimeoutError()
        self.run_delivery(); self.run_delivery()
        self.transport.assert_called_once()

    def test_durable_reservation_before_transport(self):
        def send(*args):
            rows = json.loads(self.path.read_text())['records']
            self.assertEqual(rows[-1]['status'], 'ATTEMPTED')
            return 204, b''
        self.transport.side_effect = send
        self.run_delivery()

    def test_secrets_never_persisted(self):
        self.transport.side_effect = RuntimeError('https://example.invalid/hook test-secret')
        self.run_delivery()
        text = self.path.read_text()
        self.assertNotIn('test-secret', text)
        self.assertNotIn('example.invalid', text)
        self.assertNotIn('Closed-candle', text)

    def test_render_deterministic_and_input_unchanged(self):
        p = packet(); before = copy.deepcopy(p)
        self.assertEqual(render_message(p), render_message(dict(reversed(list(p.items())))))
        self.run_delivery(p)
        self.assertEqual(p, before)
        self.assertEqual(self.transport.call_args.args[2]['alert'], before)

    def test_identity_distinguishes_setup_and_time(self):
        p = packet(); other = copy.deepcopy(p); other['entry_zone'] = {'id': 'new-setup'}
        self.assertNotEqual(alert_identity(p), alert_identity(other))
        other = dict(p, generated_at=p['generated_at']+300)
        self.assertNotEqual(alert_identity(p), alert_identity(other))

    def test_adapters(self):
        cases = {
            'TELEGRAM': {'TOKEN': 'fake', 'CHAT_ID': 'fake-chat'},
            'DISCORD': {'URL': 'https://discord.com/api/webhooks/fake'},
            'LINE': {'TOKEN': 'fake', 'TO': 'fake-user'},
            'WEBHOOK': {'URL': 'https://example.invalid/hook'}}
        for channel, values in cases.items():
            env = {'GRIM_NOTIFY_'+channel+'_'+k: v for k,v in values.items()}
            env['GRIM_NOTIFY_'+channel+'_ENABLED'] = 'true'
            request, reason = adapter_request(channel, env, packet(), 'identity')
            self.assertIsNone(reason)
            if channel == 'DISCORD':
                self.assertEqual(request[2]['allowed_mentions'], {'parse': []})
            if channel == 'LINE':
                self.assertEqual(request[1]['Authorization'], 'Bearer fake')

    def test_corrupt_journal_blocks_send_not_caller(self):
        self.path.write_text('broken')
        self.assertEqual(self.run_delivery()['status'], 'DELIVERY_FAILED')
        self.transport.assert_not_called()

    def test_concurrent_lock_blocks_send(self):
        lock = self.path.with_suffix('.json.lock'); lock.touch()
        self.assertEqual(self.run_delivery()['status'], 'DELIVERY_FAILED')
        self.assertTrue(lock.exists()); self.transport.assert_not_called()

    def test_router_failure_does_not_escape_dispatch(self):
        with patch('market_reviewer.notification_delivery.deliver', side_effect=RuntimeError('fake')):
            dispatch_completed_reviews({'BTC': {'opportunity_alert': packet()}}, self.path)

    def test_status_read_only(self):
        self.run_delivery(); before = self.path.read_bytes()
        status = notification_status(self.path, self.env)
        self.assertEqual(status['enabled_channels'], ['WEBHOOK'])
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_priority_not_reinterpreted(self):
        p = packet(); p['alert_level'] = 'HIGH_PRIORITY_ALERT'
        self.assertEqual(self.run_delivery(p)['status'], 'SKIPPED_INVALID_PACKET')
        self.transport.assert_not_called()

    def test_no_http_or_redirect(self):
        self.env['GRIM_NOTIFY_WEBHOOK_URL'] = 'http://example.invalid'
        self.assertEqual(self.run_delivery()['records'][-1]['error'], 'INVALID_CHANNEL_URL')
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,None,None,'https://other.invalid'))

    def test_cli_status_no_send(self):
        from market_reviewer.cli import main
        with patch('builtins.print'), patch('market_reviewer.notification_delivery.http_transport') as network:
            self.assertEqual(main(['notification-status', '--path', str(self.path)]), 0)
        network.assert_not_called(); self.assertFalse(self.path.exists())

    def test_runner_commit_survives_delivery_errors_and_next_cycle(self):
        from contextlib import ExitStack
        from market_reviewer.observation_runner import RunnerConfig, _execute_ready_cycle
        cfg = RunnerConfig(output_dir=Path(self.tmp.name), state_path=Path(self.tmp.name)/'production.json',
                           research_tracker_path=Path(self.tmp.name)/'research.json')
        def production(*args):
            cfg.state_path.write_text('production-final')
            return {'reviews': {'BTC': {'opportunity_alert': packet()}}}
        def research(*args):
            cfg.research_tracker_path.write_text('research-final')
            return {'research_persistence': 'PASS'}
        with ExitStack() as stack:
            for name in ('_write_observation_intent', '_update_runner_state', '_write_production_head'):
                stack.enter_context(patch('market_reviewer.observation_runner.'+name))
            intent = stack.enter_context(patch('market_reviewer.observation_runner._update_observation_intent'))
            stack.enter_context(patch('market_reviewer.observation_runner._previous_review_timestamps', return_value={}))
            stack.enter_context(patch.dict('os.environ', self.env, clear=True))
            def timeout(*args):
                self.assertEqual(intent.call_args.kwargs['status'], 'COMPLETE')
                raise TimeoutError('fake')
            network = stack.enter_context(patch('market_reviewer.notification_delivery.http_transport', side_effect=timeout))
            for n in (1, 2):
                result = _execute_ready_cycle(cfg, {}, n, production, research, lambda: 123, 'test')
                self.assertEqual(result['production_result'], 'PASS')
                self.assertEqual(result['research_result'], 'PASS')
            network.assert_called_once()
        self.assertEqual(cfg.state_path.read_text(), 'production-final')
        self.assertEqual(cfg.research_tracker_path.read_text(), 'research-final')

    def test_telegram_provider_rejection(self):
        self.env = {'GRIM_NOTIFY_TELEGRAM_ENABLED': 'true', 'GRIM_NOTIFY_TELEGRAM_TOKEN': 'fake',
                    'GRIM_NOTIFY_TELEGRAM_CHAT_ID': 'fake'}
        self.transport.return_value = (200, b'{"ok":false}')
        self.assertEqual(self.run_delivery()['records'][0]['status'], 'DELIVERY_FAILED')

    def test_disk_failure_never_sends(self):
        with patch('market_reviewer.notification_delivery.atomic_write_json', side_effect=OSError('disk')):
            self.assertEqual(self.run_delivery()['status'], 'DELIVERY_FAILED')
        self.transport.assert_not_called()

    def test_oversized_message_is_not_truncated(self):
        p = packet(); p['market_story'] = 'x' * 110000
        self.assertEqual(self.run_delivery(p)['records'][-1]['error'], 'MESSAGE_TOO_LONG')
        self.transport.assert_not_called()
