"""
Check whether you are logged into https://www.zuxiashiguang.com/.
- Reuses the shared chrome_profile to keep login state.
- If not logged in, waits for manual login and exits when detected.

Usage:
python check_zxsg_login.py
"""

import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from common.browser import create_driver, env_headless, safe_get

LOGIN_URL = "https://www.zuxiashiguang.com/"
TIMEOUT_SECONDS = 180


def start_browser() -> webdriver.Chrome:
    # 需要手动登录（含验证码），默认显示窗口；HEADLESS=true 时仅检查
    return create_driver(headless=env_headless(False), load_images=True, page_load_strategy="eager")


def wait_for_ready(driver: webdriver.Chrome, timeout: int = 10) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(1)
        return True
    except TimeoutException:
        return False


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """Check header for login indicators."""
    indicators_logged_in = [
        (By.CSS_SELECTOR, "#um .vwmy"),
        (By.CSS_SELECTOR, "#um .avt img"),
        (By.XPATH, "//a[contains(@href,'logout') or contains(text(),'退出')]"),
    ]
    for locator in indicators_logged_in:
        try:
            element = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located(locator)
            )
            if element.is_displayed():
                return True
        except TimeoutException:
            continue

    indicators_login_form = [
        (By.CSS_SELECTOR, "form#lsform"),
        (By.CSS_SELECTOR, "form#lsform input[name='username']"),
        (By.CSS_SELECTOR, "form#lsform input[name='password']"),
        (By.XPATH, "//button[contains(.,'登录')]"),
        (By.XPATH, "//a[contains(.,'立即注册')]"),
    ]
    for locator in indicators_login_form:
        try:
            element = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located(locator)
            )
            if element.is_displayed():
                return False
        except TimeoutException:
            continue

    try:
        source = driver.page_source.lower()
        if "logout" in source or "退出" in source:
            return True
        if "立即注册" in source or "username" in source or "登录" in source:
            return False
    except Exception:
        pass

    return True


def main() -> None:
    print("Opening login page...")
    driver = start_browser()

    try:
        safe_get(driver, LOGIN_URL)
        if not wait_for_ready(driver, 10):
            print("Page load timed out; you may retry.")
            return

        if is_logged_in(driver):
            print("Login detected; closing.")
            return

        print("Not logged in. Please login manually in the opened browser.")
        print(f"Waiting up to {TIMEOUT_SECONDS} seconds...")
        start_ts = time.time()

        while time.time() - start_ts < TIMEOUT_SECONDS:
            time.sleep(2)
            if is_logged_in(driver):
                print("Login detected; you can close this window.")
                return

        print("Timed out waiting for login; please try again.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
