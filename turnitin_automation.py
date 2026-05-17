import logging
import os
import random
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from login_automation import build_driver

logger = logging.getLogger(__name__)

ERROR_LOG = Path(__file__).parent / "error.log"


def human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """Sleep for a random duration to mimic human think-time between actions."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Human delay: %.2f s", delay)
    time.sleep(delay)


TURNITIN_LOGIN_URL = "https://www.turnitin.com/login_page.asp?lang=en_us"


DASHBOARD_LINKS_FILE = Path(__file__).parent / "reports" / "dashboard_links.txt"


def _save_dashboard_links(driver, reports_dir: Path) -> str:
    """
    Capture every visible <a> tag on the current page, log each one,
    and write them all to dashboard_links.txt.

    Returns the path to the saved file.
    """
    links = driver.find_elements(By.TAG_NAME, "a")
    rows = []
    for el in links:
        try:
            text = el.text.strip()
            href = el.get_attribute("href") or ""
            if not text and not href:
                continue
            rows.append((text or "(no text)", href or "(no href)"))
        except Exception:
            continue

    reports_dir.mkdir(exist_ok=True)
    out_path = str(reports_dir / "dashboard_links.txt")

    logger.info("── Dashboard links captured (%d total) ──", len(rows))
    lines = [f"Page URL : {driver.current_url}\n", f"Total links: {len(rows)}\n\n"]
    for text, href in rows:
        line = f"[{text}]  →  {href}"
        logger.info("  %s", line)
        lines.append(line + "\n")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    logger.info("Dashboard links saved to: %s", out_path)
    return out_path


def _capture_page_links(driver, reports_dir: Path, filename: str) -> str:
    """
    Save every visible <a> tag (text + href) on the current page to a file.
    Returns the saved file path.
    """
    links = driver.find_elements(By.TAG_NAME, "a")
    buttons = driver.find_elements(By.TAG_NAME, "button")
    inputs = driver.find_elements(By.XPATH, "//input[@type='submit' or @type='button']")

    reports_dir.mkdir(exist_ok=True)
    out_path = reports_dir / filename

    rows: list[str] = [
        f"Page URL : {driver.current_url}\n",
        f"Page title: {driver.title}\n\n",
        "─── LINKS ────────────────────────────────────────────\n",
    ]
    for el in links:
        try:
            text = el.text.strip()
            href = el.get_attribute("href") or ""
            if text or href:
                entry = f"[{text or '(no text)'}]  →  {href or '(no href)'}"
                logger.info("  link: %s", entry)
                rows.append(entry + "\n")
        except Exception:
            continue

    rows.append("\n─── BUTTONS ──────────────────────────────────────────\n")
    for el in buttons:
        try:
            text = el.text.strip()
            if text:
                entry = f"[button] {text}"
                logger.info("  btn : %s", entry)
                rows.append(entry + "\n")
        except Exception:
            continue

    rows.append("\n─── SUBMIT INPUTS ────────────────────────────────────\n")
    for el in inputs:
        try:
            val = el.get_attribute("value") or ""
            if val:
                entry = f"[input] value='{val}'"
                logger.info("  inp : %s", entry)
                rows.append(entry + "\n")
        except Exception:
            continue

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.writelines(rows)

    logger.info("Page links saved to: %s  (%d entries)", out_path, len(rows))
    return str(out_path)


def _capture_failure(driver, exc: BaseException, reports_dir: Path,
                     dashboard_links_path: str | None = None,
                     assignment_links_path: str | None = None) -> dict:
    """
    Centralised failure handler. Always called when the automation hits an error.

    - Logs full traceback to console
    - Appends traceback + page URL to error.log
    - Saves a Selenium screenshot

    Returns a dict suitable for use as the function's return value.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("Turnitin automation failed:\n%s", tb_text)

    # ── Page URL ───────────────────────────────────────────────────────────
    try:
        page_url = driver.current_url
    except Exception:
        page_url = "unknown (driver unavailable)"

    # ── Screenshot ────────────────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(reports_dir / f"error_{timestamp}.png")
    try:
        reports_dir.mkdir(exist_ok=True)
        driver.save_screenshot(screenshot_path)
        logger.info("Error screenshot saved: %s", screenshot_path)
    except Exception as ss_exc:
        logger.warning("Could not save screenshot: %s", ss_exc)
        screenshot_path = None

    # ── error.log ─────────────────────────────────────────────────────────
    try:
        with open(ERROR_LOG, "a") as fh:
            fh.write(f"\n{'='*60}\n")
            fh.write(f"Timestamp : {datetime.now().isoformat()}\n")
            fh.write(f"Page URL  : {page_url}\n")
            fh.write(f"Screenshot: {screenshot_path}\n")
            fh.write(f"Traceback :\n{tb_text}\n")
    except Exception as log_exc:
        logger.warning("Could not write error.log: %s", log_exc)

    return {
        "success": False,
        "message": str(exc),
        "traceback": tb_text,
        "page_url": page_url,
        "screenshot_path": screenshot_path,
        "dashboard_links_path": dashboard_links_path,
        "assignment_links_path": assignment_links_path,
        "login_debug_html_path": None,
        "login_failed": False,
        "report_path": None,
    }


