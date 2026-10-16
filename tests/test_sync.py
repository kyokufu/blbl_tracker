import copy
import unittest
from unittest.mock import Mock

from blbl_tracker.http import Blocked, GlobalError, SyncError
from blbl_tracker.notion import Notion
from blbl_tracker.sync import synchronize

BV = "BV1xx411c7mD"
SOURCE = "00000000-0000-0000-0000-000000000001"
STAMP = "2026-10-16T12:00:00+00:00"
STATS = {"views": 100, "likes": 10, "favorites": 5, "coins": 2}
CONFIG = {"properties": {key: key for key in ["source", *STATS, "last_synced"]},
          "published": {"property": "published", "type": "checkbox", "value": True}}


def schema():
    return {"properties": {key: {"id": f"id_{key}", "type": kind}
                           for key, kind in {"source": "url", **dict.fromkeys(STATS, "number"),
                                             "last_synced": "date", "published": "checkbox"}.items()}}


def row(identifier="row1", bvid=BV):
    properties = {}
    for key, prop in schema()["properties"].items():
        kind = prop["type"]
        value = (bvid if key == "source" else True if key == "published" else
                 None if key == "last_synced" else STATS[key])
        properties[key] = {**prop, kind: value}
    return {"id": identifier, "object": "page", "parent": {"data_source_id": SOURCE},
            "properties": properties, "archived": False}


class FakeTransport:
    def __init__(self, rows=None):
        self.schema = schema()
        self.pages = [{"results": rows if rows is not None else [row()], "has_more": False}]
        self.current = row()
        self.patches = []
        self.calls = []
        self.patch_error = None

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "PATCH":
            self.patches.append(copy.deepcopy(body))
            if self.patch_error:
                raise self.patch_error
            return {"object": "page", "id": path.rsplit("/", 1)[1]}
        if method == "POST":
            page = self.pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page
        if path.startswith("/v1/pages/"):
            current = copy.deepcopy(self.current)
            current["id"] = path.rsplit("/", 1)[1]
            return current
        return self.schema


