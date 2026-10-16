"""One-time, review-first URL reconciliation. Not imported by recurring sync."""

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import time
import tomllib
import unicodedata
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .bilibili import BV, parse_bvid
from .http import Blocked, GlobalError, SyncError, Transport
from .notion import Notion, segment

UID = 19997041
CHINA = timezone(timedelta(hours=8))
log = logging.getLogger(__name__)


def normalize(text):
    """Preserve scripts/diacritics; remove only common performance decorations."""
    text = unicodedata.normalize("NFKC", html.unescape(text)).casefold()
    decoration = r"(?:cover|official\s+(?:music\s+)?video|mv|pv)"
    text = re.sub(r"\b(?:covered\s+by|cover\s+by)\b", " ", text)
    text = re.sub(r"(?<=[(\[【])\s*" + decoration + r"\s*(?=[)\]】])", " ", text)
    text = re.sub(r"\s+" + decoration + r"\s*$", " ", text)
    text = re.sub(r"^" + decoration + r"\s*[:|/—–-]\s*", " ", text)
    for word in ("歌ってみました", "歌ってみた", "翻唱版", "翻唱", "弾いてみた", "弹唱", "彈唱"):
        text = text.replace(word, " ")
    return " ".join("".join(c if unicodedata.category(c)[0] in "LN" else " " for c in text).split())


def compact(text):
    return normalize(text).replace(" ", "")


