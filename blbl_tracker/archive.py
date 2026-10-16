"""Read-only historical archive preview. There is deliberately no apply stage."""

import argparse
import copy
import csv
import io
import json
import logging
import os
import re
import tempfile
import time
import tomllib
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .bilibili import BV, METRICS, parse_bvid, validate_stats
from .discovery import (UID, Tracker, candidate, compact, load_uploads, normalize,
                        publication_day, write_private)
from .http import Blocked, GlobalError, SyncError, Transport
from .notion import segment

SOURCE = "15a22148-d475-4b8c-8757-39678fd3437e"
TYPES = {"Cover", "Bass Tab", "Cover + Tab", "Other"}
log = logging.getLogger(__name__)


class ReadOnly:
    """Allow Notion reads/query POSTs only, independent of CLI flags."""
    def __init__(self, transport, source):
        self.transport, self.source = transport, source

    def request(self, method, path, body=None):
        schema = f"/v1/data_sources/{segment(self.source)}"
        if not ((method == "GET" and path == schema) or
                (method == "POST" and path == schema + "/query")):
            raise GlobalError("archive preview refuses Notion writes or unrelated requests")
        return self.transport.request(method, path, body)


def detail(payload, bvid):
    code = payload.get("code")
    if type(code) is not int or code != 0:
        if code in (-352, -412):
            raise Blocked("Bilibili application block; retry in a later batch")
        raise SyncError("Bilibili application error")
    try:
        data = payload["data"]
        owner = data["owner"]["mid"]
        if data["bvid"] != bvid or type(owner) is not int:
            raise ValueError
        if owner != UID:
            return {"bvid": bvid, "status": "owner_mismatch", "uid": owner,
                    "reason": "detail API confirms a different owner"}
        if type(data["pubdate"]) is not int or data["pubdate"] < 0:
            raise ValueError
        if not isinstance(data["title"], str) or not data["title"].strip():
            raise ValueError
        description = data.get("desc", "")
        if not isinstance(description, str):
            raise ValueError
        try:
            stats = validate_stats(payload, bvid)
        except SyncError:
            stats = None
        return {"bvid": bvid, "title": data["title"], "description": description,
                "uid": owner, "pubdate": data["pubdate"],
                "published": publication_day(data["pubdate"]), "stats": stats,
                "status": "verified"}
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise SyncError("invalid detail metadata; owner/date not verified") from None


class MetadataCache:
    def __init__(self, path):
        self.path = Path(path)
        self.records = {}
        if self.path.is_symlink():
            raise GlobalError("cache must not be a symlink")
        if self.path.exists():
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("version") != 1 or document.get("uid") != UID:
                raise GlobalError("cache version/account mismatch")
            self.records = document["records"]
            if not isinstance(self.records, dict):
                raise GlobalError("invalid cache records")
            for bvid, item in self.records.items():
                if not BV.fullmatch(bvid) or item.get("bvid") != bvid:
                    raise GlobalError("invalid cache identity")
                if item.get("status") not in {"verified", "owner_mismatch", "unverified"}:
                    raise GlobalError("invalid cached state")
                if item["status"] == "verified":
                    checked = detail({"code": 0, "data": {"bvid": bvid, "title": item["title"],
                                    "owner": {"mid": item["uid"]}, "pubdate": item["pubdate"],
                                    "desc": item.get("description", "")}}, bvid)
                    if checked["status"] != "verified" or checked["published"] != item["published"]:
                        raise GlobalError("invalid cached verification")
                    if item.get("stats") is not None:
                        validate_stats({"code": 0, "data": {"bvid": bvid, "stat": {
                            field: item["stats"].get(key) for key, field in METRICS.items()}}}, bvid)
                if item["status"] == "owner_mismatch" and (type(item.get("uid")) is not int or item["uid"] == UID):
                    raise GlobalError("invalid cached owner mismatch")

    def save(self, record):
        self.records[record["bvid"]] = record
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".metadata-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "uid": UID, "records": self.records}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def enrich(self, uploads, transport, *, batch_size=20, offline=False, refresh=False):
        results, calls, stopped = [], 0, False
        for upload in uploads:
            bvid = upload["bvid"]
            old = self.records.get(bvid)
            if old and old["status"] in {"verified", "owner_mismatch"} and not refresh:
                results.append(copy.deepcopy(old) | {"provenance": "cache"})
                continue
            base = {"bvid": bvid, "title": upload["title"], "status": "unverified",
                    "uid": None, "published": None}
            if offline or stopped or calls >= batch_size:
                reason = "offline" if offline else "block_or_deadline" if stopped else "batch_limit"
                results.append(base | {"reason": reason, "previous_failure": old.get("reason") if old else None})
                continue
            calls += 1
            try:
                record = detail(transport.request("GET", f"/x/web-interface/view?bvid={bvid}"), bvid)
            except (Blocked, GlobalError):
                record = base | {"reason": "block_or_deadline"}
                stopped = True
            except SyncError:
                record = base | {"reason": "lookup_failed"}
            record["fetched_at"] = datetime.now(timezone.utc).isoformat()
            if old and old["status"] == "verified" and record["status"] == "unverified":
                retained = old | {"last_refresh_failure": record["reason"], "last_attempt": record["fetched_at"]}
                self.save(retained)
                results.append(copy.deepcopy(retained) | {"provenance": "cache_after_failed_refresh"})
                continue
            self.save(record)  # Every completed lookup survives an interrupted batch.
            results.append(copy.deepcopy(record) | {"provenance": "live"})
        return results


