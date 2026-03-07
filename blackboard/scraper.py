"""
blackboard/scraper.py

Blackboard Ultra course content extractor using Playwright (sync API).
Extracts Spring 2026 course content objects and saves them to
output/content_objects_<timestamp>.json.

Phase 1: Course discovery + virtualization scroll harvesting (unchanged).
Phase 2: All course items are captured as structured content objects with
         course name, course id, container name, title, content type, url,
         and due date. Container assignment uses sequential tracking (primary)
         combined with JS ancestor-walking (secondary). No filtering applied.

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
            self._expand_all_folders(page, content_url)
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
        Click every collapsed Learning Module toggle one at a time until none remain.

        Re-queries the DOM before each click so stale locators are never used.
        If a click accidentally navigates away from the outline, the method
        returns to the course URL and stops expanding to avoid an infinite loop.
        """
        LM_TOGGLE_SELECTOR = (
            'button[data-analytics-id="course.learning.module.base.item.toggleLm.button"]'
            '[aria-expanded="false"]'
        )

        for attempt in range(30):  # higher cap for deeply nested modules
            collapsed = page.locator(LM_TOGGLE_SELECTOR)
            count = collapsed.count()
            if count == 0:
                break
            print(f"    [DEBUG] Expanding {count} collapsed Learning Module(s), attempt {attempt + 1}...", flush=True)
            try:
                collapsed.first.click()
                time.sleep(1)
            except Exception as e:
                print(f"    [WARN] Could not click learning module toggle: {e}", flush=True)
                break

            # Safety: if the click navigated away, go back to course outline
            if course_url and not page.url.rstrip("/").endswith("/outline"):
                print(f"    [WARN] Navigation detected during LM expansion ({page.url}), returning to outline...", flush=True)
                page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_selector("div.content-list-item", timeout=PAGE_LOAD_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    pass
                break  # Stop expansion after recovery to avoid loop

    def _expand_all_folders(self, page: Page, course_url: str):
        """
        Click every collapsed Folder toggle one at a time until none remain.

        Detects folders via their svg[aria-label="Folder"] icon (stable) rather
        than a hard-coded analytics-id (fragile). Falls back to analytics-id if
        the icon-based selector finds nothing.

        Runs as a second independent pass after _expand_all_modules so that
        folders nested inside Learning Modules are already visible in the DOM.
        Same recovery logic as _expand_all_modules.
        """
        # [STABLE] Icon-based: finds collapsed toggle buttons inside any content-list-item
        # that contains a Folder svg icon.
        FOLDER_ICON_TOGGLE = (
            'div.content-list-item:has(svg[aria-label="Folder"]) button[aria-expanded="false"]'
        )
        # [FRAGILE] analytics-id fallback — may change after Blackboard updates
        FOLDER_ANALYTICS_TOGGLE = (
            'button[data-analytics-id="course.folder.base.item.toggleLm.button"]'
            '[aria-expanded="false"]'
        )

        for attempt in range(30):
            # Prefer the icon-based selector; fall back to analytics-id
            collapsed = page.locator(FOLDER_ICON_TOGGLE)
            count = collapsed.count()
            if count == 0:
                collapsed = page.locator(FOLDER_ANALYTICS_TOGGLE)
                count = collapsed.count()
            if count == 0:
                break

            print(f"    [DEBUG] Expanding {count} collapsed Folder(s), attempt {attempt + 1}...", flush=True)
            try:
                collapsed.first.click()
                time.sleep(1)
            except Exception as e:
                print(f"    [WARN] Could not click folder toggle: {e}", flush=True)
                break

            # Safety: if the click navigated away, go back to course outline
            if course_url and not page.url.rstrip("/").endswith("/outline"):
                print(f"    [WARN] Navigation detected during Folder expansion ({page.url}), returning to outline...", flush=True)
                page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_selector("div.content-list-item", timeout=PAGE_LOAD_TIMEOUT_MS)
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
                        if ((pType === 'Learning Module' || pType === 'Folder' || pType === 'Open Folder') && hasChildList) {
                            const toggleBtn = ancestor.querySelector('button[aria-expanded]');
                            parent_container = toggleBtn ? toggleBtn.textContent.trim() : '';
                            break;  // found a valid container — stop
                        }
                        // Non-container content-list-item (e.g. Text Document, PDF) — keep walking up
                    }
                    ancestor = ancestor.parentElement;
                }

                // is_nested: true when the item's parent .content-list is itself
                // inside a .content-list-item (i.e. the item lives inside a container),
                // false when the parent .content-list is a top-level one.
                const parentList = item.parentElement;
                const is_nested = (parentList && parentList.classList && parentList.classList.contains('content-list'))
                    ? !!parentList.closest('.content-list-item')
                    : false;

                return { content_type, title, href, time_datetime, parent_container, is_nested };
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

    # Content types that act as expandable containers.
    # Items of these types are structural — they are not emitted as content objects.
    _MODULE_CONTAINER_TYPES = {"Learning Module", "Folder", "Open Folder"}

    def _build_content_objects(self, course_info: dict, raw_items: list[dict]) -> list[dict]:
        """
        Convert every raw DOM item from _extract_modules_and_items into a
        self-contained content object.

        All items are captured — no type filtering is applied here.
        Items with no title are skipped (they have no identifier).

        Container assignment strategy (primary: sequential tracking; secondary: JS
        ancestor-walk via parent_container):
          - current_container tracks the most recently seen container item by title.
          - When content_type is in _MODULE_CONTAINER_TYPES: record title as
            current_container and skip emitting the item.
          - For non-container items: prefer parent_container (JS ancestor-walk) when
            non-empty; otherwise fall back to current_container.
          - When a non-container item has no JS parent_container AND is not nested
            inside a container in the DOM (is_nested=False), reset current_container
            to None so it does not bleed into unrelated top-level items.
        """
        if not raw_items:
            print(f"    [INFO] No content items found on page.")
            return []

        course_name = course_info["course_name"]
        course_id   = course_info["course_id"]
        course_url  = f"{BASE_URL}/ultra/courses/{course_id}/outline"

        content_objects: list[dict] = []
        current_container: str | None = None

        for idx, item in enumerate(raw_items):
            try:
                content_type     = item.get("content_type", "")
                title            = (item.get("title") or "").strip()
                href             = item.get("href", "")
                time_datetime    = item.get("time_datetime", "")
                parent_container = (item.get("parent_container") or "").strip()
                is_nested        = item.get("is_nested", False)

                if not title:
                    continue  # unidentifiable item — skip

                # Container items are structural: record them and skip emitting
                if content_type in self._MODULE_CONTAINER_TYPES:
                    current_container = title
                    continue

                # Determine container_name for this item
                if parent_container:
                    # JS ancestor-walk succeeded — use it directly
                    container_name = parent_container
                else:
                    # JS walk found nothing; use sequential tracking
                    container_name = current_container
                    # Reset tracker when this item is clearly at the top level
                    # so it doesn't bleed into subsequent unrelated items
                    if not is_nested:
                        current_container = None

                if idx < 10:
                    print(
                        f"    [DEBUG] item[{idx}] type={content_type!r} "
                        f"title={title!r} container={container_name!r}",
                        flush=True,
                    )

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
