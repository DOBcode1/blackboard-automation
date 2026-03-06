"""
blackboard/scraper.py

Blackboard Ultra course content extractor using Playwright (sync API).
Extracts Spring 2026 course content objects and saves them to
output/content_objects_<timestamp>.json.

Phase 1: Course discovery + virtualization scroll harvesting (unchanged).
Phase 2: All course items are captured as structured content objects with
         course name, course id, container name, title, content type, url,
         and due date. Container assignment uses sequential sibling tracking
         (Learning Module / Folder items seen first in DOM order set the
         container for items that follow them). No filtering applied.

SELECTOR NOTES:
  Selectors marked [STABLE] rely on href patterns or ARIA labels.
  These are unlikely to break between Blackboard SaaS updates.

  Selectors marked [FRAGILE] rely on class names that may change.
  Check these first if the scraper stops finding elements after a Blackboard update.
  All selector constants are defined at the top of this file for easy adjustment.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_URL = "https://fordham.blackboard.com"
LOGIN_URL = f"{BASE_URL}/"
COURSES_PAGE_URL = f"{BASE_URL}/ultra/course"
TERM_FILTER = "Spring 2026"
LOGIN_TIMEOUT_SECONDS = 600     # 10 minutes for manual login
PAGE_LOAD_TIMEOUT_MS = 15_000   # 15s for SPA content to appear
POLL_INTERVAL_SECONDS = 2


# ---------------------------------------------------------------------------
# SELECTORS — adjust these if the scraper stops finding elements
# ---------------------------------------------------------------------------

# [STABLE] Course card links — Ultra course URLs always follow this pattern
COURSE_LINK_SELECTOR = "a[href*='/ultra/courses/']"

# [FRAGILE] Term section container — uses :has-text() with a class pattern
# If this returns 0 courses, inspect the institution page in DevTools and update
TERM_SECTION_SELECTOR = f"[class*='term']:has-text('{TERM_FILTER}')"

# [STABLE] Assignment items by ARIA label (case-insensitive)
ASSIGNMENT_ARIA_SELECTOR = "[aria-label*='assignment' i]"

# [STABLE] Assignment items by href pattern
ASSIGNMENT_LINK_SELECTOR = "a[href*='/assignment/'], a[href*='/assessments/']"

# [STABLE] Due date as a <time datetime="..."> element — gives ISO 8601 directly
DUE_DATE_TIME_ELEMENT_SELECTOR = "time[datetime]"

# [FRAGILE] Due date by class name — adjust after inspecting live DOM if needed
DUE_DATE_CLASS_SELECTOR = "[class*='due-date'], [class*='dueDate']"

# [STABLE] Confirm course outline has loaded
OUTLINE_LOADED_SELECTOR = "main, [role='main'], [class*='outline']"

# Content types that represent actionable graded work.
# Only items whose svg[aria-label] matches one of these will be extracted.
# Update this set if Blackboard introduces new graded item types.
ACTIONABLE_TYPES = {"Assignment", "Test", "Quiz", "Discussion"}


# ---------------------------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------------------------

class BlackboardScraper:

    def __init__(self):
        self.base_url = BASE_URL
        self._debug_course_saved = False
        self.results = {
            "extracted_at": "",
            "term": TERM_FILTER,
            "courses": [],
            "total_content_objects": 0,
        }

    def run(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()

            try:
                self._login(page)

                print(f"\nNavigating to courses page...")
                page.goto(COURSES_PAGE_URL, wait_until="networkidle")

                # Gate: block until the Spring 2026 term group is in the DOM.
                # This ensures React has rendered term cards before any scrolling.
                page.wait_for_selector(
                    f".course-card-term-name:has-text('{TERM_FILTER}')",
                    timeout=PAGE_LOAD_TIMEOUT_MS,
                )
                print(f"[OK] '{TERM_FILTER}' term group detected.")

                # Harvested during scrolling: id -> name (dict keys are the dedup set).
                # setdefault keeps the first-seen name for each ID.
                course_snapshot: dict[str, str] = {}
                prev_height   = -1
                prev_count    = -1
                stable_rounds = 0

                while True:
                    # Advance incrementally so each virtualized window mounts.
                    scroll_result = page.evaluate("""
