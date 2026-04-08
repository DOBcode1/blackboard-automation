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
CONSECUTIVE_ERROR_THRESHOLD = 3
MAX_COURSE_RETRIES = 2


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
        self._consecutive_errors = 0
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
                scroll_start  = time.time()

                while True:
                    if time.time() - scroll_start > 60:
                        print(f"[WARNING] Scroll loop timed out after 60 seconds; proceeding with {len(course_snapshot)} courses collected so far.")
                        break

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
                    retries = 0
                    while True:
                        try:
                            course_data = self._scrape_course(page, course_info)
                            self.results["courses"].append(course_data)
                            self.results["total_content_objects"] += course_data["item_count"]
                            self._consecutive_errors = 0
                            break
                        except Exception as e:
                            self._consecutive_errors += 1
                            print(f"  [ERROR] Course '{course_info['course_name']}' "
                                  f"(attempt {retries + 1}/{MAX_COURSE_RETRIES + 1}): {e}")

                            if self._consecutive_errors >= CONSECUTIVE_ERROR_THRESHOLD:
                                if not self._check_session_alive(page):
                                    self._recover_session(page)
                                else:
                                    self._consecutive_errors = 0

                            retries += 1
                            if retries > MAX_COURSE_RETRIES:
                                print(f"  [SKIP] Giving up on '{course_info['course_name']}' after {retries} attempt(s).")
                                break

                output_path = self._write_output()
                print(f"\nDone. {self.results['total_content_objects']} content object(s) "
                      f"saved to: {output_path}")

            finally:
                browser.close()

    # -----------------------------------------------------------------------
    # SESSION HEALTH
    # -----------------------------------------------------------------------

    def _check_session_alive(self, page: Page) -> bool:
        """Return True if the session is still authenticated, False if redirected to login."""
        try:
            page.goto(BASE_URL + "/ultra/course", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
            return "/ultra/" in page.url
        except Exception:
            return False

    def _recover_session(self, page: Page):
        """Block until the user re-logs in after session expiry."""
        print()
        print("=" * 60)
        print("  SESSION EXPIRED — Manual re-login required")
        print("=" * 60)
        page.goto(LOGIN_URL)

        if "/ultra/" in page.url:
            print("[OK] Already on /ultra/ after navigation. Resuming...")
            self._consecutive_errors = 0
            return

        print(f"Waiting for re-login (up to {LOGIN_TIMEOUT_SECONDS}s)...")
        try:
            page.wait_for_url("**/ultra/**", timeout=LOGIN_TIMEOUT_SECONDS * 1000)
            print("[OK] Re-login detected. Resuming...")
            self._consecutive_errors = 0
        except PlaywrightTimeoutError:
            raise TimeoutError(
                f"Re-login not detected within {LOGIN_TIMEOUT_SECONDS}s."
            )

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
            self._dismiss_overlays(page)
            self._load_all_hidden_items(page)
            self._expand_all_modules(page, content_url)
            self._expand_all_folders(page, content_url)
            self._stabilize_course_page(page)
            self._stabilize_item_count(page)

            # Save post-expansion HTML for the first course to aid debugging
            if not self._debug_course_saved:
                self._save_debug_html(page, "course_content_post_expand")
                self._debug_course_saved = True

            print(f"    [DEBUG] URL after expand:  {page.url}", flush=True)

            raw_items, container_ids = self._extract_modules_and_items(page)
            content_objects = self._build_content_objects(course_info, raw_items, container_ids)
            course_data["content_objects"] = content_objects
            course_data["item_count"] = len(content_objects)

            print(f"    {len(content_objects)} content object(s) captured.")

        except Exception as e:
            print(f"    [ERROR] {e}")

        return course_data

    # -----------------------------------------------------------------------
    # COURSE PAGE HELPERS
    # -----------------------------------------------------------------------

    def _dismiss_overlays(self, page: Page):
        """
        Dismiss announcement modals and popover/context menus that can block clicks.

        Tries a series of close-button selectors for announcement modals (first match
        wins), then presses Escape to collapse any open popover menus.
        """
        _ANNOUNCEMENT_CLOSE_SELECTORS = [
            'button[aria-label="Close Announcements"]',
            'div[class*="Announcement"] button[aria-label="Close"]',
            'button:has(svg[data-testid="CloseIcon"])',
            'button[aria-label="Close"]',
        ]
        for selector in _ANNOUNCEMENT_CLOSE_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=500):
                    btn.click(force=True)
                    time.sleep(0.5)
                    break
            except Exception:
                pass

        page.keyboard.press("Escape")
        time.sleep(0.3)

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
        Ensure all content-list-items are rendered before extraction.

        Phase 1 — wheel scroll + load-more buttons:
          An initial wheel scroll forces React to mount items below the visible
          fold so load-more buttons appear. Clicks are serialized because Angular
          re-renders the list after each click, invalidating pre-collected locators.

        Phase 2 — scroll-based lazy loading:
          Some courses render content purely through scrolling with no load-more
          button. Scroll the main content area in 800px increments and keep going
          as long as the div.content-list-item count keeps rising. Stop after 3
          consecutive scrolls with no new items, or after 30 iterations.
        """
        # Phase 1: wheel priming + load-more buttons
        for _ in range(15):
            page.mouse.wheel(0, 500)
            time.sleep(0.2)

        for _ in range(30):
            load_btns = page.locator('button[data-analytics-id*="loadMoreButton"]:not([disabled])')
            if load_btns.count() == 0:
                break
            load_btns.first.click(force=True)
            time.sleep(1.5)

        # Phase 2: scroll-based lazy loading (no load-more button)
        prev_count = page.locator("div.content-list-item").count()
        stable_rounds = 0
        for i in range(30):
            page.evaluate("""