class SyncTests(unittest.TestCase):
    def make(self, rows=None):
        transport = FakeTransport(rows)
        notion = Notion(transport, SOURCE, copy.deepcopy(CONFIG))
        bili = Mock()
        bili.fetch.return_value = (dict(STATS), STAMP)
        return transport, notion, bili

    def test_dry_run_is_read_only_and_deduplicates(self):
        transport, notion, bili = self.make([row(), row("row2")])
        self.assertEqual(synchronize(notion, bili), 0)
        bili.fetch.assert_called_once_with(BV)
        self.assertEqual(transport.patches, [])
        self.assertFalse(any(path.startswith("/v1/pages/") for _, path, _ in transport.calls))

    def test_unchanged_counts_still_update_timestamp_only(self):
        transport, notion, bili = self.make()
        self.assertEqual(synchronize(notion, bili, apply=True), 0)
        self.assertEqual(transport.patches, [{"properties": {"id_last_synced": {"date": {"start": STAMP}}}}])

    def test_changed_counts_only_patch_mapped_fields_and_allow_decrease(self):
        transport, notion, bili = self.make()
        bili.fetch.return_value = ({**STATS, "views": 0}, STAMP)
        self.assertEqual(synchronize(notion, bili, apply=True), 0)
        self.assertEqual(transport.patches[0]["properties"], {
            "id_views": {"number": 0}, "id_last_synced": {"date": {"start": STAMP}}})

    def test_pagination_complete_before_first_write(self):
        transport, notion, bili = self.make()
        transport.pages = [{"results": [row()], "has_more": True, "next_cursor": "next"},
                           {"results": [row("row2")], "has_more": False}]
        self.assertEqual(synchronize(notion, bili, apply=True), 0)
        posts = [body for method, _, body in transport.calls if method == "POST"]
        self.assertEqual(posts, [{"page_size": 100}, {"page_size": 100, "start_cursor": "next"}])
        self.assertEqual(len(transport.patches), 2)

    def test_pagination_failure_never_writes(self):
        transport, notion, bili = self.make()
        transport.pages = [{"results": [row()], "has_more": True, "next_cursor": "next"}, SyncError("failed")]
        with self.assertRaises(GlobalError):
            synchronize(notion, bili, apply=True)
        self.assertEqual(transport.patches, [])
        bili.fetch.assert_not_called()

    def test_duplicate_cursor_or_row_rejected(self):
        for second in ({"results": [], "has_more": True, "next_cursor": "next"},
                       {"results": [row()], "has_more": False}):
            transport, notion, bili = self.make()
            transport.pages = [{"results": [row()], "has_more": True, "next_cursor": "next"}, second]
            with self.assertRaises(GlobalError):
                synchronize(notion, bili, apply=True)
            self.assertEqual(transport.patches, [])

    def test_invalid_schema_prevents_fetch_and_write(self):
        for mutation in ("wrong_type", "duplicate_target", "missing", "bad_published"):
            transport, notion, bili = self.make()
            if mutation == "wrong_type":
                transport.schema["properties"]["views"]["type"] = "formula"
            elif mutation == "duplicate_target":
                notion.config["properties"]["views"] = "likes"
            elif mutation == "bad_published":
                notion.config["published"]["type"] = "formula"
            else:
                del transport.schema["properties"]["coins"]
            with self.subTest(mutation=mutation), self.assertRaises(GlobalError):
                synchronize(notion, bili, apply=True)
            self.assertFalse(transport.patches)
            bili.fetch.assert_not_called()

    def test_row_type_mismatch_aborts_complete_discovery(self):
        first, second = row(), row("row2")
        second["properties"]["published"]["checkbox"] = "true"
        transport, notion, bili = self.make([first, second])
        with self.assertRaises(GlobalError):
            synchronize(notion, bili, apply=True)
        self.assertFalse(transport.patches)

    def test_skips_unpublished_blank_and_archived(self):
        unpublished, blank, archived = row(), row("row2", ""), row("row3")
        unpublished["properties"]["published"]["checkbox"] = False
        archived["archived"] = True
        transport, notion, bili = self.make([unpublished, blank, archived])
        self.assertEqual(synchronize(notion, bili, apply=True), 0)
        bili.fetch.assert_not_called()
        self.assertFalse(transport.patches)

    def test_invalid_url_isolated(self):
        transport, notion, bili = self.make([row(bvid="secret invalid link"), row("row2")])
        with self.assertLogs("blbl_tracker.sync") as logs:
            self.assertEqual(synchronize(notion, bili, apply=True), 1)
        self.assertNotIn("secret invalid link", "\n".join(logs.output))
        self.assertEqual(len(transport.patches), 1)

    def test_failed_video_preserves_data_and_other_video_continues(self):
        transport, notion, bili = self.make([row(), row("row2", "BV17x411w7KC")])
        transport.current["properties"]["source"]["url"] = "BV17x411w7KC"
        bili.fetch.side_effect = [SyncError("unavailable video"), (STATS, STAMP)]
        self.assertEqual(synchronize(notion, bili, apply=True), 1)
        self.assertEqual(len(transport.patches), 1)

    def test_failed_fetch_is_cached(self):
        transport, notion, bili = self.make([row(), row("row2")])
        bili.fetch.side_effect = SyncError("missing counts")
        self.assertEqual(synchronize(notion, bili, apply=True), 1)
        bili.fetch.assert_called_once()
        self.assertFalse(transport.patches)

    def test_http_block_stops_further_fetches(self):
        transport, notion, bili = self.make([row(), row("row2", "BV17x411w7KC")])
        bili.fetch.side_effect = Blocked("blocked")
        self.assertEqual(synchronize(notion, bili, apply=True), 1)
        bili.fetch.assert_called_once()
        self.assertFalse(transport.patches)

    def test_reread_changed_source_or_eligibility_or_parent_never_writes(self):
        for change in ("source", "published", "parent", "archived"):
            transport, notion, bili = self.make()
            if change == "source":
                transport.current["properties"]["source"]["url"] = "BV17x411w7KC"
            elif change == "published":
                transport.current["properties"]["published"]["checkbox"] = False
            elif change == "parent":
                transport.current["parent"]["data_source_id"] = "other"
            else:
                transport.current["archived"] = True
            with self.subTest(change=change):
                self.assertEqual(synchronize(notion, bili, apply=True), 1)
                self.assertFalse(transport.patches)

    def test_patch_failure_does_not_send_separate_timestamp(self):
        transport, notion, bili = self.make()
        transport.patch_error = SyncError("update failed")
        self.assertEqual(synchronize(notion, bili, apply=True), 1)
        self.assertEqual(len(transport.patches), 1)

    def test_authorization_failure_aborts_other_writes(self):
        transport, notion, bili = self.make([row(), row("row2")])
        transport.patch_error = GlobalError("Notion authorization failed")
        with self.assertRaises(GlobalError):
            synchronize(notion, bili, apply=True)
        self.assertEqual(len(transport.patches), 1)

    def test_property_ids_and_rich_text_supported(self):
        transport, notion, bili = self.make()
        notion.config["properties"]["source"] = "id_source"
        transport.schema["properties"]["source"]["type"] = "rich_text"
        prop = transport.pages[0]["results"][0]["properties"]["source"]
        prop["type"] = "rich_text"
        prop["rich_text"] = [{"plain_text": BV}]
        self.assertEqual(synchronize(notion, bili), 0)
        bili.fetch.assert_called_once_with(BV)

    def test_status_select_date_eligibility(self):
        for kind, value, expected in (("status", {"name": "Done"}, "Done"),
                                     ("select", {"name": "Done"}, "Done"),
                                     ("date", {"start": "2026-10-16"}, None)):
            transport, notion, bili = self.make()
            notion.config["published"].update(type=kind, value=expected)
            transport.schema["properties"]["published"]["type"] = kind
            prop = transport.pages[0]["results"][0]["properties"]["published"]
            prop.update(type=kind)
            prop[kind] = value
            with self.subTest(kind=kind):
                self.assertEqual(synchronize(notion, bili), 0)
                bili.fetch.assert_called_once()
