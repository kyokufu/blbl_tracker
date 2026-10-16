import copy
import unittest
from unittest.mock import Mock

from blbl_tracker.bilibili import Bilibili, parse_bvid, validate_stats
from blbl_tracker.http import SyncError

BV = "BV1xx411c7mD"


def payload():
    return {"code": 0, "data": {"bvid": BV, "stat": {"view": 0, "like": 12, "favorite": 3, "coin": 4}}}


class BilibiliTests(unittest.TestCase):
    def test_supported_identifiers(self):
        for value in (BV, f"  {BV} ", f"https://www.bilibili.com/video/{BV}",
                      f"https://bilibili.com/video/{BV}/?p=2#fragment"):
            with self.subTest(value=value):
                self.assertEqual(parse_bvid(value), BV)

    def test_reject_unsafe_or_invalid_identifiers(self):
        for value in ("", "av123", "bv1xx411c7mD", BV + "x", "BV１２３４５６７８９０",
                      f"http://www.bilibili.com/video/{BV}", "https://b23.tv/example",
                      f"https://evil.test/video/{BV}", f"https://bilibili.com.evil.test/video/{BV}",
                      f"https://user@bilibili.com/video/{BV}", f"https://bilibili.com:443/video/{BV}",
                      f"https://bilibili.com/video/{BV}/extra", "https://[invalid/"):
            with self.subTest(value=value), self.assertRaises(SyncError):
                parse_bvid(value)

    def test_valid_zero(self):
        self.assertEqual(validate_stats(payload(), BV), {"views": 0, "likes": 12, "favorites": 3, "coins": 4})

    def test_required_count_validation(self):
        for key in ("view", "like", "favorite", "coin"):
            for value in (None, True, -1, 1.0, "1"):
                item = payload()
                item["data"]["stat"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(SyncError):
                    validate_stats(item, BV)
            item = payload()
            del item["data"]["stat"][key]
            with self.assertRaises(SyncError):
                validate_stats(item, BV)

    def test_identity_status_and_shape(self):
        variants = [{}, {"code": False}, {"code": "0"}, {"code": -404},
                    {"code": 0, "data": None}, {"code": 0, "data": {"bvid": BV, "stat": []}}]
        item = copy.deepcopy(payload())
        item["data"]["bvid"] = "BV17x411w7KC"
        for item in [*variants, item]:
            with self.subTest(item=item), self.assertRaises(SyncError):
                validate_stats(item, BV)

    def test_fetch_fixed_endpoint_and_utc_timestamp(self):
        transport = Mock()
        transport.request.return_value = payload()
        stats, timestamp = Bilibili(transport).fetch(BV)
        transport.request.assert_called_once_with("GET", f"/x/web-interface/view?bvid={BV}")
        self.assertEqual(stats["views"], 0)
        self.assertTrue(timestamp.endswith("+00:00"))
