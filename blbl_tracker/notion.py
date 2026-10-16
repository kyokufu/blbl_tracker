"""Existing-schema adapter; never creates or alters schema/pages."""

from urllib.parse import quote

from .bilibili import METRICS
from .http import GlobalError, SyncError


def segment(value):
    return quote(value, safe="")


class Notion:
    def __init__(self, transport, source_id, config):
        self.transport, self.source_id, self.config = transport, source_id, config
        self.mapping = {}

    def preflight(self):
        try:
            schema = self.transport.request("GET", f"/v1/data_sources/{segment(self.source_id)}")
            properties = schema["properties"]
            wanted = dict(self.config["properties"])
            published = self.config["published"]
            kind = published["type"]
            if kind not in {"checkbox", "status", "select", "date"}:
                raise ValueError
            if kind == "checkbox" and published.get("value") is not True:
                raise ValueError
            if kind in {"status", "select"} and not isinstance(published.get("value"), str):
                raise ValueError
            if kind in {"status", "select"} and not published["value"]:
                raise ValueError
            wanted["published"] = published["property"]
            if set(wanted) != {"source", "last_synced", "published", *METRICS}:
                raise ValueError
            for logical, identifier in wanted.items():
                if not isinstance(identifier, str) or not identifier:
                    raise ValueError
                matches = [p for name, p in properties.items()
                           if identifier in {name, p["id"]}]
                if len(matches) != 1:
                    raise ValueError
                prop = matches[0]
                expected = ({"url", "rich_text"} if logical == "source" else
                            {kind} if logical == "published" else
                            {"date"} if logical == "last_synced" else {"number"})
                if prop["type"] not in expected:
                    raise ValueError
                self.mapping[logical] = (prop["id"], prop["type"])
            if len({p[0] for p in self.mapping.values()}) != len(self.mapping):
                raise ValueError
        except GlobalError:
            raise
        except (SyncError, KeyError, TypeError, ValueError, AttributeError):
            raise GlobalError("schema/config mismatch; verify mapped property IDs and types") from None

    def rows(self):
        result, cursor, seen, ids = [], None, set(), set()
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            try:
                page = self.transport.request("POST", f"/v1/data_sources/{segment(self.source_id)}/query", body)
                if not isinstance(page["results"], list) or type(page["has_more"]) is not bool:
                    raise ValueError
                for row in page["results"]:
                    if row.get("object") != "page" or not isinstance(row.get("id"), str):
                        raise ValueError
                    if row["id"] in ids:
                        raise ValueError
                    ids.add(row["id"])
                    # Validate all row property structures before any writes.
                    for logical in self.mapping:
                        self.value(row, logical)
                    result.append(row)
                if not page["has_more"]:
                    return result
                cursor = page["next_cursor"]
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError
                seen.add(cursor)
            except GlobalError:
                raise
            except (SyncError, KeyError, TypeError, ValueError, AttributeError):
                raise GlobalError("Notion discovery failed; no writes performed") from None

    def value(self, row, logical):
        identifier, kind = self.mapping[logical]
        matches = [p for p in row["properties"].values() if p.get("id") == identifier]
        if len(matches) != 1 or matches[0].get("type") != kind:
            raise ValueError("row schema mismatch")
        value = matches[0][kind]
        valid = {
            "number": lambda: value is None or (type(value) in {int, float}),
            "url": lambda: value is None or isinstance(value, str),
            "rich_text": lambda: isinstance(value, list) and all(
                isinstance(item, dict) and isinstance(item.get("plain_text"), str) for item in value),
            "title": lambda: isinstance(value, list) and all(
                isinstance(item, dict) and isinstance(item.get("plain_text"), str) for item in value),
            "multi_select": lambda: isinstance(value, list) and all(
                isinstance(item, dict) and isinstance(item.get("name"), str) for item in value),
            "checkbox": lambda: type(value) is bool,
            "date": lambda: value is None or (isinstance(value, dict) and isinstance(value.get("start"), str)),
            "status": lambda: value is None or (isinstance(value, dict) and isinstance(value.get("name"), str)),
            "select": lambda: value is None or (isinstance(value, dict) and isinstance(value.get("name"), str)),
        }
        if not valid[kind]():
            raise ValueError("row property value invalid")
        return value

    def source(self, row):
        value = self.value(row, "source")
        if self.mapping["source"][1] == "rich_text":
            return "".join(item["plain_text"] for item in value)
        return value or ""

    def eligible(self, row):
        if row.get("archived") or row.get("in_trash"):
            return False
        value = self.value(row, "published")
        kind = self.mapping["published"][1]
        if kind == "date":
            return isinstance(value, dict) and bool(value.get("start"))
        if kind in {"select", "status"}:
            return isinstance(value, dict) and value.get("name") == self.config["published"]["value"]
        return value is True

    def reread(self, row):
        current = self.transport.request("GET", f"/v1/pages/{segment(row['id'])}")
        if current.get("parent", {}).get("data_source_id") != self.source_id:
            raise SyncError("row moved to another source")
        return current

    def update(self, row, stats, timestamp):
        properties = {self.mapping[key][0]: {"number": count}
                      for key, count in stats.items() if self.value(row, key) != count}
        properties[self.mapping["last_synced"][0]] = {"date": {"start": timestamp}}
        response = self.transport.request("PATCH", f"/v1/pages/{segment(row['id'])}", {"properties": properties})
        if response.get("object") != "page" or response.get("id") != row["id"]:
            raise SyncError("Notion update acknowledgement invalid")
