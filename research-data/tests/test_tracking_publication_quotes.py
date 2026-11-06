import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_research import core, tracking, tracking_quotes as quotes


class TrackingPublicationQuoteTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(core, 'now', return_value=datetime.fromisoformat('2026-10-22T08:00:00+08:00'))
        clock.start(); self.addCleanup(clock.stop)
        self.report = {'id': 'fixture-report', 'market': 'SH', 'code': '600001', 'company': '示例',
                       'period': '2026Q3', 'generated_at': '2026-10-22T01:30:00+08:00', 'rating': 'unrated',
                       'score': 19.9, 'quote_at_analysis': {}}
        self.quote = {'price': 7.79, 'asof': '2026-10-21T15:30:00+08:00',
                      'fetched_at': '2026-10-22T01:30:01+08:00', 'source': 'https://example.com/quote',
                      'cross_validation': {'status': 'unavailable'}}
        self.record = {'report_id': self.report['id'], 'code': '600001', 'market': 'SH', 'state': 'available',
                       'report_generated_at': self.report['generated_at'], 'kind': 'publication',
                       'captured_at': self.report['generated_at'], 'quote': self.quote}

    def baseline(self, record=None, existing=None):
        return tracking.capture_baselines([self.report], existing, captured_at='2026-10-22T03:00:00+08:00',
                                          publication_quotes={'fixture-report': record or self.record})['fixture-report']

    def test_saved_capture_fills_initial_and_existing_missing_price_without_model_or_original_edits(self):
        original = copy.deepcopy(self.report)
        base = self.baseline()
        self.assertEqual((base['price'], base['price_basis']), (7.79, 'publication_capture'))
        old = {**base, 'price': None, 'price_asof': None, 'score': 11, 'rating': 'neutral', 'provenance': 'on_time'}
        result = self.baseline(existing={'fixture-report': old})
        self.assertEqual(result['price'], 7.79)
        self.assertEqual((result['score'], result['rating'], result['provenance']), (11, 'neutral', 'on_time'))
        self.assertEqual(self.report, original)

    def test_existing_nonempty_anchor_and_original_report_quote_take_priority(self):
        base = self.baseline()
        self.assertEqual(self.baseline(existing={'fixture-report': {**base, 'price': 8}})['price'], 8)
        self.report['quote_at_analysis'] = {**self.quote, 'price': 8}
        self.assertEqual(self.baseline()['price'], 8)
        self.assertEqual(self.baseline(existing={'fixture-report': {**base, 'price': None}})['price'], 8)

    def test_same_closed_session_backfill_is_labelled_and_preserves_actual_fetch_time(self):
        late = {**self.record, 'kind': 'backfill', 'captured_at': '2026-10-22T02:33:41+08:00',
                'quote': {**self.quote, 'fetched_at': '2026-10-22T02:33:43+08:00'}}
        result = self.baseline(late)
        self.assertEqual(result['price_basis'], 'closed_session_reconstruction')
        self.assertEqual(result['price_collected_at'], '2026-10-22T02:33:43+08:00')
        self.assertEqual(result['price_cross_validation']['status'], 'unavailable')

    def test_crossing_next_open_lunch_break_intraday_or_unknown_calendar_cannot_reconstruct(self):
        for generated, captured, asof in (
            ('2026-10-22T01:30:00+08:00', '2026-10-22T09:16:00+08:00', self.quote['asof']),
            ('2026-10-22T11:45:00+08:00', '2026-10-22T12:00:00+08:00', '2026-10-22T11:30:00+08:00'),
            ('2026-10-22T10:00:00+08:00', '2026-10-22T10:15:00+08:00', '2026-10-22T10:00:00+08:00'),
            ('2027-01-01T20:00:00+08:00', '2027-01-01T20:15:00+08:00', '2027-01-01T15:00:00+08:00')):
            with self.subTest(generated=generated):
                report = {**self.report, 'generated_at': generated}
                record = {**self.record, 'kind': 'backfill', 'captured_at': captured,
                          'quote': {**self.quote, 'asof': asof, 'fetched_at': captured}}
                self.assertEqual(quotes.baseline_fields(report, record), {})

    def test_publication_latest_visible_even_when_no_safe_original_baseline_exists(self):
        late = {**self.record, 'kind': 'backfill', 'captured_at': '2026-10-22T10:00:00+08:00',
                'quote': {**self.quote, 'price': 8, 'asof': '2026-10-22T10:00:00+08:00',
                          'fetched_at': '2026-10-22T10:00:00+08:00'}}
        base = self.baseline(late)
        self.assertIsNone(base['price'])
        result = tracking.build_tracking([self.report], {}, {}, {'fixture-report': base},
                    asof='2026-10-22T10:01:00+08:00', publication_quotes={'fixture-report': late})['reports'][0]
        self.assertEqual(result['current_price'], 8)
        self.assertIsNone(result['current_change_pct'])
        # A newer live quote must not be replaced by a report's earlier snapshot.
        current = {'quote': {**late['quote'], 'price': 9, 'asof': '2026-10-22T10:30:00+08:00'}}
        result = tracking.build_tracking([self.report], {'600001': current}, {}, {'fixture-report': base},
                    publication_quotes={'fixture-report': late})['reports'][0]
        self.assertEqual(result['current_price'], 9)

    def test_read_checks_identity_timestamp_and_never_collects_network_or_models(self):
        from stock_research.publication_market import path
        with tempfile.TemporaryDirectory() as directory, patch.object(core, 'VAULT', Path(directory)), \
                patch('stock_research.market_sources.latest_quote') as network, patch('stock_research.models.completion') as model:
            target = path(self.report); target.parent.mkdir(parents=True)
            target.write_text(json.dumps(self.record))
            self.assertEqual(quotes.saved(self.report)['quote']['price'], 7.79)
            for bad in ({'code': '600002'}, {'report_generated_at': '2026-10-21'},
                        {'quote': {**self.quote, 'asof': '2099-01-01T15:00:00+08:00'}},
                        {'captured_at': '2099-01-01T15:00:00+08:00'},
                        {'quote': []},
                        {'quote': {**self.quote, 'price': -1}}):
                target.write_text(json.dumps({**self.record, **bad}))
                self.assertEqual(quotes.saved(self.report), {})
            network.assert_not_called(); model.assert_not_called()

    def test_tracking_publication_connects_saved_quote_to_baseline_and_latest(self):
        from stock_research.publication_market import path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); vault = root / 'vault'; site = root / 'site'; site.mkdir()
            with patch.object(core, 'VAULT', vault), patch.object(tracking, 'SITE', site), \
                    patch.object(tracking, 'BASELINES', root / 'baselines.json'), \
                    patch('stock_research.market_sources.latest_quote') as network, \
                    patch('stock_research.models.completion') as model:
                target = path(self.report); target.parent.mkdir(parents=True)
                target.write_text(json.dumps(self.record))
                tracking.publish_tracking([self.report], {}, {}, vault=vault)
                row = json.loads((site / 'tracking.json').read_text())['reports'][0]
                self.assertEqual((row['price'], row['current_price'], row['current_change_pct']), (7.79, 7.79, 0))
                self.assertEqual(row['current_cross_validation']['status'], 'unavailable')
                network.assert_not_called(); model.assert_not_called()