def test_login() -> dict:
    """
    Run only the login step and return the result.

    Returns a dict with keys:
        success (bool), message (str), url (str), page_title (str),
        screenshot_path (str | None), login_debug_html_path (str | None)
    """
    email = os.environ.get("TURNITIN_EMAIL")
    password = os.environ.get("TURNITIN_PASSWORD")
    if not email or not password:
        return {
            "success": False,
            "message": "TURNITIN_EMAIL or TURNITIN_PASSWORD environment variable is not set.",
            "url": "",
            "page_title": "",
            "screenshot_path": None,
            "login_debug_html_path": None,
        }

    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)

    driver = build_driver(user_data_dir=CHROME_PROFILE_DIR)
    try:
        logger.info("[test_login] Navigating to %s", TURNITIN_LOGIN_URL)
        driver.get(TURNITIN_LOGIN_URL)
        human_delay(2.0, 3.5)

        logger.info(
            "[test_login] Login page — URL: %s | Title: %s",
            driver.current_url, driver.title,
        )

        logger.info("[test_login] Filling credentials for: %s", email)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "email"))
        ).send_keys(email)
        human_delay(0.8, 1.5)
        driver.find_element(By.ID, "password").send_keys(password)
        human_delay(0.8, 1.5)
        driver.find_element(
            By.CSS_SELECTOR, "input[type='submit'], button[type='submit']"
        ).click()

        human_delay(3.0, 5.0)
        logger.info(
            "[test_login] After submit — URL: %s | Title: %s",
            driver.current_url, driver.title,
        )

        # ── Check authentication ───────────────────────────────────────────
        success, auth_reason = _is_authenticated(driver)
        if success:
            logger.info("[test_login] ✓ Authenticated — %s", auth_reason)
        else:
            logger.error("[test_login] Authentication failed — %s", auth_reason)

        # ── Step 2: If login OK, check class is visible ───────────────────
        class_found = False
        class_url = ""
        assignment_found = False
        assignment_url = ""

        if success:
            human_delay(1.5, 2.5)
            logger.info(
                "[test_login] Checking dashboard for class '%s' (ID: %s)",
                TARGET_CLASS_NAME, TARGET_CLASS_ID,
            )
            try:
                class_link = WebDriverWait(driver, 20).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, f"//a[contains(@href, '{TARGET_CLASS_ID}')]")
                    )
                )
                class_url = class_link.get_attribute("href") or ""
                class_found = True
                logger.info(
                    "[test_login] ✓ Class found — text: '%s'  href: %s",
                    class_link.text.strip(), class_url,
                )
            except TimeoutException:
                # Fall back to matching by visible text
                try:
                    class_link = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable(
                            (By.XPATH,
                             f"//*[normalize-space(text())='{TARGET_CLASS_NAME}'"
                             f" and (self::a or ancestor::a)]")
                        )
                    )
                    class_url = class_link.get_attribute("href") or ""
                    class_found = True
                    logger.info(
                        "[test_login] ✓ Class found by name — href: %s", class_url
                    )
                except TimeoutException:
                    logger.warning(
                        "[test_login] Class '%s' not found on dashboard",
                        TARGET_CLASS_NAME,
                    )

        # ── Step 3: If class found, open it and check assignment ──────────
        if class_found:
            human_delay(1.0, 2.0)
            driver.get(class_url)
            human_delay(2.0, 3.5)
            logger.info(
                "[test_login] Opened class page — URL: %s | Title: %s",
                driver.current_url, driver.title,
            )
            logger.info(
                "[test_login] Checking for assignment '%s'", TARGET_ASSIGNMENT_NAME
            )
            _asgn_lower = TARGET_ASSIGNMENT_NAME.lower()
            _LCASE = "abcdefghijklmnopqrstuvwxyz"
            _UCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            _ci_text = (
                f"contains("
                f"translate(normalize-space(.),"
                f"'{_UCASE}','{_LCASE}'),"
                f"'{_asgn_lower}')"
            )
            try:
                # XPath: case-insensitive, full descendant text
                asgn_el = WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located(
                        (By.XPATH, f"//*[{_ci_text}]")
                    )
                )
                raw = " ".join(asgn_el.text.split())
                logger.info(
                    "[test_login] Assignment element found — text: '%s'", raw[:150]
                )
                # Walk up to find the nearest ancestor with a View link
                href = asgn_el.get_attribute("href") or ""
                assignment_url = href
                assignment_found = True
                logger.info(
                    "[test_login] ✓ Assignment confirmed — text: '%s'", raw[:100]
                )
            except TimeoutException:
                # Python fallback: scan all elements
                logger.warning(
                    "[test_login] XPath timed out — running Python text scan"
                )
                all_els = driver.find_elements(By.XPATH, "//*")
                for el in all_els:
                    try:
                        if not el.is_displayed():
                            continue
                        raw = " ".join(el.text.split()).lower()
                        if _asgn_lower in raw:
                            full_text = " ".join(el.text.split())
                            logger.info(
                                "[test_login] Python scan match: '%s'", full_text[:150]
                            )
                            assignment_found = True
                            break
                    except Exception:
                        continue
                if not assignment_found:
                    logger.warning(
                        "[test_login] Assignment '%s' not found in class",
                        TARGET_ASSIGNMENT_NAME,
                    )

        # ── Always capture screenshot + HTML ──────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = str(reports_dir / f"test_login_{timestamp}.png")
        try:
            driver.save_screenshot(screenshot_path)
            logger.info("[test_login] Screenshot saved: %s", screenshot_path)
        except Exception as ss_exc:
            logger.warning("[test_login] Could not save screenshot: %s", ss_exc)
            screenshot_path = None

        html_path = str(reports_dir / "login_debug.html")
        try:
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(driver.page_source)
            logger.info("[test_login] HTML saved: %s", html_path)
        except Exception as html_exc:
            logger.warning("[test_login] Could not save HTML: %s", html_exc)
            html_path = None

        # Build summary message
        if not success:
            msg = "Login failed — dashboard not detected"
        elif not class_found:
            msg = f"Login OK, but class '{TARGET_CLASS_NAME}' not found on dashboard"
        elif not assignment_found:
            msg = (
                f"Login OK, class found, but assignment "
                f"'{TARGET_ASSIGNMENT_NAME}' not found"
            )
        else:
            msg = "Login OK, class found, assignment found — all checks passed"

        return {
            "success": success,
            "message": msg,
            "url": driver.current_url,
            "page_title": driver.title,
            "class_found": class_found,
            "class_url": class_url,
            "assignment_found": assignment_found,
            "assignment_url": assignment_url,
            "screenshot_path": screenshot_path,
            "login_debug_html_path": html_path,
        }

    except Exception as exc:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error("[test_login] Exception:\n%s", tb_text)
        return {
            "success": False,
            "message": str(exc),
            "url": "",
            "page_title": "",
            "screenshot_path": None,
            "login_debug_html_path": None,
        }
    finally:
        driver.quit()


