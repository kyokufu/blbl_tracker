# Bilibili → Notion tracker

Python 3.12, no third-party dependencies. Refresh Views, Likes, Favorites, Coins,
and Last Synced on existing Notion pages, preserving schema, content, relations,
statuses, and views. No paid hosting, historical analytics, or dashboards.

**Status:** implemented and tested offline. Real schema/API compatibility,
GitHub runner access, and account billing safeguards remain unverified. No live
Notion changes have been made. Review a successful dry run before enabling writes.

## Local setup

Run from the repository root with Python 3.12 or newer:

```sh
python -m unittest discover -s tests -v
cp config.example.toml config.local.toml
```

Nothing needs installing with pip. Tests use synthetic responses and no network.
Use the existing isolated checkout; no worktree is necessary.

Edit the local TOML with existing property names or IDs. Example names are
placeholders. Use one URL or rich-text source property containing a Bilibili
HTTPS video URL or bare BV ID. Four distinct Number targets and one Date target
are required; formula/rollup targets are rejected. The app does not add fields.

Published mappings support:

| `type` | TOML `value` | Eligibility |
| --- | --- | --- |
| `checkbox` | `true` | Checked |
| `status` / `select` | Exact published option name | Option matches |
| `date` | Omit value | Nonempty date, including future dates |

Select the mapping that matches your tracker; a URL alone never means published.
The entire underlying data source is paginated, not just the rows visible in a
linked view. Archived, unpublished, and blank-source rows are skipped. Invalid
nonblank sources fail individually. Short links and AV IDs are not supported.

Create a Notion internal connection with read/update content access; share only
the intended tracker. Insert-content permission is unnecessary. Configure the
actual **data source UUID**, not a database/view ID. A database can have multiple
sources: discover them using the database retrieval API and choose explicitly.
Provide sanitized property names/types for review if the mapping is uncertain.

Enter credentials securely, e.g. in Bash without token values in shell history:

```sh
read -r -s -p 'Notion token: ' NOTION_TOKEN
printf '\n'
export NOTION_TOKEN
read -r -p 'Notion data source UUID: ' NOTION_DATA_SOURCE_ID
export NOTION_DATA_SOURCE_ID
python -m blbl_tracker --config config.local.toml --check-schema
python -m blbl_tracker --config config.local.toml --dry-run
```

The default is dry-run even without `--dry-run`. Schema checking validates only
the configured mapping. Dry-run reads rows and retrieves counts, but issues no
PATCH requests. Review your mapping, candidate counts/timestamps, and expected
eligible/skipped totals. Logs use row ordinals, meaningful only within that run,
instead of private titles/URLs. Zero eligible rows does not verify video retrieval.

**Only after reviewing a successful dry run and choosing to write:**

```sh
python -m blbl_tracker --config config.local.toml --apply
```

Apply uses the same validation, then re-reads each row's source/eligibility
before writing. Avoid overlapping runs and edits: a narrow race remains between
re-read and PATCH; there is no distributed lock or cross-row transaction.

Last Synced records the UTC retrieval time of valid counts, saved only after a
successful update. Unchanged counts still cause a timestamp write. Failed
fetches never erase counts or advance the timestamp. All four counts must be
valid; explicit zero and legitimate decreases are accepted. Retrieval time is
not a guarantee of upstream freshness. Duplicate BV IDs are fetched once/run.

## Private GitHub Actions and zero-overage safeguards

Workflows use standard `ubuntu-latest`, five-minute job timeouts, SHA-pinned
official actions, read-only repository access, no dependency installation,
caches, artifacts, or paid services. Sync has a three-hour UTC cron
(`17 */3 * * *`) and manual trigger. Application runtime is bounded to 240
seconds; slow runner setup can leave less time before the job timeout.

**Billing is an account setting, not something this code can enforce.** Verify
your current free allowance and an enforced account-level Actions spending
hard stop before enabling any jobs. Alerts alone do not prevent charges.
GitHub documents blocking usage after quota exhaustion when no valid payment
method exists; with a payment method, verify the applicable enforced budget.
If zero-overage behavior cannot be established, keep Actions disabled and use
local execution. Free service terms can change and quota exhaustion can stop
automation. No paid fallback is required.

Private-repository minutes are shared with all your other jobs. At 248 runs in
a 31-day month, five minutes/job is approximately 1,240 minutes before tests,
manual runs, or other repos. Measure actual usage. Schedules can be delayed or
dropped and only run from the default branch.

Configure repository **secrets**:

- `NOTION_TOKEN`: internal connection token.
- `NOTION_DATA_SOURCE_ID`: selected source UUID.
- `TRACKER_CONFIG`: complete mapped TOML contents.

