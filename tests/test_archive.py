import copy
import json
import os
import tempfile
import unittest
import contextlib
import io
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from blbl_tracker.archive import (SOURCE, ArchiveTracker, MetadataCache, ReadOnly, Snapshot,
                                  classify, detail, main, outputs, preview)
from blbl_tracker.discovery import UID
from blbl_tracker.http import Blocked, GlobalError, SyncError

BV = "BV1xx411c7mD"
OTHER = "BV17x411w7KC"
THIRD = "BV1ab411c7mD"
CONFIG = {"discovery": {"song": "Song", "artist": "Artist", "publish_date": "Publish Date",
                         "status": "Status", "accounts": "Published Acc.", "url": "url"},
          "archive": {"type_property": "Type", "notes_property": "Notes"}}


def payload(bvid=BV, title="【贝斯cover】Song A - Artist", when="2026-10-16T00:30:00+08:00", **changes):
    data = {"bvid": bvid, "title": title, "desc": "", "owner": {"mid": UID},
            "pubdate": int(datetime.fromisoformat(when).timestamp()),
            "stat": {"view": 100, "like": 10, "favorite": 3, "coin": 2}}
    data.update(changes)
    return {"code": 0, "data": data}


def upload(bvid=BV):
    return {"bvid": bvid, "title": "Browser title", "uid": None, "published": None}


def notion_row(identifier="page1", song="Song A", kind="Cover", url=None):
    specs = {"song": ("title", [{"plain_text": song}]),
             "artist": ("rich_text", [{"plain_text": "Artist"}]),
             "publish_date": ("date", {"start": "2026-10-16"}),
             "status": ("select", {"name": "Published"}),
             "accounts": ("multi_select", [{"name": "B站"}, {"name": "YouTube"}]),
             "url": ("url", url), "content_type": ("select", {"name": kind}),
             "notes": ("rich_text", [{"plain_text": "Keep my notes"}])}
    return {"object": "page", "id": identifier, "parent": {"data_source_id": SOURCE},
            "properties": {key: {"id": key, "type": typ, typ: value} for key, (typ, value) in specs.items()}}


def snapshot(rows=None, optional=False):
    row = notion_row()
    names = dict(CONFIG["discovery"], content_type="Type", notes="Notes")
    schema = {names[key]: {"id": key, "type": value["type"]} for key, value in row["properties"].items()}
    schema["Countdown"] = {"id": "formula", "type": "formula"}
    if optional:
        schema["Views"] = {"id": "views", "type": "number"}
        schema["Bilibili Title"] = {"id": "audit", "type": "rich_text"}
    return {"data_source_id": SOURCE, "schema": schema, "rows": rows if rows is not None else [row]}


