"""统一的 Chrome 启动配置，所有脚本共用同一个 chrome_profile。"""

import os
from pathlib import Path
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from common import config
from common.config import CHROME_PROFILE_DIR, PROJECT_ROOT


def env_headless(default: bool) -> bool:
    """读取 HEADLESS 环境变量（true/false），未设置时使用 default。"""
    value = os.environ.get("HEADLESS")
    if value is None:
        return default
    return value.strip().lower() == "true"


def create_driver(headless: bool, load_images: bool = True,
                  page_load_strategy: str = "eager",
                  block_urls: Optional[List[str]] = None) -> webdriver.Chrome:
    """
    block_urls: 不加载的请求（支持 * 通配），如统计脚本。
    这类请求在无头模式下可能让渲染进程卡住，导致页面只有标题没有内容。
    """
    options = Options()
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--log-level=3")
    # 去掉"受自动化控制"提示，并屏蔽 chromedriver 往控制台打印的 GPU/DevTools 日志
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")

    # 图片开关必须每次显式设置：该设置会写入共享的 chrome_profile，
    # 若残留"禁用"，有界面登录时验证码/二维码将无法显示
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 1 if load_images else 2,
        "profile.default_content_setting_values.notifications": 2,
    })
    options.page_load_strategy = page_load_strategy

    local_driver = Path(config.get("browser.chromedriver_path", "chromedriver.exe"))
    if not local_driver.is_absolute():
        local_driver = PROJECT_ROOT / local_driver
    # 没有本地 chromedriver 时由 Selenium Manager 自动匹配浏览器版本
    service = Service(str(local_driver)) if local_driver.exists() else Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    })
    if block_urls:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": block_urls})
    driver.implicitly_wait(0)
    driver.set_page_load_timeout(config.get("browser.page_load_timeout", 25))
    driver.set_script_timeout(10)
    return driver


def safe_get(driver: webdriver.Chrome, url: str) -> bool:
    """
    打开页面。页面加载超时（如 "Timed out receiving message from renderer"）时
    停止加载并继续使用已加载的部分；只有页面完全不可用时才返回 False。
    """
    try:
        driver.get(url)
        return True
    except TimeoutException:
        pass
    except WebDriverException:
        return False

    has_body = "return !!document.body"
    try:
        driver.execute_script("window.stop();")
        if driver.execute_script(has_body):
            return True
        # 渲染进程超时后页面可能停在只有标题、没有 body 的状态，刷新一次即可恢复
        try:
            driver.refresh()
        except TimeoutException:
            driver.execute_script("window.stop();")
        return bool(driver.execute_script(has_body))
    except WebDriverException:
        return False
