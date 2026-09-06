# Hosted review corrections — 6 September 2026

The 17 reliability PRs (#976–#992) address the confirmed audit issues #932–#975. This report records the follow-up review of every nonempty hosted review body captured before publication: **258 records**, including **206 inline threads**. Records include repeated comments and summaries; these counts are not counts of distinct bugs.

The [per-comment ledger](hosted-review-dispositions.csv) records the original GitHub URL, file, outcome, integrated commits and evidence for every record. Outcomes: **140 fixed, 90 duplicate, 7 already resolved and 21 not actionable**. No captured record remains pending. The [503-file inventory](reliability-file-inventory.csv) maps every tracked file to its role and affected audit issues. The [reliability map](reliability-map.md) connects those files to the complete resume flows and reproducible checks.

## Stack and file ownership

Merge in ascending order. #976 targets `main`; each later PR targets the preceding reliability branch. The assembled stack is based on `e3a63a77129be4df4cd7aaa22011c9904c8161b2`.

| PR   | Issues                 | Main files and resulting behavior                                                                                                                      |
| ---- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| #976 | #932, #972             | `config.py`, `config_cache.py`, backend test setup: isolated configuration paths, path-aware cache, atomic writes preserving symlinks and permissions. |
| #977 | #975                   | `llm.py`, temperature transport tests: conservative model capabilities and all three actual JSON retry requests checked.                               |
| #978 | #939, #964, #965       | Frontend API client and upload hook/dialog: current operation ownership, capacity, cancellation and response metadata.                                 |
| #979 | #933, #934             | `resume-builder.tsx`, draft storage: server baseline, scoped identity and recovery validation.                                                         |
| #980 | #935, #936, #937       | Builder and attachment editors/storage: serialized saves, generation, Back and export; accurate backup and popup/error recovery.                       |
| #981 | #938, #967, #968       | Wizard page/storage and creation endpoint: paired text styles/dates, truthful progress and durable completion replay.                                  |
| #982 | #966                   | Dashboard and refresh tests: request ordering, bounded polling and authoritative post-upload list replacement.                                         |
| #983 | #952, #953, #954       | Parser, resume router and database: bounded extraction, referenced PDF stream validation, token-owned processing/retries and retained worker cleanup.  |
| #984 | #955, #956, #957, #958 | Database, models, job/application routes: reserved transactions, deduplication, retryable contention and orphan cleanup.                               |
| #985 | #948, #949, #950, #951 | Preview/database/resume routes and tailor page: registered snapshots, bounded lifetime, atomic confirmation, replay and expired-preview recovery.      |
| #986 | #943, #944, #945, #969 | LLM wrapper, result schemas, AI services and enrichment: strict structured outputs, real Router retry contracts and visible partial failures.          |
| #987 | #940, #941, #947, #970 | Parser, refiner, preservation helper and confirmation: source identity/section preservation, grounded changes and approved append handling.            |
| #988 | #946                   | Wizard service and prompt: stable IDs, explicit additions and correction matching.                                                                     |
| #989 | #942                   | AI budget/limit helpers and routes: shared deadlines, source bounds, bounded cleanup and dedicated error propagation.                                  |
| #990 | #959, #963, #971       | Monitor, judge, evaluation scorers and refinement statistics: process ownership, artifact failures, isolated paid opt-in and final-output metrics.     |
| #991 | #960, #961, #962       | PDF service, resume CSS and renderer tests: bounded admission, retained cleanup, browser recovery and multilingual geometry/glyph checks.              |
| #992 | #973, #974             | Remaining cross-flow ownership, browser tests, architecture docs and workflow validation; integrated follow-ups described below.                       |

Corrections were inserted at their owning stage where dependencies allowed. Builder baseline validation also uses attachment definitions introduced in #980. Later integration corrections reside in #992: viewer completion after navigation, direct-improve atomic persistence, deadline/prompt failure propagation, confirmation source-limit alignment, bounded processing cleanup, shared final statistics and duplicate-signature wizard echoes. Earlier PRs therefore rely on the complete stack for all review corrections; do not treat an individual intermediate tip as the final reviewed state.

## Evidence and important distinctions

Behavioral fixes were driven by failing cases at actual API, transaction, hook, component or browser boundaries. Independent integration review found and corrected additional cases: approved append changes disappearing at confirmation, invented appended revenue, an abandoned viewer retry reloading a different page, required direct-improve relations written separately, swallowed deadline errors, confirmation rejecting a valid near-limit preview, unbounded cleanup waiting and a zero-ID wizard echo overwriting the first of two identically identified entries.

Direct improvement now writes its resume and required Improvement relation in one transaction. Cancellation before commit leaves neither; expiration after commit can return a timeout with both records present. Tracker creation remains best effort. Confirmation has its separate durable replay contract. See [storage transactions](storage-transactions.md) and [preview confirmation](../features/preview-confirmation.md).

Cleanup waiting is bounded while tracked tasks retain ownership of work that cannot finish immediately. This is not a promise that cancellation instantly stops a converter, browser or database operation. See [AI operation budgets](ai-operation-budgets.md) and [PDF lifecycle](../design/pdf-template-guide.md).

Not-actionable comments have concrete explanations in the ledger, including the installed LZW iterator API, Python cancellation inheritance/reinjection behavior, valid TypeScript type queries, Next App Router navigation semantics, model-specific sampling defaults and already-supported full month names. Unsupported premises were not implemented as behavior changes.

## Verification

At the final code tip `5941dd17`, the complete isolated backend suite passed **970 tests**, with **2 paid evaluations deselected**. The frontend suite passed **436 tests in 54 files**; frontend lint, TypeScript, formatting and production build passed. No frontend behavior changed after that frontend run. An AST audit found no missing annotations among **669 changed Python functions** against the stack base. `git diff --check` and actionlint 1.7.12 passed.

Five real Next browser controls passed: A→Back→B request ordering, downloaded PDF bytes, viewer-to-builder navigation, query-only builder identity/attachment reset and two-tab draft isolation. Additional production-browser controls verified that a late retry no longer reloads a replacement page, while a retry on the still-current resume reloads correctly. No page errors occurred. Browser API responses and resume fixtures were synthetic; the download control tests the browser path. Separate backend tests exercised actual Chromium PDF rendering, geometry and embedded CJK glyph programs for long A4/Letter documents.

These checks do not establish live-provider latency, quality, cost, every font/template combination or external ATS certification. Tests used isolated data/configuration and blocked external provider traffic. Paid generated-result evaluations remain explicitly opt-in.

## Workflow validation

`docker-publish.yml` retains existing main/tag publication and adds a manual boolean `dry_run` input. An explicit dry run skips registry login and publication while building both `linux/amd64` and `linux/arm64`. The final branch must be dispatched with `dry_run=true`; hosted run status is recorded in the PR validation notes after completion. Local workflow lint is complete; this document does not claim an unobserved hosted run succeeded. Actual registry publication and future merge runs depend on GitHub and registry availability and credentials.

No PRs were merged and no review threads were programmatically resolved. Original review bodies, red/green logs, independent review reports, browser artifacts and publication receipts are retained in the local `docs/reports/2026-09-06-hosted-review-fixes/` evidence directory.