() => {
    const c = document.querySelector('#main-content-inner');
    if (c) c.scrollTop = Math.min(c.scrollTop + 600, c.scrollHeight);
    return c ? {
        scrollHeight: c.scrollHeight,
        atBottom: c.scrollTop + c.clientHeight >= c.scrollHeight
    } : { scrollHeight: 0, atBottom: true };
}
""")
                    current_height = scroll_result['scrollHeight']
                    at_bottom      = scroll_result['atBottom']
                    time.sleep(0.5)  # allow React to mount newly visible items

                    # Harvest whatever is currently mounted; names captured here
                    # so we never need a post-scroll DOM re-query.
                    harvested = page.evaluate("""
() => Array.from(document.querySelectorAll('article[data-course-id]')).map(card => {
    const id   = card.getAttribute('data-course-id') || '';
    const h4   = card.querySelector('h4.js-course-title-element');
    return { id, name: (h4 ? h4.textContent.trim() : '') || id };
})
""")
                    for item in harvested:
                        if item['id']:
                            course_snapshot.setdefault(item['id'], item['name'])

                    height_stable = current_height == prev_height
                    ids_stable    = len(course_snapshot) == prev_count

                    if height_stable and ids_stable and at_bottom:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0

                    prev_height = current_height
                    prev_count  = len(course_snapshot)

                    if stable_rounds >= 3:
                        break

                print(f"[DEBUG] Total unique course IDs collected during scroll: {len(course_snapshot)}")

                spring_courses = [
                    {"course_id": cid, "course_name": cname}
                    for cid, cname in course_snapshot.items()
                    if cname and TERM_FILTER in cname
                ]

                course_links = [
                    {
                        "course_id": c["course_id"],
                        "course_name": c["course_name"],
                        "url": f"{BASE_URL}/ultra/courses/{c['course_id']}/cl/outline"
                    }
                    for c in spring_courses
                ]

                print(f"Found {len(course_links)} {TERM_FILTER} course(s).\n")

                if not course_links:
                    print(f"[WARN] No '{TERM_FILTER}' courses found. "
                          "The term section selector may need adjustment — "
                          "inspect the institution page in DevTools and update "
                          "TERM_SECTION_SELECTOR in scraper.py.")
                    return

                self.results["extracted_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

                for course_info in course_links:
                    course_data = self._scrape_course(page, course_info)
                    self.results["courses"].append(course_data)
                    self.results["total_content_objects"] += course_data["item_count"]

                output_path = self._write_output()
                print(f"\nDone. {self.results['total_content_objects']} content object(s) "
                      f"saved to: {output_path}")

            finally:
                browser.close()

    # -----------------------------------------------------------------------
    # LOGIN
    # -----------------------------------------------------------------------

    def _login(self, page: Page):
        """Navigate to the login page and wait for manual SSO completion."""
        print(f"Opening {LOGIN_URL} ...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Fast-path: session cookie still active, already on /ultra/
        if "/ultra/" in page.url:
            print("[OK] Already logged in.")
            return

        print(f"Waiting for manual login (up to {LOGIN_TIMEOUT_SECONDS}s)...")
        print("Please complete the login process in the browser window.\n")

        try:
            page.wait_for_url("**/ultra/**", timeout=LOGIN_TIMEOUT_SECONDS * 1000)
            print("[OK] Login detected.")
            page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError:
            raise TimeoutError(
                f"Login not detected within {LOGIN_TIMEOUT_SECONDS}s. "
                "Ensure you completed the login in the browser window."
            )

    # -----------------------------------------------------------------------
    # COURSE DISCOVERY
    # -----------------------------------------------------------------------

    def _get_spring_2026_course_links(self, page: Page) -> list[dict]:
        """
        Find Spring 2026 courses by scanning all article[data-course-id] elements
        and filtering by TERM_FILTER in JS.
        """
        try:
            page.wait_for_selector("article[data-course-id]",
                                   timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[WARN] Course list did not load. Saving debug HTML.")
            self._save_debug_html(page, "courses_page")
            return []

        time.sleep(2)

        # Force hydration of lazy-rendered course cards
        page.evaluate("""
