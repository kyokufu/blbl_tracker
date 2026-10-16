"""Bounded JSON transport. Error messages intentionally exclude remote bodies."""

import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SyncError(Exception):
    pass


class GlobalError(SyncError):
    pass


class Blocked(SyncError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, host, *, token=None, interval=1.0, deadline=None):
        if host not in {"api.notion.com", "api.bilibili.com"}:
            raise GlobalError("unsupported API host")
        self.host, self.token, self.interval = host, token, interval
        self.deadline = deadline if deadline is not None else time.monotonic() + 240
        self.last = float("-inf")
        self.opener = build_opener(NoRedirect())

    def wait(self, seconds):
        if time.monotonic() + max(seconds, 0) + 1 >= self.deadline:
            raise GlobalError("run deadline reached; remaining work deferred")
        time.sleep(max(seconds, 0))

    def request(self, method, path, body=None, *, attempts=3):
        if not path.startswith("/") or path.startswith("//") or urlsplit(path).netloc:
            raise GlobalError("invalid API path")
        headers = {"User-Agent": "blbl-tracker/1.0 (personal statistics sync)",
                   "Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            if self.host != "api.notion.com":
                raise GlobalError("credentials allowed only for Notion")
            headers.update({"Authorization": f"Bearer {self.token}",
                            "Notion-Version": "2025-09-03"})
        data = json.dumps(body).encode() if body is not None else None
        if attempts not in {1, 3}:
            raise GlobalError("unsupported attempt limit")
        for attempt in range(attempts):
            self.wait(self.interval - (time.monotonic() - self.last))
            self.last = time.monotonic()
            retry_after = None
            try:
                req = Request(f"https://{self.host}{path}", data=data,
                              headers=headers, method=method)
                with self.opener.open(req, timeout=min(15, self.deadline - time.monotonic())) as response:
                    raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise SyncError("API response too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise SyncError("API response is not an object")
                return result
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After")
                error.close()
                if self.host == "api.notion.com" and status in {401, 403}:
                    raise GlobalError(f"Notion authorization failed (HTTP {status})") from None
                if self.host == "api.bilibili.com" and status in {403, 412, 429}:
                    raise Blocked(f"Bilibili access blocked (HTTP {status})") from None
                if status != 429 and not 500 <= status <= 599:
                    raise SyncError(f"API request rejected (HTTP {status})") from None
            except URLError as error:
                # A denied CONNECT is an environment prerequisite, not a
                # transient video failure. Do not expose arbitrary reason text.
                reason = str(error.reason)
                if "Tunnel connection failed" in reason and any(code in reason for code in ("403", "407")):
                    raise GlobalError("network proxy denied API access; check environment egress policy") from None
            except (TimeoutError, OSError):
                pass
            except (ValueError, UnicodeError):
                raise SyncError("API returned invalid JSON") from None
            if attempt == attempts - 1:
                limit = "three attempts" if attempts == 3 else "one attempt"
                raise SyncError(f"API request failed after {limit}")
            delay = 2 ** attempt + random.random()
            if retry_after:
                try:
                    server_delay = float(retry_after)
                except ValueError:
                    try:
                        server_delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        raise SyncError("invalid Retry-After; deferring request") from None
                if not 0 <= server_delay < float("inf"):
                    raise SyncError("invalid Retry-After; deferring request")
                delay = max(delay, server_delay)
            self.wait(delay)
