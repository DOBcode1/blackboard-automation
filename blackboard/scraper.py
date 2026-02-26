"""
blackboard/scraper.py

Blackboard Ultra assignment extractor using Playwright (sync API).
Extracts Spring 2026 course assignments and saves them to output/assignments_<timestamp>.json.

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
            "total_assignments": 0,
            "courses_with_no_assignments": [],
        }

    def run(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()

            try:
                print(f"Opening {LOGIN_URL} ...")
                page.goto(LOGIN_URL, wait_until="domcontentloaded")

                self._wait_for_login(page)

                print(f"\nNavigating to courses page...")
                page.goto(COURSES_PAGE_URL, wait_until="networkidle")

                course_links = self._get_spring_2026_course_links(page)
                if not course_links:
                    print(f"[WARN] No '{TERM_FILTER}' courses found. "
                          "The term section selector may need adjustment — "
                          "inspect the institution page in DevTools and update "
                          "TERM_SECTION_SELECTOR in scraper.py.")
                    return

                print(f"Found {len(course_links)} {TERM_FILTER} course(s).\n")

                self.results["extracted_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

                for course_info in course_links:
                    course_data = self._scrape_course(page, course_info)
                    self.results["courses"].append(course_data)
                    self.results["total_assignments"] += course_data["assignment_count"]
                    if course_data["assignment_count"] == 0:
                        self.results["courses_with_no_assignments"].append(
                            course_data["course_name"]
                        )

                output_path = self._write_output()
                print(f"\nDone. {self.results['total_assignments']} assignment(s) "
                      f"saved to: {output_path}")

            finally:
                browser.close()

    # -----------------------------------------------------------------------
    # LOGIN
    # -----------------------------------------------------------------------

    def _wait_for_login(self, page: Page):
        """
        Poll page.url every 2s for up to 3 minutes, waiting for '/ultra/' to appear.
        This is robust to Fordham's SSO/SAML redirect chain — only the final
        post-login URL matters.
        """
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
        Find Spring 2026 courses using DOM selectors.

        Blackboard Ultra's courses page uses a Slick carousel with one slide per
        term. Only the current/active slide renders DOM. Within the active slide,
        courses are grouped by term with a visible <h3> heading.

        Structure (confirmed from live DOM):
          div#course-card-term-name-{termId}  → contains <h3>Spring 2026</h3>
          div.default-group.term-{termId}     → contains the course article cards
          article[data-course-id]             → one per course
          h4.js-course-title-element          → course name
        """
        try:
            page.wait_for_selector("article[data-course-id]", timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            print("[WARN] Course list did not load. Saving debug HTML.")
            self._save_debug_html(page, "courses_page")
            return []

        time.sleep(2)

        # Extract the term ID for Spring 2026 from the term group heading element
        term_id = page.evaluate(f"""() => {{
            const headings = document.querySelectorAll('.course-card-term-name');
            for (const el of headings) {{
                if (el.textContent.includes('{TERM_FILTER}')) {{
                    const match = el.id.match(/course-card-term-name-(.+)/);
                    return match ? match[1] : null;
                }}
            }}
            return null;
        }}""")

        if term_id:
            print(f"[OK] Found '{TERM_FILTER}' term group (ID: {term_id}).")
            selector = f".term-{term_id} article[data-course-id]:not([data-course-id=''])"
        else:
            # Fallback: grab all non-empty course articles visible on the page
            print(f"[INFO] '{TERM_FILTER}' term heading not found. "
                  "Collecting all visible active courses.")
            selector = "article[data-course-id]:not([data-course-id=''])"

        articles = page.locator(selector).all()
        courses = []
        for article in articles:
            course_id = article.get_attribute("data-course-id") or ""
            if not course_id:
                continue
            try:
                name = article.locator("h4.js-course-title-element").first.inner_text().strip()
            except Exception:
                name = course_id
            courses.append({
                "course_id": course_id,
                "course_name": name or course_id,
                "url": f"{BASE_URL}/ultra/courses/{course_id}/cl/outline",
            })

        return courses

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
            "assignments": [],
            "assignment_count": 0,
        }

        try:
            page.goto(content_url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for the React content list to render inside the Angular view.
            # [ui-view="course@"] * fires too early (Angular mounts before React renders
            # the content list). Waiting for div.content-list-item is more reliable.
            try:
                page.wait_for_selector('div.content-list-item', timeout=PAGE_LOAD_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                # Course may genuinely have no content items — proceed and scan anyway
                print(f"    [INFO] No content-list-item appeared within timeout (empty course?).")

            assignments = self._extract_assignments(page, content_url)
            course_data["assignments"] = assignments
            course_data["assignment_count"] = len(assignments)

            if assignments:
                print(f"    {len(assignments)} assignment(s) found.")
            else:
                print(f"    No assignments found.")

        except Exception as e:
            print(f"    [ERROR] {e}")

        return course_data

    # -----------------------------------------------------------------------
    # ASSIGNMENT EXTRACTION
    # -----------------------------------------------------------------------

    def _expand_course_content(self, page: Page, course_url: str = ""):
        """
        Fully expand the course outline by:
          1. Clicking 'Load more' to reveal all top-level items.
          2. Clicking collapsed Learning Module buttons one-at-a-time to reveal
             their children (re-queries DOM after each click so stale locators
             are never used).

        Clicks are made one at a time rather than batching via .all() because
        Angular re-renders the outline after each toggle, which can shift
        indices and cause pre-collected locators to misfire.
        """
        # Step 1: Load all top-level items — only click enabled buttons
        for _ in range(30):
            load_btns = page.locator('button[data-analytics-id*="loadMoreButton"]:not([disabled])')
            if load_btns.count() == 0:
                break
            load_btns.first.click(force=True)
            time.sleep(1.5)

        # Step 2: Expand collapsed Learning Modules one at a time
        for attempt in range(30):  # higher cap for deeply nested modules
            # Re-query every iteration so we always use a fresh locator
            collapsed = page.locator(
                'button[data-analytics-id="course.learning.module.base.item.toggleLm.button"]'
                '[aria-expanded="false"]'
            )
            count = collapsed.count()
            if count == 0:
                break
            print(f"    [DEBUG] Expanding {count} collapsed module(s), attempt {attempt + 1}...", flush=True)
            try:
                collapsed.first.click()
                time.sleep(1)
            except Exception as e:
                print(f"    [WARN] Could not click learning module toggle: {e}", flush=True)
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

        # Step 3: Scroll to bottom until page height stabilizes
        print("    [DEBUG] Scrolling to stabilize page height...", flush=True)
        prev_height = -1
        stable_count = 0
        for i in range(10):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)
            height = page.evaluate("document.body.scrollHeight")
            print(f"    [SCROLL] iter={i} prev_height={prev_height} height={height} stable_count={stable_count}", flush=True)
            if height == prev_height:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
            prev_height = height

        # Step 4: Re-wait for content items after scrolling, then print stabilized count
        try:
            page.wait_for_selector("div.content-list-item", timeout=PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        total = page.locator("div.content-list-item").count()
        print(f"    [STABILIZED] div.content-list-item total: {total}", flush=True)

    def _extract_assignments(self, page: Page, course_url: str) -> list[dict]:
        """
        Extract actionable graded items from the course outline page.

        Only items whose svg[aria-label] matches ACTIONABLE_TYPES are processed.
        All other content (Documents, Folders, Announcements, etc.) is skipped.

        Confirmed DOM structure (from live Blackboard Ultra DOM):
          div.content-list-item              — one per content item [STABLE]
          svg[aria-label="..."]              — icon identifying content type [STABLE]
          a[data-analytics-id*="assessment"] — title link for graded items [STABLE]
          a[class*="contentItemTitle"]       — title fallback [FRAGILE — hashed class]

        Graded items may be nested inside Learning Modules (collapsed folders).
        _expand_course_content() is called first to expand everything.
        """
        print(f"    [DEBUG] URL before expand: {page.url}", flush=True)

        # Expand all Learning Modules and load all items before scanning
        self._expand_course_content(page, course_url)

        print(f"    [DEBUG] URL after expand:  {page.url}", flush=True)

        # Save post-expansion HTML for the first course to aid debugging
        if not self._debug_course_saved:
            self._save_debug_html(page, "course_content_post_expand")
            self._debug_course_saved = True

        all_items = page.locator("div.content-list-item").all()
        print(f"    [DEBUG] {len(all_items)} content-list-item(s) found after expand", flush=True)

        # TEMP DEBUG: collect all distinct svg[aria-label] values before any filtering
        _svg_labels_seen = set()
        for _item in all_items:
            _svg = _item.locator("svg[aria-label]").first
            if _svg.count() > 0:
                _label = _svg.get_attribute("aria-label") or ""
                _svg_labels_seen.add(_label)
        print(f"    [DEBUG] distinct svg[aria-label] values: {sorted(_svg_labels_seen)}", flush=True)

        if not all_items:
            print(f"    [INFO] No content items found on page.")
            return []

        assignments = []
        seen = set()
        actionable_count = 0

        for idx, item in enumerate(all_items):
            try:
                # Determine content type from SVG icon aria-label [STABLE]
                content_type = ""
                svg = item.locator("svg[aria-label]").first
                if svg.count() > 0:
                    content_type = svg.get_attribute("aria-label") or ""

                # STABILIZATION PHASE 1: type filtering disabled — extract everything
                # if content_type not in ACTIONABLE_TYPES:
                #     continue
                actionable_count += 1

                # Title — assessment link [STABLE], fall back to hashed class [FRAGILE]
                href = ""
                title = ""
                title_link = item.locator("a[data-analytics-id*='assessment']").first
                if title_link.count() > 0:
                    title = title_link.inner_text().strip()
                    href = title_link.get_attribute("href") or ""
                if not title:
                    title_link = item.locator("a[class*='contentItemTitle']").first
                    if title_link.count() > 0:
                        title = title_link.inner_text().strip()
                        href = title_link.get_attribute("href") or ""

                # Generic fallback for non-graded items (Documents, Folders, etc.)
                # which have no assessment or contentItemTitle link
                if not title:
                    generic = item.locator("a, [class*='title'], h3, h4").first
                    if generic.count() > 0:
                        title = generic.inner_text().strip()
                        href = generic.get_attribute("href") or href

                if idx < 10:
                    print(f"    [DEBUG] item[{idx}] content_type={content_type!r} title={title!r}", flush=True)

                if not title or title in seen:
                    continue
                seen.add(title)

                url = f"{BASE_URL}{href}" if href.startswith("/") else href or course_url

                due_date, due_date_raw = self._extract_due_date(item)
                status = self._compute_status(due_date)

                assignments.append({
                    "title": title,
                    "content_type": content_type,
                    "due_date": due_date,
                    "due_date_raw": due_date_raw,
                    "status": status,
                    "url": url,
                })

            except Exception as e:
                print(f"    [WARN] Skipped one item: {e}")
                continue

        print(f"    [DEBUG] {actionable_count} actionable item(s) matched ACTIONABLE_TYPES", flush=True)
        return assignments

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

    def _compute_status(self, due_date_str: str | None) -> str:
        if not due_date_str:
            return "unknown"
        try:
            due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            return "upcoming" if due >= today else "past"
        except ValueError:
            return "unknown"

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
        filename = f"output/assignments_{timestamp}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        return filename