def classify(title, description=""):
    text = normalize(title + " " + description)
    raw = (title + " " + description).casefold()
    tab = bool(re.search(r"(?<![a-z])tab(?:lature)?(?![a-z])|附[谱譜]|滚动[谱譜]|滾動[谱譜]", raw))
    performance = bool(re.search(r"(?<![a-z])cover(?![a-z])|翻[弹彈唱]|performance|演奏", raw))
    scrolling = bool(re.search(r"scrolling\s+tab|synchroni[sz]ed\s+tab|滚动[谱譜]|滾動[谱譜]|スクロール", raw))
    negated = bool(re.search(r"(?:no|without)\s+(?:scrolling\s+)?tab|无[谱譜]|無[谱譜]", raw))
    if tab and ("附谱" in raw or "附譜" in raw) and not scrolling:
        return {"value": None, "confidence": "review", "evidence": "attached score alone does not establish scrolling TAB"}
    if negated and tab:
        return {"value": None, "confidence": "review", "evidence": "negated/unclear TAB wording"}
    if performance and scrolling:
        return {"value": "Cover + Tab", "confidence": "high", "evidence": "performance and explicit scrolling TAB wording"}
    if tab and not performance and re.search(r"贝斯|貝斯|bass|ベース|自扒", raw):
        return {"value": "Bass Tab", "confidence": "high", "evidence": "standalone bass TAB wording without performance clue"}
    if tab:
        return {"value": None, "confidence": "review", "evidence": "TAB/performance relationship unclear"}
    if re.search(r"钢琴|鋼琴|ピアノ|piano|原创|原創|original composition|arrangement|改编|編曲|rehearsal|排练|排練", text):
        return {"value": "Other", "confidence": "high", "evidence": "explicit piano/arrangement/original/rehearsal wording"}
    if performance:
        return {"value": "Cover", "confidence": "medium", "evidence": "explicit performance clue without TAB wording"}
    return {"value": None, "confidence": "review", "evidence": "insufficient content-type evidence"}


def versions(title):
    raw = normalize(title)
    result = set()
    for label, expression in {"live": r"\blive\b|现场|現場|ライブ", "studio": r"\bstudio\b|录音室|錄音室",
                              "rerecording": r"re\s*record(?:ing|ed)?|重录|重錄"}.items():
        if re.search(expression, raw):
            result.add(label)
    return sorted(result)


class ArchiveTracker(Tracker):
    def preflight(self):
        super().preflight()
        self.schema = self.transport.request("GET", f"/v1/data_sources/{segment(self.source_id)}")["properties"]
        for logical, key, kind in (("content_type", self.config.get("archive", {}).get("type_property", "Type"), "select"),
                                   ("notes", self.config.get("archive", {}).get("notes_property", "Notes"), "rich_text")):
            matches = [p for name, p in self.schema.items() if key in {name, p["id"]}]
            if len(matches) != 1 or matches[0]["type"] != kind:
                raise GlobalError("archive Type/Notes mapping mismatch")
            self.mapping[logical] = (matches[0]["id"], kind)
        if len({v[0] for v in self.mapping.values()}) != len(self.mapping):
            raise GlobalError("archive properties must be distinct")
        confirmed = {"song": "title", "artist": "rich_text", "publish_date": "date", "status": "select",
                     "accounts": "multi_select", "url": "url", "content_type": "select", "notes": "rich_text"}
        if any(self.mapping[key][1] != kind for key, kind in confirmed.items()):
            raise GlobalError("archive schema differs from confirmed property types")


