"""Untrusted identifier parsing and strict four-count validation."""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .http import SyncError

METRICS = {"views": "view", "likes": "like", "favorites": "favorite", "coins": "coin"}
BV = re.compile(r"BV[A-Za-z0-9]{10}", re.ASCII)


def parse_bvid(value):
    value = value.strip()
    if BV.fullmatch(value):
        return value
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in {"bilibili.com", "www.bilibili.com"}
                or url.username is not None or url.password is not None or url.port is not None):
            raise ValueError
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", url.path, re.ASCII)
        if not match:
            raise ValueError
        return match[1]
    except ValueError:
        raise SyncError("invalid Bilibili URL/BV ID") from None


def validate_stats(payload, bvid):
    # No speculative application-level retry/block codes: HTTP blocks stop calls.
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise SyncError("Bilibili application error")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("bvid") != bvid:
        raise SyncError("Bilibili video identity mismatch")
    stats = data.get("stat")
    if not isinstance(stats, dict):
        raise SyncError("Bilibili statistics missing")
    result = {key: stats.get(field) for key, field in METRICS.items()}
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise SyncError("Bilibili required counts invalid or missing")
    return result


class Bilibili:
    def __init__(self, transport):
        self.transport = transport

    def fetch(self, bvid):
        if not BV.fullmatch(bvid):
            raise SyncError("invalid BV ID")
        stats = validate_stats(self.transport.request("GET", f"/x/web-interface/view?bvid={bvid}"), bvid)
        timestamp = datetime.now(timezone.utc).isoformat()
        return stats, timestamp
