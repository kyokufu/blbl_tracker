"""Shared dry-run/apply orchestration with per-video failure isolation."""

import logging
from collections import Counter

from .bilibili import METRICS, parse_bvid
from .http import Blocked, GlobalError, SyncError

log = logging.getLogger(__name__)


def synchronize(notion, bilibili, *, apply=False):
    notion.preflight()
    rows = notion.rows()  # All discovery must succeed before writing anything.
    counts = Counter(scanned=len(rows), eligible=0, updated=0, unchanged=0, skipped=0, failed=0, checked=0)
    cache, blocked = {}, False
    for index, row in enumerate(rows, 1):
        try:
            if not notion.eligible(row) or not notion.source(row).strip():
                counts["skipped"] += 1
                continue
            bvid = parse_bvid(notion.source(row))
            counts["eligible"] += 1
            if bvid not in cache:
                if blocked:
                    raise SyncError("Bilibili blocked; remaining fetches deferred")
                try:
                    cache[bvid] = bilibili.fetch(bvid)
                except GlobalError:
                    raise
                except SyncError as error:
                    cache[bvid] = error
                    if isinstance(error, Blocked):
                        blocked = True
            if isinstance(cache[bvid], Exception):
                raise cache[bvid]
            stats, timestamp = cache[bvid]
            if apply:
                current = notion.reread(row)
                if not notion.eligible(current) or parse_bvid(notion.source(current)) != bvid:
                    raise SyncError("row eligibility/source changed; update skipped")
            else:
                current = row
            unchanged = all(notion.value(current, key) == stats[key] for key in METRICS)
            if apply:
                notion.update(current, stats, timestamp)
                counts["updated"] += 1
            counts["checked"] += 1
            counts["unchanged"] += int(unchanged)
            log.info("row=%s valid statistics; counts=%s mode=%s", index,
                     "unchanged" if unchanged else "changed", "apply" if apply else "dry-run")
            log.info("row=%s views=%s likes=%s favorites=%s coins=%s retrieved=%s",
                     index, stats["views"], stats["likes"], stats["favorites"], stats["coins"], timestamp)
        except GlobalError:
            log.error("aborted summary %s", " ".join(f"{key}={value}" for key, value in counts.items()))
            raise
        except (SyncError, ValueError, KeyError, TypeError, AttributeError) as error:
            counts["failed"] += 1
            # Only our controlled errors carry safe messages; never log raw data.
            reason = str(error) if isinstance(error, SyncError) else "invalid row data"
            log.warning("row=%s failed: %s", index, reason)
    log.info("summary %s", " ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["failed"] else 0