() => {
    const cards = document.querySelectorAll('article[data-course-id]');
    cards.forEach(card => {
        card.scrollIntoView({ block: 'center' });
    });
}
""")
        time.sleep(2)

        articles_data = page.evaluate("""
() => {
    return Array.from(document.querySelectorAll('article[data-course-id]'))
        .map(card => {
            const id = card.getAttribute('data-course-id') || '';
            const h4 = card.querySelector('h4.js-course-title-element');
            const name = h4 ? h4.textContent.trim() : id;
            return { course_id: id, course_name: name || id };
        })
        .filter(c => c.course_id);
}
""")

        print(f"[DEBUG] Total article[data-course-id] found (no term filter): {len(articles_data)}")
        for i, c in enumerate(articles_data):
            print(f"[DEBUG]   [{i}] {c['course_name']}")

        return [
            {
                "course_id":   ad["course_id"],
                "course_name": ad["course_name"],
                "url":         f"{BASE_URL}/ultra/courses/{ad['course_id']}/cl/outline",
            }
            for ad in articles_data
        ]

    def _parse_course_link(self, link) -> dict | None:
        href = link.get_attribute("href") or ""
        course_id = self._extract_course_id(href)
        if not course_id:
            return None
        name = link.inner_text().strip() or course_id
        url = f"{BASE_URL}{href}" if href.startswith("/") else href
        return {"course_id": course_id, "course_name": name, "url": url}

    def _extract_course_id(self, href: str) -> str | None:
        match = re.search(r"/ultra/courses/([^/]+)/", href)
        return match.group(1) if match else None

    # -----------------------------------------------------------------------
    # COURSE SCRAPING
    # -----------------------------------------------------------------------

    def _scrape_course(self, page: Page, course_info: dict) -> dict:
        name = course_info["course_name"]
        course_id = course_info["course_id"]

        # Course Content tab — correct URL confirmed from live DOM
        content_url = f"{BASE_URL}/ultra/courses/{course_id}/outline"
        print(f"  Scraping: {name}")

        course_data = {
            "course_id": course_id,
            "course_name": name,
            "course_url": content_url,
            "content_objects": [],
            "item_count": 0,
        }

        try:
            self._open_course_outline(page, content_url)
            self._load_all_hidden_items(page)
            self._expand_all_modules(page, content_url)
            self._stabilize_course_page(page)

            # Save post-expansion HTML for the first course to aid debugging
            if not self._debug_course_saved:
                self._save_debug_html(page, "course_content_post_expand")
                self._debug_course_saved = True

            print(f"    [DEBUG] URL after expand:  {page.url}", flush=True)

            raw_items = self._extract_modules_and_items(page)
            content_objects = self._build_content_objects(course_info, raw_items)
            course_data["content_objects"] = content_objects
            course_data["item_count"] = len(content_objects)

            print(f"    {len(content_objects)} content object(s) captured.")

        except Exception as e:
            print(f"    [ERROR] {e}")

        return course_data

    # -----------------------------------------------------------------------
    # COURSE PAGE HELPERS
    # -----------------------------------------------------------------------

    def _open_course_outline(self, page: Page, course_url: str):
        """Navigate to the course outline URL and wait for the content list to render."""
        page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)

        # Wait for the React content list to render inside the Angular view.
        # [ui-view="course@"] * fires too early (Angular mounts before React renders
        # the content list). Waiting for div.content-list-item is more reliable.
        try:
            page.wait_for_selector('div.content-list-item', timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # Course may genuinely have no content items — proceed and scan anyway
            print(f"    [INFO] No content-list-item appeared within timeout (empty course?).")

        print(f"    [DEBUG] URL before expand: {page.url}", flush=True)

    def _load_all_hidden_items(self, page: Page):
        """
        Scroll to trigger lazy rendering, then click all 'Load more' buttons
        until no enabled ones remain.

        An initial wheel scroll is needed to force React to mount items that
        are below the visible fold before the load-more buttons appear.
        Clicks are serialized (one at a time) because Angular re-renders the
        list after each click, which invalidates pre-collected locators.
        """
        for _ in range(15):
            page.mouse.wheel(0, 500)
            time.sleep(0.2)

        for _ in range(30):
            load_btns = page.locator('button[data-analytics-id*="loadMoreButton"]:not([disabled])')
            if load_btns.count() == 0:
                break
            load_btns.first.click(force=True)
            time.sleep(1.5)

    def _expand_all_modules(self, page: Page, course_url: str):
        """
        Click every collapsed Learning Module or Folder toggle one at a time until none remain.

        Re-queries the DOM before each click so stale locators are never used.
        If a click accidentally navigates away from the outline, the method
        returns to the course URL and stops expanding to avoid an infinite loop.

        NOTE: The folder toggle selector follows the same naming convention as
        the learning module toggle. Verify against a live course with Folders
        in DevTools if it stops working after a Blackboard update.
        """
        # Select any collapsed toggle button regardless of data-analytics-id.
        # aria-expanded="false" is the canonical indicator that a container is
        # collapsed in Blackboard Ultra, covering both Learning Modules and Folders.
        COLLAPSED_TOGGLE_SELECTOR = 'button[aria-expanded="false"]'

        for attempt in range(30):  # higher cap for deeply nested modules
            collapsed = page.locator(COLLAPSED_TOGGLE_SELECTOR)
            count = collapsed.count()
            if count == 0:
                break
            print(f"    [DEBUG] Expanding {count} collapsed module/folder toggle(s), attempt {attempt + 1}...", flush=True)
            try:
                collapsed.first.click()
                time.sleep(1)
            except Exception as e:
                print(f"    [WARN] Could not click module/folder toggle: {e}", flush=True)
                break

            # Safety: if the click navigated away, go back to course outline
            if course_url and not page.url.rstrip("/").endswith("/outline"):
                print(f"    [WARN] Navigation detected during expansion ({page.url}), returning to outline...", flush=True)
                page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_selector('[ui-view="course@"] *', timeout=PAGE_LOAD_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    pass
                break  # Stop expansion after recovery to avoid loop

    def _stabilize_course_page(self, page: Page):
        """
        Scroll the main content area repeatedly until its scrollHeight stops
        growing, then confirm the content list is present and log its item count.

        Two consecutive rounds with the same height are required before stopping,
        to guard against Blackboard's deferred rendering of module children.
        """
        print("    [DEBUG] Scrolling to stabilize page height...", flush=True)
        prev_height = -1
        stable_count = 0
        for i in range(20):
            page.evaluate("""
