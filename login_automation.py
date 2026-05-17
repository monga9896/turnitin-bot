import logging
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CHROMIUM_PATH = "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium-browser"
CHROMEDRIVER_PATH = "/usr/bin/chromedriver"

# Matches the installed Chromium version; keeps User-Agent consistent
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def build_driver(user_data_dir: str | None = None) -> webdriver.Chrome:
    """
    Build a stealth-hardened Chrome driver.

    Stealth measures applied:
    - AutomationControlled blink feature disabled
    - useAutomationExtension and enable-automation switch removed
    - Realistic Windows/Chrome user-agent injected
    - navigator.webdriver hidden via CDP on every new document
    - Normal 1366×768 laptop viewport (not a suspicious 1920×1080 headless default)
    - Headless mode controlled by env var HEADLESS (default: true).
      Set HEADLESS=false to run with a visible window (requires a display server).

    Args:
        user_data_dir: Path to a persistent Chrome profile directory.
                       Cookies, sessions, and local storage survive across runs.
    """
    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    options = Options()
    options.binary_location = CHROMIUM_PATH

    # ── Stealth: remove automation fingerprints ───────────────────────────
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # ── Stealth: realistic user-agent ─────────────────────────────────────
    options.add_argument(f"--user-agent={CHROME_USER_AGENT}")

    # ── Stealth: normal laptop window size ────────────────────────────────
    options.add_argument("--window-size=1366,768")

    # ── Headless / display ────────────────────────────────────────────────
    if headless:
        options.add_argument("--headless=new")
        logger.info("Chrome running in headless mode")
    else:
        logger.info("Chrome running in visible (non-headless) mode — display required")

    # ── Sandbox / shared-memory flags needed in container envs ───────────
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    # ── Persistent profile ────────────────────────────────────────────────
    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={user_data_dir}")
        options.add_argument("--profile-directory=Default")
        logger.info("Using persistent Chrome profile at: %s", user_data_dir)

        service = Service(CHROMEDRIVER_PATH)
        driver = webdriver.Chrome(service=service, options=options)
    # ── Stealth: hide navigator.webdriver on every page load ──────────────
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": (
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
                "Object.defineProperty(navigator, 'languages', "
                "{get: () => ['en-US', 'en']});"
                "Object.defineProperty(navigator, 'plugins', "
                "{get: () => [1, 2, 3]});"
            )
        },
    )

    driver.implicitly_wait(5)
    return driver


def login(
    url: str,
    username: str,
    password: str,
    username_selector: str = "input[name='username'], input[name='email'], input[type='email']",
    password_selector: str = "input[name='password'], input[type='password']",
    submit_selector: str = "button[type='submit'], input[type='submit']",
    success_indicator: str | None = None,
    timeout: int = 15,
) -> dict:
    """
    Perform a headless Chrome login workflow.

    Args:
        url: Login page URL.
        username: Username or email to enter.
        password: Password to enter.
        username_selector: CSS selector for the username/email field.
        password_selector: CSS selector for the password field.
        submit_selector: CSS selector for the submit/login button.
        success_indicator: Optional CSS selector that appears after a successful login.
        timeout: Max seconds to wait for elements.

    Returns:
        dict with keys: success (bool), message (str), final_url (str), title (str).
    """
    driver = build_driver()
    wait = WebDriverWait(driver, timeout)

    try:
        logger.info("Navigating to %s", url)
        driver.get(url)

        logger.info("Locating username field")
        username_field = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, username_selector))
        )
        username_field.clear()
        username_field.send_keys(username)

        logger.info("Locating password field")
        password_field = driver.find_element(By.CSS_SELECTOR, password_selector)
        password_field.clear()
        password_field.send_keys(password)

        logger.info("Submitting login form")
        submit_button = driver.find_element(By.CSS_SELECTOR, submit_selector)
        submit_button.click()

        if success_indicator:
            logger.info("Waiting for success indicator: %s", success_indicator)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, success_indicator)))

        final_url = driver.current_url
        title = driver.title
        logger.info("Login complete. Final URL: %s | Title: %s", final_url, title)

        return {
            "success": True,
            "message": "Login workflow completed successfully.",
            "final_url": final_url,
            "title": title,
        }

    except TimeoutException as exc:
        logger.error("Timed out waiting for element: %s", exc)
        return {
            "success": False,
            "message": f"Timeout: {exc}",
            "final_url": driver.current_url,
            "title": driver.title,
        }
    except NoSuchElementException as exc:
        logger.error("Element not found: %s", exc)
        return {
            "success": False,
            "message": f"Element not found: {exc}",
            "final_url": driver.current_url,
            "title": driver.title,
        }
    finally:
        driver.quit()


if __name__ == "__main__":
    # Example: replace with your target login page and credentials
    result = login(
        url="https://the-internet.herokuapp.com/login",
        username="tomsmith",
        password="SuperSecretPassword!",
        success_indicator=".flash.success",
    )

    print("\n--- Login Result ---")
    for key, value in result.items():
        print(f"{key}: {value}")