The mapping is written to a restricted temporary file and removed on normal
completion. Never put secrets in workflow inputs or commit real config. Local
`.env` files are ignored but are not automatically loaded. Sync secrets are
never used on pull requests. Dispatch only from trusted branches.

Set repository **variables** in order:

1. `COST_SAFEGUARDS_CONFIRMED=true` after verifying actual billing enforcement.
   This only records your confirmation; it does not configure or prove billing.
2. Run **Offline tests**, then **Sync statistics** manually with `apply`
   unchecked. Review the live dry run and runner connectivity.
3. After review, set `LIVE_SYNC_APPROVED=true`. Manual runs with `apply` checked
   can then write. First use an agreed test row/narrow source scope; the app
   processes every eligible row in the selected source.
4. After manual apply verification, set `SCHEDULE_ENABLED=true` for scheduled
   writes. Verify at least two actual scheduled runs.

Without these confirmations, jobs are skipped before allocating a runner.
Remove the schedule flag to stop scheduled writes, the live flag to stop all
workflow writes, or the cost flag to stop all jobs in these workflows. Tests
are manual-only to avoid unintended usage before billing is checked. Workflows
have not been pushed or remotely executed during implementation.

## Failures, security, and fallback

Exit codes: **0** healthy completion (including normal skips), **1** per-row or
partial failure, **2** configuration/discovery/global failure. Summary fields:
`scanned`, `eligible`, `checked` (valid observations successfully handled),
`updated` (successful PATCHes; zero in dry-run), `unchanged` (same counts,
possibly timestamp updated), `skipped`, `failed`. Already completed writes are
not rolled back if a later row fails.

Transport uses verified TLS, fixed hosts, no redirects, pacing, 15-second
request timeouts, and at most three transient-failure attempts. Notion 429
honors `Retry-After`; a wait exceeding the deadline aborts rather than retrying
early. Bilibili HTTP 403/412/429 stops further fetches for the run. Application
errors fail validation; unknown application-level anti-bot codes still require
live diagnosis. Previously fetched valid observations can still be used for
duplicate rows after a block. No cookies, login scraping, challenge bypass,
proxy rotation, or paid infrastructure is included.

If GitHub requests are restricted, disable scheduling and use the same local
dry-run/apply commands. If local requests are also blocked, preserve data and
stop. For schema/authentication errors check mappings, sharing, and token;
rotate/revoke the token in Notion and update the local environment/Actions
secret, then repeat dry-run. Never share authenticated response bodies or tokens.

A timed-out PATCH might already have been accepted by Notion. Retries send
the same absolute values and timestamp, but exactly-once writes cannot be
guaranteed. No separate timestamp update is sent after a failed count update.

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for API research. Notion
uses the explicit `2025-09-03` contract. The candidate Bilibili endpoint remains
subject to live verification and is not a guaranteed API service.

## One-time URL discovery and reconciliation

This is a **separate command**, never called by the recurring statistics sync
or its workflow. It targets only Bilibili UID **19997041**. It does not update
statistics, Last Synced, schema, or existing URLs.

```sh
cp discovery.example.toml discovery.local.toml
# Check the six property mappings against your actual tracker.
python -m blbl_tracker.discovery --config discovery.local.toml --dry-run
```

Use the same secure `NOTION_TOKEN` and `NOTION_DATA_SOURCE_ID` environment
variables as above. Discovery defaults to dry-run. The configuration maps Song,
Artist, Publish Date, Status, Published Acc., and the existing lowercase `url`
property by name or ID. Song supports title/rich text; Artist supports title,
rich text, select, or multi-select; date must be Date; Status supports status or
select; accounts supports multi-select, select, or rich text; `url` must be URL.
Other types fail schema validation rather than being guessed or modified.

Only rows with **Status exactly `Published`** and **Published Acc. containing
`B站`** are considered. For select/multi-select this means an exact option named
`B站`; for rich text it means a substring. Archived/trash rows are excluded.
Rows with any existing URL are reported as `existing_url` and never assigned.

The command tries the unsigned public account listing
`/x/space/arc/search?mid=19997041&pn=…&ps=30&order=pubdate`, paginates it, and
checks every video's `/x/web-interface/view` response for matching BV ID and
`owner.mid == 19997041`. It retrieves the verified title and publication time
from that detail response. These public-facing endpoints may change or be
blocked; no login, WBI/challenge workaround, or scraping fallback is included.
The current cloud proxy blocks the live listing, so its current live response
contract has not been verified here. A block is reported, never treated as an
empty account or a successful discovery.

To retrieve/export uploads locally without connecting to Notion:

```sh
python -m blbl_tracker.discovery --export-uploads uploads-account.json
```

If automatic listing is blocked, supply your own UTF-8 JSON or CSV export:

```json
[
  {"uid": 19997041, "bvid": "BV1xx411c7mD", "title": "Example song / Artist (Cover)", "published": "2026-10-16"}
]
```

