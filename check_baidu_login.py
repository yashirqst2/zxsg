"""
Quickly check whether Baidu Netdisk (pan.baidu.com) is logged in.
- If already logged in, print a message and exit.
- If not logged in, open the homepage, surface the login dialog/QR, and exit once login is detected.

Usage:
python check_baidu_login.py
"""

import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from common.browser import create_driver, env_headless, safe_get

CHECK_URL = "https://pan.baidu.com"


def start_browser() -> webdriver.Chrome:
    # 需要扫码登录，默认显示窗口并加载图片（二维码）
    driver = create_driver(headless=env_headless(False), load_images=True, page_load_strategy="normal")
    driver.implicitly_wait(3)  # is_logged_in 依赖 find_element 等待页面元素出现
    return driver


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """Detect login state using cookies, current URL, and navbar elements."""
    cookie_names = {c.get("name") for c in driver.get_cookies()}
    if "BDUSS" in cookie_names:
        return True

    # Passport domain means we're still on login.
    if "passport.baidu.com" in driver.current_url.lower():
        return False

    try:
        driver.find_element(
            By.XPATH,
            "//a[contains(text(),'\\u767b\\u5f55') or contains(text(),'\\u6ce8\\u518c')]"
            " | //span[contains(text(),'\\u53bb\\u767b\\u5f55')]",
        )
        return False
    except NoSuchElementException:
        pass

    # Login panel in dialog (QR/account/SMS tabs)
    try:
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'pass-login-tab') or contains(@class,'pass-form-account')]"
            " | //span[contains(text(),'\\u626b\\u7801\\u767b\\u5f55')]"
            " | //span[contains(text(),'\\u5e10\\u53f7\\u767b\\u5f55')]",
        )
        return False
    except NoSuchElementException:
        pass

    try:
        driver.find_element(
            By.XPATH,
            "//img[contains(@class,'avatar') or contains(@class,'userinfo')]"
            " | //span[contains(@class,'user') or contains(@class,'vip') or contains(@class,'name')]",
        )
        return True
    except NoSuchElementException:
        return False


def wait_for_ready(driver: webdriver.Chrome, timeout: int = 10) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(1)
        return True
    except TimeoutException:
        return False


def open_login_panel(driver: webdriver.Chrome) -> None:
    """Click the login entry on the homepage to open the login dialog."""
    try:
        btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//a[contains(@href,'passport') or contains(text(),'\\u767b\\u5f55')]"
                    " | //span[contains(text(),'\\u53bb\\u767b\\u5f55')]",
                )
            )
        )
        btn.click()
    except Exception:
        pass


def switch_to_passport_iframe(driver: webdriver.Chrome) -> bool:
    """If a passport iframe exists, switch into it."""
    try:
        frame = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='passport']"))
        )
        driver.switch_to.frame(frame)
        return True
    except TimeoutException:
        return False


def ensure_qr_login_shown(driver: webdriver.Chrome) -> None:
    """
    Make sure the QR login tab is active and its image is refreshed,
    addressing blank/broken QR images.
    """
    print("正在加载登录页面...")
    
    time.sleep(2)  # 等待页面稳定
    
    # 尝试切换到iframe（静默处理）
    try:
        switch_to_passport_iframe(driver)
    except Exception:
        pass

    # 尝试点击二维码登录标签（静默处理）
    try:
        qr_tab = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//span[contains(text(),'\\u626b\\u7801\\u767b\\u5f55') or contains(text(),'\\u4e8c\\u7ef4\\u7801\\u767b\\u5f55') or contains(text(),'扫码登录')]",
                )
            )
        )
        qr_tab.click()
        time.sleep(2)
    except Exception:
        pass  # 可能已经在二维码页面

    # 简单检查二维码是否加载（最多等待8秒）
    qr_loaded = False
    for _ in range(4):
        try:
            qr_info = driver.execute_script(
                """
                var imgs = document.querySelectorAll('img');
                for (var i = 0; i < imgs.length; i++) {
                    var img = imgs[i];
                    var src = img.src || '';
                    if (src.includes('qrcode') || src.includes('passport') || 
                        img.className.includes('qrcode') || img.className.includes('pass-qrcode')) {
                        return {
                            found: true,
                            complete: img.complete,
                            naturalWidth: img.naturalWidth
                        };
                    }
                }
                return {found: false};
                """
            )
            
            if qr_info.get('found') and qr_info.get('complete') and qr_info.get('naturalWidth', 0) > 50:
                print("✓ 二维码已加载，请使用百度网盘APP扫码")
                qr_loaded = True
                break
            time.sleep(2)
        except Exception:
            time.sleep(2)
    
    if not qr_loaded:
        print("提示：如果二维码未显示，可以点击'账号登录'或'短信登录'标签")
    
    # 切回主页面
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


def main() -> None:
    print("正在打开百度网盘检查登录状态...\n")
    driver = start_browser()

    try:
        safe_get(driver, CHECK_URL)
        wait_for_ready(driver, 10)

        if is_logged_in(driver):
            print("✓ 已登录百度网盘")
            return

        open_login_panel(driver)
        ensure_qr_login_shown(driver)

        print("\n等待登录中... (按 Ctrl+C 可以取消)")

        check_count = 0
        while True:
            time.sleep(2)
            check_count += 1
            
            if is_logged_in(driver):
                print("\n✓ 登录成功！")
                time.sleep(2)
                return
            
            # 每30秒提示一次
            if check_count % 15 == 0:
                print(f"  仍在等待... (已等待 {check_count * 2} 秒)")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