def _is_authenticated(driver) -> tuple[bool, str]:
    """
    Broad authentication check — returns (authenticated: bool, reason: str).

    FAILURE only when:
      - URL contains 'login_page.asp'
      - URL contains 'err=' (Turnitin error param after failed login)
      - Both #email and #password form fields are still present on the page

    SUCCESS when any of:
      - URL contains t_home.asp, s_home, home_student, class_id, assignment_id
      - A Logout link is visible (href or text)
      - Text "All Classes", "Instructor", "My Classes" visible on page
      - s_class / t_class links present
      - #content-wrapper element present
    """
    url = driver.current_url

    # ── Hard failure signals ───────────────────────────────────────────────
    if "login_page.asp" in url:
        return False, f"Still on login page: {url}"

    if "err=" in url:
        return False, f"Turnitin returned an error param in URL: {url}"

    # If both email + password fields are visible → form not submitted / rejected
    try:
        email_field = driver.find_element(By.ID, "email")
        pwd_field = driver.find_element(By.ID, "password")
        if email_field.is_displayed() and pwd_field.is_displayed():
            return False, "Login form still visible on page"
    except NoSuchElementException:
        pass  # form gone → good sign

    # ── Success signals (URL-based) ────────────────────────────────────────
    AUTH_URL_KEYWORDS = (
        "t_home.asp", "s_home", "home_student",
        "s_class", "t_class", "class_id", "assignment_id",
    )
    if any(kw in url for kw in AUTH_URL_KEYWORDS):
        return True, f"Authenticated URL detected: {url}"

    # ── Success signals (page content) ────────────────────────────────────
    _AUTH_XPATH = (
        "//a[contains(translate(@href,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logout')] | "
        "//*[normalize-space(text())='Logout' or normalize-space(text())='Log out'] | "
        "//*[contains(normalize-space(text()),'All Classes')] | "
        "//*[contains(normalize-space(text()),'My Classes')] | "
        "//*[contains(normalize-space(text()),'Instructor')] | "
        "//a[contains(@href,'s_class')] | "
        "//a[contains(@href,'t_class')] | "
        "//*[@id='content-wrapper']"
    )
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.XPATH, _AUTH_XPATH))
        )
        return True, f"Authenticated dashboard element found (URL: {url})"
    except TimeoutException:
        return False, f"No dashboard elements detected within 25 s (URL: {url})"


