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

## Known scraper bugs

Require another Blackboard account to test — NOT available right now.

- **Social Psychology popover** — MUI Popover context menu intercepts folder expansion clicks, ~55 items missed
- **British Government last 3 items** — scroll-based lazy loading doesn't reach very bottom
- **International Internship container bleed** — Learning Modules with non-standard DOM not detected by `containerMap`

---

## Things to work on now (no second account needed)

- Parallel reader for speed (3–4 simultaneous tabs, cut 60 min to 20–30 min) — **HIGHEST PRIORITY**
- Tighten course matching (currently over-inclusive — "Business" matches multiple courses)
- Polish web UI (progress bar, Sync Courses button, dark mode)
- Document export (download AI responses as Word docs)
- Document upload (drag-and-drop files into chat for AI analysis)
- Term selector (make `TERM_FILTER` configurable in UI)
- Test edge cases on own account (incremental runs, headless mode)

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

**Google Calendar sync (Phase 6.5b):**
- OAuth flow on first opt-in
- Two-way sync: events added in-app push to Google Calendar; changes made in Google Calendar pull back
- User can choose which courses sync (e.g., sync academic courses, skip electives)
- Single source of truth = in-app DB; Google Calendar is a mirror

**Notification system:**
- Per-event notification preferences (24h before, 1 week before, day-of, custom)
- Global defaults the user sets once ("always notify me 48h before any midterm/final")
- **Delivery channels:**
  - **Email** (Phase 6.5 launch): SendGrid or Resend, branded templates
  - **SMS** (Phase 6.5b): Twilio integration, user verifies phone number on signup, opt-in per event type, daily digest option to avoid spam
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

### Phase 7: Study tools
- Study guides generated from course materials
- Practice tests with AI-generated questions
- Document upload (drag-and-drop files into chat for analysis)
- Document export (download AI responses as Word docs)

### Phase 8: AI vision for scanned documents
- OCR / vision model pass on `image_based` and scanned-PDF items
- Re-runs through pre-processing pipeline so scanned content is queryable

### Phase 9: Announcement & message scraping
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

## Important constraints

- GitHub repo is **PRIVATE** — keep it that way
- **Never** store Blackboard passwords — manual login only
- API keys **never** in code — environment variables only
- All code changes delivered as Claude Code prompts for pasting
- Short, targeted Claude Code prompts preferred — long ones cause debug cycles
- `output/` is in `.gitignore`, never pushed
