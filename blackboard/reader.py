"""
blackboard/reader.py

Blackboard Ultra course content text extractor using Playwright (sync API).
Reads a Phase 2 content_objects JSON file, visits each item's URL in
Blackboard, and extracts visible text content.

Outputs content_text_<timestamp>.json with the same structure as Phase 2
plus extracted_text, text_type, and read_status fields.

USAGE:
    python reader.py <path_to_content_objects.json>

    Example:
    python reader.py output/content_objects_20260316_113920.json

TEXT EXTRACTION STRATEGIES (by URL pattern):

  /outline/file/         — PDF/DOCX/PPTX previewed in an iframe with a
                           react-pdf viewer.  Text lives in .textLayer divs.
                           If textLayer is empty for a PDF → flagged as
                           image_based (scanned document).

  /outline/edit/document/ — Inline HTML documents rendered directly on the
                           page.  Body text grabbed from the main content area.

  /outline/assessment/   — Assignment / Test detail panels with description,
                           due date, and instructions.

  /outline/discussion/   — Discussion topic pages with the prompt text.

SKIPPED ITEMS:
  - url == "#" or url pointing to course outline root (LTI / external tools)
  - External links (non-Blackboard URLs)
  - content_type == "Photo" → flagged as image_based
  - content_type == "Video" → skipped

PROGRESS:
  The output file is saved after every item so progress is not lost on crash.
  Re-running with the same output file skips already-read items.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_URL = "https://fordham.blackboard.com"
LOGIN_URL = f"{BASE_URL}/"
LOGIN_TIMEOUT_SECONDS = 600
PAGE_LOAD_TIMEOUT_MS = 15_000
IFRAME_LOAD_TIMEOUT_MS = 20_000
TEXT_LAYER_WAIT_MS = 10_000
CONSECUTIVE_ERROR_THRESHOLD = 3
MAX_ITEM_RETRIES = 2


# ---------------------------------------------------------------------------
# READER
# ---------------------------------------------------------------------------

class BlackboardReader:

    def __init__(self, input_path: str, output_path: str | None = None):
        self.input_path = input_path
        self.output_path = output_path or self._default_output_path()
        self.data = self._load_input()
        self.results = self._load_or_init_results()
        self._consecutive_errors = 0

    def _default_output_path(self) -> str:
        os.makedirs("output", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"output/content_text_{timestamp}.json"

    def _load_input(self) -> dict:
        with open(self.input_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_or_init_results(self) -> dict:
        """Load existing results for incremental runs, or initialize from input."""
        if os.path.exists(self.output_path):
            print(f"[INFO] Resuming from existing output: {self.output_path}")
            with open(self.output_path, "r", encoding="utf-8") as f:
                return json.load(f)

        # Initialize results from input data, adding reader fields to each item
        results = {
            "extracted_at": self.data.get("extracted_at", ""),
            "read_at": "",
            "term": self.data.get("term", ""),
            "courses": [],
            "total_items": 0,
            "total_read": 0,
            "total_skipped": 0,
        }

        for course in self.data["courses"]:
            course_copy = {
                "course_id": course["course_id"],
                "course_name": course["course_name"],
                "course_url": course["course_url"],
                "content_objects": [],
            }
            for obj in course["content_objects"]:
                item = dict(obj)  # shallow copy
                item["extracted_text"] = None
                item["text_type"] = "pending"
                item["read_status"] = "pending"
                course_copy["content_objects"].append(item)

            results["courses"].append(course_copy)

        return results

    def _save_results(self):
        """Save current results to disk (called after each item)."""
        # Update summary counts
        total = 0
        read = 0
        skipped = 0
        for course in self.results["courses"]:
            for obj in course["content_objects"]:
                total += 1
                if obj["read_status"] == "success":
                    read += 1
                elif obj["read_status"] in ("skipped", "image_based"):
                    skipped += 1

        self.results["total_items"] = total
        self.results["total_read"] = read
        self.results["total_skipped"] = skipped
        self.results["read_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)

    # -----------------------------------------------------------------------
    # ITEM CLASSIFICATION
    # -----------------------------------------------------------------------

    def _classify_item(self, item: dict) -> str:
        """
        Determine what extraction strategy to use for an item.
        Returns one of: 'file', 'document', 'assessment', 'discussion',
                        'image_based', 'skip'
        """
        url = item.get("url", "")
        content_type = item.get("content_type", "")

        # Photos → image_based (for AI vision later)
        if content_type == "Photo":
            return "image_based"

        # Videos → skip
        if content_type == "Video":
            return "skip"

        # No URL or hash URL → skip
        if not url or url == "#":
            return "skip"

        # External links → skip
        if url.startswith("http") and "blackboard" not in url:
            return "skip"

        # Course outline root URLs → skip
        if url.rstrip("/").endswith("/outline"):
            return "skip"

        # Blackboard internal URLs — classify by path
        if "/outline/file/" in url:
            return "file"
        if "/outline/edit/document/" in url:
            return "document"
        if "/outline/assessment/" in url:
            return "assessment"
        if "/outline/discussion/" in url:
            return "discussion"

        # Fallback → skip
        return "skip"

    # -----------------------------------------------------------------------
    # MAIN RUN
    # -----------------------------------------------------------------------

    def run(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()

            try:
                self._login(page)

                for course in self.results["courses"]:
                    course_name = course["course_name"]
                    items = course["content_objects"]
                    print(f"\n{'='*60}")
                    print(f"Reading: {course_name}")
                    print(f"  {len(items)} item(s)")

                    for idx, item in enumerate(items):
                        # Skip already-processed final states (incremental)
                        status = item["read_status"]
                        text_type_existing = item.get("text_type", "pending")
                        if status in ("success", "skipped", "image_based"):
                            continue
                        if status == "no_text" and text_type_existing in ("image_based", "no_viewer"):
                            continue

                        title = item.get("title", "(untitled)")
                        classification = self._classify_item(item)

                        if classification == "skip":
                            item["read_status"] = "skipped"
                            item["text_type"] = "skipped"
                            self._save_results()
                            continue

                        if classification == "image_based":
                            item["read_status"] = "image_based"
                            item["text_type"] = "image_based"
                            self._save_results()
                            continue

                        print(f"  [{idx+1}/{len(items)}] {classification}: {title}")

                        retries = 0
                        while retries <= MAX_ITEM_RETRIES:
                            try:
                                if classification == "file":
                                    text, text_type = self._read_file_item(page, item)
                                elif classification == "document":
                                    text, text_type = self._read_document_item(page, item)
                                elif classification == "assessment":
                                    text, text_type = self._read_assessment_item(page, item)
                                elif classification == "discussion":
                                    text, text_type = self._read_discussion_item(page, item)
                                else:
                                    text, text_type = None, "unknown"

                                item["extracted_text"] = text
                                item["text_type"] = text_type
                                item["read_status"] = "success" if text else "no_text"
                                self._consecutive_errors = 0
                                break

                            except Exception as e:
                                self._consecutive_errors += 1
                                print(f"    [ERROR] (attempt {retries+1}/{MAX_ITEM_RETRIES+1}) {e}")

                                if self._consecutive_errors >= CONSECUTIVE_ERROR_THRESHOLD:
                                    if not self._check_session_alive(page):
                                        self._recover_session(page)
                                        # Session recovery gives a free retry; don't charge retries
                                        continue
                                    else:
                                        # Genuine item errors — reset streak
                                        self._consecutive_errors = 0

                                retries += 1
                                if retries > MAX_ITEM_RETRIES:
                                    print(f"    [ERROR] All retries exhausted for: {title}")
                                    item["extracted_text"] = None
                                    item["text_type"] = "error"
                                    item["read_status"] = "error"

                        self._save_results()

                print(f"\n{'='*60}")
                print(f"Done. {self.results['total_read']} read, "
                      f"{self.results['total_skipped']} skipped.")
                print(f"Output: {self.output_path}")

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
    # LOGIN (same as scraper)
    # -----------------------------------------------------------------------

    def _login(self, page: Page):
        print(f"Opening {LOGIN_URL} ...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

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
    # NAVIGATION HELPERS
    # -----------------------------------------------------------------------

    def _verify_navigation(self, page: Page, expected_path: str):
        """Wait for networkidle, then verify current URL contains expected_path.
        If not, waits 2 more seconds and checks again. Logs a warning if still wrong."""
        page.wait_for_load_state("networkidle")
        if expected_path not in page.url:
            time.sleep(2)
            if expected_path not in page.url:
                print(f"    [WARN] URL mismatch: expected path '{expected_path}' not in '{page.url}'")

    def _wait_for_heading(self, page: Page, title: str, timeout_ms: int = 10_000):
        """Wait until an h1 or [role='heading'] element containing title text appears."""
        try:
            page.wait_for_function(
                "(title) => {"
                "  const headings = document.querySelectorAll('h1, [role=\"heading\"]');"
                "  return Array.from(headings).some(h => h.textContent.includes(title));"
                "}",
                arg=title,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            print(f"    [WARN] Heading not found for: {title}")

    # -----------------------------------------------------------------------
    # FILE ITEMS (PDF, DOCX, PPTX via iframe react-pdf viewer)
    # -----------------------------------------------------------------------

    def _read_file_item(self, page: Page, item: dict) -> tuple[str | None, str]:
        """
        Navigate to a /file/ URL, wait for the iframe document viewer,
        switch into the iframe, and extract text from all .textLayer divs.

        Returns (extracted_text, text_type).
        text_type is 'extracted' if text was found, 'image_based' if
        the text layer was empty (scanned PDF / image-based document).
        """
        url = item["url"]
        content_type = item.get("content_type", "")
        title = item.get("title", "")

        page.goto("about:blank")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._verify_navigation(page, "/outline/file/")

        # Wait for the iframe containing the document viewer
        try:
            page.wait_for_selector(
                'iframe[src*="bbcswebdav"], iframe[src*="doc-viewer"]',
                timeout=IFRAME_LOAD_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            print(f"    [WARN] No document viewer iframe found for: {title}")
            return None, "no_viewer"

        # Get the iframe and switch into it
        iframe_el = page.query_selector(
            'iframe[src*="bbcswebdav"], iframe[src*="doc-viewer"]'
        )
        if not iframe_el:
            return None, "no_viewer"

        frame = iframe_el.content_frame()
        if not frame:
            return None, "no_viewer"

        # Wait for the react-pdf text layers to render
        try:
            frame.wait_for_selector(
                '.textLayer, .react-pdf_Page__textContent',
                timeout=TEXT_LAYER_WAIT_MS,
            )
        except PlaywrightTimeoutError:
            print(f"    [WARN] Text layer did not appear for: {title}")
            # Could be a scanned PDF or image
            if content_type == "PDF" or title.lower().endswith(".pdf"):
                return None, "image_based"
            return None, "no_text"

        # Small extra wait for all pages to render their text layers
        time.sleep(2)

        # Extract text from all text layer divs across all pages
        text = frame.evaluate("""() => {
            const layers = document.querySelectorAll('.textLayer, .react-pdf_Page__textContent');
            return Array.from(layers).map(layer => layer.textContent).join('\\n\\n');
        }""")

        text = (text or "").strip()

        if not text:
            if content_type == "PDF" or title.lower().endswith(".pdf"):
                return None, "image_based"
            return None, "no_text"

        print(f"    [OK] Extracted {len(text)} chars from {title}")
        return text, "extracted"

    # -----------------------------------------------------------------------
    # INLINE DOCUMENT ITEMS (/edit/document/)
    # -----------------------------------------------------------------------

    def _read_document_item(self, page: Page, item: dict) -> tuple[str | None, str]:
        """
        Navigate to an /edit/document/ URL and extract the inline HTML text.
        These render directly on the page without an iframe.
        """
        url = item["url"]
        title = item.get("title", "")

        page.goto("about:blank")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._verify_navigation(page, "/outline/edit/document/")
        self._wait_for_heading(page, title)

        # Wait for content to render
        time.sleep(3)

        # Check for a document viewer iframe first — defer to file handler if present
        iframe_el = page.query_selector('iframe[src*="bbcswebdav"], iframe[src*="doc-viewer"]')
        if iframe_el:
            return self._read_file_item(page, item)

        # Extract inline document content from Quill rich text editor
        text = page.evaluate("""() => {
            const el = document.querySelector('.ql-editor.bb-editor');
            if (el) return el.textContent.trim();
            return '';
        }""")

        text = (text or "").strip()

        if not text:
            # --- Attachment fallback ---
            # Some /edit/document/ pages contain file attachments instead of inline text.
            # Look for the "Preview file" expand buttons next to each attached file.
            attach_buttons = page.query_selector_all('button[title="Preview file"]')
            if not attach_buttons:
                attach_buttons = page.query_selector_all('button[aria-label*="Preview File"]')

            if not attach_buttons:
                print(f"    [WARN] No text found for inline document: {title}")
                return None, "no_text"

            print(f"    [INFO] Found {len(attach_buttons)} file attachment(s), expanding to extract...")

            attachment_results: list[str] = []
            any_text = False

            for btn in attach_buttons:
                # Determine filename from aria-label or nearby fileText span
                aria_label = btn.get_attribute("aria-label") or ""
                if aria_label.startswith("Preview File "):
                    filename = aria_label[len("Preview File "):]
                else:
                    # Try nearby span with class containing "fileText"
                    span = btn.query_selector('xpath=..//*[contains(@class,"fileText")]')
                    if not span:
                        # Walk up one level and search siblings
                        span = page.evaluate("""(btn) => {
                            const parent = btn.closest('[class*="fileText"]') ||
                                           btn.parentElement?.querySelector('[class*="fileText"]');
                            return parent ? parent.textContent.trim() : null;
                        }""", btn)
                        filename = span if isinstance(span, str) else "attachment"
                    else:
                        filename = (span.text_content() or "attachment").strip()

                # Click to expand the inline file viewer
                btn.click()
                time.sleep(2)

                # Wait for the inline preview iframe
                iframe_sel = 'div[class*="js-file-viewer-inline-preview"] iframe, iframe[id^="file-preview-"]'
                try:
                    page.wait_for_selector(iframe_sel, timeout=15_000)
                except PlaywrightTimeoutError:
                    print(f"    [WARN] Inline preview iframe did not appear for: {filename}")
                    attachment_results.append(f"--- {filename} --- [preview iframe not found]")
                    continue

                iframe_el = page.query_selector(iframe_sel)
                if not iframe_el:
                    attachment_results.append(f"--- {filename} --- [preview iframe not found]")
                    continue

                frame = iframe_el.content_frame()
                if not frame:
                    attachment_results.append(f"--- {filename} --- [could not access iframe frame]")
                    continue

                # Wait for PDF text layers to render inside the iframe
                try:
                    frame.wait_for_selector(
                        '.textLayer, .react-pdf_Page__textContent',
                        timeout=TEXT_LAYER_WAIT_MS,
                    )
                except PlaywrightTimeoutError:
                    if filename.lower().endswith(".pdf"):
                        attachment_results.append(f"--- {filename} --- [image-based, no text layer]")
                    else:
                        attachment_results.append(f"--- {filename} --- [no text layer]")
                    continue

                # Extra wait for all pages to finish rendering
                time.sleep(2)

                attach_text = frame.evaluate("""() => {
                    const layers = document.querySelectorAll('.textLayer, .react-pdf_Page__textContent');
                    return Array.from(layers).map(l => l.textContent).join('\\n\\n');
                }""")
                attach_text = (attach_text or "").strip()

                if not attach_text:
                    if filename.lower().endswith(".pdf"):
                        attachment_results.append(f"--- {filename} --- [image-based, no text layer]")
                    else:
                        attachment_results.append(f"--- {filename} --- [no text]")
                else:
                    attachment_results.append(f"--- {filename} ---\n{attach_text}")
                    any_text = True

            joined = "\n\n".join(attachment_results)

            if any_text:
                print(f"    [OK] Extracted attachment text ({len(joined)} chars) from {title}")
                return joined, "extracted"
            else:
                print(f"    [WARN] All attachments were image-based or empty for: {title}")
                return None, "image_based"

        print(f"    [OK] Extracted {len(text)} chars from {title}")
        return text, "extracted"

    # -----------------------------------------------------------------------
    # ASSESSMENT ITEMS (/assessment/)
    # -----------------------------------------------------------------------

    def _read_assessment_item(self, page: Page, item: dict) -> tuple[str | None, str]:
        """
        Navigate to an /assessment/ URL and extract the description,
        due date, and any other details from the assessment detail panel.
        """
        url = item["url"]
        title = item.get("title", "")

        page.goto("about:blank")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._verify_navigation(page, "/outline/assessment/")
        self._wait_for_heading(page, title)

        # Wait for the assessment panel to render
        time.sleep(3)

        text = page.evaluate("""() => {
            const parts = [];

            const dueDateEl = document.querySelector('.js-due-date');
            if (dueDateEl) {
                const t = dueDateEl.textContent.trim();
                if (t) parts.push(t);
            }

            const attemptsEl = document.querySelector('.overview-attempts-section');
            if (attemptsEl) {
                const t = attemptsEl.textContent.trim();
                if (t) parts.push(t);
            }

            const descEl = document.querySelector('.ql-editor.bb-editor');
            if (descEl) {
                const t = descEl.textContent.trim();
                if (t) parts.push(t);
            }

            return parts.join('\\n');
        }""")

        text = (text or "").strip()

        if not text:
            print(f"    [WARN] No text found for assessment: {title}")
            return None, "no_text"

        print(f"    [OK] Extracted {len(text)} chars from {title}")
        return text, "extracted"

    # -----------------------------------------------------------------------
    # DISCUSSION ITEMS (/discussion/)
    # -----------------------------------------------------------------------

    def _read_discussion_item(self, page: Page, item: dict) -> tuple[str | None, str]:
        """
        Navigate to a /discussion/ URL and extract the discussion topic text.
        """
        url = item["url"]
        title = item.get("title", "")

        page.goto("about:blank")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._verify_navigation(page, "/outline/discussion/")

        # Wait for the discussion content to render
        time.sleep(3)

        text = page.evaluate("""() => {
            // Topic text is in the Quill editor with contenteditable="false"
            const topicEl = document.querySelector('.ql-editor[contenteditable="false"]');
            if (topicEl) return topicEl.textContent.trim();
            return '';
        }""")

        text = (text or "").strip()

        if not text:
            print(f"    [WARN] No text found for discussion: {title}")
            return None, "no_text"

        print(f"    [OK] Extracted {len(text)} chars from {title}")
        return text, "extracted"


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python reader.py <path_to_content_objects.json> [output_path]")
        print("Example: python reader.py output/content_objects_20260316_113920.json")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(input_path):
        print(f"[ERROR] Input file not found: {input_path}")
        sys.exit(1)

    reader = BlackboardReader(input_path, output_path)
    reader.run()


if __name__ == "__main__":
    main()
