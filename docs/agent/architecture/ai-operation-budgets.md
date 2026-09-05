# AI operation budgets

`AIOperationRoute` wraps POST requests in the resume, enrichment and wizard routers. The deadline starts before FastAPI validation and includes database preloads, model calls, retries and persistence. GET/PDF routes use their own export lifecycle. POST actions in these routers that do not call AI also receive the same total deadline.

`REQUEST_TIMEOUT_SECONDS` remains 240 seconds by default, configurable from 30 to 1,800 seconds. Keep the frontend `NEXT_PUBLIC_REQUEST_TIMEOUT_MS` and proxy timeout aligned when increasing it for local models. This is an operation budget, not an extra allowance for each stage.

`app/ai_budget.py` stores an absolute monotonic deadline in a ContextVar. Nested budgets retain the earliest deadline. Completion and JSON calls cap their existing model/token-dependent transport timeout to the remaining budget. Content retries recalculate remaining time. Enrichment analysis no longer imposes a separate fixed 180-second ceiling. The outer task cancels provider retries and queued item work when the operation expires; HTTP clients receive a generic 504 and server logs identify the route and elapsed time. Explicit caller cancellation propagates unchanged.

Cancellation is cooperative. Synchronous validation or a noninterruptible converter thread cannot be forcibly stopped by an asyncio timer. Owned resource cleanup may finish after the work deadline; upload and PDF cleanup policies retain their capacity reservations until those resources have actually stopped. A cancelled upload/retry settles any in-flight processing claim and marks only its owned attempt failed. It cannot recreate a deleted row or retire a newer retry.

## Input policy

Oversized input is rejected rather than silently truncated. Schema and stored-source failures return 422 before the relevant AI stage.

| Input | Limit |
| --- | --- |
| Regeneration items | 20 per request |
| Active regeneration workers | 4 per request |
| Enhancement answers | 40 per request |
| Answer / regeneration content leaf | 6,000 characters |
| Original enhancement question | 2,000 characters |
| Regeneration content entries | 100 per item |
| Regeneration instruction | 2,000 characters |
| Enhancement / regeneration request JSON | 200,000 serialized characters |
| Resume source / confirmation request JSON | 200,000 serialized characters |
| Job description | 100,000 characters |
| Final LLM prompt plus system text | 512,000 characters |
| Wizard history | 15 entries; each resume snapshot bounded to 200,000 serialized characters |
| Wizard question / history answer | 2,000 / 6,000 characters |
| Inferred wizard skills | 100 entries, 200 characters each |

JSON bounds count the Unicode characters in `json.dumps(..., ensure_ascii=False)`, including structure. They are distinct from the document ingestion byte limits. The final prompt check is a defense at the LLM boundary; source-specific route/schema checks provide the useful 422 response earlier.

Regeneration keeps ordinary item failures alongside successful items. Expiry or caller cancellation cancels active and queued work instead of returning a partial success from an abandoned operation. This concurrency limit is per request, not a global provider rate limiter.

## Verification

`tests/integration/test_ai_operation_budgets.py` exercises the real ASGI routes with slow synthetic preloads/model stages, upload cancellation before persistence, external cancellation, oversized saved sources, request bounds, nested budget restoration, declining transport timeout values, and bounded regeneration with item failures and cancellation. No provider credentials or live paid calls are needed.