class Snapshot:
    def __init__(self, document, source):
        if document.get("data_source_id") != source:
            raise GlobalError("Notion snapshot source mismatch")
        self.schema, self.rows = document["schema"], document["rows"]
        if not isinstance(self.rows, list):
            raise GlobalError("invalid snapshot rows")

    def request(self, method, path, body=None):
        if method == "GET":
            return {"properties": self.schema}
        start = int((body or {}).get("start_cursor", "0"))
        rows = self.rows[start:start + 100]
        more = start + len(rows) < len(self.rows)
        return {"results": rows, "has_more": more, "next_cursor": str(start + 100) if more else None}


def alias_song(song, title, aliases):
    song, title = song.replace("&", " and "), title.replace("&", " and ")
    for entry in aliases:
        if not isinstance(entry.get("evidence"), str) or not entry["evidence"].strip():
            raise GlobalError("alternate titles require explicit evidence")
        names = [name.replace("&", " and ") for name in [entry["song"], *entry["alternatives"]]]
        if compact(song) in {compact(s) for s in names}:
            for name in names:
                match = candidate(name, "", None, {"title": title, "bvid": "", "published": None})
                if match and match["evidence"]["song_exact"]:
                    return name, entry["evidence"]
    return song, None


def title_fields(video):
    title = video["title"]
    # Remove only category labels; meaningful song/version bracket contents survive.
    cleaned = re.sub(r"[【\[][^】\]]*(?:贝斯|貝斯|bass|cover|翻弹|翻彈|翻唱|钢琴|鋼琴|piano|TAB)[^】\]]*[】\]]", "", title, flags=re.I).strip()
    artist = re.search(r"(?:原曲|original artist)\s*[:：]\s*([^\]】)]+)", cleaned, re.I)
    song = re.search(r"[『「《](.+?)[』」》]", cleaned)
    song_value = song[1].strip() if song else None
    return {"song": {"value": song_value, "confidence": "medium" if song else "review",
                     "evidence": "quoted work title" if song else "cleaned title is a suggestion, not a verified work name",
                     "suggestion": cleaned or title},
            "artist": {"value": artist[1].strip() if artist else None,
                       "confidence": "medium" if artist else "review",
                       "evidence": "explicit original-artist annotation" if artist else "original artist not identified"}}


