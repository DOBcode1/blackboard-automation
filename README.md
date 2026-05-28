# blackboard-automation

A Blackboard Ultra course content extraction system with an AI-powered conversational query engine. Scrapes courses, extracts text from every item, pre-processes content with AI, and provides a chat interface for students to ask natural-language questions about assignments, due dates, and course materials.

---

## What the project does

A tool that scrapes Blackboard Ultra, extracts text from every course item, pre-processes it with AI, and provides a conversational query engine where students ask questions about their courses. Includes a working local web UI (FastAPI + chat interface).

---

## Project structure

```
blackboard-automation/
  blackboard/
    scraper.py      — Phase 2: course content metadata extraction
    reader.py       — Phase 3: text extraction from each item
    __init__.py
  extractor.py      — entry point for scraper
  run_reader.py     — entry point for reader
  query.py          — Phase 4: AI query engine (terminal + used by app.py)
  app.py            — Phase 5: FastAPI local web UI
  output/           — JSON output files + pre-processed caches
  debug/            — HTML dumps for selector debugging
```

---

## How to run everything

Open PowerShell:
```powershell
cd blackboard-automation
```

Open Claude Code:
```powershell
claude
```

Set API key (required each new PowerShell session unless set permanently in Windows Environment Variables):
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-key-here"
```

Run scraper:
```powershell
python extractor.py
```

Check output filenames:
```powershell
dir output\
```

Run reader:
```powershell
python run_reader.py output/content_objects_TIMESTAMP.json
```

Run query engine (terminal):
```powershell
python query.py output/content_text_TIMESTAMP.json
```

Run web UI:
```powershell
python app.py output/content_text_TIMESTAMP.json
```
Then open browser to `http://localhost:8000`.

**Most recent output files:**
- Reader: `content_text_20260514_084311.json`
- Scraper: `content_objects_20260514_073953.json`

**Git workflow:**
```powershell
git add . && git commit -m "message" && git push
```

---

## Current state — everything working

- **Scraper**: captures 300–470 items across 6 courses, session recovery, retry logic, empty folder handling
- **Reader**: 270+ successful extractions, 0 errors, attachment handler for embedded files, session recovery
- **Query engine**: two-layer architecture — pre-processing extracts assignments / due dates / material maps per course (cached to JSON), query layer sends pre-processed summaries + compact index + fuzzy-matched full documents to `claude-sonnet-4-6` API
- **Web UI**: FastAPI serving chat interface at `localhost:8000`, streaming responses, course sidebar, markdown rendering

### Key features in query engine

- Cross-course queries ("all my classes" triggers all-course context)
- Document-based course matching (detects courses by item titles, not just course names)
- Fuzzy document matching ("chapter 7 slides" finds the right document)
- Source citations at end of every response
- Follow-up action suggestions
- Source transparency (flags course materials vs AI general knowledge)
- Rate limit handling (15s delays, auto-retry on 429)
- Conversation memory for follow-ups

---

## Architectural principles

This system is designed to scale from a single-user prototype to a multi-school B2B2C product without rewrites. The following principles govern every design decision. They exist to make the codebase legible and defensible to experienced software engineers reviewing it for technical due diligence.

### 1. Provider-agnostic LLM access

Every LLM call in the system goes through a single adapter layer (`llm_adapter.py`), never directly to a provider SDK. The adapter exposes capability-tier functions (`call_fast`, `call_main`, `call_vision`) rather than provider-specific calls. The current implementation routes all tiers to Anthropic Claude (Haiku 4.5 for fast, Sonnet 4.6 for main, Sonnet 4.6 vision for vision). Swapping providers — to GPT, Gemini, or a self-hosted model — is a single-file change followed by prompt re-validation. No call site in app.py, query.py, or any future module ever imports a provider SDK directly.

**Rationale:** Provider lock-in is the single largest risk to an AI product's economics. Adapter-pattern abstraction is the standard industry mitigation. It also makes A/B testing across providers possible without code changes.

### 2. Storage-backend agnostic data layer

All persistent state is accessed through helper modules (`chat_history_helper.py`, `overrides_helper.py`, future `documents_helper.py`) that expose CRUD-style functions. The helpers wrap an underlying storage backend that is currently JSON files. In Phase 10, the storage backend becomes PostgreSQL. The helper function signatures and call sites do not change. Migration is implemented by swapping the storage class behind the helpers, not by rewriting consumers.

**Rationale:** Storage migrations are the most common source of architectural debt. Doing them by abstracting storage from day one converts a multi-week refactor into a few-day swap.

### 3. Multi-tenant from day one

Every persistent record carries `user_id` and `school_id` fields, even though the current product has one user (`local_dev`) and one school (`fordham`). This includes chat threads, deadlines, overrides, uploaded documents, and future memory summaries. The single-tenant present is a special case of the multi-tenant future.

**Rationale:** Retrofitting multi-tenancy into a single-tenant system is one of the most expensive architectural rewrites in software. Designing for it from the start costs almost nothing today.

### 4. Unified content schema

All textual content the system has access to — scraped Blackboard items, OCR'd scanned items, user-uploaded documents, AI-generated study materials, manually entered notes — lives in a single unified data structure (the `documents` collection). The retrieval layer searches across this collection uniformly. The chat does not distinguish between "scraped content" and "uploaded content" — it retrieves relevant text and generates a response.

**Rationale:** A user thinks of all their academic materials as one body of knowledge. Splitting them into separate stores based on provenance (where they came from technically) creates artificial silos that hurt both retrieval quality and user experience. One collection, one schema, one retrieval layer.

### 5. Fail-open routing

Every routing decision in the system (which courses are relevant, which documents to retrieve, which user-corrections to apply) is implemented with a fallback path that returns the broader/safer answer on failure. The Haiku router (Phase 6.5f) is the reference implementation: if Haiku is unavailable, the router returns all courses instead of failing the query. This pattern extends to all future routing — embeddings retrieval, semester filtering, course-content matching.

**Rationale:** Routing failures should never be user-visible. A query that returns slightly more context than necessary is acceptable; a query that errors out because a routing call failed is not. Fail-open is the production-grade default.

### 6. Cost-tier discipline

Each LLM call is matched to the cheapest model that can perform the task acceptably. Concretely:
- **Haiku 4.5** (`$1/$5` per million tokens): routing, classification, extraction, OCR of scanned documents, simple summarization.
- **Sonnet 4.6** (`$3/$15` per million tokens): main chat generation, pre-processing course summaries, complex multi-document analysis.
- **Opus 4.7** (`$5/$25` per million tokens): not used in this system at the current stage. Reserved for future capabilities that demonstrably require it (complex agent workflows, multi-step planning) and only after empirical validation that Sonnet is insufficient.

**Rationale:** Every senior engineer reviewing the unit economics will ask "why are you using Sonnet for this?" Having a documented tier-assignment policy converts a vague concern into a clear, defensible decision.

### 7. Retrieval over inflation

As content per user grows (multi-semester history, accumulated uploaded documents), the system retrieves relevant chunks rather than inflating the context window. This means a graduating senior with 4 years of content costs roughly the same per query as a freshman with one semester. See Phase 7 below for the full retrieval architecture.

**Rationale:** "Just send everything to the LLM" is the architecture that bankrupts AI startups. Retrieval-based context assembly is the industry standard and is required for the unit economics to work at scale.

### 8. Observability as a first-class concern

