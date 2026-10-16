# Bilibili → Notion implementation plan

Planning review: 2026-10-16. Input: uploaded `PROJECT_SPEC.md`, read in full. The user subsequently authorized MVP implementation: configurable schema, fewer than 100 initial videos, Views/Likes/Favorites/Coins/Last Synced, private repository, three-hour schedule, five-minute timeout, zero-overage safeguards, and local fallback. Live Notion writes require review of a successful dry run first. Schema changes and deployment have not been performed.

Implementation status: dependency-free CLI, offline tests, gated workflows, and local documentation are implemented. Actual schema inspection, live API/runner verification, account billing verification, dry-run review, and remote workflow execution remain pending. These gates block rollout, not independent offline implementation. The current user decisions supersede the historical proposals below; `Last Synced` is the chosen timestamp property label, and a single configurable URL/BV source field is sufficient.

## Recommendation and scope

Use one synchronous Python 3.12 CLI, one existing Notion data source, and GitHub Actions. Runtime and test dependencies: **none outside the standard library** (`urllib.request`, `json`, `tomllib`, `argparse`, `logging`, `datetime`, `unittest`, `unittest.mock`). No SDK, web service, database, Docker, scheduler library, dotenv loader, or packaging/build tool is needed initially. Use a virtual environment locally if desired; no `pip install` is necessary.

V1 reads an explicitly mapped published flag and URL/BV field, fetches each distinct BV ID once per run, and updates four number properties (views, likes, favorites, coins) plus one date. Support local dry-run/apply and manual Actions execution before scheduling. Defer comments, shares, formulas, history, notifications, age-based cadence, short-link expansion, and Notion buttons. An existing BV field can be used; do not require users to add one alongside a working URL field.

## Verification evidence and limits

Read-only HTTPS research was performed on 2026-10-16 with certificate verification enabled. Downloads are research material outside the checkout, not application code.

### Notion: contract verified from official SDK source; workspace access unverified

The current official JavaScript SDK source sets `defaultNotionVersion = "2025-09-03"`. Its generated endpoint definitions confirm:

| Operation | Contract |
| --- | --- |
| Discover a database's sources | `GET /v1/databases/{database_id}` returns `data_sources` |
| Inspect the source schema | `GET /v1/data_sources/{data_source_id}` returns `properties` |
| Query entries | `POST /v1/data_sources/{data_source_id}/query`, with `filter`, `page_size`, `start_cursor`; response includes `results`, `has_more`, `next_cursor` |
| Patch an entry | `PATCH /v1/pages/{page_id}`, with only the intended `properties` |

Send `Authorization: Bearer …`, `Notion-Version: 2025-09-03`, and JSON content type. Pin this verified contract explicitly; do not assume it is the newest available API version simply because it is the SDK default. Do not copy old `POST /databases/{id}/query` examples or treat database ID, view ID, and data source ID as interchangeable. A database can have multiple data sources; select explicitly rather than choosing the first.

