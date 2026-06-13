# STATUS — blackboard-automation

_Living status doc. Update at the end of each work session. For deep architecture, see README.md (canonical)._

## What this is
Blackboard Ultra content extraction + AI conversational query engine, heading toward a subscription product. Free Fordham beta first. Solo non-technical founder; Claude Code (Sonnet) executes, planning done in chat. Windows/PowerShell.

## Current state (updated 2026-06-12)
- Phase 7 Stage A (RAG pipeline) — DONE: llm_adapter, ingestion, chunking, embeddings (local fastembed), retrieval, query.build_context.
- Phase 7 Stage B (uploads + attachments) — DONE: file/vision extractors, sync + async upload endpoints, full attachment workflow (attach as part of a message, send with/without text, auto-summary + course classification on a bare file, click-to-preview with inline PDF/image + download, draft persistence across reload, send blocked while a file is still reading, user message saved before generation so it survives a mid-stream reload).
- Repo housekeeping — DONE: requirements.txt pinned + clean-install verified, scratch file removed, dead notion/ integration deleted, README structure + priorities updated.

## Immediately next
- Stage D — decouple course summaries from the overrides system; move summaries into retrieval instead of injecting them wholesale. Mechanism (on-demand vs stored-and-retrieved) still TO BE DESIGNED — the trap is preserving the USER-CONFIRMED OVERRIDES authority. Good candidate for a focused design pass.
- Phase 9 — operational maturity: structured logging (replace prints), per-call token tracking, retry/backoff on streaming, first tests for ingestion + retrieval.
- Phase 9.5 — announcement scraping (student-visible, no copyright issue).
- Fordham IT outreach (parallel, longest lead time) — register Anthology developer account, then contact Kanchan Thaokar (Sr. Manager Enterprise Learning Systems). NOT yet sent. Unblocks the Phase 10 auth path.

## On hold / blocked
- Stage C (OCR backfill of publisher materials) — on hold pending attorney review (copyright). Publisher-owned content (e.g. Cengage decks) currently in corpus is a known exposure.
- REST API integration / Phase 10 deploy / multi-school expansion — gated on the Fordham IT conversation + attorney sign-off.

## Known small issues (parked, not worth fixing yet)
- Attaching a file then switching chats mid-read loses the in-progress attachment (narrow window; the file still completes server-side).
- Three scraper bugs (Social Psych popover, British Gov lazy-load, International Internship DOM) — need a second Blackboard account to test.
- Haiku router JSON fragility — route_question_to_courses parses Haiku's output with a bare json.loads. Observed a live JSONDecodeError fall back to all courses (fail-open worked, user got a correct answer, but the fallback sent ~32K tokens to Sonnet at ~$0.11 for one query). Likely cause: Haiku occasionally wrapping its JSON array in markdown ```json fences. Low-effort fix (strip code fences before parsing) but belongs in P2's extraction-robustness work, not a one-off patch. Flagged because the silent failure has a real cost tail — every fail-open query costs like a worst-case all-courses query, which is the exact scenario the router exists to prevent.
- Vision OCR of publisher/scanned PDFs is blocked by Anthropic's content filter (confirmed live: "Output blocked by content filtering policy" on a scanned Ethics PDF). Code handles it cleanly — logs status=failed, extraction_method=vision_ocr — but the USER-FACING story is bad: the user waits through polling and gets a silent failure with no explanation. Entangled with the Stage C attorney hold. P4 should add a clear user-facing message for filter-blocked uploads. Not a code bug.
- Measured per-query cost is at/above the README's $0.05-0.10 target. First logged runs: a normal 6-course chat = ~$0.099 (32K input tokens); an attachment-summary chat = ~$0.127 (34K tokens). Router was working correctly (all-courses is right for general questions) — the cost driver is injecting full course summaries as context, not a routing failure. This is direct measured evidence that Stage D (move summaries into retrieval rather than inject whole) and the Phase 10 structured-routing short-circuit are unit-economics necessities, not just cleanups. Captured here so the dollar figures aren't lost before that work happens.

## Key decisions & principles (orient fast)
- The scraper is PLUMBING, not the moat. Moats = distribution, per-school data network effects, workflow lock-in. Everything from ingestion.py onward is already REST-API-ready; migrating off Playwright means writing a new ingestion script feeding the same document store — the coupling is the data SCHEMA, not Playwright. Don't over-invest in the scraper; don't parallelize it before the API-path decision.
- AI is invisible plumbing — positioned as removing student cognitive load, NOT as an "AI study tool."
- Cost discipline: cheap router (Haiku) + expensive generator (Sonnet); fail-open routing (errors fall back to all courses). RAG exists so a heavy user can't cost more than the subscription.
- Legal: reproducing publisher EXPRESSION is the exposed area; facts (deadlines, schedules) and the student's OWN content are safe. Pursue institutional sanction — do not engineer around content filters or hide infringement.

## Working setup
- Stack: Python, FastAPI, Playwright, anthropic SDK (claude-sonnet-4-6), local fastembed (BAAI/bge-small-en-v1.5, 384-dim), JSON storage. Postgres/pgvector + Next.js/Vercel planned for Phase 10.
- Run the UI: python app.py output/content_text_<timestamp>.json  (localhost:8000)
- README.md is the canonical architecture doc and tie-breaker.