Every significant operation in the system (LLM calls, routing decisions, retrieval queries, document ingestion, scraper sessions) emits structured logs with enough context to debug a specific user's specific query in production. Cost-per-operation is tracked. Error rates are tracked. Phase 9 (Operational maturity) makes this systematic.

**Rationale:** "It worked on my machine" is not a debugging strategy at scale. The cost of building observability after problems start is far higher than building it as you go.

---

## Working hypothesis and revision policy

This README captures the current best thinking about the system's architecture, roadmap, and business strategy. It is comprehensive and detailed by design — comprehensive enough to onboard a collaborator, defend an architecture decision to a technical reviewer, or recover the project's context after a long break.

It is also explicitly NOT a specification. It is a working hypothesis.

### Why this matters

The architecture, phasing, and even some principles documented here were scoped during planning sessions before the corresponding code was written. Building things teaches you what designing them could not. Each major build (Phase 7 stages, Phase 9, Phase 10) is expected to surface constraints, opportunities, and better approaches that this document cannot anticipate. The roadmap will be revised — sometimes meaningfully — as that happens.

This is not a weakness of the plan. It is how production software gets built. Plans that cannot adapt to what is learned during execution result in worse outcomes than plans that can.

### When to revisit this document

After each of the following, the relevant sections of this README are explicitly re-evaluated:

- Completion of any Phase 7 stage
- Completion of Phase 9 (operational maturity)
- The first paying user
- The first conversation with Fordham IT
- The first 100 active Fordham users
- Any time a piece of this architecture proves harder, slower, more expensive, or less effective than expected

The revision can be: minor (tightening language, updating cost estimates), moderate (resequencing stages, changing technology choices within a layer), or major (rethinking a phase entirely). All three are valid outcomes.

### What stays stable

Some commitments do not change as the implementation evolves:

- The product positioning ("removes the cognitive load of being a student" — not "an AI study tool")
- The business model staging (Fordham beta → Fordham paid → sanctioned expansion → institutional)
- The architectural principles (provider-agnostic LLMs, storage abstraction, multi-tenancy from day one, unified content schema, fail-open routing, cost-tier discipline, retrieval over inflation, observability as first-class)
- The data sovereignty principles (per-school knowledge layer, no cross-school data leakage, FERPA-mindful design)

These are decisions, not hypotheses. Everything below them is a working hypothesis subject to revision.

### How to use this document in practice

When facing a build decision during development:

1. Check the README. If it has guidance, follow it.
2. If the guidance does not fit the situation, that is a signal — the situation has revealed something the planning sessions missed.
3. Pause development on that piece. Discuss the misfit with whoever you are working with (collaborator, technical advisor, or AI assistant in a planning conversation rather than coding session).
4. Decide whether to: adjust the implementation to fit the plan, OR adjust the plan to fit what you have learned.
5. Update the README. The change becomes part of the new working hypothesis.

This loop — build, learn, revise the document, continue — is the actual mechanism by which the system improves over time. The README is a living artifact, not an instruction manual.

### A note on solo-developer context

The project is currently being built by a single non-technical founder working with AI coding assistance. This places specific constraints on what kinds of revisions are practical:

- Architectural changes that require deep systems expertise to evaluate (e.g., choosing between vector database vendors, deciding on async vs. sync execution models) are best handled by bringing in a technical advisor at the relevant moment rather than by the founder alone
- Small revisions (tightening a prompt, updating a cost estimate, resequencing two stages) can be made independently
- The "revisit triggers" listed above are deliberately chosen to coincide with moments when an outside perspective would be especially valuable — completion milestones, first users, first institutional conversation

This is not a limitation to apologize for. It is a documented operating constraint that informs how the plan should adapt.

---

## Known scraper bugs

Require another Blackboard account to test — NOT available right now.

- **Social Psychology popover** — MUI Popover context menu intercepts folder expansion clicks, ~55 items missed
- **British Government last 3 items** — scroll-based lazy loading doesn't reach very bottom
- **International Internship container bleed** — Learning Modules with non-standard DOM not detected by `containerMap`

---

## Things to work on now

In order of priority for the next several weeks of work:

1. **Phase 7 Stage A (Document ingestion pipeline)** — foundational build. Includes the LLM adapter migration, the unified document schema, ingestion pipeline, chunking, embedding, and retrieval. No new user-visible features but unlocks everything that follows.

2. **Phase 7 Stage B (User document upload)** — first user-visible feature built on Stage A. Drag-and-drop, paperclip button, document management page.

3. **Phase 7 Stage C (OCR backfill)** — extracts the ~160 currently-unreadable items per semester into the unified document store. Can run in parallel with Stage B if desired.

4. **Phase 9 (Operational maturity)** — structured logging, cost tracking, and the test suite. Distributed work that happens alongside Phase 7 stages, not as a standalone phase.

5. **The Fordham IT email** — drafted and sent. This is the path to Stage 3 (sanctioned API access) and has the longest timeline (weeks to months). Send it before the technical work is done.

6. **Three scraper bugs** — parked until a second Blackboard account is available.

---

## Full roadmap

### Phase 6: Speed improvements
- Parallel reader (3–4 simultaneous tabs)
- Headless mode for production runs
- Incremental sync (only re-process changed items)

### Phase 6.5: Deadline & Notification System
*The killer feature for a student-facing product. Deadline management is the daily-use hook that gets people opening the app.*

**Core idea:** the query engine's pre-processing layer already extracts assignments and due dates into cached JSON. That cache IS the deadline database — the calendar and notification features surface what's already extracted, not re-derive it.

**Onboarding — capturing the semester anchor:**

Every calendar feature depends on knowing when the semester starts and ends.
Week N → date calculations in the extraction pipeline, semester-window sanity
checks in the aggregator, and the calendar's display range all read from this
anchor. For the single-developer phase, the anchor lives in semester_config.json
and is edited manually. For the production product (Phase 10), this becomes
the very first onboarding step after Blackboard login.

Three options for capturing it, listed in order of user effort:

- **Auto-detect from Blackboard course names (Phase 11):** Course titles
  almost always contain the term ("Spring 2026 Ethics in Business..."). A
  per-university registrar lookup maps the term name to standard start/end
  dates. Zero user effort but requires per-school maintenance and breaks on
  inconsistent course naming. Best as a *suggestion layer* that pre-fills
  the form below, not a silent default.

- **Manual form (Phase 10 launch default):** Two date pickers — "When did
  your semester start?" / "When does it end?" — plus a term name field that
  pre-fills from the most common term in the user's Blackboard courses.
  Takes 10 seconds, bulletproof, works on day one with no per-school setup.

- **Syllabus upload (power-user option):** User drags in one syllabus PDF.
  AI extracts semester start/end (and ideally class schedule) in one shot.
  More complex, depends on syllabus quality, but extremely demo-friendly
  if it works. Probably Phase 10.5 once the simpler flow is proven.

Recommended launch path: ship the manual form for Phase 10. Layer in auto-detect
as a pre-fill once enough Fordham users have entered data to validate the
registrar mapping. Add syllabus upload later as an alternative entry point.

Anchor confirmation step: regardless of which method is used, show the user
the parsed dates before saving. Same UX pattern as the class schedule confirmation
already documented below.

**Known design wrinkles (worth solving in Phase 10, not Phase 6.5):**