() => {
    const main = document.querySelector('[role="main"]') || document.querySelector('main');
    if (main) {
        main.scrollBy(0, 1000);
    }
}
""")
            time.sleep(1.5)
            height = page.evaluate("""
() => {
    const main = document.querySelector('[role="main"]') || document.querySelector('main');
    return main ? main.scrollHeight : document.body.scrollHeight;
}
""")
            print(f"    [SCROLL] iter={i} prev_height={prev_height} height={height} stable_count={stable_count}", flush=True)
            if height == prev_height:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
            prev_height = height

        try:
            page.wait_for_selector("div.content-list-item", timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        total = page.locator("div.content-list-item").count()
        print(f"    [STABILIZED] div.content-list-item total: {total}", flush=True)

    def _extract_modules_and_items(self, page: Page) -> list[dict]:
        """
        Single JS round-trip that walks the content tree in visual order and
        returns raw data (content type, title, href, due-date datetime).

        Traversal: starts at the top-level .content-list, iterates direct
        children (:scope > .content-list-item), then recurses into any nested
        .content-list before moving to the next sibling.  This preserves the
        visual hierarchy so that sequential container tracking in
        _build_content_objects assigns container_name correctly.

        Confirmed DOM structure (from live Blackboard Ultra DOM):
          div.content-list-item              — one per content item [STABLE]
          svg[aria-label="..."]              — icon identifying content type [STABLE]
          a[data-analytics-id*="assessment"] — title link for graded items [STABLE]
          a[class*="contentItemTitle"]       — title fallback [FRAGILE — hashed class]
        """
        raw_items: list[dict] = page.evaluate("""() => {
            function extractItem(item) {
                // content_type from svg[aria-label] [STABLE]
                const svg = item.querySelector('svg[aria-label]');
                const content_type = svg ? (svg.getAttribute('aria-label') || '') : '';

                // title + href: assessment link [STABLE] → contentItemTitle [FRAGILE] → generic
                let title = '';
                let href  = '';

                const assessmentLink = item.querySelector('a[data-analytics-id*="assessment"]');
                if (assessmentLink) {
                    title = (assessmentLink.textContent || '').trim();
                    href  = assessmentLink.getAttribute('href') || '';
                }

                if (!title) {
                    const titleLink = item.querySelector('a[class*="contentItemTitle"]');
                    if (titleLink) {
                        title = (titleLink.textContent || '').trim();
                        href  = titleLink.getAttribute('href') || '';
                    }
                }

                if (!title) {
                    const generic = item.querySelector('a, [class*="title"], h3, h4');
                    if (generic) {
                        title = (generic.textContent || '').trim();
                        href  = generic.getAttribute('href') || href;
                    }
                }

                // due date: search parent container first (mirrors _extract_due_date) [STABLE]
                const searchRoot = item.parentElement || item;
                const timeEl = searchRoot.querySelector('time[datetime]');
                const time_datetime = timeEl ? (timeEl.getAttribute('datetime') || '') : '';

                // parent_container: walk up ancestors to find the nearest content-list-item
                // whose type is exactly "Learning Module" or "Folder" (best-effort, [FRAGILE]).
                // Non-container ancestors (Text Document, PDF, Link, etc.) are skipped so
                // only real expandable containers are used.
                // Returns empty string if no valid container ancestor exists.
                let parent_container = '';
                let ancestor = item.parentElement;
                while (ancestor && ancestor !== document.body) {
                    if (ancestor.classList && ancestor.classList.contains('content-list-item')) {
                        const pSvg = ancestor.querySelector('svg[aria-label]');
                        const pType = pSvg ? (pSvg.getAttribute('aria-label') || '') : '';
                        const hasChildList = ancestor.querySelector('.content-list');
                        if ((pType === 'Learning Module' || pType === 'Folder') && hasChildList) {
                            const pLink = ancestor.querySelector(
                                'a[data-analytics-id*="assessment"], a[class*="contentItemTitle"], a'
                            );
                            parent_container = pLink ? (pLink.textContent || '').trim() : '';
                            break;  // found a valid container — stop
                        }
                        // Non-container content-list-item (e.g. Text Document, PDF) — keep walking up
                    }
                    ancestor = ancestor.parentElement;
                }

                return { content_type, title, href, time_datetime, parent_container };
            }

            return Array.from(document.querySelectorAll('div.content-list-item'))
                .map(item => extractItem(item));
        }""")

        print(f"    [DEBUG] {len(raw_items)} content-list-item(s) found after expand", flush=True)

        # TEMP DEBUG: collect all distinct svg[aria-label] values before any filtering
        _svg_labels_seen = {d['content_type'] for d in raw_items if d['content_type']}
        print(f"    [DEBUG] distinct svg[aria-label] values: {sorted(_svg_labels_seen)}", flush=True)

        return raw_items

    # -----------------------------------------------------------------------
    # CONTENT OBJECT CONSTRUCTION
    # -----------------------------------------------------------------------

    # Content types that act as containers for sequential tracking.
    # When one of these is encountered in list order it becomes the active
    # container_name for all subsequent items until the next container.
    _MODULE_CONTAINER_TYPES = {"Learning Module", "Folder"}

    def _build_content_objects(self, course_info: dict, raw_items: list[dict]) -> list[dict]:
        """
        Convert every raw DOM item from _extract_modules_and_items into a
        self-contained content object.

        All items are captured — no type filtering is applied here.
        Items with no title are skipped (they have no identifier).

        Container context uses sequential tracking: Blackboard Ultra renders
        module/folder headers and their child items as siblings in the DOM
        (not as nested elements), so ancestor-walking is unreliable. Instead,
        we iterate items in order: when a Learning Module or Folder is seen it
        becomes the active container; all subsequent items inherit that container
        name until the next container appears.
        """
        if not raw_items:
            print(f"    [INFO] No content items found on page.")
            return []

        course_name = course_info["course_name"]
        course_id   = course_info["course_id"]
        course_url  = f"{BASE_URL}/ultra/courses/{course_id}/outline"

        content_objects: list[dict] = []
        current_container: str | None = None  # sequential container tracking

        for idx, item in enumerate(raw_items):
            try:
                content_type  = item.get("content_type", "")
                title         = (item.get("title") or "").strip()
                href          = item.get("href", "")
                time_datetime = item.get("time_datetime", "")

                if not title:
                    continue  # unidentifiable item — skip

                if idx < 10:
                    print(
                        f"    [DEBUG] item[{idx}] type={content_type!r} "
                        f"title={title!r} container={current_container!r}",
                        flush=True,
                    )

                # Sequential container tracking:
                #   A Learning Module or Folder item becomes the active container;
                #   all following items are assigned to it until the next container.
                if content_type in self._MODULE_CONTAINER_TYPES:
                    current_container = title
                    container_name = None   # the container itself has no parent
                else:
                    container_name = current_container

                url      = f"{BASE_URL}{href}" if href.startswith("/") else href or course_url
                due_date = self._normalize_date(time_datetime) if time_datetime else None

                content_objects.append({
                    "course_name":    course_name,
                    "course_id":      course_id,
                    "container_name": container_name,
                    "title":          title,
                    "content_type":   content_type,
                    "url":            url,
                    "due_date":       due_date,
                    "due_date_raw":   time_datetime or None,
                })

            except Exception as e:
                print(f"    [WARN] Skipped one item: {e}")
                continue

        print(f"    [DEBUG] Built {len(content_objects)} content object(s).", flush=True)
        return content_objects

    def _extract_due_date(self, elem) -> tuple[str | None, str | None]:
        """
        Search for a due date in the element and its parent container.
        Returns (YYYY-MM-DD or None, raw_text or None).
        Only structural selectors are used — no keyword/text-matching selectors.
        """
        try:
            parent = elem.locator("..").first
        except Exception:
            parent = elem

        # 1. <time datetime="..."> — most reliable if present [STABLE]
        try:
            dt_attr = parent.locator(DUE_DATE_TIME_ELEMENT_SELECTOR).first.get_attribute("datetime")
            if dt_attr:
                return self._normalize_date(dt_attr), dt_attr
        except Exception:
            pass

        # 2. Class-based selector [FRAGILE]
        try:
            raw = parent.locator(DUE_DATE_CLASS_SELECTOR).first.inner_text().strip()
            if raw:
                return self._parse_due_text(raw), raw
        except Exception:
            pass

        return None, None

    # -----------------------------------------------------------------------
    # DATE UTILITIES
    # -----------------------------------------------------------------------

    def _normalize_date(self, date_str: str) -> str | None:
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            return date_str[:10]
        return None

    def _parse_due_text(self, text: str) -> str | None:
        clean = re.sub(r"^[Dd]ue:?\s*", "", text).strip()
        formats = [
            "%b %d, %Y %I:%M %p",
            "%B %d, %Y %I:%M %p",
            "%b %d, %Y",
            "%B %d, %Y",
            "%m/%d/%Y",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    # -----------------------------------------------------------------------
    # DEBUG
    # -----------------------------------------------------------------------

    def _save_debug_html(self, page: Page, name: str):
        """Save the current page HTML to debug/<name>.html for selector inspection."""
        os.makedirs("debug", exist_ok=True)
        path = f"debug/{name}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"       [DEBUG] Page HTML saved to: {path}")

    # -----------------------------------------------------------------------
    # OUTPUT
    # -----------------------------------------------------------------------

    def _write_output(self) -> str:
        os.makedirs("output", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"output/content_objects_{timestamp}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        return filename