Official sources retrieved successfully:
- [SDK version header](https://github.com/makenotion/notion-sdk-js/blob/main/src/Client.ts)
- [Database definition](https://github.com/makenotion/notion-sdk-js/blob/main/src/api-endpoints/databases.ts)
- [Data source definition](https://github.com/makenotion/notion-sdk-js/blob/main/src/api-endpoints/data-sources.ts)
- [Page update definition](https://github.com/makenotion/notion-sdk-js/blob/main/src/api-endpoints/pages.ts)

Direct access to `developers.notion.com` was denied by this environment's egress proxy (CONNECT 403). Therefore the latest version announcement, exact current request-limit guidance, and free-plan integration terms were **not independently verified**. No Notion token/source binding is available in the current setup, and the actual tracker has not been inspected. Source-code verification establishes the proposed API shape, not successful authentication or compatibility with the user's schema. Before implementation, recheck the [versioning docs](https://developers.notion.com/reference/versioning), [request limits](https://developers.notion.com/reference/request-limits), and free-plan integration availability. Pace serially and honor server retry guidance rather than treating a remembered rate limit as a guarantee.

### Bilibili: live verification blocked; do not claim endpoint readiness

Candidate from the specification: `GET https://api.bilibili.com/x/web-interface/view?bvid=…`, expected fields `data.stat.view`, `like`, `favorite`, `coin`. An unauthenticated probe using public example `BV1xx411c7mD` was denied by the environment's egress proxy (CONNECT 403), before reaching Bilibili. This is **not evidence that Bilibili rejected the request**, and neither the current response fields nor anonymous access were verified.

The [previously common community reference](https://github.com/SocialSisterYi/bilibili-API-collect) now reports permanent shutdown and removal of documentation following a legal warning, dated January 2026. Its old video-information document returns 404. It is not a current authoritative contract. Do not rely on archived examples as live verification or assume public-facing means an officially supported automation API. Check applicable platform rules before launch; if permitted anonymous access does not work, retain manual/local operation or pause this integration rather than add paid services or bypass protections.

Required next evidence: an actual permitted live response with HTTP status, application `code`, matching `data.bvid`, and all four valid counts; then the same probe from a standard GitHub-hosted Linux runner. A local success does not establish Actions reachability. Network access needed for remaining research: `developers.notion.com`, `docs.github.com`, `api.bilibili.com`; live sync also needs `api.notion.com`. No environment network settings were changed during this planning task.

### GitHub Actions: billing and scheduling verified from official docs source

Current GitHub documentation says standard hosted runners are free for public repositories. GitHub Free private repositories receive 2,000 minutes/month, shared with the owner's other usage. Larger runners are billed even for public repositories. Overages can be charged when a payment method exists; lack of a valid payment method blocks use after the quota is exhausted.

For a private repo, verify an account-level Actions spending hard stop covering all relevant usage before scheduling; alerts alone are insufficient. If a verified zero-overage configuration is unavailable, use public standard-runner execution with the inactivity caveat, or local/manual-only operation. Never accept automatic paid overages to preserve scheduling. No code can guarantee external providers retain their free plans forever.

Use standard `ubuntu-latest`, no stored artifacts or dependency caches, no paid marketplace services, `permissions: contents: read`, and SHA-pinned official checkout/setup actions. Tentative schedule: `17 */3 * * *` (UTC). Scheduling runs on the default branch, may be delayed/dropped, and public repositories have schedules disabled after 60 days without repository activity. Document manual re-enabling; do not implement artificial keepalive commits.

At eight runs/day, a 31-day month has 248 runs. A five-minute job limit bounds scheduled job usage to approximately 1,240 rounded runner minutes, leaving only 760 of a 2,000-minute allowance for manual runs, tests, and other repositories. This is a planning bound, not a verified account budget or suitable limit for any collection size. Measure actual collection size and execution time; use a less frequent schedule if necessary. A two-minute average would use about 496 minutes/month. Fail safely when the quota is exhausted; missed runs are acceptable under the zero-cost priority.

Official sources retrieved through GitHub's source repository:
- [Actions billing](https://github.com/github/docs/blob/main/content/billing/concepts/product-billing/github-actions.md)
- [Included quotas](https://github.com/github/docs/blob/main/data/reusables/billing/actions-included-quotas.md)
- [Budgets and hard stops](https://github.com/github/docs/blob/main/content/billing/how-tos/set-up-budgets.md)
- [Scheduled events](https://github.com/github/docs/blob/main/content/actions/reference/workflows-and-actions/events-that-trigger-workflows.md)
- [Schedule delay caveat](https://github.com/github/docs/blob/main/data/reusables/actions/schedule-delay.md)

## Missing requirements and proposed defaults

1. **Tracker mapping:** Obtain a sanitized property-name/type list, exact published semantics, and the chosen source ID. Published may be checkbox, status, select, or date; support only the actual type at first. A formula/rollup cannot be a writable statistics target. Linked views are not the underlying source, and view filters are not automatically applied to API queries. Never infer eligibility from a nonempty URL alone.
2. **Schema changes:** The user adds or explicitly authorizes the four number fields and date field. The application validates; it does not migrate the database. Map existing properties by ID where practical so renames do not silently redirect writes. Fail the entire run before writes for invalid/ambiguous mappings or target/source type mismatches. Reject duplicate target IDs and mappings onto eligibility/source fields.
3. **Identifier handling:** Accept a standalone `BV` plus ten ASCII alphanumeric characters, preserving case, or an HTTPS `bilibili.com`/`www.bilibili.com` URL with `/video/{BV_ID}`; allow ordinary query strings/trailing slash. Reject AV IDs, short links, arbitrary hosts, credentials, nonstandard ports, and conflicting configured URL/BV fields. If both fields are present they must agree. Never request a URL supplied by a Notion row: construct a fixed Bilibili API URL from the validated ID. Disable redirects for API requests to avoid forwarding the Notion token elsewhere.
4. **Counts:** Require all four integer counts, excluding booleans, and values >= 0 before writing anything for that video. Missing required count means no count or timestamp update. Zero is valid only when explicitly returned. Counts may legitimately decrease after platform corrections; do not impose monotonic growth. Public counts have no guaranteed freshness/SLA; record fetch time, not a fictional source-generation time.
5. **Timestamp:** `Last Synced` means the last successfully fetched and stored observation. Send a timezone-aware UTC fetch timestamp in the same PATCH as the counts. If counts are unchanged, update the timestamp only; this deliberately incurs one write per eligible row each successful run. Report “unchanged counts” separately from “no write.” Failed or ambiguous writes never trigger a separate timestamp-only success update.
6. **Retries and deadlines:** Start with serial requests, at least one second between Bilibili fetches, conservative Notion pacing, a 15-second per-request timeout, at most three attempts for transient transport/5xx/429 failures, and bounded jitter/backoff honoring `Retry-After`. If retry guidance exceeds the remaining run budget, fail/defer rather than retry early. Do not retry authentication/configuration errors. Stop further Bilibili calls for the run on explicit anti-bot/block signals (e.g. HTTP 403/412 or documented application block codes verified during probing); preserve data and report affected work. Do not invent a comprehensive application-code list before observing current behavior.
7. **Failure semantics:** One video's failure does not stop independent videos. Invalid credentials/schema and source-query/pagination failure abort before writes; collect and validate the complete query first. Stop further Notion writes on global authorization failure. Proposed exit codes: 0 for a completed healthy run, 1 for per-record/partial failures, 2 for configuration/global failures. Blank links and unpublished entries are normal skips; malformed nonblank identifiers are reported as invalid and make the run partial failure. A zero-eligible run is allowed but visibly reported, not treated as proof that sync works.
8. **Concurrency and consistency:** Use one workflow concurrency group with `cancel-in-progress: false`; prevent overlapping local apply runs operationally. No distributed lock or transactional sync engine in v1. Re-read source ID/eligibility immediately before each PATCH and skip changed rows; document that Notion provides no assumed compare-and-swap here, so a narrow edit race remains. Never create pages; retries send the same absolute values and observation timestamp. Do not claim cross-row atomicity.
9. **Scale:** Ask for expected eligible-row count and source size. Deduplicate fetches in memory, but update each associated row. Paginate through all results and enforce a run deadline shorter than Actions' job timeout. If the collection cannot fit comfortably, reduce cadence or reconsider bounded batching explicitly; do not silently process the same first subset forever.
10. **Privacy/security:** Prefer a personal internal Notion connection with read content and update content only, shared only with the tracker; no insert-content capability. Store `NOTION_TOKEN` in Actions secrets and a local environment variable. Store source ID in a repository variable or secret according to privacy preference. Keep schema TOML local if names are private; Actions can receive its contents through a secret into a temporary file with restrictive permissions. Do not print tokens, full response bodies, private titles, URLs, or source IDs. Use opaque row references in logs. Avoid fork/PR workflows with sync secrets, `pull_request_target`, shell interpolation of untrusted inputs, and dispatch inputs that override API hosts.

## Proposed repository layout and commands

```text
AGENTS.md
IMPLEMENTATION_PLAN.md
README.md
.gitignore
config.example.toml                 # placeholders; mapping/type + published value
blbl_tracker/
  __init__.py
  __main__.py                       # CLI/config validation and exit status
  http.py                           # fixed-host HTTP, timeout/retry/deadline policy
  bilibili.py                       # BV parsing and response validation
  notion.py                         # schema/query/pagination/narrow page updates
  sync.py                           # deduplication, orchestration, summary
tests/
  fixtures/                         # synthetic or sanitized public responses only
  test_bilibili.py
  test_notion.py
  test_sync.py
.github/workflows/
  test.yml                          # no credentials; manual initially
  sync.yml                          # manual first; schedule added after gates pass
```

No `requirements.txt` is necessary with zero external dependencies. Use `python -m blbl_tracker --config config.local.toml --check-schema` for read-only schema inspection, `python -m blbl_tracker --config config.local.toml --dry-run` (also the default), and add `--apply` to write. These interfaces are implemented and exercised with mocked clients; live integration remains pending. Tests: `python -m unittest discover -s tests -v`. Keep `config.local.toml`, `.env`, virtual environments, and diagnostic downloads ignored. Read environment variables directly; do not silently load or execute an `.env` file. Document safe local credential entry without putting tokens in command arguments or shell history.

## Independently testable phases

### 0 — Resolve external and schema rollout gates

- Obtain sanitized tracker mapping, explicit source selection, current row-count estimate, and approval of intended added properties. No secret values in chat.
- Recheck current official Notion docs/free integration terms, inspect schema read-only when authorized credentials are available, and verify anonymous Bilibili behavior and permitted use.
- Decide private/public repository and verify the no-overage configuration.
- **Accept:** recorded property IDs/types and published predicate; a real matching Bilibili response with four counts; documented Notion version/permissions and free-plan compatibility; an agreed cost safeguard. If these cannot be established, mark the dependent phase blocked rather than claim endpoint readiness. GitHub-runner reachability is an additional automation gate in phase 4.

### 1 — Pure configuration, parsing, and validation

- Implement the package/CLI, TOML mapping, BV parser, and normalized stats model without live writes.
- **Accept:** offline tests cover bare IDs, normal URLs, query strings, invalid/short/hostile links, conflicting identifiers, Unicode/length edge cases, missing fields, malformed JSON, booleans/negative counts, mismatched returned BV ID, and explicit valid zero. Configuration rejects overlapping/wrong-type mappings; no credentials appear in captured logs. CLI defaults to dry-run and rejects contradictory flags.

### 2 — Read-only clients and complete discovery

- Implement fixed-host HTTPS clients, header/version handling, schema preflight, published selection, full pagination, Bilibili fetch and deduplication, timeout/retry/deadline rules.
- **Accept:** mocked multi-page results visit every page exactly once; cursor failures and schema/auth failures produce zero PATCH calls; duplicates fetch once; retry limits and `Retry-After` are honored; redirects cannot leak credentials; blocks stop Bilibili calls. Explicit live dry-run verifies one or two public videos and the actual source without changing any Notion page. Distinguish offline passes from live passes.

### 3 — Controlled writes and failure recovery

- Implement explicit apply, per-row revalidation, minimal PATCH, observation timestamp, and summaries.
- **Accept:** a user-approved test row gets all four correct counts and UTC timestamp; unrelated properties and page content stay unchanged. Re-run creates no pages and sends only the timestamp when counts are unchanged. Mock missing stats, unavailable video, failed PATCH, changed eligibility, auth loss, lower legitimate counts, and ambiguous timeout: prior good data is preserved on fetch failure, no separate success timestamp is written after update failure, independent rows continue, and exit codes reflect partial/global failures. No ordinary test writes production data.

### 4 — Manual Actions and cost validation

- Add credential-free tests and manually triggered sync using official actions pinned by full commit SHA, Python 3.12, minimal permissions, timeout, concurrency, and safe secret handling. Start with manual dry-run; allow explicit apply only after its review.
- **Accept:** anonymous Bilibili probe works on a standard GitHub Linux runner without cookies; schema check/dry-run succeeds; manual apply updates only the approved row. Logs contain no credentials/private row content. No artifacts/caches or paid runners are configured; owner verifies billing hard stop and measures total duration against all account usage. If runner access is blocked, preserve the validated local/manual fallback and do not enable schedule.

### 5 — Scheduled rollout and handoff

- Add `17 */3 * * *` on the default branch only after prior gates pass; start with the approved small set and then expand to the agreed tracker scope. Document setup, mapping, limitations, billing, token rotation, manual re-enable, and local troubleshooting.
- **Accept:** at least two actual scheduled runs complete unattended with correct counts/timestamps; a subsequent failure leaves previous data intact; runtime fits the agreed budget and all eligible rows are accounted for. Manual and local modes remain usable. Document skipped/delayed schedules and public-repo inactivity behavior. Do not infer scheduled success from a manual run.

## Confirmed decisions and remaining rollout inputs

User decisions: preserve the existing schema using configurable mappings; published plus valid URL/BV ID defines eligibility; fewer than 100 initial videos with pagination; Views/Likes/Favorites/Coins/Last Synced; timestamp advances on successful retrieval even with unchanged counts; private GitHub repo; three-hour schedule, five-minute timeout, enforced zero-overage safeguards; local manual fallback. Optional features remain excluded.

Still needed: sanitized property names/types and published value (or authorized read-only schema access), the exact data source ID and secure token binding, successful live dry-run review, current API compatibility and runner reachability evidence, and owner confirmation of actual billing enforcement. No secret values should be sent in chat. Repository gate variables are acknowledgements, not billing configuration.

Implemented offline validation covers strict counts/identity, safe URL parsing, source-type mappings, pagination, retries, circuit breaking for Bilibili HTTP 403/412/429, row revalidation, narrow updates, timestamp-only writes, and CLI dry-run defaults. Runtime and test dependencies remain zero. See README for actual configuration and rollout steps. Workflow syntax is checked locally; remote execution remains untested.

No live Notion writes, schema changes, remote workflow execution, commit, or push has been performed. Live access remains blocked on configuration/network prerequisites; do not interpret offline test success as live readiness.

## One-time discovery extension

Authorized scope: a separate read-only-by-default reconciliation command for UID 19997041, with explicit approved assignments to an empty `url` only. Filter Status=Published and Published Acc. containing B站. Use Song/Artist/Publish Date with Unicode normalization, explicit confidence/evidence, ambiguity refusal, paginated upload discovery, per-video live ownership verification, and JSON/CSV fallback. No discovery code runs in scheduled statistics sync.

Acceptance: offline tests cover Japanese/Chinese/English, near ties and duplicate tracker rows, eligibility, existing URLs, UID mismatches, untrusted export claims, blocked APIs, pagination, stale approvals, changed rows/metadata, and URL-only single-attempt writes. Real upload retrieval/report and live Notion dry-run review remain blocked on network/schema access. No actual assignments are approved or performed by this implementation task. See README's one-time discovery section for commands and documented read/PATCH race and incomplete-export limitations.