- **Per-course semester windows:** semester_config.json already has structure
  for per-course overrides because not every course follows the main university
  calendar. Study-abroad programs, 8-week accelerated courses, year-long thesis
  seminars, and graduate independent studies frequently run off-cycle. The
  International Internship course in current test data is exactly this case.
  Calendar v1 ignores per-course overrides and uses one global window; Phase 10
  needs per-course capture in onboarding (or a "this course runs on a different
  schedule" toggle).

- **Concurrent terms:** A user with a fall thesis course plus a summer intensive
  has two active terms simultaneously. The data model needs to support this —
  courses.term_name already exists, so the right query becomes "deadlines for
  any active term as of today" rather than "deadlines for the current term."

- **Holidays and breaks:** Spring break, Thanksgiving, fall reading week all
  break the "Week N = start_date + 7*(N-1) days" assumption. Most syllabi count
  Week 10 in academic weeks (skipping break) not calendar weeks. Calendar v1
  accepts this drift as a confidence-3 problem; production should ingest the
  academic calendar (which Phase 11 already plans to scrape from registrar pages)
  and use it to skip break weeks in date resolution.

- **Re-enrollment:** When the user returns for a new term, onboarding needs to
  re-trigger for the new semester anchor. Not a calendar v1 problem but the
  data model should make "active term" a queryable concept from day one.

**Onboarding — capturing course context:**

For the calendar and deadline resolver to work, each course needs three pieces of context: a semester anchor (start/end dates), a class meeting schedule (days, times, location), and optionally a final exam schedule. Users can provide this via three input methods, picking whichever requires the least effort:

- **Screenshot upload (preferred):** user drags in a screenshot of their registrar's class schedule grid (Fordham's "View Registration Information" page, or equivalent at other schools like Banner, PeopleSoft, Workday Student). Vision-capable AI parses the grid into structured course meeting times. Same flow works for finals schedule PDFs from the registrar.
- **Natural language:** user types something like "Investments and Security Analysis Tuesdays and Thursdays from 11:30 to 12:45, Marketing Research Tuesdays and Fridays at noon..." and the AI extracts the same structured data.
- **Manual form:** standard form fields per course (days of week checkboxes, start/end time, location) as a fallback for users who want explicit control or when the other methods produce errors.

All three methods feed into the same JSON structure and are followed by a mandatory user confirmation step: AI shows what it parsed, user edits or approves before anything is saved. This prevents silent extraction errors from corrupting the calendar.

The screenshot method is the demo-friendly path — drag-drop-done is the magic moment for marketing.

**Calendar features:**
- In-app calendar view at `/calendar`, color-coded by course
- AI-driven additions via chat:
  - "Add my Social Psych midterm to my calendar" → AI confirms extracted date/time, adds event
  - "What do I have due in the next two weeks?" → AI answers from cache, then offers: *"Want me to add these to your calendar?"*
  - Each calendar entry includes a source link back to the original Blackboard item, so clicking a deadline opens the assignment page in a new tab. Critical for users to verify or get details.
  - "Add all my Brit Gov assignments to my calendar" → bulk-add with confirmation summary
- Manual editing of calendar entries (professors change dates, AI misreads occasionally) — original AI-extracted value preserved as fallback
- Confidence threshold: low-confidence dates ("end of week 4", "before spring break") flagged for user confirmation before adding silently
- Recurring events for weekly assignments, labs, discussion posts
- Confidence-aware UI: deadlines with confidence 4-5 display normally; confidence 1-3 entries show a "review" badge and don't trigger notifications until user confirms.

**Google Calendar sync (Phase 6.5d):**
- OAuth flow on first opt-in
- Two-way sync: events added in-app push to Google Calendar; changes made in Google Calendar pull back
- User can choose which courses sync (e.g., sync academic courses, skip electives)
- Single source of truth = in-app DB; Google Calendar is a mirror

**Notification system:**
- Per-event notification preferences (24h before, 1 week before, day-of, custom)
- Global defaults the user sets once ("always notify me 48h before any midterm/final")
- **Delivery channels:**
  - **Email** (Phase 6.5 launch): SendGrid or Resend, branded templates
  - **SMS** (Phase 6.5e): Twilio integration, user verifies phone number on signup, opt-in per event type, daily digest option to avoid spam
  - **Push notifications** (Phase 10, when mobile/PWA ships)
- Digest mode: "Your week ahead" email every Sunday with all upcoming deadlines
- Smart notifications: AI infers urgency from item type (final exam = earlier and more reminders than weekly discussion post)

**Final exam handling:**

Final exams are a special case — they often happen outside normal class meeting times and at different locations than the regular course. The AI cannot reliably guess final exam schedules from syllabi alone.

- Manual entry per final at onboarding (or whenever finals schedule is released, usually mid-semester): user enters date, time, and location. 5 finals × ~30 seconds = under 3 minutes per semester.
- Finals schedule PDF upload (power-user option): user uploads their school's official finals schedule PDF, AI reads it via the existing document reader pipeline and matches finals to the user's courses by name fuzzy-match.
- Any assignment the AI extracts with type "final exam" and no user-provided final exam data is automatically flagged confidence-1 and marked "needs your input" in the calendar.

**Data model sketch:**

Schema additions in Phase 6.5b (dismissed, user_edited), Phase 6.5c (weight,
weight_confidence), and a future categorization phase (category) are forward-
compatible — calendar v1 ignores unknown fields, so adding them doesn't break
existing data.

Anchor data captured during onboarding (see above); per-course overrides support off-cycle courses like study-abroad and accelerated terms.

- `courses` table: id, user_id, course_id, course_name, semester_start, semester_end, term_name, created_at, updated_at
- `class_meetings` table: id, course_id, day_of_week (mon-sun), start_time, end_time, location
- `final_exams` table: id, course_id, date, start_time, end_time, location, user_provided (bool)
- `deadlines` table: id, user_id, course_id, source_item_id, title, type, category (deadline/ongoing/recurring/optional/reference, nullable), due_date_raw, due_date_resolved (ISO datetime, nullable), confidence_score (1-5), weight (float percentage, nullable), weight_confidence (1-5, nullable), dismissed (bool, default false), ai_extracted_raw, user_edited (bool), source_link (Blackboard URL), created_at, updated_at
- `notifications` table: id, deadline_id, channel (email/sms/push), trigger_offset_minutes, sent_at (nullable), status
- `user_notification_preferences`: defaults per item type, quiet hours, digest settings, weekly digest opt-in

### Phase 6.5b: Manual deadline editing

AI extraction is good but never perfect. Professors change dates after syllabi are published, Blackboard placeholders mask real deadlines (e.g., the 11:59 PM default on assignment pages when the actual deadline is during class), and the AI sometimes misreads ambiguous text. Users need a way to fix individual deadlines without re-running the entire pipeline.

**Scope:**
- Editable fields in the calendar event modal: title, date, time, type, notes
- "Reset to AI extraction" path to revert user edits
- "Add to calendar" action on needs_attention items — user clicks an item on
  the needs-attention page, gets prompted to fill in/edit the missing date
  (or confirm a low-confidence one), then the item moves to the resolved
  bucket and appears on the calendar.
- "Not a deadline" dismissal action on needs_attention items AND on calendar
  events. Dismissed items disappear from both views but are preserved in the
  data layer with a `dismissed: true` flag. A "Show dismissed" toggle lets
  users recover them.
- Edits persist across aggregator re-runs (next pipeline run respects user
  overrides, never silently overwrites). Implementation likely involves a
  separate user_overrides.json file the aggregator merges in after parsing.