def tracker(document=None):
    document = snapshot() if document is None else document
    client = ArchiveTracker(ReadOnly(Snapshot(document, SOURCE), SOURCE), SOURCE, copy.deepcopy(CONFIG))
    client.preflight()
    return client


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "metadata.json"

    def test_china_pubdate_not_ctime_and_optional_stats(self):
        result = detail(payload(ctime=0), BV)
        self.assertEqual(result["published"], "2026-10-16")
        self.assertEqual(result["pubdate"], payload()["data"]["pubdate"])
        self.assertEqual(result["stats"]["views"], 100)
        broken = payload()
        del broken["data"]["pubdate"]
        broken["data"]["ctime"] = 0
        with self.assertRaises(SyncError):
            detail(broken, BV)
        broken = payload(stat={"view": 0})
        self.assertIsNone(detail(broken, BV)["stats"])

    def test_owner_mismatch_distinct_from_missing_owner(self):
        self.assertEqual(detail(payload(owner={"mid": 1}), BV)["status"], "owner_mismatch")
        with self.assertRaises(SyncError):
            detail(payload(owner={}), BV)

    def test_cache_resumes_successes_and_retries_failures(self):
        api = Mock()
        api.request.side_effect = [payload(), SyncError("failed")]
        first = MetadataCache(self.path).enrich([upload(), upload(OTHER)], api)
        self.assertEqual([v["status"] for v in first], ["verified", "unverified"])
        api.reset_mock()
        api.request.side_effect = [payload(OTHER)]
        second = MetadataCache(self.path).enrich([upload(), upload(OTHER)], api)
        api.request.assert_called_once_with("GET", f"/x/web-interface/view?bvid={OTHER}")
        self.assertEqual([v["status"] for v in second], ["verified", "verified"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_success_survives_interruption_and_batch_limit(self):
        api = Mock()
        api.request.side_effect = [payload(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            MetadataCache(self.path).enrich([upload(), upload(OTHER)], api)
        self.assertEqual(MetadataCache(self.path).records[BV]["status"], "verified")
        api.reset_mock()
        api.request.side_effect = [payload(OTHER)]
        result = MetadataCache(self.path).enrich([upload(), upload(OTHER), upload(THIRD)], api, batch_size=1)
        self.assertEqual(result[-1]["reason"], "batch_limit")
        self.assertEqual(api.request.call_count, 1)

    def test_http_412_and_application_block_preserve_prior_success(self):
        for failure in (Blocked("HTTP 412"), {"code": -352}):
            path = self.path.with_name(str(len(str(failure))) + ".json")
            api = Mock()
            api.request.side_effect = [payload(), failure]
            results = MetadataCache(path).enrich([upload(), upload(OTHER), upload(THIRD)], api)
            self.assertEqual([v["status"] for v in results], ["verified", "unverified", "unverified"])
            self.assertEqual(api.request.call_count, 2)

    def test_nonblock_app_error_is_individual_and_refresh_preserves_good_data(self):
        api = Mock()
        api.request.side_effect = [{"code": -404}, payload(OTHER)]
        result = MetadataCache(self.path).enrich([upload(), upload(OTHER)], api)
        self.assertEqual([v["status"] for v in result], ["unverified", "verified"])
        api.request.side_effect = [SyncError("failed")]
        result = MetadataCache(self.path).enrich([upload(OTHER)], api, refresh=True)
        self.assertEqual(result[0]["status"], "verified")
        self.assertEqual(result[0]["last_refresh_failure"], "lookup_failed")
        self.assertEqual(MetadataCache(self.path).records[OTHER]["status"], "verified")

    def test_offline_does_not_trust_export_uid_or_dates(self):
        api = Mock()
        result = MetadataCache(self.path).enrich([upload() | {"uid": UID, "published": "2020-01-01"}], api, offline=True)
        api.request.assert_not_called()
        self.assertIsNone(result[0]["published"])
        self.assertIsNone(result[0]["uid"])


class ArchiveTests(unittest.TestCase):
    def test_type_definitions_and_attached_score_ambiguity(self):
        cases = {"【贝斯cover】Song": "Cover", "【自扒/贝斯TAB】Song": "Bass Tab",
                 "Bass cover with synchronized scrolling TAB": "Cover + Tab",
                 "【贝斯cover/附谱】Song": None, "Piano rehearsal": "Other",
                 "Original composition": "Other", "Arrangement": "Other", "Unknown": None}
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(classify(title)["value"], expected)

    def test_type_date_and_versions_disambiguate_same_song(self):
        client = tracker()
        videos = [detail(payload(), BV), detail(payload(OTHER, title="【贝斯TAB】Song A - Artist"), OTHER)]
        report = preview(client, client.rows(), videos)
        self.assertEqual(report["matches"][0]["state"], "suggested_update")
        self.assertEqual(report["suggested_updates"][0]["bvid"], BV)
        videos[0] = detail(payload(title="【贝斯cover】Song A - Artist Live"), BV)
        report = preview(client, client.rows(), videos)
        self.assertFalse(report["suggested_updates"])
        self.assertEqual(len(report["creations"]), 2)
        self.assertTrue(all(c["state"] == "pending_review" for c in report["creations"]))

    def test_exact_date_default_and_explicit_tolerance(self):
        client = tracker()
        video = detail(payload(when="2026-10-15T12:00:00+08:00"), BV)
        self.assertFalse(preview(client, client.rows(), [video])["suggested_updates"])
        report = preview(client, client.rows(), [video], tolerance=1)
        self.assertEqual(report["suggested_updates"][0]["confidence"], "medium")

    def test_ampersands_kana_and_evidenced_alternates(self):
        client = tracker(snapshot([notion_row(song="A & B")]))
        video = detail(payload(title="【贝斯cover】A and B - Artist"), BV)
        self.assertTrue(preview(client, client.rows(), [video])["suggested_updates"])
        client = tracker(snapshot([notion_row(song="ヨルニカケル")]))
        video = detail(payload(title="【贝斯cover】夜に駆ける - Artist"), BV)
        self.assertFalse(preview(client, client.rows(), [video])["suggested_updates"])
        aliases = [{"song": "ヨルニカケル", "alternatives": ["夜に駆ける"], "evidence": "user confirmation"}]
        self.assertTrue(preview(client, client.rows(), [video], aliases=aliases)["suggested_updates"])
        aliases[0]["evidence"] = ""
        with self.assertRaises(GlobalError):
            preview(client, client.rows(), [video], aliases=aliases)
        client = tracker(snapshot([notion_row(song="ガラス")]))
        video = detail(payload(title="【贝斯cover】カ\u3099ラス - Artist"), BV)
        self.assertTrue(preview(client, client.rows(), [video])["suggested_updates"])

    def test_near_ties_and_one_to_one_conflicts(self):
        client = tracker()
        videos = [detail(payload(), BV), detail(payload(OTHER), OTHER)]
        report = preview(client, client.rows(), videos)
        self.assertEqual(report["matches"][0]["state"], "ambiguous")
        self.assertFalse(report["suggested_updates"])
        client = tracker(snapshot([notion_row(), notion_row("page2")]))
        report = preview(client, client.rows(), videos[:1])
        self.assertTrue(all(m["state"] == "ambiguous" for m in report["matches"]))

    def test_identity_across_all_pages_preserves_rows_and_conflicts(self):
        rows = [notion_row(str(i), url=f"https://www.bilibili.com/video/{BV}/" if i in (0, 101) else "https://other.test") for i in range(102)]
        rows[101]["properties"]["status"]["select"] = {"name": "Draft"}
        document = snapshot(rows)
        before = copy.deepcopy(document)
        client = tracker(document)
        report = preview(client, client.rows(), [detail(payload(), BV)])
        self.assertEqual(len(client.rows()), 102)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertFalse(report["creations"])
        self.assertFalse(report["suggested_updates"])
        self.assertEqual(document, before)

    def test_historical_and_recent_creation_proposals_and_optional_existing_fields(self):
        client = tracker(snapshot([], optional=True))
        videos = [detail(payload(title="【贝斯cover】『Old Song』 (原曲: Artist)", when="2025-01-02T00:30:00+08:00"), BV),
                  detail(payload(OTHER, title="Piano rehearsal", when="2026-09-02T12:00:00+08:00"), OTHER)]
        report = preview(client, [], videos)
        self.assertEqual(len(report["creations"]), 2)
        old, recent = report["creations"]
        self.assertEqual(old["fields"]["song"]["value"], "Old Song")
        self.assertEqual(old["fields"]["artist"]["value"], "Artist")
        self.assertEqual(old["properties"]["publish_date"], {"date": {"start": "2025-01-02"}})
        self.assertEqual(old["properties"]["status"], {"select": {"name": "Published"}})
        self.assertEqual(old["properties"]["accounts"], {"multi_select": [{"name": "B站"}]})
        self.assertEqual(old["properties"]["notes"], {"rich_text": []})
        self.assertEqual(old["properties"]["views"], {"number": 100})
        self.assertIn("audit", old["properties"])
        self.assertNotIn("formula", old["properties"])
        self.assertEqual(recent["fields"]["content_type"]["value"], "Other")
        self.assertIsNone(recent["fields"]["artist"]["value"])
        self.assertEqual(report, preview(client, [], videos))
        client = tracker(snapshot([notion_row(url=f"https://www.bilibili.com/video/{BV}/")]))
        self.assertFalse(preview(client, client.rows(), videos[:1])["creations"])

    def test_unknown_dates_and_rejected_owner_are_separate(self):
        client = tracker()
        videos = [upload() | {"status": "unverified", "reason": "lookup_failed"},
                  detail(payload(OTHER, owner={"mid": 1}), OTHER)]
        report = preview(client, client.rows(), videos)
        self.assertEqual(report["rejected"][0]["state"], "confirmed_owner_mismatch")
        self.assertEqual(len(report["creations"]), 1)
        self.assertIsNone(report["creations"][0]["fields"]["publish_date"]["value"])
        self.assertNotIn("publish_date", report["creations"][0]["properties"])

    def test_read_only_guard_and_wrong_schema(self):
        api = Mock()
        guard = ReadOnly(api, SOURCE)
        for method, path in (("POST", "/v1/pages"), ("PATCH", "/v1/pages/id"), ("DELETE", "/v1/pages/id")):
            with self.assertRaises(GlobalError):
                guard.request(method, path)
        api.request.assert_not_called()
        doc = snapshot()
        doc["schema"]["Status"]["type"] = "status"
        with self.assertRaises(GlobalError):
            tracker(doc)

    def test_apply_stage_is_not_available(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(["apply", "--uploads", "unused.json", "--output", "unused"])
        self.assertEqual(result.exception.code, 2)

    def test_report_escapes_titles_and_never_emits_formula_fields(self):
        client = tracker(snapshot([]))
        metadata = detail(payload(title="=HYPERLINK(<script>)"), BV)
        report = preview(client, [], [metadata])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report"
            outputs(path, report)
            text = (path / 'preview.md').read_text()
            self.assertNotIn('<script>', text)
            self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('formula', report['creations'][0]['properties'])

    def test_cli_offline_preview_produces_json_markdown_csv_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads, snap = root / "uploads.json", root / "snapshot.json"
            uploads.write_text(json.dumps([upload()]))
            snap.write_text(json.dumps(snapshot()))
            output = root / "preview"
            with patch.dict(os.environ, {"NOTION_DATA_SOURCE_ID": SOURCE}), patch('blbl_tracker.archive.Transport') as api:
                result = main(["preview", "--uploads", str(uploads), "--notion-snapshot", str(snap),
                               "--cache", str(root / "cache.json"), "--config", "archive.example.toml",
                               "--output", str(output), "--offline"])
                api.return_value.request.assert_not_called()
            self.assertEqual(result, 1)
            for filename in ("preview.json", "preview.md", "preview.csv", "notion-snapshot.json"):
                self.assertTrue((output / filename).exists())
                self.assertEqual((output / filename).stat().st_mode & 0o777, 0o600)
            self.assertTrue(json.loads((output / "preview.json").read_text())["preview_only"])
            self.assertIn("No operations have been approved", (output / "preview.md").read_text())


if __name__ == "__main__":
    unittest.main()