This is an **illustrative format**, not an actual upload or ownership claim.
Browser-exported JSON records containing only `bvid`, `title`, and `url` are
also accepted, for example:

```json
[
  {"bvid":"BV1xx411c7mD","title":"Example song / Artist (Cover)","url":"https://www.bilibili.com/video/BV1xx411c7mD"}
]
```

Missing UID/publication date values are filled from the per-video detail API,
using `owner.mid` and `pubdate`. The API owner must be UID 19997041 even if the
export claims that UID or marks itself verified. The API title/date are used
for matching. Supplied URLs must be valid Bilibili video URLs matching the BV
ID; they are never fetched directly. Supplied invalid IDs/titles, wrong UIDs,
invalid dates, or mismatched URLs still fail input validation.

JSON can also be `{"uid":19997041,"uploads":[...]}`. CSV uses headers
`uid,bvid,title,published` with quoted fields for commas/newlines. `mid` may
replace `uid`; `created` may replace `published` and contain Unix seconds.
UID and date columns may be omitted or blank in CSV as well.
Dates accept `YYYY-MM-DD` or timezone-aware ISO timestamps; timed values are
compared as calendar dates in Asia/Shanghai (UTC+8), the Bilibili publication
day. Date-only Notion values are used as written. Duplicate BV IDs or supplied
wrong UIDs are rejected. The list should contain the complete public uploads;
a partial export can hide competing candidates and is not independently proven
complete by this command.

```sh
python -m blbl_tracker.discovery --config discovery.local.toml \
  --uploads uploads-account.json --report discovery-report-local.json
```

An export's `uid`/`verified` fields are **not** proof of ownership. Each video is
still checked live. If blocked, an offline report can contain provisional
`unverified` suggestions, but those cannot be applied. A definite owner mismatch
is excluded. Missing API publication dates, malformed detail responses, unavailable
videos, and ordinary lookup failures are recorded per video; other videos are
still processed. Requests keep one-second pacing, 15-second timeouts, and at
most three attempts for transient errors. An explicit block/global network
failure stops further lookups rather than hammering the endpoint. Provisional
records without dates cannot receive date-match credit or be approved. After
access is restored, generate a fresh verified report for
review. Unverified or incomplete verification returns exit 1; discovery failure
or invalid configuration returns 2. A report with ambiguity can otherwise be a
successful read-only run; its state labels determine what can be approved.

Reports are UTF-8 JSON with upload titles, BV IDs, dates, per-row candidate
rankings, confidence/evidence, ownership status, and a row/schema fingerprint.
They contain private tracker labels/IDs: keep them local and never commit them.
Default report/export/approval filenames are ignored; custom filenames may not
be. Files are created owner-readable/writable only, and existing files are never
overwritten. Choose a new report filename for each review.

Matching applies Unicode NFKC/case folding, punctuation/spacing normalization,
HTML entity decoding, and common cover/翻唱/歌ってみた decorations. It preserves
Japanese, Chinese, English, and diacritics; it does **not** translate or
transliterate titles or infer artist aliases. All meaningful bracket contents
remain except recognized decoration words. Scores are heuristics, not
statistical probabilities:

- **High:** normalized song present, artist present, same publication day.
- **Medium:** normalized song present and same/adjacent publication day, with
  weaker artist/date evidence.
- **Low:** fuzzy title evidence or missing/distant date; review manually, not
  eligible for application.
- Near ties (score gap under 10) or one leading video shared by multiple
  candidate Notion rows are **ambiguous**, never eligible for application.

Every assignment, even high confidence, requires a separate explicit approval
file. After reviewing a successful report, create `approvals-local.json` with
only selected `matched` rows (substitute actual IDs from your report):

```json
[{"page_id":"REVIEWED_NOTION_PAGE_UUID","bvid":"REVIEWED_BV_ID"}]
```

Only when you have explicitly approved these choices:

```sh
python -m blbl_tracker.discovery --config discovery.local.toml \
  --report discovery-report-local.json --apply --approvals approvals-local.json
```

Apply rechecks live ownership and metadata, queries the current source, reruns
matching/ambiguity checks, rejects stale row/schema fingerprints, and validates
the entire approval list before writing. It re-reads each row immediately before
one PATCH containing **only the empty URL property**. Existing URLs, changed
eligibility, ambiguous/low/unverified matches, duplicate/conflicting approvals,
or changed metadata stop application. There are no PATCH retries: an ambiguous
timeout requires a re-read and fresh report. Previously completed assignments
cannot be rolled back if a later row fails.

Notion offers no assumed conditional-update/compare-and-swap API here. Do not
edit URLs or run another writer during apply: a narrow race between the final
read and PATCH cannot be eliminated. No Notion writes were performed during
implementation, and no approval file has been generated automatically.
