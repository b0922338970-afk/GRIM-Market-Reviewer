import copy
import json
import unittest
from pathlib import Path

from market_reviewer.tactical_provenance import deduplicate_zones, exposure_validation, valid_exposure, bind_tactical_identity
from market_reviewer.tactical_provenance_status import coverage_buckets
import test_tactical_provenance as fixtures


class ZoneIdentityTests(unittest.TestCase):
    def test_identical_full_payload_deduplicated(self):
        r, e = fixtures.chain()
        e['zones'].append(copy.deepcopy(e['zones'][0]))
        before = copy.deepcopy(e)
        result = deduplicate_zones(e)
        self.assertEqual(len(result['zones']), 1)
        self.assertEqual(result['zone_identity_diagnostics']['zone_deduplicated_count'], 1)
        self.assertTrue(valid_exposure(result, r, 12000))
        self.assertEqual(e, before)
        self.assertEqual(bind_tactical_identity(r, result, 12000)['status'], 'BOUND')

    def test_canonical_key_order_normalized(self):
        _, e = fixtures.chain()
        e['zones'].append(dict(reversed(list(e['zones'][0].items()))))
        self.assertEqual(len(deduplicate_zones(e)['zones']), 1)

    def test_deterministic_sort_and_ids_unchanged(self):
        _, e = fixtures.chain()
        e['zones'].append(dict(e['zones'][0], event_id='another'))
        a = deduplicate_zones(e)
        e['zones'].reverse()
        self.assertEqual(a, deduplicate_zones(e))
        self.assertEqual({z['event_id'] for z in a['zones']}, {'Z', 'another'})

    def test_collision_not_deduplicated(self):
        r, e = fixtures.chain()
        different = copy.deepcopy(e['zones'][0])
        different['evidence']['displacement_strength'] = 2.5
        e['zones'].append(different)
        result = deduplicate_zones(e)
        self.assertEqual(len(result['zones']), 2)
        self.assertEqual(result['zone_identity_diagnostics']['collision_group_count'], 1)
        self.assertEqual(exposure_validation(result, r, 12000)['reason'], 'EVENT_ID_COLLISION_DIFFERENT_PAYLOAD')
        binding = bind_tactical_identity(r, result, 12000)
        self.assertEqual(binding['reason'], 'EXPOSURE_VALIDATION_FAILED')
        self.assertNotEqual(binding['reason'], 'RAW_EVIDENCE_UNAVAILABLE')
        self.assertFalse(binding['origin_creation_allowed'])

    def test_entire_payload_compared(self):
        for key, value in [('setup_id', 'different'), ('available_at', 11999), ('producer', 'different')]:
            r, e = fixtures.chain()
            e['zones'].append(dict(e['zones'][0], **{key: value}))
            self.assertFalse(valid_exposure(deduplicate_zones(e), r, 12000))

    def test_other_collections_unchanged(self):
        _, e = fixtures.chain()
        e['zones'].append(copy.deepcopy(e['zones'][0]))
        result = deduplicate_zones(e)
        for key in ('raw_directional_displacement', 'structure_events', 'liquidity_events',
                    'production_selected_displacement', 'checkpoint'):
            self.assertEqual(result[key], e[key])

    def test_validator_does_not_silently_dedupe_persisted_zone(self):
        r, e = fixtures.chain()
        e['zones'].append(copy.deepcopy(e['zones'][0]))
        self.assertEqual(exposure_validation(e, r, 12000)['reason'], 'DUPLICATE_EVENT_ID')

    def test_raw_missing_distinct_from_invalid(self):
        r, e = fixtures.chain()
        self.assertEqual(exposure_validation(None, r, 12000)['status'], 'RAW_EVIDENCE_UNAVAILABLE')
        e['checkpoint'] = 0
        self.assertEqual(exposure_validation(e, r, 12000)['status'], 'EXPOSURE_VALIDATION_FAILED')
        t = dict(tactical_direction='SHORT', checkpoint=12000)
        self.assertEqual(coverage_buckets(t, r, e)['raw'], 'EXPOSURE_VALIDATION_FAILED')

    def test_mixed_collision_group_keeps_all_rows(self):
        r, e = fixtures.chain()
        e['zones'] += [copy.deepcopy(e['zones'][0]), dict(e['zones'][0], setup_id='other')]
        self.assertEqual(len(deduplicate_zones(e)['zones']), 3)
        self.assertEqual(exposure_validation(e, r, 12000)['reason'], 'EVENT_ID_COLLISION_DIFFERENT_PAYLOAD')

    def test_persisted_282_readonly_replay(self):
        path = Path('artifact/observation-commit-journal.json')
        if not path.exists():
            self.skipTest('Optional live #282 archive not installed')
        before = path.read_bytes()
        transactions = [t for t in json.loads(before)['transactions']
                        if t.get('observation_number') == 282 and t.get('status') == 'COMPLETE']
        if not transactions:
            self.skipTest('Optional #282 archive not installed')
        p = transactions[0]['recovery_payload']
        original = copy.deepcopy(p)
        for symbol, groups in [('BTC', 10), ('ETH', 11)]:
            with self.subTest(symbol=symbol):
                e = p['non_canonical_research_evidence']['tactical_provenance'][symbol]
                corrected = deduplicate_zones(e)
                self.assertEqual(corrected['zone_identity_diagnostics']['collision_group_count'], groups)
                self.assertEqual(corrected['zone_identity_diagnostics']['zone_deduplicated_count'], 0)
                v = exposure_validation(corrected, p['reviews'][symbol], p['canonical_checkpoint'])
                self.assertFalse(v['valid'])
                self.assertEqual(v['reason'], 'EVENT_ID_COLLISION_DIFFERENT_PAYLOAD')
                b = bind_tactical_identity(p['reviews'][symbol], corrected, p['canonical_checkpoint'])
                self.assertEqual(b['reason'], 'EXPOSURE_VALIDATION_FAILED')
        self.assertEqual(p, original)
        self.assertEqual(before, path.read_bytes())