def preview(tracker, rows, videos, *, tolerance=0, aliases=()):
    if type(tolerance) is not int or not 0 <= tolerance <= 7:
        raise GlobalError("date tolerance must be between 0 and 7 days")
    identity, conflicts, existing, decisions = defaultdict(list), [], [], []
    videos = copy.deepcopy(videos)
    for video in videos:
        video["classification"] = classify(video.get("title", ""), video.get("description", ""))
    by_bv = {v["bvid"]: v for v in videos}
    if len(by_bv) != len(videos):
        raise GlobalError("duplicate source BV IDs")
    for row in rows:
        url = tracker.value(row, "url")
        if url:
            try:
                bvid = parse_bvid(url)
                identity[bvid].append(row["id"])
                existing.append({"page_id": row["id"], "bvid": bvid, "state": "existing_url",
                                 "metadata_status": by_bv.get(bvid, {}).get("status", "not_in_source")})
            except SyncError:
                existing.append({"page_id": row["id"], "state": "existing_non_bilibili_or_invalid_url"})
    for bvid, pages in identity.items():
        if len(pages) > 1:
            conflicts.append({"bvid": bvid, "page_ids": pages, "reason": "pre_existing_duplicate_BV"})
    for row in rows:
        if not tracker.eligible(row) or tracker.value(row, "url"):
            continue
        day = tracker.value(row, "publish_date")
        day = publication_day(day["start"]) if day else None
        candidates = []
        for video in videos:
            if video["status"] == "owner_mismatch" or video["bvid"] in identity:
                continue
            song, alias_evidence = alias_song(tracker.text(row, "song"), video["title"], aliases)
            item = candidate(song, tracker.text(row, "artist").replace("&", " and "), day,
                             video | {"title": video["title"].replace("&", " and ")})
            if not item:
                continue
            known = video["status"] == "verified"
            distance = item["evidence"]["date_distance_days"] if known else None
            kind = video["classification"]["value"]
            row_type = tracker.text(row, "content_type")
            row_version, video_version = versions(tracker.text(row, "song")), versions(video["title"])
            type_match = bool(kind and row_type == kind)
            version_conflict = row_version != video_version and bool(row_version or video_version)
            item.update(content_type=video["classification"], versions=video_version)
            item["title"] = video["title"]
            item["evidence"].update(alias_evidence=alias_evidence, type_match=type_match,
                                    version_conflict=version_conflict, date_tolerance_days=tolerance,
                                    date_distance_days=distance)
            safe = (known and item["evidence"]["song_exact"] and item["evidence"]["artist_in_title"]
                    and type_match and not version_conflict and distance is not None and distance <= tolerance)
            item["confidence"] = "high" if safe and distance == 0 else "medium" if safe else "review"
            item["score"] += 20 * type_match - 40 * version_conflict
            item["owner_verified"] = known
            candidates.append(item)
        candidates.sort(key=lambda c: (-c["score"], c["bvid"]))
        state = "unmatched"
        if candidates:
            top = candidates[0]
            state = ("ambiguous" if len(candidates) > 1 and top["score"] - candidates[1]["score"] < 10
                     else "unverified_metadata" if not top["owner_verified"] else
                     "suggested_update" if top["confidence"] in {"high", "medium"} else "review_required")
        decisions.append({"page_id": row["id"], "song": tracker.text(row, "song"), "state": state,
                          "fingerprint": tracker.fingerprint(row), "candidates": candidates})
    leaders = Counter(d["candidates"][0]["bvid"] for d in decisions if d["candidates"])
    for item in decisions:
        if item["candidates"] and leaders[item["candidates"][0]["bvid"]] > 1:
            item["state"] = "ambiguous"
    updates = [{"operation_id": "url:" + d["page_id"] + ":" + d["candidates"][0]["bvid"],
                "page_id": d["page_id"], "bvid": d["candidates"][0]["bvid"],
                "fingerprint": d["fingerprint"], "confidence": d["candidates"][0]["confidence"],
                "properties": {tracker.mapping["url"][0]: {"url": "https://www.bilibili.com/video/" + d["candidates"][0]["bvid"] + "/"}}}
               for d in decisions if d["state"] == "suggested_update"]
    reserved = set(identity) | {u["bvid"] for u in updates}
    possible = {c["bvid"] for d in decisions for c in d["candidates"]}
    creations, rejected = [], []
    for video in videos:
        if video["status"] == "owner_mismatch":
            rejected.append({"bvid": video["bvid"], "uid": video["uid"], "state": "confirmed_owner_mismatch"})
            continue
        if video["bvid"] in reserved:
            continue
        fields = title_fields(video)
        fields.update(content_type=video["classification"],
                      publish_date={"value": video.get("published") if video["status"] == "verified" else None,
                                    "confidence": "high" if video["status"] == "verified" else "review", "evidence": "verified pubdate in Asia/Shanghai"},
                      status={"value": "Published", "confidence": "high", "evidence": "public-upload import intent"},
                      accounts={"value": ["B站"], "confidence": "high", "evidence": "Bilibili source"},
                      url={"value": f"https://www.bilibili.com/video/{video['bvid']}/", "confidence": "high", "evidence": "canonical BV identity"},
                      notes={"value": "", "confidence": "high", "evidence": "empty by default"})
        props = {}
        types = {"song": "title", "artist": "rich_text", "content_type": "select", "publish_date": "date",
                 "status": "select", "accounts": "multi_select", "url": "url", "notes": "rich_text"}
        for key, kind in types.items():
            value = fields[key]["value"]
            if value is None:
                continue
            wrapped = ([{"text": {"content": value}}] if value else []) if kind in {"title", "rich_text"} else (
                {"name": value} if kind == "select" else [{"name": v} for v in value] if kind == "multi_select" else
                {"start": value} if kind == "date" else value)
            props[tracker.mapping[key][0]] = {kind: wrapped}
        if video["status"] == "verified":
            for name, key in (("Views", "views"), ("Likes", "likes"), ("Favorites", "favorites"), ("Coins", "coins")):
                prop = tracker.schema.get(name)
                if prop and prop["type"] == "number" and video.get("stats") is not None:
                    props[prop["id"]] = {"number": video["stats"][key]}
                    fields[key] = {"value": video["stats"][key], "confidence": "high", "evidence": "validated detail API stat"}
            audit = tracker.schema.get("Bilibili Title")
            if audit and audit["type"] == "rich_text":
                props[audit["id"]] = {"rich_text": [{"text": {"content": video["title"]}}]}
                fields["bilibili_title"] = {"value": video["title"], "confidence": "high", "evidence": "detail API title"}
        reasons = [key + " uncertain" for key in ("song", "artist", "content_type", "publish_date") if fields[key]["value"] is None]
        if video["status"] != "verified":
            reasons.append("metadata unverified")
        if video["bvid"] in possible:
            reasons.append("possible existing-row candidate: resolve before creating")
        creations.append({"operation_id": "create:" + video["bvid"], "bvid": video["bvid"],
                          "source_title": video["title"], "versions": versions(video["title"]),
                          "state": "pending_review" if reasons else "suggested_creation",
                          "review_reasons": reasons, "fields": fields,
                          "parent": {"type": "data_source_id", "data_source_id": tracker.source_id}, "properties": props})
    counts = {"source_videos": len(videos), "metadata_verified": sum(v["status"] == "verified" for v in videos),
              "metadata_unverified": sum(v["status"] == "unverified" for v in videos),
              "matched": len(({v for v in identity if v in by_bv}) | {u["bvid"] for u in updates}),
              "exact_existing": len({v for v in identity if v in by_bv}), "suggested_updates": len(updates),
              "suggested_creations": sum(c["state"] == "suggested_creation" for c in creations),
              "pending_creations": sum(c["state"] == "pending_review" for c in creations),
              "skipped": sum(bool(tracker.value(r, "url")) or not tracker.eligible(r) for r in rows),
              "errors": sum(v["status"] != "verified" or bool(v.get("last_refresh_failure")) for v in videos) + len(conflicts)}
    return {"version": "archive-preview-1", "preview_only": True, "uid": UID,
            "data_source_id": tracker.source_id, "counts": counts, "metadata": videos,
            "existing": existing, "conflicts": conflicts, "matches": decisions,
            "suggested_updates": updates, "creations": creations, "rejected": rejected}