- Edits include a flag distinguishing them from AI-extracted values, so the
  UI can visually indicate "this was edited by you" (e.g., small pencil icon
  next to edited fields)
- Open question for Phase 10: where overrides live (localStorage works for
  single-device single-user; production needs server-side storage tied to
  user accounts)

**Cross-reference:** Phase 6.5b is a prerequisite for Phase 7 (study tools) to be useful — incorrect AI-extracted deadlines feeding into study guides would compound errors.

### Phase 6.5c: Weight extraction and importance UI

Currently the extraction pipeline captures dates, types, and confidence —
but not assignment weight (how much each item contributes to the course
grade). Adding weight unlocks two things: (1) students can see at a glance
which deadlines are high-stakes vs. minor, and (2) Phase 7 study tools can
prioritize materials by weight ("focus my practice test on items worth
≥10% of my grade").

**Scope:**
- Update the preprocessing prompt in query.py to extract a `weight` field
  when available, expressed as a percentage. Match assignments to the
  syllabus' grading breakdown section. When weight is unclear or
  aggregated (e.g., "Essays: 30%" for three essays without per-essay
  breakdown), the AI should distribute evenly and flag with a lower
  weight_confidence score.
- Add `weight` (float, nullable) and `weight_confidence` (1-5, nullable)
  fields to each deadline in deadlines.json
- Display weight in the calendar event modal as a percentage. Add a small
  badge (⭐ or similar) for high-stakes items (threshold: ≥15% by default,
  configurable later)
- Add a "High stakes only" filter toggle to the calendar — shows only items
  with weight ≥ threshold
- When weight is null/unknown, show "Weight: unknown" in the modal. Don't
  hide the row — surfacing the uncertainty matters
- Cross-reference Phase 7: weight extraction is a prerequisite for
  weight-aware study tools

Future consideration — item categorization: beyond weight, items vary by
*kind* in ways that affect treatment. A weekly discussion isn't really a
"deadline" the same way a final exam is; ongoing participation isn't a
calendar event at all. A future phase (likely 6.5d or folded into Phase 7)
may introduce a `category` field with values like: deadline, ongoing,
recurring, optional, reference. Categories would drive differentiated UI:
ongoing items become a sidebar widget, recurring items render as repeating
calendar events, optional items get a softer visual treatment, reference
items (practice quizzes with no due date) get filtered to a separate
"Resources" tab. Deferring this until 6.5b and 6.5c land — the right
taxonomy will be clearer after seeing user behavior on the simpler
dismissal-based approach.

### Phase 6.5d: Google Calendar sync

(See description in the Phase 6.5 block above. Full subsection to be expanded
when work begins.)

### Phase 6.5e: SMS notifications

(See description in the Phase 6.5 Notification system section above. Full
subsection to be expanded when work begins.)

### Phase 6.5f: LLM-based query routing

Replace the keyword-based course matcher (`detect_courses` in query.py)
with a Haiku-based router. The matcher has a known bug where stopwords
("in", "of", "and") cause false-positive matches on courses with
prepositions in their names. More fundamentally, deterministic keyword
matching is the wrong tool for natural-language routing — the LLM does
this job better, more robustly, and handles edge cases the matcher
cannot.

**Scope:**
- New `route_question_to_courses` function in query.py that takes the
  user's question and the course map, calls Haiku 4.5 with a routing
  system prompt, and returns a list of course_ids to include in context.
- Routing prompt instructs Haiku to: identify named courses or course
  topics in the question; identify general/cross-course questions
  ("what's due", "my schedule") and return all course_ids; never return
  empty (fall back to all courses on ambiguity).
- Output format: JSON list of course_ids. Wrap in try/except — if Haiku
  fails or returns malformed output, fall back to "return all course
  IDs." Never block the main query on the router.
- Latency target: sub-500ms. Routing call sends only course names and
  question (~200 tokens input).
- Cost target: ~$0.002 per query (Haiku at $1/$5 per million tokens).
- Delete or archive the `detect_courses` keyword logic, including
  `_CROSS_COURSE_PHRASES`. Keep `fuzzy_match_titles` for now — it's used
  by `build_context` to pull full document text, which is a different
  job.
- The context bar in the chat UI continues to display whatever the
  router returned.

**Why this matters:**
- Fixes the stopword false-positive bug immediately
- Establishes the two-stage architecture (cheap router → expensive
  generator) that the rest of the product will build on
- Sets up the pattern for Phase 7 retrieval and beyond
- Haiku at $1/$5 per million tokens makes this effectively free at
  Stage 1-2 scale

**Prerequisite:** None. This can ship before document upload (Phase 7).

### Phase 7: Universal content layer

This phase merges what were previously three separate phases (document upload, embeddings-based retrieval, AI vision for scanned documents) into one coherent architectural build. They were artificially separated by *acquisition channel* (user upload vs. scraper vs. OCR) rather than by their actual function in the product. They are one capability: the system extracts text from any document a user has access to, normalizes it into a unified schema, embeds it for retrieval, and makes it available to the chat.

This is the single largest architectural build in the roadmap. It splits into four clearly-shippable stages.

#### The unified document model

Every piece of textual content in the system — regardless of where it came from — is stored as a `Document` record with the following structure:

```
{
  "id": "doc_<uuid>",
  "user_id": "...",
  "school_id": "...",
  "source_type": "blackboard_scraped" | "blackboard_ocr" | "user_upload" | "ai_generated" | "manual_entry",
  "course_id": "..." | null,           // null if the document is not tied to a specific course
  "topic": "..." | null,               // free-text topic for unaffiliated documents
  "title": "...",                      // user-facing display name
  "original_filename": "..." | null,
  "content_type": "syllabus" | "lecture_notes" | "assignment" | "exam_prep" | "homework" | "reading" | "image_text" | "other",
  "extracted_text": "...",             // canonical text content
  "extraction_method": "native_text" | "vision_ocr" | "user_typed" | "ai_generated",
  "extraction_confidence": 1-5,        // 5 = native text, 4 = high-quality OCR, lower for noisy OCR
  "original_file_path": "..." | null,  // local path or cloud storage URI
  "mime_type": "...",                  // application/pdf, image/png, etc.
  "file_size_bytes": ...,
  "thread_id": "..." | null,           // null if not tied to a specific chat thread
  "embeddings_status": "pending" | "embedded" | "failed",
  "embedding_model": "..." | null,     // which embedding model produced the vectors
  "chunk_count": ...,                  // how many chunks this document was split into
  "user_provided_metadata": {...},     // optional user tags, notes, etc.
  "created_at": "ISO 8601 with timezone",
  "updated_at": "ISO 8601 with timezone",
  "deleted_at": "ISO 8601 or null"
}
```

The corresponding `DocumentChunk` records store the chunks used for retrieval:

```
{
  "id": "chunk_<uuid>",
  "document_id": "doc_<uuid>",
  "user_id": "...",                    // denormalized for query performance
  "school_id": "...",                  // denormalized for query performance
  "chunk_index": ...,                  // 0-indexed position within the document
  "text": "...",                       // the chunk text (~500-1000 tokens)
  "embedding": [...],                  // vector representation
  "metadata": {                        // copied from parent for filtered retrieval
    "source_type": "...",
    "course_id": "..." | null,
    "content_type": "...",
    "semester_id": "..." | null
  },
  "created_at": "..."
}
```

This schema is the source of truth. The current `content_text_TIMESTAMP.json` files become a transitional input — Stage A below includes migrating their content into the unified Document model.

#### Stage A — Document ingestion pipeline (foundational build)

Build the core ingestion pipeline that all subsequent stages consume. This stage ships no user-visible features; it builds the plumbing.

**Components to build:**

1. `documents_helper.py` — CRUD helpers for the Document and DocumentChunk collections, mirroring the pattern in `chat_history_helper.py` and `overrides_helper.py`. Atomic JSON writes for the pre-Postgres period. Storage initially at `output/documents.json` and `output/document_chunks.json`.

2. `llm_adapter.py` — The provider-agnostic LLM access layer described in Architectural Principle 1. Exposes:
   - `call_fast(messages, system, max_tokens, ...)` — routes to Haiku 4.5
   - `call_main(messages, system, max_tokens, ...)` — routes to Sonnet 4.6
   - `call_vision(messages, system, max_tokens, ...)` — routes to Sonnet 4.6 with image content blocks
   - `embed(text)` — routes to the chosen embedding model (initially Voyage AI's voyage-3 or OpenAI text-embedding-3-large; the choice is a one-line config swap)

   All existing direct `anthropic.Anthropic` calls in query.py and app.py are migrated to use the adapter as part of this stage.

3. `ingestion.py` — The unified ingestion pipeline. Takes a file path or in-memory file object, detects content type, dispatches to the appropriate extractor, normalizes the result into a Document record. Extractors:
   - Native PDF text → pdfplumber or pypdf (existing reader logic)
   - Image-only PDFs → render pages to images, send to vision adapter for OCR
   - Standalone images (PNG/JPG/WebP) → vision adapter
   - Office formats (DOCX, etc.) → text extraction libraries
   - Plain text / markdown → direct read

4. `chunking.py` — Splits Document.extracted_text into ~500-1000 token chunks with ~50 token overlap. Uses a recursive character-based splitter (LangChain's RecursiveCharacterTextSplitter pattern or equivalent custom implementation — no LangChain dependency, just the algorithm).

5. `embeddings.py` — Takes a chunk's text, calls the adapter's `embed()` function, stores the resulting vector on the DocumentChunk record. Handles batching (embed multiple chunks per API call), retries with exponential backoff, and graceful failure (a chunk that fails to embed gets marked `embeddings_status: "failed"` and is excluded from retrieval rather than blocking the whole document).

6. `retrieval.py` — Given a query string and filter criteria (user_id, school_id, optionally course_id or content_type), embeds the query, performs cosine similarity search across DocumentChunk vectors, returns the top-K matches. Initially backed by an in-memory vector search using numpy (fine for thousands of chunks); migrates to pgvector in Phase 10.

7. Update `query.py` to use the new retrieval layer. The existing `build_context` function evolves: instead of dumping full course summaries, it calls `retrieval.search(query, user_id, school_id)` and assembles a context block from the returned chunks. Pre-processed course summaries from the existing pipeline become a special class of Document with `source_type: "blackboard_scraped"` and `content_type: "course_summary"`.

**What does NOT change in Stage A:**
- The web UI (no new user-facing features yet)
- The scraper, reader, or aggregator
- Chat persistence, calendar, deadlines, overrides

**Definition of done for Stage A:**
- Every existing piece of scraped course content has been migrated into the Document model
- The chat now retrieves chunks from the unified store instead of stuffing full course summaries
- Cost per query has been measured before and after; should decrease materially for users with multiple courses
- All existing tests pass; new tests added for the ingestion pipeline, chunking, retrieval

#### Stage B — User document upload (first user-facing feature)

With Stage A's infrastructure in place, Stage B exposes user-facing upload functionality.

**Components to build:**

1. Backend upload endpoint: `POST /api/documents/upload` accepts multipart file uploads, validates (size ≤ 20 MB, allowed MIME types: PDF, PNG, JPG, JPEG, WebP, DOCX, TXT, MD), saves the file to `output/uploads/<user_id>/<doc_id>/<original_filename>`, calls the ingestion pipeline from Stage A, returns the resulting Document record.

2. Backend metadata endpoint: `PATCH /api/documents/{id}` allows the user to set/update `title`, `course_id`, `topic`, `content_type`, `user_provided_metadata`.

3. Frontend upload UI in the chat: drag-and-drop zone over the input area, paperclip button next to the send button. Both trigger the same upload flow.

4. Frontend display: uploaded files appear as chips above the input bar before sending (with remove button), then as chips/thumbnails in the message bubble after sending. Failed uploads show clear error messaging.

5. Frontend document management: a `/documents` page (similar to `/calendar`) listing all the user's documents, organized by source type. User can view extracted text, edit metadata, delete documents.

6. Thread linkage: uploaded documents are linked to the thread they were uploaded in via the `thread_id` field, but they remain queryable from other threads (the user explicitly references them, or the retrieval layer surfaces them as relevant).

7. Incognito handling: documents uploaded in incognito threads are stored in-memory only, never written to disk. They are discarded when the thread ends, consistent with incognito chat behavior.

**Definition of done for Stage B:**
- User can drag-and-drop a PDF or image into chat and ask questions about it
- The uploaded document persists across page refresh (non-incognito) or is cleanly discarded (incognito)
- Documents can be tagged with a course or topic and managed from a documents page
- Retrieval surfaces uploaded content in subsequent queries when relevant
- File size, MIME type, and malicious upload protections are in place (see Architectural Principle 8 — also covered in Phase 9)

#### Stage C — OCR backfill of scraper output

The reader currently extracts text from ~300 of ~460 scraped items per semester. The remaining ~160 are scanned PDFs, image-only PDFs, embedded images, and edge formats that the native text extractors cannot handle. Stage C uses the vision extraction infrastructure from Stage A to process these items and bring them into the unified document store as first-class queryable content.

**Components to build:**

1. `ocr_backfill.py` — Iterates over scraped items flagged `image_based` or `no_text` in the reader output. For each item, fetches the source file, dispatches to the vision extractor, produces a Document record with `source_type: "blackboard_ocr"` and `extraction_method: "vision_ocr"`.

2. Integration with the existing reader pipeline: items that fail native text extraction are automatically routed to the OCR path going forward, not just on backfill. The reader's current `image_based` flag becomes a routing signal, not a dead-end.

3. Cost monitoring: OCR is the most expensive per-document operation in the system. Each backfill run logs total cost. Per-user OCR cost is tracked in the user record for future billing/quota decisions.

4. Quality validation: a sample of OCR'd documents is spot-checked manually. Extraction confidence scores are reviewed. Documents with confidence ≤ 2 are flagged for the user to review.

**Definition of done for Stage C:**
- All ~160 currently-unreadable items per semester are extracted as queryable documents
- New scraper runs automatically OCR unreadable items as part of the standard pipeline
- Per-user OCR cost is measured and falls within budget targets (see cost validation below)
- The chat can answer questions sourced from scanned textbook pages, handwritten notes uploaded by professors, etc.

#### Stage D — Multi-semester archival and retrieval

With Stages A through C complete, the unified document store contains the user's entire semester of content (scraped + uploaded + OCR'd). Stage D extends this across semesters and implements the hot/cold retrieval pattern that keeps cost flat as users accumulate history.

**Components to build:**

1. Semester scoping: every Document carries a `semester_id` field (added to the schema, backfilled from existing data using term names). The retrieval layer can filter on this field.

2. Hot/cold retrieval logic: by default, retrieval boosts current-semester chunks. Past-semester chunks are still searchable but rank lower unless the query explicitly references them ("when I took Accounting 1", "from freshman year") — signals detected by the Haiku router.

3. Per-semester archival: completed semesters can optionally be moved to "archived" status, which keeps them retrievable but excluded from default search scoring. Reduces background noise as history accumulates.

4. Cross-semester analysis features: with multi-semester data available, the chat can answer queries like "compare the workload of my finance courses across semesters" or "what topics from my Accounting 1 notes are relevant to this Advanced Accounting assignment?"

**Definition of done for Stage D:**
- A user with 4 semesters of content can ask questions across all of them
- Cost per query remains within ~$0.05-0.10 regardless of semester count
- Default queries (about current semester) are not degraded by the presence of past-semester content
- Explicit past-semester queries return relevant content

#### Cost validation across all stages

Target cost per query at scale: **$0.05-0.10** regardless of user history size.

Breakdown:
- Haiku routing call: ~$0.002
- Embedding the query: ~$0.00001
- Retrieving top-K chunks: free (local computation pre-Postgres, indexed query post-Postgres)
- Sonnet generation call with ~20K tokens of retrieved context: ~$0.06 input + variable output

At 30 queries/day per user, monthly cost is **$1.50-3.00 per user**. On a $5-8/month subscription, this leaves a 60-70% gross margin even for power users with extensive document libraries and multi-semester history.

OCR cost (Stage C) is a one-time per-document expense:
- A 20-page scanned chapter at Haiku 4.5 vision pricing: ~$0.05
- A typical semester with ~30 OCR'd items averaging 10 pages each: ~$0.75-1.50 per user per semester

This is one-time per content, not per query, and is treated as a user onboarding cost.

#### Prerequisites and ordering

- Stage A can begin immediately after this README update is committed
- Stage B depends on Stage A being functional
- Stage C depends on Stage A being functional but is independent of Stage B
- Stage D depends on multi-semester data, which means it ships after a user has actually used the product across two or more semesters — likely Stage 2 timing in the business model

Stages B and C can ship in parallel if desired. Recommended order for solo-developer time efficiency: A first, then B (user-visible value), then C (backfill what's missing), then D (long-term scaling).

### Phase 9: Operational maturity

This phase exists because production software requires engineering discipline beyond shipping features. None of the work in this phase produces a new user-visible capability. All of it is required before the system can withstand the scrutiny of a senior engineer reviewing it for technical due diligence, or before it can serve paying users reliably.

Most of this work happens in parallel with feature development from Phase 7 onward — it is documented as one phase for clarity, but the underlying tasks are distributed across the build calendar.

#### 9a. Structured logging

Replace ad-hoc `print()` calls throughout the codebase with structured logging via Python's `logging` module with a JSON formatter. Every log entry includes: timestamp, log level, module name, user_id (when known), thread_id (when relevant), operation type, duration_ms, cost_usd (for LLM calls).

Logs are written to both stdout (for live debugging) and a rotating file (`logs/app-YYYY-MM-DD.log`). In Phase 10, logs ship to a cloud logging service (Logflare, Datadog, or equivalent).

The Haiku router's existing `[router]` stderr lines become the prototype for what structured logging should look like everywhere.

#### 9b. Cost tracking

Every LLM call through the adapter logs its input tokens, output tokens, model, and computed cost in USD. Costs are aggregated per user_id per day and stored. A `/admin/costs` endpoint (or simple CLI) shows cost-per-user-per-day for the past N days.

This is the foundation for: detecting cost anomalies, validating unit economics, eventually implementing per-user quotas, and answering "is user X profitable?" before they cancel.

#### 9c. Error monitoring

Unhandled exceptions are captured and reported. In development this is a local log file. In Phase 10 production, this routes to Sentry, Honeybadger, or equivalent.

Critical paths (chat send, scraper run, reader run, document upload) have explicit try/except around external dependencies (LLM calls, file I/O, network requests) with sensible fallback behavior. The Haiku router's fail-open pattern is the reference standard.

#### 9d. Integration tests

A minimum test suite covering the critical paths:
- A scraper run on a known-good fixture produces valid output
- A reader run on a sample scraper output produces valid text extractions
- The query engine with a known input returns a coherent, sourced answer
- The Haiku router correctly routes a set of representative questions (general → all courses, specific → single course, ambiguous → all courses)
- A document upload of a sample PDF produces a valid Document record with non-empty chunks
- Chat persistence round-trips correctly (write, read, soft-delete, restore)

Tests run via `pytest` and execute against fixture data, not live APIs (LLM calls are mocked). A subset of tests can be run against live APIs in a separate `pytest -m live` invocation for periodic validation.

#### 9e. Security hardening

- All user input (chat messages, document filenames, metadata fields) is validated and sanitized before storage or inclusion in prompts
- Uploaded files are scanned for known malicious patterns (in production: integrate a service like ClamAV or VirusTotal)
- File uploads are stored outside the web-accessible directory
- API key handling is audited: nothing is logged, nothing is committed, environment variables only (already in place)
- In Phase 10, all production endpoints require auth; rate limiting is applied per user

#### 9f. Performance budgets

Documented performance targets, monitored automatically:
- Chat response: first token within 3 seconds, full response within 30 seconds at p95
- Document upload (PDF, ≤ 5 MB): full ingestion within 10 seconds at p95
- Calendar load: within 1 second at p95
- Page refresh: within 2 seconds at p95

Anything that exceeds budget is investigated. Budgets get tightened over time as the system matures.

#### 9g. Documentation

- A `CONTRIBUTING.md` describing how to set up a development environment
- A `docs/architecture.md` expanding on the architectural principles documented in this README
- A `docs/runbook.md` describing how to respond to common production issues (scraper breaking, LLM provider outage, cost anomaly, etc.)
- API endpoints documented (Swagger/OpenAPI generated from FastAPI, served at `/docs`)

#### Definition of done for Phase 9

The system is reviewable by an experienced software engineer who can:
1. Find the answer to "why does this code do X?" in either the code or the docs
2. Reproduce a production issue from a structured log entry
3. Estimate the per-user cost of the product
4. Run the test suite and see it pass
5. Identify the security and operational gaps that remain (this is the honest list maintained in the README's "Current limitations" section, not hidden)

### Phase 9.5: Announcement & message scraping
- Course announcements pulled into the deadline extractor (professors often announce deadline changes here)
- Direct messages parsed for actionable items
- When announcement scraping is added, the calendar modal's "As written" text should broaden its source labeling — announcements become a third legitimate source of raw deadline text alongside syllabi and Blackboard assignment pages.

### Phase 10: Production web product
- Next.js frontend, FastAPI backend, PostgreSQL
- Stripe subscription billing
- Auth (probably Clerk or Auth.js)
- Cloud-hosted scraper (users authenticate via Blackboard credentials, scraper runs server-side)
- Mobile-responsive / PWA
- Push notifications via service worker
- **Structured query routing in chat:** Refactor chat so deadline-shaped
  questions ("what's due tomorrow", "when is my Ethics final", "show me all
  my exams") query the deadlines database directly and short-circuit the LLM.
  Free-form questions ("explain photosynthesis", "summarize chapter 7") still
  go through the LLM as today. Benefits: faster responses (no LLM round-trip
  for structured questions), lower API costs at scale, and structured
  questions become deterministic — no chance of the LLM hallucinating a date.
  Prerequisite: the Postgres-backed deadlines table from this phase. Until
  then, Phase 6.5b's chat sync (markdown rewriting with user overrides applied
  in memory before sending to the LLM) handles user edits correctly across
  any LLM choice.

**Storage migration:**

Phase 10 is when the storage abstraction (Architectural Principle 2) is exercised end-to-end. The storage backend swaps from JSON files to PostgreSQL with the following table structure:

- `users` (id, email, school_id, microsoft_oauth_id, google_oauth_id, created_at, updated_at, last_login_at, ...)
- `schools` (id, name, domain, scraper_config_json, created_at, ...)
- `threads` (existing fields from chat_history_helper)
- `messages` (existing fields, with thread_id foreign key)
- `documents` (the unified Document model from Phase 7)
- `document_chunks` (with pgvector for the embedding column)
- `deadlines` (existing fields)
- `user_overrides` (existing fields)
- `class_meetings`, `final_exams`, `courses`, `semesters`, `class_schedules`
- `notifications`, `notification_preferences`
- `cost_ledger` (per-call cost tracking from Phase 9b)
- `audit_log` (significant user actions for compliance and debugging)

All helpers (chat_history_helper, overrides_helper, documents_helper, etc.) get their storage backends swapped to a Postgres implementation. Call sites do not change.

**Authentication:**

Microsoft OAuth (primary, for institutional accounts) and Google OAuth (secondary, for personal/Google Drive integration). No email/password. School affiliation is derived from the email domain at sign-in time and used to route the user to the correct per-school knowledge cache.

**Deployment:**

- Application server: a managed service (Render, Railway, or Fly.io for early production; AWS or GCP for scale)
- Database: managed Postgres with pgvector extension (Supabase, Neon, or AWS RDS)
- File storage: S3-compatible object storage (Cloudflare R2 or AWS S3)
- Logs: structured JSON shipped to a logging service (Logflare or Datadog)
- Errors: Sentry or equivalent
- Secrets: managed by the platform; never in code, never in environment variables checked into git

### Phase 11: Multi-school support
- Generalize scraper across Blackboard Ultra deployments at different universities
- Per-school config layer (different login flows, term naming, course structures)
- Begin testing with one additional university
- Internal tool for auto-discovering each school's academic calendar (term dates, midterm/final windows, breaks) from registrar pages — speeds up per-school onboarding from manual research to point-and-scrape.
- Screenshot/schedule parser tested across multiple SIS platforms (Banner, PeopleSoft, Workday Student) to ensure the onboarding flow works regardless of which system the school uses.

### Phase 12: Privacy, legal, launch
- Privacy policy, ToS
- Data retention / deletion controls
- FERPA review where applicable
- Launch marketing

---

## Future plans

This section captures product strategy and business direction beyond the technical roadmap above. Where the "Full roadmap" lists what to build, this section addresses why, who pays, and how the product becomes defensible.

### Product positioning

The product is NOT an AI study tool, an AI chatbot, or a calendar app. Positioning it that way puts it in a saturated market with no edge. The product is "the thing that removes the cognitive load of being a student" — peace of mind during a semester. The AI is invisible plumbing. Every piece of marketing, every pitch, every feature decision should reinforce this framing.

The differentiator is that the product is personalized to a student's actual enrolled courses without the student manually uploading anything. ChatGPT, Quizlet, Chegg, Notion AI — none of them connect to Blackboard. None of them know what's in this semester's courses. That gap is the wedge.

### What people actually pay for

The right question is not "why would a student pay for information they already have access to?" The right question is "what painful task does this product remove from a student's week?" Identified painful tasks the product addresses:

- "What's due this week and how should I prioritize?" — currently solved by mental load and out-of-date spreadsheets
- "I have an exam in 6 days, what do I study?" — currently solved by 3 hours of scrolling through PDFs and a syllabus
- "I missed class last week, how do I catch up?" — currently solved by texting friends and office hours
- "I'm in 5 classes and have no idea which deadlines are coming" — solved by the existing calendar
- "I'm bad at studying and want a study plan" — currently solved by writing one Sunday and abandoning it Tuesday

These are the features that drive subscription willingness, not the AI itself.

### Moats and defensibility

The scraper is not a moat. Anyone competent can replicate it in a weekend. Real defensibility comes from three layers, in priority order:

1. **Distribution.** Word-of-mouth + visible-on-campus presence at Fordham. Once 40% of students at one school use it, no competitor can dislodge through technology alone.
2. **Per-school data network effects.** Every user who edits a deadline, dismisses an item, or confirms an AI extraction teaches the system. Across thousands of users at one school, the corrected ground-truth dataset for every course becomes a moat that compounds. New users get a vastly better day-one experience. A competitor showing up two years later starts with zero of that data.
3. **Workflow lock-in.** Google Calendar sync, SMS notifications, study guides built from a student's own materials, daily digests — the deeper the integration into a study routine, the higher the switching cost.

The scraper is plumbing. The product layer (calendar, chat, notifications, study tools, document generation) is what users actually pay for.

### Business model — staged plan

**Stage 1 — Free Fordham beta (months 0–6).** Goal: 200 weekly active Fordham users. Free, no payment. Validation over revenue. Two parallel build priorities: features that drive daily use (calendar, SMS, weekly digest, AI chat, document upload), and features that drive viral growth (one-tap share, social proof, referral mechanics). Distribution is the slow part — start building it before there is anything to distribute.

**Stage 2 — Monetize Fordham (months 6–12).** Subscription at $5–8/month with a generous free tier. Free tier: calendar, basic chat, weekly digest. Paid tier: SMS notifications, AI study guides, practice tests, document export, Google Calendar sync, unlimited chat. Student-friendly pricing loops: free during summer, $40 annual option, refer-three-friends-get-a-month, free first month of each semester. Target 15% conversion. At 1,000 Fordham users, that is $750/month — not life-changing, but proves unit economics.

**Stage 3 — Sanctioned expansion (year 2).** With 1,000+ Fordham users and real engagement data, the conversation with Fordham IT changes from "asking permission to scrape" to "asking partnership on a tool your students already use." Migrate to Blackboard Learn REST API at this point — sanctioned access, kills ToS risk, replaces the entire scraper layer with cleaner code. In parallel, cold-email peer universities with the Fordham case study.

**Stage 4 — Institutional revenue (year 3+).** B2B2C model. Universities pay $2–3/student/year to provide the tool as a sanctioned campus resource. 50 universities at 10,000 students each = $1–1.5M ARR. Universities have budgets for "student success tools." This path also eliminates the largest legal risk (the gray-zone scraper concern) and changes the investor pitch from "consumer app at universities" to "edtech with institutional revenue."

### Per-school knowledge layer

Data collection should be scoped per school. When a student authenticates against Fordham's Blackboard, the resulting corrections, dismissals, manual edits, and confirmed deadlines pool into a Fordham-specific knowledge cache. Anonymized aggressively — no user-identifying data crosses the cache boundary. A new Fordham user benefits from every prior Fordham user's corrections. A new Boston College user starts a fresh cache.

This is the technical implementation of the data network effect moat. Worth designing the data model with this from day one, even if Stage 1 only has one school.

The per-school correction dataset also accumulates over time and benefits from the same retrieval architecture as personal history. As the Fordham cache grows — thousands of corrected deadlines, confirmed weights, dismissed false positives — the volume eventually exceeds what can be embedded in a system prompt. Phase 7's retrieval layer applies here too: at query time, pull the top-K most relevant per-school corrections for the current question rather than dumping the entire cache. The data moat compounds across both axes: individual user history and the per-school corrected dataset, and both require the same infrastructure to remain economically viable at scale.

### Authentication and account creation

Account creation via Microsoft and Google OAuth, not email/password.

- **Microsoft sign-in is the primary path** for most users. Universities (including Fordham) overwhelmingly use Microsoft 365 for student accounts. Sign-in-with-Microsoft means students use their `@fordham.edu` Microsoft account, which gives lowest-friction onboarding, verified institutional affiliation, and sets up OneDrive integration in the same OAuth flow.
- **Google sign-in is secondary** — used for personal accounts and Google Drive integration.

The school the user belongs to is inferred from their email domain at sign-in time and used to route them to the correct per-school knowledge cache and scraper config.

### Document handling

**Input:** Students drag-and-drop documents, images, screenshots, and PDFs into chat for AI analysis. Use cases: handwritten notes from a missed class, a homework PDF, a syllabus from a course not in Blackboard, a photo of a whiteboard. This is high perceived value, low build cost — should be prioritized earlier in the roadmap than its current Phase 7 placement.

**Output:** Chat responses can be exported as downloadable files. Priority order:

1. Word (.docx) and PDF — covers most study guide and summary use cases
2. Excel (.xlsx) — for assignment trackers, study schedules, grade calculators
3. Cloud sync — push directly to the user's OneDrive (if Microsoft sign-in) or Google Drive (if Google sign-in), one OAuth scope already granted at account creation
4. PowerPoint (.pptx) — lower priority; students rarely need AI-generated slides

Each feature is incrementally useful — ship Word + PDF first, layer in the rest as demand justifies.

### What NOT to do

- Don't position the product as an AI tool. The category is saturated and the framing puts the product in competition with ChatGPT, which it will lose.
- Don't try to build all of this at once. Stage 1 means picking the 3 features that matter most for a free Fordham beta and ruthlessly deferring everything else. Suggested Stage 1 priority: Microsoft auth → document upload → document export. Everything else waits.
- Don't ship to other schools until Fordham works. Multi-school is a distraction until single-school is proven.
- Don't build the scraper for parallelization or scale before deciding on the sanctioned-API path. Parallelization increases detection risk. Speed gains aren't worth it if the long-term plan is to delete the scraper.
- Don't treat the scraper as the product. It is data acquisition. The product is everything built on top of it.
- Don't try to scale the AI chat to multi-semester users with the current "send full course summaries" architecture. By the user's second or third semester this becomes economically unworkable ($25+/month in API fees on a $7 subscription). Phase 7 (universal content layer with embeddings retrieval) is the architectural answer and must be in place before users start accumulating semesters at scale.

### The Fordham IT email

Send this earlier rather than later, even before there's anything to demo. The conversation timeline at universities is weeks to months. Running that conversation in parallel with development costs nothing and unlocks the Stage 3 path. Initial framing: a student-built study tool seeking sanctioned access via the Blackboard Learn REST API, with a clear story about good-faith development on the student's own account and a desire to do this the right way as the tool grows.

---

## Important constraints

- GitHub repo is **PRIVATE** — keep it that way
- **Never** store Blackboard passwords — manual login only
- API keys **never** in code — environment variables only
- All code changes delivered as Claude Code prompts for pasting
- Short, targeted Claude Code prompts preferred — long ones cause debug cycles
- `output/` is in `.gitignore`, never pushed

---

## Current limitations and honest assessment

This section documents the gaps between the current state of the codebase and the architectural principles above. It exists because experienced engineers reviewing a system can spot gaps faster than they can be hidden, and an honest accounting of what is and is not yet built is more credible than a polished facade.

### What is solid today

- Scraper produces reliable course content extraction across multiple Blackboard Ultra deployments
- Reader achieves ~95% text extraction success rate on items it attempts (270+ of 286 attempted)
- Query engine produces accurate, well-sourced answers when given correctly-routed context
- Chat persistence with threaded UI is feature-complete and matches industry conventions (Claude.ai, ChatGPT)
- Deadline extraction, calendar, and user override system work end-to-end
- Haiku-based query routing has eliminated the entire class of false-positive course matches caused by the prior keyword matcher

### What is in flight or pending

- The Universal Content Layer (Phase 7) is the next major build. Document upload, OCR backfill, and embeddings retrieval are all components of this single architectural effort.
- Structured logging, cost tracking, and integration tests (Phase 9) are partially in place but not systematic. Most operations still use `print()` instead of structured logging.
- The LLM adapter pattern (Architectural Principle 1) is not yet implemented. Current code makes direct calls to the Anthropic SDK in multiple places. Phase 7 Stage A includes this refactor.
- Multi-tenancy (Architectural Principle 3) is partially implemented — `user_id` and `school_id` fields exist in some records (chat threads) but not others (deadlines, overrides). Phase 7 will normalize this.
- Storage abstraction (Architectural Principle 2) is informal — helpers exist but they are not behind an interface that would allow seamless swap to Postgres. Phase 10 will formalize this.

### Known weaknesses being addressed

- **Test coverage is zero.** No automated tests exist. Phase 9d makes this systematic.
- **No structured logging.** `print()` and `print(..., file=sys.stderr)` are used throughout. Phase 9a replaces this.
- **No cost tracking.** Per-query cost is estimated theoretically, not measured. Phase 9b instruments this.
- **No error monitoring.** Unhandled exceptions crash the relevant request but are not aggregated or reported. Phase 9c addresses this.
- **No security audit.** Inputs are not systematically sanitized; uploaded files (when implemented) need malicious-file scanning. Phase 9e addresses this before any external user has access.
- **Three known scraper bugs require a second Blackboard account to test** (Social Psych popover, British Government scroll, International Internship container bleed). These are minor and isolated; the scraper handles the majority of cases reliably.

### What this codebase is NOT yet

This is honest about the gap to production-ready:
- This is not a multi-user system. There is no auth. There is no session isolation. Multi-tenancy fields exist for forward compatibility, but they are not enforced anywhere.
- This is not a hosted product. It runs locally only. The web UI is intended for a single developer on a single machine.
- This is not a sanctioned Blackboard integration. The scraper operates on the user's own credentials via browser automation. Phase 11 plans the migration to the Blackboard Learn REST API once institutional partnership is in place.
- This has not undergone external security review.
- This has not been load-tested.

### How this maps to the business model stages

- **Stage 1 (free Fordham beta, months 0-6):** Phases 7, 9, and a subset of Phase 10 must be complete. The system must be hosted, authenticated, observable, and cost-tracked. Phase 11 is not yet required.
- **Stage 2 (Fordham subscription, months 6-12):** Phases 10 and 11 must be complete enough to support paying users at the scale of low-thousands. Phase 12 (privacy/legal) is required for actual billing.
- **Stage 3 (sanctioned expansion, year 2):** The migration from scraper to Blackboard Learn REST API happens here. The unified content schema is what makes this migration straightforward — the API becomes another `source_type` for Documents, not a replacement architecture.
- **Stage 4 (institutional revenue, year 3+):** The architectural foundations laid in Phases 7, 9, and 10 are what make this stage technically possible. Without them, an institutional partner would not (and should not) trust the system with their students' data.

The work documented in this README is the work that converts a clever prototype into a system that can carry an institutional contract.
