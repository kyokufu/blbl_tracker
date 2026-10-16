import io
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from blbl_tracker.http import Blocked, GlobalError, NoRedirect, SyncError, Transport


def response(value):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read.return_value = json.dumps(value).encode()
    return result


def http_error(code, retry=None):
    headers = Message()
    if retry is not None:
        headers['Retry-After'] = retry
    return HTTPError('https://api.notion.com/private', code, 'private response', headers, io.BytesIO(b'secret'))


class HttpTests(unittest.TestCase):
    def make(self, host='api.notion.com', token='test-token'):
        transport = Transport(host, token=token, interval=0)
        transport.opener = Mock()
        return transport

    def test_fixed_host_headers_and_json_body(self):
        transport = self.make()
        transport.opener.open.return_value = response({'ok': True})
        self.assertEqual(transport.request('POST', '/v1/example', {'key': 1}), {'ok': True})
        req = transport.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.notion.com/v1/example')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-token')
        self.assertEqual(req.get_header('Notion-version'), '2025-09-03')
        self.assertEqual(json.loads(req.data), {'key': 1})
        self.assertLessEqual(transport.opener.open.call_args.kwargs['timeout'], 15)

    def test_no_credentials_to_bilibili(self):
        transport = self.make('api.bilibili.com')
        with self.assertRaises(GlobalError):
            transport.request('GET', '/test')
        transport.opener.open.assert_not_called()

    def test_unsafe_host_path_and_redirect_rejected(self):
        with self.assertRaises(GlobalError):
            Transport('evil.test')
        transport = self.make()
        for path in ('//evil.test', 'https://evil.test', 'relative'):
            with self.assertRaises(GlobalError):
                transport.request('GET', path)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test'))
        transport.opener.open.side_effect = http_error(302)
        with self.assertRaises(SyncError):
            transport.request('GET', '/test')
        self.assertEqual(transport.opener.open.call_count, 1)

    def test_auth_errors_are_global_and_not_retried(self):
        for code in (401, 403):
            transport = self.make()
            transport.opener.open.side_effect = http_error(code)
            with self.assertRaises(GlobalError) as raised:
                transport.request('GET', '/test')
            self.assertNotIn('private', str(raised.exception))
            self.assertNotIn('secret', str(raised.exception))
            self.assertEqual(transport.opener.open.call_count, 1)

    def test_bilibili_blocks_and_rate_limit_circuit_break(self):
        for code in (403, 412, 429):
            transport = self.make('api.bilibili.com', None)
            transport.opener.open.side_effect = http_error(code)
            with self.assertRaises(Blocked):
                transport.request('GET', '/test')
            self.assertEqual(transport.opener.open.call_count, 1)

    @patch('blbl_tracker.http.time.sleep')
    def test_retry_after_seconds_honored(self, sleep):
        transport = self.make()
        transport.opener.open.side_effect = [http_error(429, '7'), response({'ok': 1})]
        self.assertEqual(transport.request('POST', '/test', {}), {'ok': 1})
        self.assertIn(7.0, [call.args[0] for call in sleep.call_args_list])
        self.assertEqual(transport.opener.open.call_count, 2)

    @patch('blbl_tracker.http.time.sleep')
    def test_retry_after_http_date_honored(self, sleep):
        transport = self.make()
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        transport.opener.open.side_effect = [http_error(429, future), response({})]
        transport.request('GET', '/test')
        self.assertTrue(any(call.args[0] >= 28 for call in sleep.call_args_list))

    def test_retry_after_exceeding_deadline_does_not_retry_early(self):
        transport = self.make()
        transport.opener.open.side_effect = http_error(429, '9999')
        with self.assertRaises(GlobalError):
            transport.request('GET', '/test')
        self.assertEqual(transport.opener.open.call_count, 1)

    @patch('blbl_tracker.http.time.sleep')
    def test_transient_retries_bounded(self, sleep):
        for error in (http_error(503), URLError('sensitive network details'), TimeoutError()):
            transport = self.make()
            transport.opener.open.side_effect = error
            with self.assertRaisesRegex(SyncError, 'three attempts'):
                transport.request('PATCH', '/test', {'fixed': 1})
            self.assertEqual(transport.opener.open.call_count, 3)
            payloads = [call.args[0].data for call in transport.opener.open.call_args_list]
            self.assertEqual(payloads, [payloads[0]] * 3)

    def test_invalid_and_oversized_json_not_retried(self):
        for raw in (b'not json', b'[]', b'null', b'x' * 4_000_001):
            transport = self.make()
            result = response({})
            result.read.return_value = raw
            transport.opener.open.return_value = result
            with self.assertRaises(SyncError):
                transport.request('GET', '/test')
            self.assertEqual(transport.opener.open.call_count, 1)

    def test_deadline_prevents_network(self):
        transport = self.make()
        transport.deadline = time.monotonic() - 1
        with self.assertRaises(GlobalError):
            transport.request('GET', '/test')
        transport.opener.open.assert_not_called()

    def test_proxy_denial_is_global_and_not_retried(self):
        transport = self.make()
        transport.opener.open.side_effect = URLError(OSError('Tunnel connection failed: 403 Forbidden private'))
        with self.assertRaises(GlobalError) as raised:
            transport.request('GET', '/test')
        self.assertNotIn('private', str(raised.exception))
        self.assertEqual(transport.opener.open.call_count, 1)

    @patch('blbl_tracker.http.time.sleep')
    def test_single_attempt_url_assignment_does_not_retry_timeout(self, sleep):
        transport = self.make()
        transport.opener.open.side_effect = TimeoutError()
        with self.assertRaisesRegex(SyncError, 'one attempt'):
            transport.request('PATCH', '/test', {'properties': {'url': {'url': 'https://example.test'}}}, attempts=1)
        self.assertEqual(transport.opener.open.call_count, 1)
