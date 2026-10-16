import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch

from blbl_tracker.discovery import (UID, Tracker, Uploads, apply_approved, candidate,
                                    load_uploads, main, normalize, publication_day,
                                    reconcile, verify_uploads, write_private)
from blbl_tracker.http import Blocked, GlobalError, SyncError, Transport as HttpTransport
from test_http import http_error, response
from test_sync import SOURCE

BV = 'BV1xx411c7mD'
OTHER = 'BV17x411w7KC'
CONFIG = {'discovery': {'song': 'Song', 'artist': 'Artist', 'publish_date': 'Publish Date',
                        'status': 'Status', 'accounts': 'Published Acc.', 'url': 'url'}}
KINDS = {'song': 'title', 'artist': 'rich_text', 'publish_date': 'date',
         'status': 'status', 'accounts': 'multi_select', 'url': 'url'}


def video(bvid=BV, title='【歌ってみた】夜に駆ける / YOASOBI (Cover)', published='2026-10-16'):
    return {'bvid': bvid, 'uid': UID, 'title': title, 'published': published, 'verified': True}


def row(identifier='r1'):
    values = {'song': [{'plain_text': '夜に駆ける'}], 'artist': [{'plain_text': 'YOASOBI'}],
              'publish_date': {'start': '2026-10-16'}, 'status': {'name': 'Published'},
              'accounts': [{'name': 'B站'}, {'name': 'YouTube'}], 'url': None}
    return {'object': 'page', 'id': identifier, 'parent': {'data_source_id': SOURCE},
            'properties': {key: {'id': key, 'type': KINDS[key], KINDS[key]: value}
                           for key, value in values.items()}}


class Transport:
    def __init__(self, rows=None):
        self.rows = [row()] if rows is None else rows
        self.current = copy.deepcopy(self.rows[0]) if self.rows else row()
        self.calls = []
        self.schema = {'properties': {CONFIG['discovery'][key]: {'id': key, 'type': kind}
                                      for key, kind in KINDS.items()}}

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body), kwargs))
        if method == 'POST':
            return {'results': copy.deepcopy(self.rows), 'has_more': False}
        if method == 'PATCH':
            return {'object': 'page', 'id': path.rsplit('/', 1)[1]}
        if path.startswith('/v1/pages/'):
            result = copy.deepcopy(self.current)
            result['id'] = path.rsplit('/', 1)[1]
            return result
        return self.schema


def tracker(rows=None):
    transport = Transport(rows)
    client = Tracker(transport, SOURCE, copy.deepcopy(CONFIG))
    client.preflight()
    return client


class MatchingTests(unittest.TestCase):
    def test_multilingual_decorations(self):
        cases = [('夜に駆ける', 'YOASOBI', '【歌ってみた】夜に駆ける - YOASOBI / Cover'),
                 ('晴天', '周杰伦', '【翻唱】晴天 ／ 周杰伦'),
                 ('Rolling in the Deep', 'Adele', 'Rolling In The Deep — ＡＤＥＬＥ (Cover)'),
                 ('A & B', 'Artist', 'A &amp; B - Artist 【翻唱版】')]
        for song, artist, title in cases:
            with self.subTest(song=song):
                result = candidate(song, artist, '2026-10-16', video(title=title))
                self.assertIsNotNone(result)
                self.assertTrue(result['evidence']['artist_in_title'])
                if len(song) > 2:
                    self.assertEqual(result['confidence'], 'high')

    def test_english_word_boundaries(self):
        result = candidate('Love', 'Artist', '2026-10-16', video(title='Glove Artist'))
        self.assertTrue(result is None or not result['evidence']['song_exact'])

    def test_date_timezone_and_csv_epoch(self):
        self.assertEqual(publication_day('2026-10-15T20:00:00Z'), '2026-10-16')
        self.assertEqual(publication_day('0'), '1970-01-01')
        with self.assertRaises(ValueError):
            publication_day('2026-10-16T12:00:00')

    def test_does_not_transliterate_or_discard_unrelated_brackets(self):
        self.assertNotEqual(normalize('夜に駆ける'), normalize('Yoru ni Kakeru'))
        self.assertIn('live', normalize('Song [Live]'))
        self.assertEqual(normalize('Cover Me'), 'cover me')
        self.assertEqual(normalize('【Cover】Cover Me'), 'cover me')

    def test_confidence_missing_artist_and_date(self):
        self.assertEqual(candidate('夜に駆ける', 'YOASOBI', '2026-10-16', video())['confidence'], 'high')
        self.assertEqual(candidate('夜に駆ける', 'Different', '2026-10-16', video())['confidence'], 'medium')
        self.assertEqual(candidate('夜に駆ける', 'YOASOBI', None, video())['confidence'], 'low')
        self.assertEqual(candidate('夜に駆ける', 'YOASOBI', '2020-01-01', video())['confidence'], 'low')

    def test_exact_eligibility_and_existing_url(self):
        items = [row(str(i)) for i in range(5)]
        items[0]['properties']['status']['status']['name'] = 'Draft'
        items[1]['properties']['accounts']['multi_select'] = [{'name': '非B站'}]
        items[2]['properties']['url']['url'] = 'https://example.test/existing'
        items[3]['archived'] = True
        client = tracker(items)
        result = reconcile(client, client.rows(), [video()])
        self.assertEqual([m['page_id'] for m in result], ['2', '4'])
        self.assertEqual([m['state'] for m in result], ['existing_url', 'matched'])

    def test_ambiguous_uploads_and_duplicate_notion_rows(self):
        client = tracker()
        matches = reconcile(client, client.rows(), [video(), video(OTHER)])
        self.assertEqual(matches[0]['state'], 'ambiguous')
        client = tracker([row(), row('r2')])
        self.assertEqual([m['state'] for m in reconcile(client, client.rows(), [video()])], ['ambiguous'] * 2)

    def test_unverified_export_never_becomes_matched(self):
        client = tracker()
        result = reconcile(client, client.rows(), [video() | {'verified': False}])
        self.assertEqual(result[0]['state'], 'unverified')

    def test_wrong_url_type_rejected(self):
        client = tracker()
        client.transport.schema['properties']['url']['type'] = 'rich_text'
        with self.assertRaises(GlobalError):
            client.preflight()


