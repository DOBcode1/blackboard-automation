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

**Calendar features:**
- In-app calendar view at `/calendar`, color-coded by course
- AI-driven additions via chat:
  - "Add my Social Psych midterm to my calendar" → AI confirms extracted date/time, adds event
  - "What do I have due in the next two weeks?" → AI answers from cache, then offers: *"Want me to add these to your calendar?"*
  - "Add all my Brit Gov assignments to my calendar" → bulk-add with confirmation summary
- Manual editing of calendar entries (professors change dates, AI misreads occasionally) — original AI-extracted value preserved as fallback
- Confidence threshold: low-confidence dates ("end of week 4", "before spring break") flagged for user confirmation before adding silently
- Recurring events for weekly assignments, labs, discussion posts

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

**Data model sketch:**
- `deadlines` table: id, user_id, course_id, source_item_id, title, due_at, confidence_score, ai_extracted_raw, user_edited (bool), created_at, updated_at
- `notifications` table: id, deadline_id, channel (email/sms/push), trigger_offset_minutes, sent_at (nullable), status
- `user_notification_preferences`: defaults per item type, quiet hours, digest settings

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