() => {
    const main = document.querySelector('[role="main"]') || document.querySelector('main');
    if (main) main.scrollBy(0, 500);
}
""")
            time.sleep(1)
            new_count = page.locator("div.content-list-item").count()
            if new_count > prev_count:
                stable_rounds = 0
                prev_count = new_count
            else:
                stable_rounds += 1
                if stable_rounds >= 5:
                    break

        # Final forced scroll to the very bottom to catch any remaining items
        page.evaluate("""
() => {
    const main = document.querySelector('[role="main"]') || document.querySelector('main');
    if (main) main.scrollTop = main.scrollHeight;
}
""")
        time.sleep(2)
        final_count = page.locator("div.content-list-item").count()
        print(f"    [DEBUG] Phase 2 final count after bottom scroll: {final_count}", flush=True)

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
                page.keyboard.press("Escape")
                time.sleep(0.3)
                collapsed.first.scroll_into_view_if_needed()
                time.sleep(0.3)
                collapsed.first.click(force=True)
                time.sleep(1)
            except Exception as e:
                print(f"    [WARN] Could not click learning module toggle: {e}", flush=True)
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.5)
                    page.locator("body").click(position={"x": 10, "y": 10}, force=True)
                    time.sleep(0.5)
                    collapsed = page.locator(LM_TOGGLE_SELECTOR)
                    collapsed.first.click(force=True)
                except Exception:
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
                page.keyboard.press("Escape")
                time.sleep(0.3)
                collapsed.first.scroll_into_view_if_needed()
                time.sleep(0.3)
                collapsed.first.click(force=True)
                time.sleep(1)
            except Exception as e:
                print(f"    [WARN] Could not click folder toggle: {e}", flush=True)
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.5)
                    page.locator("body").click(position={"x": 10, "y": 10}, force=True)
                    time.sleep(0.5)
                    collapsed = page.locator(FOLDER_ICON_TOGGLE)
                    if collapsed.count() == 0:
                        collapsed = page.locator(FOLDER_ANALYTICS_TOGGLE)
                    collapsed.first.click(force=True)
                except Exception:
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
                if stable_count >= 3:
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

    def _stabilize_item_count(self, page: Page):
        """
        Poll div.content-list-item count every 2 seconds until it stops
        changing for 3 consecutive checks. Ensures React has finished mounting
        all items before extraction begins.
        """
        print("    [DEBUG] Waiting for div.content-list-item count to stabilize...", flush=True)
        prev_count = -1
        stable_rounds = 0
        for i in range(20):
            count = page.locator("div.content-list-item").count()
            print(f"    [ITEM_COUNT] check={i} count={count} stable_rounds={stable_rounds}", flush=True)
            if count == prev_count:
                stable_rounds += 1
                if stable_rounds >= 3:
                    print(f"    [ITEM_COUNT] Stable at {count} item(s) after {i + 1} check(s).", flush=True)
                    break
            else:
                stable_rounds = 0
            prev_count = count
            time.sleep(POLL_INTERVAL_SECONDS)

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
        result: dict = page.evaluate("""() => {
            // Pass 1: toggle-button approach (primary).
            // Covers both Learning Module (toggleLm) and Folder (toggleFolder) containers.
            const containerMap = {};
            document.querySelectorAll(
                'button[data-analytics-id*="toggleLm"], button[data-analytics-id*="toggleFolder"]'
            ).forEach(btn => {
                const li = btn.closest('.content-list-item');
                if (li) {
                    const id = li.getAttribute('data-content-id');
                    if (id) containerMap[id] = btn.textContent.trim();
                }
            });

            // Pass 2: structural approach — any content-list-item that contains a
            // child div.content-list with at least one content-list-item is a container,
            // regardless of its toggle button's analytics-id.
            document.querySelectorAll('div.content-list-item').forEach(li => {
                const id = li.getAttribute('data-content-id');
                if (!id || containerMap[id]) return;  // already captured
                const nested = li.querySelector('div.content-list > div.content-list-item');
                if (!nested) return;
                // Extract title using the same priority order as extractItem
                let title = '';
                const assessmentLink = li.querySelector('a[data-analytics-id*="assessment"]');
                if (assessmentLink) title = (assessmentLink.textContent || '').trim();
                if (!title) {
                    const titleLink = li.querySelector('a[class*="contentItemTitle"]');
                    if (titleLink) title = (titleLink.textContent || '').trim();
                }
                if (!title) {
                    const generic = li.querySelector('a, [class*="title"], h3, h4');
                    if (generic) title = (generic.textContent || '').trim();
                }
                if (title) containerMap[id] = title;
            });

            // Pass 3: broader structural detection — catches Learning Modules that use
            // non-standard analytics-id patterns (e.g. International Internship course).
            // Checks for nested content lists via learningModuleContainer, readonlyContentList,
            // or any intermediate div wrapper, supplementing Pass 2's direct child check.
            document.querySelectorAll('div.content-list-item').forEach(item => {
                const id = item.getAttribute('data-content-id');
                if (!id || containerMap[id]) return;
                const nestedList = item.querySelector(
                    ':scope > div > div > div.content-list, ' +
                    'div[class*="learningModuleContainer"] .content-list, ' +
                    'div[class*="readonlyContentList"]'
                );
                if (nestedList && nestedList.querySelector('.content-list-item')) {
                    const titleEl = item.querySelector(
                        'a[class*="contentItemTitle"], ' +
                        'a[data-analytics-id*="document.link"], ' +
                        'a[data-analytics-id*="toggleLm"], ' +
                        'button[data-analytics-id*="toggleLm"], ' +
                        'button[data-analytics-id*="toggleFolder"]'
                    );
                    if (titleEl) {
                        containerMap[id] = titleEl.textContent.trim();
                    }
                }
            });

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

                // description: plain text inside .js-description
                const descEl = item.querySelector('.js-description');
                const description = descEl ? (descEl.textContent || '').trim() : '';

                // subtext: grade/due info inside [class*="gradeInfo"]
                const gradeInfoEl = item.querySelector('[class*="gradeInfo"]');
                const subtext = gradeInfoEl ? (gradeInfoEl.textContent || '').trim() : '';

                // parent_container: look up the item's immediate .content-list-item ancestor
                // in the pre-built containerMap using its data-content-id.
                let parent_container = '';
                const parentItem = item.parentElement?.closest('.content-list-item');
                if (parentItem) {
                    const parentId = parentItem.getAttribute('data-content-id');
                    parent_container = (parentId && containerMap[parentId]) ? containerMap[parentId] : '';
                }

                // is_nested: true when the item is inside another .content-list-item ancestor
                const closestItem = item.parentElement?.closest('.content-list-item');
                const is_nested = closestItem !== null && closestItem !== undefined;

                return { content_type, title, href, time_datetime, description, subtext, parent_container, is_nested,
                         content_id: item.getAttribute('data-content-id') || '' };
            }

            const items = Array.from(document.querySelectorAll('div.content-list-item'))
                .map(item => extractItem(item));
            return { items, containerIds: Object.keys(containerMap) };
        }""")

        raw_items: list[dict] = result["items"]
        container_ids: set[str] = set(result["containerIds"])

        print(f"    [DEBUG] {len(raw_items)} content-list-item(s) found after expand", flush=True)
        print(f"    [DEBUG] {len(container_ids)} container id(s) detected: {sorted(container_ids)}", flush=True)

        # TEMP DEBUG: collect all distinct svg[aria-label] values before any filtering
        _svg_labels_seen = {d['content_type'] for d in raw_items if d['content_type']}
        print(f"    [DEBUG] distinct svg[aria-label] values: {sorted(_svg_labels_seen)}", flush=True)

        # TEMP DEBUG: container selector diagnostics
        debug_result = page.evaluate("""() => {
    const debug = {};

    // What the current containerMap finds
    debug.toggleButtons = [];
    document.querySelectorAll('button[data-analytics-id*="toggleLm"], button[data-analytics-id*="toggleFolder"]').forEach(btn => {
        const li = btn.closest('.content-list-item');
        debug.toggleButtons.push({
            id: li ? li.getAttribute('data-content-id') : null,
            text: btn.textContent.trim().substring(0, 40),
            analyticsId: btn.getAttribute('data-analytics-id')
        });
    });

    // All Learning Module SVGs
    debug.learningModuleSvgs = [];
    document.querySelectorAll('svg[aria-label="Learning Module"]').forEach(svg => {
        const item = svg.closest('.content-list-item');
        if (item) {
            const id = item.getAttribute('data-content-id');
            const titleEl = item.querySelector('a[class*="contentItemTitle"], a[data-analytics-id]');
            debug.learningModuleSvgs.push({
                id: id,
                title: titleEl ? titleEl.textContent.trim().substring(0, 40) : '(none)'
            });
        }
    });

    // Items with nested content lists
    debug.nestedContainers = [];
    document.querySelectorAll('div.content-list-item').forEach(item => {
        const nested = item.querySelector('.content-list');
        if (nested && nested.querySelector('.content-list-item')) {
            const id = item.getAttribute('data-content-id');
            const titleEl = item.querySelector('a[class*="contentItemTitle"]');
            debug.nestedContainers.push({
                id: id,
                title: titleEl ? titleEl.textContent.trim().substring(0, 40) : '(none)'
            });
        }
    });

    return debug;
}""")
        print(f"    [DEBUG-CONTAINERS] {json.dumps(debug_result, indent=2)}", flush=True)

        return raw_items, container_ids

    # -----------------------------------------------------------------------
    # CONTENT OBJECT CONSTRUCTION
    # -----------------------------------------------------------------------

    # Content types that act as expandable containers.
    # Items of these types are structural — they are not emitted as content objects.
    _MODULE_CONTAINER_TYPES = {"Learning Module", "Folder", "Open Folder"}

    def _build_content_objects(self, course_info: dict, raw_items: list[dict],
                               container_ids: set[str] | None = None) -> list[dict]:
        """
        Convert every raw DOM item from _extract_modules_and_items into a
        self-contained content object.

        All items are captured — no type filtering is applied here.
        Items with no title are skipped (they have no identifier).

        Container assignment strategy (primary: sequential tracking; secondary: JS
        ancestor-walk via parent_container):
          - current_container tracks the most recently seen container item by title.
          - An item is treated as a container when its content_type is in
            _MODULE_CONTAINER_TYPES OR its data-content-id appears in container_ids
            (the structurally-detected set from JS). This catches Learning Modules
            whose svg aria-label differs from the expected string.
          - For non-container items: prefer parent_container (JS ancestor-walk) when
            non-empty; otherwise fall back to current_container.
          - When a non-container item has no JS parent_container AND is not nested
            inside a container in the DOM (is_nested=False), reset current_container
            to None so it does not bleed into unrelated top-level items.
        """
        if not raw_items:
            print(f"    [INFO] No content items found on page.")
            return []

        if container_ids is None:
            container_ids = set()

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
                description      = (item.get("description") or "").strip()
                subtext          = (item.get("subtext") or "").strip()
                parent_container = (item.get("parent_container") or "").strip()
                is_nested        = item.get("is_nested", False)
                content_id       = item.get("content_id", "")

                if not title:
                    continue  # unidentifiable item — skip

                # Container items are structural: record them and skip emitting.
                # Use both the type-name set and the structurally-detected id set
                # so Learning Modules with unexpected aria-label values are caught.
                is_container = (content_type in self._MODULE_CONTAINER_TYPES
                                or bool(content_id and content_id in container_ids))
                if is_container:
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

                # Fallback: parse due date from subtext (e.g. "Due date: 1/21/26")
                if due_date is None and subtext:
                    m = re.search(r"[Dd]ue\s*[Dd]ate:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", subtext)
                    if m:
                        due_date = self._parse_due_text(m.group(1))

                content_objects.append({
                    "course_name":    course_name,
                    "course_id":      course_id,
                    "container_name": container_name,
                    "title":          title,
                    "content_type":   content_type,
                    "url":            url,
                    "due_date":       due_date,
                    "due_date_raw":   time_datetime or None,
                    "description":    description or None,
                    "subtext":        subtext or None,
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
            "%m/%d/%y",
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