def outputs(directory, report):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private(directory / "preview.json", report)
    def escape(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")
    lines = ["# Archive preview — read-only", "", "No operations have been approved or applied.", "",
             "Counts: " + ", ".join(f"{k}={v}" for k, v in report["counts"].items()), "",
             "| Category | Page / BV | Outcome | Proposal / evidence |", "| --- | --- | --- | --- |"]
    if report.get("synthetic"):
        lines[2:2] = ["**SYNTHETIC TEST DATA — not retrieved from the user's account or Notion.**", ""]
    table = []
    for item in report["existing"]:
        table.append(["existing", item.get("bvid", item["page_id"]), item["state"], item.get("metadata_status", "")])
    for item in report["conflicts"]:
        table.append(["conflict", item["bvid"], item["reason"], ", ".join(item["page_ids"])])
    for item in report["matches"]:
        table.append(["existing-row", item["page_id"], item["state"], item["song"]])
        for c in item["candidates"]:
            table.append(["candidate", c["bvid"], c["confidence"],
                          f"{c['title']}; date={c['published']}; type={c['content_type']['value']}; evidence=" + json.dumps(c["evidence"], ensure_ascii=False)])
    for item in report["suggested_updates"]:
        table.append(["URL addition", item["page_id"] + " → " + item["bvid"], item["confidence"],
                      "empty URL → https://www.bilibili.com/video/" + item["bvid"] + "/"])
    for item in report["creations"]:
        table.append(["new-row", item["bvid"], item["state"], item["source_title"] + "; " + "; ".join(item["review_reasons"])])
    for item in report["rejected"]:
        table.append(["rejected", item["bvid"], item["state"], "owner=" + str(item["uid"])])
    for item in report["metadata"]:
        if item["status"] == "unverified":
            table.append(["metadata", item["bvid"], "unverified", item.get("reason", "")])
    lines.extend("| " + " | ".join(escape(v) for v in row) + " |" for row in table)
    for item in report["creations"]:
        lines.extend(["", "## Proposed new row: " + item["bvid"], "", "State: " + item["state"], "",
                      "Source title: " + escape(item["source_title"]), "",
                      "| Field | Proposed value | Confidence | Evidence |", "| --- | --- | --- | --- |"])
        for key, field in item["fields"].items():
            value = "UNKNOWN — review required" if field["value"] is None else json.dumps(field["value"], ensure_ascii=False)
            lines.append("| " + " | ".join(escape(v) for v in (key, value, field["confidence"], field["evidence"])) + " |")
        if item["review_reasons"]:
            lines.extend(["", "Review: " + "; ".join(item["review_reasons"])])
    text = "\n".join(lines) + "\n"
    for name, data in (("preview.md", text),):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["category", "page_or_bvid", "outcome", "proposal_or_evidence"])
    for row in table:
        # Prevent spreadsheet formula execution on untrusted source titles.
        writer.writerow(["'" + str(v) if str(v).lstrip().startswith(("=", "+", "-", "@")) else str(v) for v in row])
    descriptor = os.open(directory / "preview.csv", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(buffer.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only historical archive reconstruction")
    parser.add_argument("stage", choices=["preview"])
    parser.add_argument("--uploads", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("archive.example.toml"))
    parser.add_argument("--cache", type=Path, default=Path(".archive-private/metadata.json"))
    parser.add_argument("--output", type=Path, required=True, help="new private output directory")
    parser.add_argument("--notion-snapshot", type=Path, help="local schema/rows JSON, instead of live Notion reads")
    parser.add_argument("--offline", action="store_true", help="cache only; no Bilibili requests")
    parser.add_argument("--refresh", action="store_true", help="recheck cached successes/mismatches")
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if not 1 <= args.batch_size <= 100:
            raise GlobalError("batch size must be 1..100")
        if args.output.exists():
            raise GlobalError("choose a new preview output directory")
        with args.config.open("rb") as handle:
            config = tomllib.load(handle)
        uploads = load_uploads(args.uploads)
        cache = MetadataCache(args.cache)
        deadline = time.monotonic() + 240
        videos = cache.enrich(uploads, Transport("api.bilibili.com", interval=1.0, deadline=deadline),
                              batch_size=args.batch_size, offline=args.offline, refresh=args.refresh)
        source = str(uuid.UUID(os.environ.get("NOTION_DATA_SOURCE_ID", SOURCE)))
        if args.notion_snapshot:
            document = json.loads(args.notion_snapshot.read_text(encoding="utf-8"))
            transport = Snapshot(document, source)
        else:
            token = os.environ.get("NOTION_TOKEN", "")
            if not token or token != token.strip() or any(c in token for c in "\r\n"):
                raise GlobalError("metadata cache saved; configure read-only Notion access or --notion-snapshot")
            transport = Transport("api.notion.com", token=token, interval=0.4, deadline=time.monotonic() + 240)
        tracker = ArchiveTracker(ReadOnly(transport, source), source, config)
        tracker.preflight()
        rows = tracker.rows()
        report = preview(tracker, rows, videos, tolerance=config.get("archive", {}).get("date_tolerance_days", 0),
                         aliases=config.get("aliases", []))
        report["notion_provenance"] = "local_snapshot" if args.notion_snapshot else "live_read_only"
        report["generated"] = datetime.now(timezone.utc).isoformat()
        outputs(args.output, report)
        write_private(args.output / "notion-snapshot.json", {"data_source_id": source, "schema": tracker.schema, "rows": rows})
        log.info("read-only preview saved; counts=%s", report["counts"])
        return 1 if report["counts"]["errors"] else 0
    except (SyncError, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        log.error("archive preview incomplete; check inputs/schema/network; retained cache can resume; no writes attempted")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