TARGET_CLASS_ID = "53199146"
TARGET_CLASS_NAME = "Political Science"
TARGET_ASSIGNMENT_NAME = "mohit sir assignment"

# Persistent Chrome profile — survives across bot restarts
CHROME_PROFILE_DIR = str(Path(__file__).parent / "chrome-profile")



def submit_and_get_report(file_path: str, timeout: int = 1800) -> dict:
    """
    Full Turnitin workflow:
    1. Launch Chrome with a persistent profile — reuses existing session if still valid.
    2. Log in only when the session has expired.
    3. Navigate directly to class 53199146 (Political Science).
    4. Select assignment "mohit sir assignment" by name.
    5. Upload the file.
    6. Poll until the similarity report is ready.
    7. Download the report PDF and return its local path.

    Args:
        file_path: Absolute path to the PDF or DOCX file to submit.
        timeout: Maximum seconds to wait for the similarity report (default 30 min).

    Returns:
        dict with keys: success (bool), message (str), report_path (str | None)
    """
    email = os.environ.get("TURNITIN_EMAIL")
    password = os.environ.get("TURNITIN_PASSWORD")
    if not email or not password:
        return {
            "success": False,
            "message": "TURNITIN_EMAIL or TURNITIN_PASSWORD not set.",
            "report_path": None,
        }

    driver = build_driver(user_data_dir=CHROME_PROFILE_DIR)
    wait = WebDriverWait(driver, 30)
    title = Path(file_path).stem
    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    dashboard_links_path: str | None = None
    assignment_links_path: str | None = None

    try:
        # ── Step 1: Navigate to login page and authenticate ───────────────
        logger.info("Navigating to Turnitin login page: %s", TURNITIN_LOGIN_URL)
        driver.get(TURNITIN_LOGIN_URL)
        human_delay(2.0, 3.5)

        logger.info(
            "Login page loaded — URL: %s | Title: %s",
            driver.current_url,
            driver.title,
        )

        logger.info("Filling credentials for: %s", email)
        wait.until(EC.presence_of_element_located((By.ID, "email"))).send_keys(email)
        human_delay(0.8, 2.0)
        driver.find_element(By.ID, "password").send_keys(password)
        human_delay(0.8, 1.8)
        driver.find_element(
            By.CSS_SELECTOR, "input[type='submit'], button[type='submit']"
        ).click()

        # Let Turnitin redirect naturally — do NOT force any URL
        human_delay(3.0, 5.0)
        logger.info(
            "Login form submitted — URL: %s | Title: %s",
            driver.current_url,
            driver.title,
        )

        # ── Verify authentication ──────────────────────────────────────────
        logger.info("Verifying authentication…")
        auth_ok, auth_reason = _is_authenticated(driver)
        login_failed = not auth_ok
        if auth_ok:
            logger.info("✓ Authenticated — %s", auth_reason)
        else:
            logger.error("Authentication failed — %s", auth_reason)

        # ── Always capture post-login screenshot + HTML ────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        post_login_screenshot = str(reports_dir / f"login_{timestamp}.png")
        try:
            driver.save_screenshot(post_login_screenshot)
            logger.info("Post-login screenshot saved: %s", post_login_screenshot)
        except Exception as ss_exc:
            logger.warning("Could not save post-login screenshot: %s", ss_exc)
            post_login_screenshot = None

        logger.info("Post-login URL: %s", driver.current_url)

        html_path = str(reports_dir / "login_debug.html")
        try:
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(driver.page_source)
            logger.info("Login page HTML saved: %s", html_path)
        except Exception as html_exc:
            logger.warning("Could not save login HTML: %s", html_exc)
            html_path = None

        # ── Abort immediately if not authenticated ─────────────────────────
        if login_failed:
            return {
                "success": False,
                "message": "Turnitin login failed",
                "report_path": None,
                "traceback": "",
                "page_url": driver.current_url,
                "screenshot_path": post_login_screenshot,
                "login_debug_html_path": html_path,
                "dashboard_links_path": None,
                "login_failed": True,
            }

        human_delay(2.0, 4.0)

        # ── Step 2: Capture all dashboard links, then navigate to class ────
        logger.info("Capturing all links visible on the dashboard")
        dashboard_links_path = _save_dashboard_links(driver, reports_dir)

        logger.info(
            "Looking for class '%s' (ID: %s) on dashboard",
            TARGET_CLASS_NAME,
            TARGET_CLASS_ID,
        )

        # Try to find a link whose href contains the class ID
        try:
            class_link = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, f"//a[contains(@href, '{TARGET_CLASS_ID}')]")
                )
            )
            found_class_text = class_link.text.strip()
            logger.info(
                "Found class link by ID — link text: '%s', href: %s",
                found_class_text,
                class_link.get_attribute("href"),
            )
        except TimeoutException:
            # Fall back: find by visible class name text
            logger.info("Class ID not found in hrefs, searching by class name text")
            class_link = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, f"//*[normalize-space(text())='{TARGET_CLASS_NAME}' and (self::a or ancestor::a)]")
                )
            )
            logger.info(
                "Found class link by name — href: %s",
                class_link.get_attribute("href"),
            )

        human_delay(1.0, 2.5)
        class_link.click()
        wait.until(EC.url_changes(TURNITIN_LOGIN_URL))
        logger.info(
            "✓ Opened class '%s' — URL: %s", TARGET_CLASS_NAME, driver.current_url
        )

        # ── Step 3: Capture all links/buttons on the class page ───────────
        human_delay(1.5, 3.0)
        logger.info("Capturing all links and buttons on class page")
        assignment_links_path = _capture_page_links(
            driver, reports_dir, "assignment_links.txt"
        )

        # ── Step 4: Find assignment row by text → click its View button ───
        logger.info(
            "Searching for assignment row containing '%s'", TARGET_ASSIGNMENT_NAME
        )

        # Walk from the text node upward to the nearest table row or list/div
        # Case-insensitive XPath: translate() lowercases both sides,
        # normalize-space(.) captures full descendant text (not just direct text nodes).
        _asgn_lower = TARGET_ASSIGNMENT_NAME.lower()
        _LCASE = "abcdefghijklmnopqrstuvwxyz"
        _UCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        _ci = (
            f"contains("
            f"translate(normalize-space(.),"
            f"'{_UCASE}','{_LCASE}'),"
            f"'{_asgn_lower}')"
        )

        _ROW_ANCESTORS = (
            f"//*[{_ci}]/ancestor::tr[1] | "
            f"//*[{_ci}]/ancestor::li[1] | "
            f"//*[{_ci}]/ancestor::div[contains(@class,'assignment')][1] | "
            f"//*[{_ci}]/ancestor::div[contains(@class,'row')][1] | "
            f"//*[{_ci}]/ancestor::div[contains(@class,'item')][1]"
        )

        assignment_row = None
        for attempt in range(2):
            try:
                candidates = driver.find_elements(By.XPATH, _ROW_ANCESTORS)
                candidates = [c for c in candidates if c.is_displayed()]
                logger.info(
                    "Attempt %d — XPath returned %d candidate row(s)",
                    attempt + 1, len(candidates),
                )
                for idx, c in enumerate(candidates):
                    raw = " ".join(c.text.split())  # collapse whitespace
                    normalized = raw.lower()
                    matched = _asgn_lower in normalized
                    logger.info(
                        "  candidate[%d] matched=%s text: %s",
                        idx, matched, raw[:150],
                    )
                    if matched and assignment_row is None:
                        assignment_row = c

                if assignment_row:
                    logger.info(
                        "✓ Assignment row confirmed (attempt %d) — text: %s",
                        attempt + 1,
                        " ".join(assignment_row.text.split())[:200],
                    )
                    break
            except Exception as row_exc:
                logger.warning("Row search attempt %d failed: %s", attempt + 1, row_exc)

            # Python-level fallback: scan every visible element for the text
            if assignment_row is None:
                logger.info(
                    "XPath candidates empty — running Python full-page text scan"
                )
                all_els = driver.find_elements(By.XPATH, "//tr | //li | //div")
                for el in all_els:
                    try:
                        if not el.is_displayed():
                            continue
                        raw = " ".join(el.text.split())
                        if _asgn_lower in raw.lower() and raw.strip():
                            logger.info(
                                "  Python scan found row: %s", raw[:150]
                            )
                            assignment_row = el
                            break
                    except Exception:
                        continue

            if assignment_row:
                break
            human_delay(1.5, 2.5)
            driver.refresh()

        if assignment_row is None:
            raise RuntimeError(
                f"Assignment row for '{TARGET_ASSIGNMENT_NAME}' not found on class page "
                f"(URL: {driver.current_url})"
            )

        # Find the View link/button inside that row
        view_btn = None
        _VIEW_XPATH = (
            ".//a[normalize-space(text())='View'] | "
            ".//button[normalize-space(text())='View'] | "
            ".//input[@value='View'] | "
            ".//a[normalize-space(text())='View All'] | "
            ".//a[contains(normalize-space(text()),'View')]"
        )
        try:
            view_btn = assignment_row.find_element(By.XPATH, _VIEW_XPATH)
        except NoSuchElementException:
            pass

        if view_btn is None:
            # Fallback: any View button on the page (not row-scoped)
            logger.warning(
                "View button not found inside row — trying page-wide search"
            )
            view_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//a[normalize-space(text())='View'] | "
                     "//button[normalize-space(text())='View'] | "
                     "//input[@value='View']")
                )
            )

        logger.info(
            "✓ View button found — text: '%s'  href: %s",
            view_btn.text.strip(),
            view_btn.get_attribute("href") or "(no href)",
        )

        # Screenshot immediately before clicking
        ts_pre = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre_click_ss = str(reports_dir / f"before_view_click_{ts_pre}.png")
        driver.save_screenshot(pre_click_ss)
        logger.info("Screenshot before View click: %s", pre_click_ss)

        # JavaScript click to avoid interception / overlay issues
        human_delay(0.8, 1.5)
        driver.execute_script("arguments[0].click();", view_btn)
        human_delay(2.0, 3.5)
        logger.info("✓ View clicked — URL after click: %s", driver.current_url)

        # Screenshot immediately after clicking
        ts_post = datetime.now().strftime("%Y%m%d_%H%M%S")
        post_click_ss = str(reports_dir / f"after_view_click_{ts_post}.png")
        driver.save_screenshot(post_click_ss)
        logger.info("Screenshot after View click: %s", post_click_ss)

        # ── Step 5: Detect and delete any existing submission ─────────────
        human_delay(2.0, 3.5)
        logger.info("Checking assignment inbox for existing submissions…")

        existing_rows = driver.find_elements(
            By.CSS_SELECTOR,
            "tr.paper-row, tr[id^='paper'], div.submission-row, "
            "table#submissions tbody tr, #paperList tr[class]",
        )
        # Filter to rows that contain meaningful content (not header/empty rows)
        existing_rows = [
            r for r in existing_rows
            if r.text.strip() and r.is_displayed()
        ]

        if existing_rows:
            logger.info(
                "Existing submission found (%d row(s)) — will delete before uploading",
                len(existing_rows),
            )
            for row in existing_rows:
                logger.info("  Submission row text: %s", row.text[:120])

            # Open "More actions" menu (ellipsis / ⋯ / kebab button)
            human_delay(1.0, 2.0)
            more_actions_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//*[normalize-space(text())='More actions' or "
                     "normalize-space(text())='Actions' or "
                     "@title='More actions' or "
                     "contains(@class,'more-actions') or "
                     "contains(@class,'kebab') or "
                     "contains(@class,'dropdown-toggle')]")
                )
            )
            human_delay(0.5, 1.2)
            more_actions_btn.click()
            logger.info("'More actions' menu opened")

            # Click delete / remove option
            human_delay(0.8, 1.5)
            delete_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//*[normalize-space(text())='Delete' or "
                     "normalize-space(text())='Remove' or "
                     "normalize-space(text())='Delete submission' or "
                     "normalize-space(text())='Remove submission']")
                )
            )
            human_delay(0.5, 1.0)
            delete_btn.click()
            logger.info("Delete/remove option clicked")

            # Confirm deletion dialog
            human_delay(1.0, 2.0)
            try:
                confirm_del = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH,
                         "//button[normalize-space(text())='Delete'] | "
                         "//button[normalize-space(text())='Yes'] | "
                         "//button[normalize-space(text())='Yes, delete'] | "
                         "//input[@value='Delete'] | "
                         "//input[@value='Yes']")
                    )
                )
                human_delay(0.5, 1.0)
                confirm_del.click()
                logger.info("Deletion confirmed")
            except TimeoutException:
                logger.warning("No confirmation dialog appeared — proceeding anyway")

            # Wait until the inbox is empty
            logger.info("Waiting for inbox to clear…")
            deadline_del = time.time() + 60
            while time.time() < deadline_del:
                human_delay(2.0, 3.0)
                driver.refresh()
                remaining_rows = driver.find_elements(
                    By.CSS_SELECTOR,
                    "tr.paper-row, tr[id^='paper'], div.submission-row, "
                    "table#submissions tbody tr, #paperList tr[class]",
                )
                remaining_rows = [
                    r for r in remaining_rows
                    if r.text.strip() and r.is_displayed()
                ]
                if not remaining_rows:
                    logger.info("✓ Submission deleted — inbox is now empty")
                    break
            else:
                logger.warning("Inbox did not clear within 60 s — attempting upload anyway")
        else:
            logger.info("No existing submission found — inbox is clear, ready to upload")

        # ── Step 6: Upload new file ────────────────────────────────────────
        logger.info("New upload started — file: %s", file_path)
        human_delay(1.5, 3.0)

        # Look for "Submit" / "Resubmit" / "Add submission" button in inbox
        upload_trigger = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 "//a[contains(normalize-space(text()),'Submit')] | "
                 "//button[contains(normalize-space(text()),'Submit')] | "
                 "//input[@value='Submit'] | "
                 "//a[contains(normalize-space(text()),'Resubmit')] | "
                 "//button[contains(normalize-space(text()),'Resubmit')] | "
                 "//a[contains(normalize-space(text()),'Add submission')]")
            )
        )
        human_delay(0.8, 1.5)
        upload_trigger.click()
        logger.info("Upload form triggered — URL: %s", driver.current_url)

        # Fill title
        human_delay(1.5, 3.0)
        try:
            title_field = wait.until(
                EC.presence_of_element_located((By.ID, "submission_title"))
            )
            title_field.clear()
            human_delay(0.5, 1.2)
            title_field.send_keys(title)
            logger.info("Title field filled: '%s'", title)
        except TimeoutException:
            logger.warning("Title field not found, skipping")

        # Set file
        human_delay(1.0, 2.0)
        file_input = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        file_input.send_keys(file_path)
        logger.info("File input set to: %s", file_path)

        # Submit the form
        human_delay(1.5, 3.0)
        confirm_btn = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR,
                 "input[type='submit'], button[type='submit'], button.btn-upload")
            )
        )
        human_delay(0.8, 1.5)
        confirm_btn.click()
        logger.info("Upload form submitted")

        # Second confirmation dialog (if any)
        human_delay(1.0, 2.5)
        try:
            second_confirm = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR,
                     "input[value='Yes, submit'], button.confirm-submit, "
                     "button[name='confirm']")
                )
            )
            human_delay(0.8, 1.5)
            second_confirm.click()
            logger.info("Second confirmation clicked")
        except TimeoutException:
            pass

        # ── Step 7: Poll for similarity score ─────────────────────────────
        logger.info("Polling for similarity report (up to %d s)…", timeout)
        report_link = None
        deadline = time.time() + timeout
        poll_interval = 30

        while time.time() < deadline:
            try:
                score_el = driver.find_element(
                    By.CSS_SELECTOR,
                    "a.similarity-score, span.similarity-score, "
                    "a[href*='report'], a[href*='similarity'], a[href*='originality']",
                )
                report_link = score_el.get_attribute("href") or driver.current_url
                logger.info("✓ Similarity report is ready — link: %s", report_link)
                break
            except NoSuchElementException:
                remaining = int(deadline - time.time())
                logger.info(
                    "Report not ready yet — waiting %d s (timeout in %d s)…",
                    poll_interval, remaining,
                )
                time.sleep(poll_interval)
                driver.refresh()
        else:
            return {
                "success": False,
                "message": "Timed out waiting for the similarity report.",
                "report_path": None,
            }

        # ── Step 8: Download report PDF ────────────────────────────────────
        logger.info("Report downloaded — navigating to: %s", report_link)
        driver.get(report_link)

        report_path = None
        try:
            pdf_link_el = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     "a[href$='.pdf'], a[href*='download'], a[href*='pdf']")
                )
            )
            pdf_url = pdf_link_el.get_attribute("href")
            logger.info("Found PDF download URL: %s", pdf_url)

            report_path = str(reports_dir / f"{title}_similarity_report.pdf")
            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            opener = urllib.request.build_opener()
            opener.addheaders = [
                ("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())),
                ("User-Agent", "Mozilla/5.0"),
            ]
            opener.retrieve(pdf_url, report_path)
            logger.info("✓ Report saved to: %s", report_path)

        except (TimeoutException, NoSuchElementException):
            logger.warning("No PDF link found — saving screenshot as fallback")
            report_path = str(reports_dir / f"{title}_report_screenshot.png")
            driver.save_screenshot(report_path)
            logger.info("Screenshot saved to: %s", report_path)

        # ── Step 9: Cleanup — delete submission after download ─────────────
        logger.info("Starting cleanup — deleting submission after report download")
        try:
            # Navigate back to the assignment inbox
            driver.back()
            human_delay(2.0, 3.5)

            cleanup_rows = driver.find_elements(
                By.CSS_SELECTOR,
                "tr.paper-row, tr[id^='paper'], div.submission-row, "
                "table#submissions tbody tr, #paperList tr[class]",
            )
            cleanup_rows = [r for r in cleanup_rows if r.text.strip() and r.is_displayed()]

            if cleanup_rows:
                more_btn = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH,
                         "//*[normalize-space(text())='More actions' or "
                         "normalize-space(text())='Actions' or "
                         "@title='More actions' or "
                         "contains(@class,'more-actions') or "
                         "contains(@class,'kebab') or "
                         "contains(@class,'dropdown-toggle')]")
                    )
                )
                human_delay(0.5, 1.0)
                more_btn.click()

                del_btn = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH,
                         "//*[normalize-space(text())='Delete' or "
                         "normalize-space(text())='Remove' or "
                         "normalize-space(text())='Delete submission' or "
                         "normalize-space(text())='Remove submission']")
                    )
                )
                human_delay(0.5, 1.0)
                del_btn.click()

                try:
                    conf = wait.until(
                        EC.element_to_be_clickable(
                            (By.XPATH,
                             "//button[normalize-space(text())='Delete'] | "
                             "//button[normalize-space(text())='Yes'] | "
                             "//input[@value='Delete'] | "
                             "//input[@value='Yes']")
                        )
                    )
                    human_delay(0.5, 1.0)
                    conf.click()
                except TimeoutException:
                    pass

                logger.info("✓ Cleanup deletion completed — submission removed from inbox")
            else:
                logger.info("Cleanup: no submission rows found to delete")

        except Exception as cleanup_exc:
            logger.warning("Cleanup deletion failed (non-fatal): %s", cleanup_exc)

        return {
            "success": True,
            "message": "Similarity report downloaded successfully.",
            "report_path": report_path,
        }

    except Exception as exc:
        return _capture_failure(
            driver, exc, reports_dir, dashboard_links_path, assignment_links_path
        )
    finally:
        driver.quit()