def publication_day(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is int:
        return datetime.fromtimestamp(value, CHINA).date().isoformat()
    if not isinstance(value, str) or not value:
        raise ValueError("publication date required")
    if len(value) == 10:
        return date.fromisoformat(value).isoformat()
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return stamp.astimezone(CHINA).date().isoformat()


def upload_record(item, *, allow_incomplete=False):
    """Validate an export hint; only detail API verification proves ownership."""
    if not isinstance(item, dict):
        raise ValueError("upload must be an object")
    bvid, title = item.get("bvid"), item.get("title")
    uid = item.get("uid", item.get("mid"))
    missing_uid = uid is None or uid == ""
    if (missing_uid and not allow_incomplete) or (not missing_uid and (isinstance(uid, bool) or str(uid) != str(UID))):
        raise ValueError("upload owner UID missing or mismatched")
    if not isinstance(bvid, str) or not BV.fullmatch(bvid) or not isinstance(title, str) or not title.strip():
        raise ValueError("invalid upload identifier/title")
    if "url" in item and item["url"] not in (None, ""):
        if not isinstance(item["url"], str) or parse_bvid(item["url"]) != bvid:
            raise ValueError("upload URL/BV ID mismatch")
    published = item.get("published", item.get("created"))
    missing_date = published is None or published == ""
    return {"bvid": bvid, "title": title, "uid": None if missing_uid else UID,
            "published": None if missing_date and allow_incomplete else publication_day(published)}


def load_uploads(path):
    if path.stat().st_size > 8_000_000:
        raise GlobalError("upload export too large")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() == ".csv":
            records = list(csv.DictReader(handle))
        elif path.suffix.lower() == ".json":
            records = json.load(handle)
            if isinstance(records, dict):
                if "uid" in records and records["uid"] != UID:
                    raise GlobalError("export account UID mismatch")
                records = records["uploads"]
        else:
            raise GlobalError("upload export must be JSON or CSV")
    if not isinstance(records, list):
        raise GlobalError("upload export must contain a list")
    uploads = [upload_record(item, allow_incomplete=True) for item in records]
    if len({item["bvid"] for item in uploads}) != len(uploads):
        raise GlobalError("duplicate BV IDs in upload export")
    return uploads


class Uploads:
    def __init__(self, transport):
        self.transport = transport

    def listing(self):
        """Try the unsigned public listing; never bypass signing/challenges."""
        uploads, seen = [], set()
        expected = None
        for page in range(1, 1001):
            payload = self.transport.request("GET", f"/x/space/arc/search?mid={UID}&pn={page}&ps=30&order=pubdate")
            try:
                if type(payload.get("code")) is not int or payload["code"] != 0:
                    raise ValueError
                data = payload["data"]
                total = data["page"]["count"]
                videos = data["list"]["vlist"]
                if type(total) is not int or total < 0 or not isinstance(videos, list):
                    raise ValueError
                if expected is None:
                    expected = total
                if total != expected:
                    raise ValueError
                for item in videos:
                    video = upload_record(item)
                    if video["bvid"] in seen:
                        raise ValueError
                    seen.add(video["bvid"])
                    uploads.append(video)
                if len(uploads) == total:
                    return uploads
                if not videos or len(uploads) > total:
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise GlobalError("Bilibili upload listing unavailable/changed; supply a JSON/CSV export") from None
        raise GlobalError("upload listing pagination exceeded limit")

    def verify(self, item):
        """An export's UID assertion is never sufficient proof of ownership."""
        payload = self.transport.request("GET", f"/x/web-interface/view?bvid={item['bvid']}")
        if type(payload.get("code")) is int and payload["code"] == -404:
            raise SyncError("video unavailable")
        if type(payload.get("code")) is not int or payload["code"] != 0:
            raise Blocked("Bilibili detail unavailable (application error); ownership unverified")
        try:
            data = payload["data"]
            if data["bvid"] != item["bvid"] or type(data["owner"]["mid"]) is not int or data["owner"]["mid"] != UID:
                raise ValueError
            return upload_record({"bvid": data["bvid"], "title": data["title"],
                                  "uid": data["owner"]["mid"], "created": data["pubdate"]}) | {"verified": True}
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise SyncError("video unavailable, changed response, or owner UID mismatch") from None


def verify_uploads(uploads, client):
    results, failures, stopped = [], [], False
    for item in uploads:
        if stopped:
            results.append(item | {"verified": False})
            continue
        try:
            results.append(client.verify(item))
        except (Blocked, GlobalError) as error:
            log.warning("ownership verification stopped: %s; export can be reviewed but not applied", error)
            failures.append({"bvid": item["bvid"], "reason": "network_or_block"})
            results.append(item | {"verified": False})
            stopped = True
        except SyncError:
            # A rejected/mismatched owner is not a candidate, even offline.
            failures.append({"bvid": item["bvid"], "reason": "unavailable_or_owner_mismatch"})
    return results, failures


class Tracker(Notion):
    def preflight(self):
        expected = {"song": {"title", "rich_text"}, "artist": {"title", "rich_text", "select", "multi_select"},
                    "publish_date": {"date"}, "status": {"status", "select"},
                    "accounts": {"multi_select", "select", "rich_text"}, "url": {"url"}}
        try:
            schema = self.transport.request("GET", f"/v1/data_sources/{segment(self.source_id)}")["properties"]
            wanted = self.config["discovery"]
            if set(wanted) != set(expected):
                raise ValueError
            for logical, key in wanted.items():
                matches = [p for name, p in schema.items() if key in {name, p["id"]}]
                if len(matches) != 1 or matches[0]["type"] not in expected[logical]:
                    raise ValueError
                self.mapping[logical] = (matches[0]["id"], matches[0]["type"])
            if len({v[0] for v in self.mapping.values()}) != len(expected):
                raise ValueError
        except GlobalError:
            raise
        except (SyncError, KeyError, TypeError, ValueError, AttributeError):
            raise GlobalError("discovery schema mismatch; verify the six mapped properties/types") from None

    def text(self, row, key):
        value = self.value(row, key)
        kind = self.mapping[key][1]
        if kind in {"title", "rich_text"}:
            return "".join(p["plain_text"] for p in value)
        if kind == "multi_select":
            return "; ".join(p["name"] for p in value)
        if kind in {"status", "select"}:
            return value["name"] if value else ""
        return value or ""

    def eligible(self, row):
        if row.get("archived") or row.get("in_trash") or self.text(row, "status") != "Published":
            return False
        kind = self.mapping["accounts"][1]
        if kind == "multi_select":
            return any(p["name"] == "B站" for p in self.value(row, "accounts"))
        if kind == "select":
            return self.text(row, "accounts") == "B站"
        return "B站" in self.text(row, "accounts")

    def snapshot(self, row):
        return {key: self.value(row, key) for key in self.mapping}

    def fingerprint(self, row):
        value = {"source": self.source_id, "mapping": self.mapping, "row": self.snapshot(row)}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def assign(self, row, bvid):
        if not BV.fullmatch(bvid) or not self.eligible(row) or self.value(row, "url"):
            raise GlobalError("URL assignment refused: existing URL or changed eligibility")
        # A retry after an ambiguous timeout could overwrite a human's new URL.
        response = self.transport.request("PATCH", f"/v1/pages/{segment(row['id'])}",
                                          {"properties": {self.mapping['url'][0]: {
                                              "url": f"https://www.bilibili.com/video/{bvid}"}}}, attempts=1)
        if response.get("object") != "page" or response.get("id") != row["id"]:
            raise GlobalError("URL update acknowledgement invalid; re-read before retrying")


def candidate(song, artist, published, video):
    normalized_title, normalized_song = normalize(video["title"]), normalize(song)
    title, song, artist = compact(video["title"]), compact(song), compact(artist)
    if not song or not title:
        return None
    exact = song in title
    if normalized_song.isascii():
        exact = re.search(r"(?:^|\s)" + re.escape(normalized_song) + r"(?:$|\s)", normalized_title) is not None
    similarity = 1.0 if exact else SequenceMatcher(None, song, title, autojunk=False).ratio()
    if similarity < 0.65:
        return None
    artist_match = bool(artist and artist in title)
    days = (abs((date.fromisoformat(published) - date.fromisoformat(video["published"])).days)
            if published and video.get("published") else None)
    score = round(65 * similarity + 20 * artist_match + (15 if days == 0 else 8 if days == 1 else 0))
    confidence = "high" if exact and artist_match and days == 0 else "medium" if exact and days in {0, 1} else "low"
    return {"bvid": video["bvid"], "title": video["title"], "published": video.get("published"),
            "score": score, "confidence": confidence, "owner_verified": video.get("verified") is True,
            "evidence": {"song_exact": exact, "song_similarity": round(similarity, 3),
                         "artist_in_title": artist_match, "date_distance_days": days}}


def reconcile(tracker, rows, videos):
    matches = []
    for row in rows:
        if not tracker.eligible(row):
            continue
        record = {"page_id": row["id"], "fingerprint": tracker.fingerprint(row),
                  "song": tracker.text(row, "song"), "artist": tracker.text(row, "artist"),
                  "publish_date": None, "state": "unmatched", "candidates": []}
        if tracker.value(row, "url"):
            record["state"] = "existing_url"
            matches.append(record)
            continue
        day = tracker.value(row, "publish_date")
        record["publish_date"] = publication_day(day["start"]) if day else None
        candidates = [c for v in videos if (c := candidate(record["song"], record["artist"], record["publish_date"], v))]
        candidates.sort(key=lambda c: (-c["score"], c["bvid"]))
        record["candidates"] = candidates
        if candidates:
            top = candidates[0]
            ambiguous = len(candidates) > 1 and top["score"] - candidates[1]["score"] < 10
            record["state"] = ("ambiguous" if ambiguous else "unverified" if not top["owner_verified"]
                               else "low_confidence" if top["confidence"] == "low" else "matched")
        matches.append(record)
    # Never silently assign one upload to multiple tracker rows.
    usage = Counter(m["candidates"][0]["bvid"] for m in matches if m["candidates"])
    for match in matches:
        if match["candidates"] and usage[match["candidates"][0]["bvid"]] > 1:
            match["state"] = "ambiguous"
    return matches


def write_private(path, value):
    # Exclusive creation prevents overwriting existing reports/exports or links.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def apply_approved(tracker, client, report, approvals):
    if report.get("uid") != UID or report.get("data_source_id") != tracker.source_id:
        raise GlobalError("report UID/source mismatch")
    if not isinstance(approvals, list) or not approvals:
        raise GlobalError("approval file must explicitly list page_id/bvid pairs")
    wanted = [(a["page_id"], a["bvid"]) for a in approvals]
    if len(set(p for p, _ in wanted)) != len(wanted) or len(set(b for _, b in wanted)) != len(wanted):
        raise GlobalError("duplicate/conflicting approvals")
    tracker.preflight()
    rows = tracker.rows()
    # Reverify all candidates from the report so ambiguity is checked again.
    inputs = [upload_record(v) for v in report["uploads"]]
    if len({v["bvid"] for v in inputs}) != len(inputs):
        raise GlobalError("report contains duplicate uploads")
    videos = [client.verify(v) for v in inputs]
    if any(any(old[key] != fresh[key] for key in ("bvid", "title", "published", "uid"))
           for old, fresh in zip(inputs, videos)):
        raise GlobalError("upload metadata changed since review; generate a fresh report")
    matches = {m["page_id"]: m for m in reconcile(tracker, rows, videos)}
    original = {m["page_id"]: m for m in report["matches"]}
    by_id = {r["id"]: r for r in rows}
    prepared = []
    for page_id, bvid in wanted:
        current, old = matches.get(page_id), original.get(page_id)
        if (not current or not old or current["state"] != "matched" or old["state"] != "matched"
                or current["fingerprint"] != old["fingerprint"]
                or current["candidates"][0]["bvid"] != bvid or old["candidates"][0]["bvid"] != bvid):
            raise GlobalError("approval rejected: ambiguous, stale, unverified, or mismatched report")
        prepared.append((by_id[page_id], bvid, current["fingerprint"]))
    # Complete approval validation first. Immediately re-read before each write.
    for row, bvid, fingerprint in prepared:
        current = tracker.reread(row)
        if tracker.fingerprint(current) != fingerprint:
            raise GlobalError("row changed since review; URL not assigned")
        tracker.assign(current, bvid)
    log.info("approved URL assignments completed=%s", len(prepared))


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-time UID 19997041 URL reconciliation; defaults to read-only")
    parser.add_argument("--config", default="discovery.local.toml")
    parser.add_argument("--uploads", type=Path, help="JSON/CSV export fallback")
    parser.add_argument("--report", type=Path, default=Path("discovery-report.json"))
    parser.add_argument("--export-uploads", type=Path, help="save fetched uploads, even without Notion access")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--approvals", type=Path, help="explicit reviewed page_id/bvid pairs; only used with --apply")
    args = parser.parse_args(argv)
    if args.apply and (not args.approvals or args.uploads or args.export_uploads):
        parser.error("--apply requires --approvals and an existing --report; no upload/export flags")
    if args.approvals and not args.apply:
        parser.error("--approvals requires --apply")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        deadline = time.monotonic() + 240
        client = Uploads(Transport("api.bilibili.com", deadline=deadline))
        if not args.apply:
            try:
                uploads = load_uploads(args.uploads) if args.uploads else client.listing()
            except SyncError:
                log.error("Bilibili upload discovery failed; retry locally or supply --uploads uploads.json/CSV")
                raise
            uploads, failures = verify_uploads(uploads, client)
            if args.export_uploads:
                write_private(args.export_uploads, {"uid": UID, "uploads": uploads, "failures": failures})
                log.info("upload export saved; ownership_verified=%s/%s", sum(v.get("verified", False) for v in uploads), len(uploads))
                return 1 if failures or any(not v.get("verified") for v in uploads) else 0
        with open(args.config, "rb") as handle:
            config = tomllib.load(handle)
        source_id = str(uuid.UUID(os.environ.get("NOTION_DATA_SOURCE_ID", "")))
        token = os.environ.get("NOTION_TOKEN", "")
        if not token or token != token.strip() or any(c in token for c in "\r\n"):
            raise GlobalError("NOTION_TOKEN is missing/invalid")
        tracker = Tracker(Transport("api.notion.com", token=token, interval=0.4, deadline=deadline), source_id, config)
        if args.apply:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            approvals = json.loads(args.approvals.read_text(encoding="utf-8"))
            apply_approved(tracker, client, report, approvals)
            return 0
        tracker.preflight()
        matches = reconcile(tracker, tracker.rows(), uploads)
        write_private(args.report, {"version": 1, "uid": UID, "data_source_id": source_id,
                                    "generated": datetime.now(timezone.utc).isoformat(),
                                    "uploads": uploads, "verification_failures": failures, "matches": matches})
        log.info("read-only report saved; states=%s", dict(Counter(m["state"] for m in matches)))
        return 1 if failures or any(not v.get("verified") for v in uploads) else 0
    except (GlobalError, SyncError) as error:
        log.error("discovery stopped: %s", error)
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        log.error("invalid discovery configuration/input or inaccessible file; no further writes attempted")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