class UploadTests(unittest.TestCase):
    def detail(self, bvid=BV, **changes):
        data = {'bvid': bvid, 'title': '夜に駆ける / YOASOBI (Cover)',
                'owner': {'mid': UID}, 'pubdate': 1792108800}
        data.update(changes)
        return {'code': 0, 'data': data}

    def browser_exports(self, records):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'uploads.json'
        path.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
        return load_uploads(path)

    def test_minimal_browser_export_is_enriched_from_detail_api(self):
        uploads = self.browser_exports([{'bvid': BV, 'title': 'ブラウザのタイトル',
                                         'url': f'https://www.bilibili.com/video/{BV}'}])
        self.assertIsNone(uploads[0]['uid'])
        self.assertIsNone(uploads[0]['published'])
        transport = Mock()
        transport.request.return_value = self.detail()
        results, failures = verify_uploads(uploads, Uploads(transport))
        self.assertEqual(failures, [])
        self.assertEqual(results[0]['uid'], UID)
        self.assertEqual(results[0]['published'], publication_day(1792108800))
        self.assertEqual(results[0]['title'], self.detail()['data']['title'])
        self.assertTrue(results[0]['verified'])
        transport.request.assert_called_once_with('GET', f'/x/web-interface/view?bvid={BV}')

    def test_missing_uid_or_date_individually_is_enriched(self):
        for missing in ('uid', 'published'):
            item = video()
            del item[missing]
            transport = Mock()
            transport.request.return_value = self.detail()
            results, failures = verify_uploads(self.browser_exports([item]), Uploads(transport))
            with self.subTest(missing=missing):
                self.assertFalse(failures)
                self.assertTrue(results[0]['verified'])
                self.assertEqual(results[0]['published'], publication_day(1792108800))

    def test_export_claimed_uid_does_not_override_owner_and_other_video_continues(self):
        items = self.browser_exports([video(), video(OTHER)])
        transport = Mock()
        transport.request.side_effect = [self.detail(owner={'mid': 123}), self.detail(OTHER)]
        results, failures = verify_uploads(items, Uploads(transport))
        self.assertEqual([v['bvid'] for v in results], [OTHER])
        self.assertEqual(failures[0]['bvid'], BV)
        self.assertEqual(transport.request.call_count, 2)

    def test_missing_or_invalid_api_pubdate_rejects_only_that_video(self):
        for value in ('missing', None, True, 'not a date'):
            bad = self.detail(pubdate=value)
            if value == 'missing':
                del bad['data']['pubdate']
            transport = Mock()
            transport.request.side_effect = [bad, self.detail(OTHER)]
            items = self.browser_exports([{'bvid': BV, 'title': 'First'}, {'bvid': OTHER, 'title': 'Second'}])
            results, failures = verify_uploads(items, Uploads(transport))
            with self.subTest(value=value):
                self.assertEqual([v['bvid'] for v in results], [OTHER])
                self.assertEqual(len(failures), 1)

    def test_api_lookup_failure_isolated_and_missing_video_does_not_stop_next(self):
        for failure in (SyncError('API request failed after three attempts'), {'code': -404}):
            transport = Mock()
            transport.request.side_effect = [failure, self.detail(OTHER)]
            items = self.browser_exports([{'bvid': BV, 'title': 'First'}, {'bvid': OTHER, 'title': 'Second'}])
            results, failures = verify_uploads(items, Uploads(transport))
            self.assertEqual([v['bvid'] for v in results], [OTHER])
            self.assertEqual(len(failures), 1)

    @patch('blbl_tracker.http.time.sleep')
    def test_enrichment_uses_pacing_timeout_and_bounded_retries(self, sleep):
        transport = HttpTransport('api.bilibili.com')
        transport.opener = Mock()
        transport.opener.open.side_effect = [http_error(503), http_error(503), http_error(503),
                                             response(self.detail(OTHER))]
        items = self.browser_exports([{'bvid': BV, 'title': 'First'}, {'bvid': OTHER, 'title': 'Second'}])
        results, failures = verify_uploads(items, Uploads(transport))
        self.assertEqual([v['bvid'] for v in results], [OTHER])
        self.assertEqual(len(failures), 1)
        self.assertEqual(transport.opener.open.call_count, 4)
        self.assertEqual(transport.interval, 1.0)
        self.assertTrue(any(call.args[0] > 0 for call in sleep.call_args_list))
        self.assertTrue(all(0 < call.kwargs['timeout'] <= 15 for call in transport.opener.open.call_args_list))

    def test_minimal_browser_exports_keep_validation_and_duplicate_detection(self):
        item = {'bvid': BV, 'title': 'Song', 'url': f'https://www.bilibili.com/video/{BV}'}
        with self.assertRaises(GlobalError):
            self.browser_exports([item, item])
        for changes in ({'bvid': 'invalid'}, {'title': ''}, {'published': 'not-a-date'},
                        {'uid': 1}, {'url': f'https://www.bilibili.com/video/{OTHER}'},
                        {'url': 'https://evil.test/video/' + BV}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, SyncError)):
                self.browser_exports([item | changes])

    def test_blocked_minimal_export_can_be_reported_without_a_date(self):
        items = self.browser_exports([{'bvid': BV, 'title': video()['title']}])
        client = Mock()
        client.verify.side_effect = Blocked('blocked')
        results, failures = verify_uploads(items, client)
        notion = tracker()
        matches = reconcile(notion, notion.rows(), results)
        self.assertEqual(matches[0]['state'], 'unverified')
        self.assertIsNone(matches[0]['candidates'][0]['published'])
        self.assertIsNone(matches[0]['candidates'][0]['evidence']['date_distance_days'])
        self.assertEqual(len(failures), 1)

    def test_owner_is_verified_per_video(self):
        transport = Mock()
        transport.request.return_value = {'code': 0, 'data': {'bvid': BV, 'owner': {'mid': UID},
                                            'title': 'Song', 'pubdate': 0}}
        result = Uploads(transport).verify(video())
        self.assertTrue(result['verified'])
        for bad in (False, '19997041', 1):
            transport.request.return_value['data']['owner']['mid'] = bad
            with self.subTest(uid=bad), self.assertRaises(SyncError):
                Uploads(transport).verify(video())
        transport.request.return_value['data']['owner']['mid'] = UID
        transport.request.return_value['data']['bvid'] = OTHER
        with self.assertRaises(SyncError):
            Uploads(transport).verify(video())

    def test_listing_paginates_and_rejects_mismatched_uid(self):
        transport = Mock()
        def page(items):
            return {'code': 0, 'data': {'page': {'count': 2}, 'list': {'vlist': items}}}
        transport.request.side_effect = [page([video()]), page([video(OTHER)])]
        self.assertEqual(len(Uploads(transport).listing()), 2)
        self.assertIn('mid=19997041&pn=2', transport.request.call_args.args[1])
        transport.request.side_effect = [page([video() | {'uid': 1}])]
        with self.assertRaises(GlobalError):
            Uploads(transport).listing()

    def test_listing_api_block_has_export_guidance(self):
        transport = Mock()
        transport.request.return_value = {'code': -352}
        with self.assertRaisesRegex(GlobalError, 'JSON/CSV'):
            Uploads(transport).listing()

    def test_detail_api_block_preserves_unverified_export_and_stops(self):
        transport = Mock()
        transport.request.return_value = {'code': -352}
        results, failures = verify_uploads([video(), video(OTHER)], Uploads(transport))
        self.assertEqual([v['verified'] for v in results], [False, False])
        self.assertEqual(len(failures), 1)
        transport.request.assert_called_once()

    def test_owner_mismatch_excluded_from_candidates(self):
        client = Mock()
        client.verify.side_effect = SyncError('owner mismatch')
        result, failures = verify_uploads([video()], client)
        self.assertEqual(result, [])
        self.assertEqual(failures[0]['reason'], 'unavailable_or_owner_mismatch')

    def test_json_csv_and_invalid_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'uploads.json'
            path.write_text(json.dumps([video()]), encoding='utf-8')
            self.assertEqual(load_uploads(path)[0]['uid'], UID)
            self.assertNotIn('verified', load_uploads(path)[0])
            path.write_text(json.dumps([video(), video()]))
            with self.assertRaises(GlobalError):
                load_uploads(path)
            path.write_text(json.dumps([video() | {'uid': 1}]))
            with self.assertRaises(ValueError):
                load_uploads(path)
            csv_path = Path(directory) / 'uploads.csv'
            csv_path.write_text(f'uid,bvid,title,published\n{UID},{BV},夜に駆ける,2026-10-16\n', encoding='utf-8-sig')
            self.assertEqual(load_uploads(csv_path)[0]['title'], '夜に駆ける')

    def test_report_exclusive_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            write_private(path, {'song': '夜に駆ける'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('夜に駆ける', path.read_text())
            with self.assertRaises(FileExistsError):
                write_private(path, {})


class ApprovalTests(unittest.TestCase):
    def setup_report(self):
        client = tracker()
        report = {'uid': UID, 'data_source_id': SOURCE, 'uploads': [video()],
                  'matches': reconcile(client, client.rows(), [video()])}
        uploads = Mock()
        uploads.verify.return_value = video()
        return client, uploads, report, [{'page_id': 'r1', 'bvid': BV}]

    def patches(self, tracker):
        return [call for call in tracker.transport.calls if call[0] == 'PATCH']

    def test_explicit_approval_only_writes_url_and_no_retry(self):
        client, uploads, report, approvals = self.setup_report()
        apply_approved(client, uploads, report, approvals)
        self.assertEqual(self.patches(client), [('PATCH', '/v1/pages/r1',
                         {'properties': {'url': {'url': f'https://www.bilibili.com/video/{BV}'}}}, {'attempts': 1})])
        uploads.verify.assert_called_once()

    def test_report_does_not_trigger_writes_without_apply(self):
        client, _, _, _ = self.setup_report()
        self.assertFalse(self.patches(client))

    def test_ambiguous_unverified_low_and_wrong_approval_rejected(self):
        for state in ('ambiguous', 'unverified', 'low_confidence', 'unmatched'):
            client, uploads, report, approvals = self.setup_report()
            report['matches'][0]['state'] = state
            with self.subTest(state=state), self.assertRaises(GlobalError):
                apply_approved(client, uploads, report, approvals)
            self.assertFalse(self.patches(client))
        client, uploads, report, _ = self.setup_report()
        with self.assertRaises(GlobalError):
            apply_approved(client, uploads, report, [{'page_id': 'r1', 'bvid': OTHER}])
        self.assertFalse(self.patches(client))

    def test_existing_url_changed_metadata_and_changed_status_never_write(self):
        for field in ('url', 'artist', 'publish_date', 'status'):
            client, uploads, report, approvals = self.setup_report()
            value = {'url': 'https://existing.test', 'artist': [{'plain_text': 'Other'}],
                     'publish_date': {'start': '2026-10-15'}, 'status': {'name': 'Draft'}}[field]
            client.transport.current['properties'][field][KINDS[field]] = value
            with self.subTest(field=field), self.assertRaises(GlobalError):
                apply_approved(client, uploads, report, approvals)
            self.assertFalse(self.patches(client))

    def test_live_owner_failure_no_writes(self):
        client, uploads, report, approvals = self.setup_report()
        uploads.verify.side_effect = Blocked('blocked')
        with self.assertRaises(Blocked):
            apply_approved(client, uploads, report, approvals)
        self.assertFalse(self.patches(client))

    def test_changed_video_metadata_no_writes(self):
        client, uploads, report, approvals = self.setup_report()
        uploads.verify.return_value = video(title='New title')
        with self.assertRaises(GlobalError):
            apply_approved(client, uploads, report, approvals)
        self.assertFalse(self.patches(client))

    def test_empty_or_duplicate_approvals_no_writes(self):
        for duplicate in (False, True):
            client, uploads, report, approvals = self.setup_report()
            with self.assertRaises(GlobalError):
                apply_approved(client, uploads, report, approvals * 2 if duplicate else [])
            self.assertFalse(self.patches(client))

    def test_cli_defaults_to_read_only_report(self):
        client = tracker()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[discovery]\nsong="Song"\n')
            path = Path(directory) / 'report.json'
            with patch.dict('os.environ', {'NOTION_TOKEN': 'test', 'NOTION_DATA_SOURCE_ID': SOURCE}), \
                 patch('blbl_tracker.discovery.Tracker', return_value=client), \
                 patch('blbl_tracker.discovery.Uploads') as uploads:
                uploads.return_value.listing.return_value = [video()]
                uploads.return_value.verify.return_value = video()
                self.assertEqual(main(['--config', str(config), '--report', str(path)]), 0)
            self.assertEqual(json.loads(path.read_text())['matches'][0]['state'], 'matched')
            self.assertFalse(self.patches(client))

    def test_cli_blocked_listing_fails_without_empty_success_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'uploads.json'
            with patch('blbl_tracker.discovery.Uploads') as uploads, self.assertLogs(level='ERROR') as logs:
                uploads.return_value.listing.side_effect = GlobalError('network proxy denied API access')
                self.assertEqual(main(['--export-uploads', str(path)]), 2)
            self.assertFalse(path.exists())
            self.assertIn('--uploads', '\n'.join(logs.output))

    def test_cli_export_fallback_writes_unverified_report_without_notion_writes(self):
        client = tracker()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[discovery]\nsong="Song"\n')
            path = Path(directory) / 'report.json'
            export = Path(directory) / 'uploads.json'
            export.write_text(json.dumps([video()]))  # Forged "verified" must be ignored.
            with patch.dict('os.environ', {'NOTION_TOKEN': 'test', 'NOTION_DATA_SOURCE_ID': SOURCE}), \
                 patch('blbl_tracker.discovery.Tracker', return_value=client), \
                 patch('blbl_tracker.discovery.Uploads') as uploads:
                uploads.return_value.verify.side_effect = Blocked('blocked')
                self.assertEqual(main(['--config', str(config), '--uploads', str(export), '--report', str(path)]), 1)
                uploads.return_value.listing.assert_not_called()
            self.assertEqual(json.loads(path.read_text())['matches'][0]['state'], 'unverified')
            self.assertFalse(self.patches(client))

    def test_minimal_browser_export_dry_run_hydrates_and_never_patches(self):
        client = tracker()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[discovery]\nsong="Song"\n')
            path, export = Path(directory) / 'report.json', Path(directory) / 'uploads.json'
            export.write_text(json.dumps([{'bvid': BV, 'title': 'Browser title',
                                           'url': f'https://www.bilibili.com/video/{BV}'}]))
            transport = Mock()
            transport.request.return_value = {'code': 0, 'data': {
                'bvid': BV, 'title': video()['title'], 'owner': {'mid': UID},
                'pubdate': int(datetime.fromisoformat('2026-10-16T12:00:00+08:00').timestamp())}}
            with patch.dict('os.environ', {'NOTION_TOKEN': 'test', 'NOTION_DATA_SOURCE_ID': SOURCE}), \
                 patch('blbl_tracker.discovery.Tracker', return_value=client), \
                 patch('blbl_tracker.discovery.Transport', return_value=transport):
                self.assertEqual(main(['--dry-run', '--config', str(config), '--uploads', str(export),
                                       '--report', str(path)]), 0)
            report = json.loads(path.read_text())
            self.assertEqual(report['matches'][0]['state'], 'matched')
            self.assertEqual(report['uploads'][0]['published'], '2026-10-16')
            self.assertTrue(report['uploads'][0]['verified'])
            self.assertFalse(self.patches(client))
            self.assertFalse(any(c.args[0] == 'PATCH' for c in transport.request.call_args_list))

    def test_apply_rejects_unapproved_entry_before_any_write(self):
        client, uploads, report, approvals = self.setup_report()
        approvals.append({'page_id': 'not-in-report', 'bvid': OTHER})
        with self.assertRaises(GlobalError):
            apply_approved(client, uploads, report, approvals)
        self.assertFalse(self.patches(client))
