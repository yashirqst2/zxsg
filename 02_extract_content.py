"""
内容提取模块
从 zuxiashiguang.com 论坛页面直接提取内容
包括：标题、百度网盘链接和提取码、解压密码

结果按帖子 tid 保存在 data/threads.json；已成功提取的帖子不会重复处理，
失败的帖子下次运行会自动重试。extracted_contents.txt 仅为便于阅读的导出文件。

使用方法:
python 02_extract_content.py
"""

import re
import sys
import time
from typing import Optional, Dict

import warnings
warnings.filterwarnings('ignore')

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, NoAlertPresentException

from common.browser import create_driver, env_headless, safe_get
from common.config import LINKS_FILE
from common.store import ThreadStore, thread_id, now, EXPORT_FILE


# 配置常量
LOGIN_URL = "https://www.zuxiashiguang.com/member.php?mod=logging&action=login"
LOGIN_TIMEOUT_SECONDS = 300
FREE_QUOTA_TEXT = "免费额度"


class BrowserManager:
    """浏览器管理器"""

    def __init__(self) -> None:
        self.driver: Optional[webdriver.Chrome] = None
        # 无头模式（默认启用）
        self.headless = env_headless(True)

    def start_browser(self, headless: Optional[bool] = None) -> bool:
        """启动浏览器（headless为None时使用环境变量配置）。无头时禁用图片以提高速度。"""
        headless = self.headless if headless is None else headless
        try:
            self.driver = create_driver(headless=headless, load_images=not headless)
            return True
        except Exception as e:
            print(f'错误: 启动浏览器失败: {e}')
            return False

    def close_browser(self) -> None:
        """关闭浏览器"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None

    def __enter__(self) -> 'BrowserManager':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_browser()


class ContentExtractor:
    """内容提取器"""

    # 正则表达式模式（更灵活匹配）
    BAIDU_LINK_PATTERN = re.compile(r'(https?://pan\.baidu\.com/s/[a-zA-Z0-9_-]+)', re.IGNORECASE)
    LINK_PWD_PATTERN = re.compile(r'pan\.baidu\.com/s/[a-zA-Z0-9_-]+\?(?:[^\s"\'<>]*&)?pwd=([a-zA-Z0-9]{4})', re.IGNORECASE)
    # 只认带"提取码"标签的写法，避免把"解压密码"等误当成提取码
    EXTRACT_CODE_PATTERN = re.compile(r'提取码\s*[：:]?\s*([a-zA-Z0-9]{4})(?![a-zA-Z0-9])', re.IGNORECASE)
    PASSWORD_PATTERN = re.compile(r'解压密码[：:\s]*(.+?)(?:\n|$|链接|提取)', re.IGNORECASE | re.DOTALL)

    def __init__(self):
        pass

    def is_logged_in(self, driver: webdriver.Chrome) -> bool:
        """检查右上角是否已登录"""
        user_indicators = [
            (By.CSS_SELECTOR, "#um .vwmy"),  # 用户名
            (By.CSS_SELECTOR, "#um .avt img"),  # 头像
            (By.XPATH, "//a[contains(@href,'logout') or contains(text(),'退出')]"),
        ]

        # 已登录信号
        for locator in user_indicators:
            try:
                element = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located(locator)
                )
                if element.is_displayed():
                    return True
            except TimeoutException:
                continue

        # 未登录信号：右上角登录表单或“立即注册”
        login_indicators = [
            (By.CSS_SELECTOR, "form#lsform"),
            (By.CSS_SELECTOR, "form#lsform input[name='username']"),
            (By.CSS_SELECTOR, "form#lsform input[name='password']"),
            (By.XPATH, "//button[contains(.,'登录')]"),
            (By.XPATH, "//a[contains(.,'立即注册')]"),
        ]

        for locator in login_indicators:
            try:
                element = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located(locator)
                )
                if element.is_displayed():
                    return False
            except TimeoutException:
                continue

        # 兜底：通过源码关键字判断
        try:
            source = driver.page_source.lower()
            if "logout" in source or "退出" in source:
                return True
            if "立即注册" in source or "username" in source or "登录" in source:
                return False
        except Exception:
            pass

        return True

    @staticmethod
    def quick_logged_in(driver: webdriver.Chrome) -> bool:
        """快速判断是否已登录（不等待，用于轮询）"""
        try:
            return bool(driver.execute_script("""
                return !!(document.querySelector('#um .vwmy') ||
                          document.querySelector('a[href*="action=logout"]'));
            """))
        except Exception:
            return False

    @staticmethod
    def has_baidu_link(driver: webdriver.Chrome) -> bool:
        """当前页面是否已显示百度网盘链接"""
        try:
            return "pan.baidu.com/s/" in driver.page_source
        except Exception:
            return False

    def find_free_quota_button(self, driver: webdriver.Chrome):
        """查找“使用1次免费额度查看付费内容”按钮（按文字定位，取最内层可见元素）"""
        try:
            return driver.execute_script("""
                var key = arguments[0];
                // 优先按 Discuz 插件按钮的 name 定位
                var direct = document.querySelector('button[name=vipfreeread_submit], input[name=vipfreeread_submit]');
                if (direct && direct.offsetParent) return direct;

                var nodes = document.querySelectorAll('button, a, input[type=button], input[type=submit], span, div');
                var best = null;
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    // 只有 input 的文字在 value 里；<button value="1"> 的文字在 innerText 里
                    var text = ((el.tagName === 'INPUT' ? el.value : el.innerText) || '').trim();
                    if (text.indexOf(key) === -1 || text.length > 60) continue;
                    if (!el.offsetParent) continue;  // 不可见
                    if (!best || best.contains(el)) best = el;
                }
                if (!best) return null;
                // 若命中的是按钮内部的文字元素，向上取可点击的父元素
                return best.closest('button, a, input') || best;
            """, FREE_QUOTA_TEXT)
        except Exception:
            return None

    def confirm_popups(self, driver: webdriver.Chrome) -> None:
        """处理点击后可能出现的确认框（浏览器alert/confirm 或 Discuz 弹窗）"""
        try:
            alert = driver.switch_to.alert
            print(f"  ℹ️ 确认对话框: {alert.text}")
            alert.accept()
            return
        except NoAlertPresentException:
            pass
        except Exception:
            pass

        try:
            driver.execute_script("""
                var wins = document.querySelectorAll('[id^="fwin_"], .fwin');
                for (var w = 0; w < wins.length; w++) {
                    if (!wins[w].offsetParent) continue;
                    var btns = wins[w].querySelectorAll('button, input[type=submit], input[type=button], a.pn');
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].value || btns[i].innerText || '').trim();
                        if (/确定|确认|使用|提交/.test(t) && !/取消|关闭/.test(t)) {
                            btns[i].click();
                            return true;
                        }
                    }
                }
                return false;
            """)
        except Exception:
            pass

    def unlock_paid_content(self, driver: webdriver.Chrome, timeout: int = 15) -> bool:
        """
        若页面需要点击“使用1次免费额度查看付费内容”才能显示链接，则点击并等待内容出现。
        返回 True 表示页面上已显示百度链接。
        """
        # 第一种情况：链接已直接显示，不消耗免费额度
        if self.has_baidu_link(driver):
            return True

        button = self.find_free_quota_button(driver)
        if button is None:
            source = driver.page_source
            if "购买主题" in source or "才能浏览" in source:
                print("  ⚠️ 付费内容，但未找到免费额度按钮（可能额度已用完）")
            return False

        # 第二种情况：点击免费额度按钮
        print("  ℹ️ 检测到付费内容，使用1次免费额度查看...")
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
            try:
                button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", button)
        except Exception as e:
            print(f"  ✗ 点击免费额度按钮失败: {e}")
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            self.confirm_popups(driver)
            if self.has_baidu_link(driver):
                self.wait_for_page_load(driver)
                print("  ✓ 已解锁付费内容")
                return True

        # 部分情况下需要刷新页面才会显示
        try:
            driver.refresh()
            self.wait_for_page_load(driver)
        except Exception:
            pass
        if self.has_baidu_link(driver):
            print("  ✓ 已解锁付费内容（刷新后）")
            return True

        print("  ✗ 点击免费额度按钮后仍未显示链接")
        return False

    def wait_for_page_load(self, driver: webdriver.Chrome, timeout: int = 10) -> bool:
        """等待页面加载完成"""
        # 页面上的广告/统计脚本可能让 readyState 长时间停在 interactive，
        # 只要帖子内容（#postlist）已出现即可继续
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script(
                    "return document.readyState === 'complete' || "
                    "(document.readyState === 'interactive' && !!document.querySelector('#postlist, #messagetext, #um'))"
                )
            )
            time.sleep(1)  # 等待内容完全渲染
            return True
        except TimeoutException:
            return False

    def extract_title(self, driver: webdriver.Chrome) -> Optional[str]:
        """提取页面标题"""
        try:
            # 尝试多种选择器来获取标题
            title_selectors = [
                ("css", "h1.thread-title"),  # Discuz常见标题类
                ("css", "h1"),
                ("css", ".thread-title"),
                ("css", "#thread_subject"),
                ("css", "td[class*='thread']"),
                ("css", ".subject"),
                ("xpath", "//h1"),
                ("title", None),  # 使用driver.title
            ]

            for selector_type, selector in title_selectors:
                try:
                    if selector_type == "title":
                        title = driver.title.strip()
                        # 移除网站名称后缀
                        if " - " in title:
                            title = title.split(" - ")[0]
                    elif selector_type == "xpath":
                        title_element = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.XPATH, selector))
                        )
                        title = title_element.text.strip()
                    else:
                        title_element = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        title = title_element.text.strip()
                    
                    if title:
                        # 处理标题：去除前面的数字点和后面的【时长】
                        processed_title = self.process_title(title)
                        if processed_title:
                            return processed_title
                except (TimeoutException, NoSuchElementException):
                    continue

            # 如果都失败，尝试从页面源码中提取
            try:
                page_source = driver.page_source
                # 查找h1标签中的标题
                import re as re_module
                h1_match = re_module.search(r'<h1[^>]*>([^<]+)</h1>', page_source)
                if h1_match:
                    title = h1_match.group(1).strip()
                    processed_title = self.process_title(title)
                    if processed_title:
                        return processed_title
            except Exception:
                pass

            return None
        except Exception:
            return None

    def process_title(self, title: str) -> str:
        """处理标题：去除前面的数字点和后面的【时长】"""
        if not title:
            return ""
        
        # 去除前面的数字点（例如：24979.）
        title = re.sub(r'^\d+\.', '', title).strip()
        
        # 去除后面的【时长】格式（例如：【66分钟】、【1:05:02】等）
        # 匹配【数字分钟】格式（包括中文数字）
        title = re.sub(r'【\d+\s*分钟】', '', title)
        # 匹配【时:分:秒】格式
        title = re.sub(r'【\d+:\d+:\d+】', '', title)
        # 匹配【时:分】格式
        title = re.sub(r'【\d+:\d+】', '', title)
        # 匹配其他可能的【时长】格式
        title = re.sub(r'【[^】]*时长[^】]*】', '', title)
        title = re.sub(r'【[^】]*分[^】]*】', '', title)
        
        return title.strip()

    def extract_page_text(self, driver: webdriver.Chrome) -> str:
        """提取页面文本内容"""
        try:
            # 尝试获取主内容区域（包括隐藏的内容）
            content_selectors = [
                ".t_f",  # Discuz常见内容类
                "#postlist",
                ".postmessage",
                ".postcontent",
                "div[class*='post']",
                "div[class*='content']",
                "div[class*='message']",
                "td.t_f",  # 更具体的Discuz选择器
                "body"
            ]

            for selector in content_selectors:
                try:
                    content_element = WebDriverWait(driver, 3).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    text = content_element.text
                    if text and len(text) > 50:  # 确保有足够内容
                        return text
                except (TimeoutException, NoSuchElementException):
                    continue

            # 如果都没找到，尝试获取所有可见和隐藏的文本
            try:
                # 尝试通过JavaScript获取所有文本内容（包括隐藏元素）
                page_text = driver.execute_script("""
                    var elements = document.querySelectorAll('.t_f, #postlist, .postmessage, div[class*="post"], div[class*="content"]');
                    var texts = [];
                    for (var i = 0; i < elements.length; i++) {
                        var text = elements[i].innerText || elements[i].textContent || '';
                        if (text.trim().length > 50) {
                            texts.push(text.trim());
                        }
                    }
                    return texts.join('\\n');
                """)
                if page_text and len(page_text) > 50:
                    return page_text
            except Exception:
                pass

            # 最后尝试获取整个body文本
            body = driver.find_element(By.TAG_NAME, "body")
            return body.text

        except Exception as e:
            print(f"提取页面文本时出错: {e}")
            return ""

    def extract_baidu_link(self, text: str) -> Optional[str]:
        """从文本中提取百度网盘链接（不含参数）"""
        match = self.BAIDU_LINK_PATTERN.search(text)
        return match.group(1) if match else None

    def extract_code(self, text: str) -> Optional[str]:
        """从文本中提取提取码：优先取"提取码：xxxx"，其次取链接里的 ?pwd=xxxx"""
        for pattern in (self.EXTRACT_CODE_PATTERN, self.LINK_PWD_PATTERN):
            match = pattern.search(text)
            if match:
                return match.group(1).upper()
        return None

    def extract_password(self, text: str) -> Optional[str]:
        """从文本中提取解压密码"""
        match = self.PASSWORD_PATTERN.search(text)
        if match:
            password = match.group(1).strip()
            # 清理可能的多余内容
            password = re.split(r'[\n\r]', password)[0].strip()
            return password
        return None

    def extract_all_content(self, driver: webdriver.Chrome, url: str) -> Dict[str, Optional[str]]:
        """提取所有内容"""
        result = {
            "url": url,
            "title": None,
            "baidu_link": None,
            "extract_code": None,
            "password": None,
            "success": False,
            "need_login": False
        }

        try:
            # 访问页面
            if not safe_get(driver, url):
                print("  ⚠️ 页面打开失败")
                return result

            # 等待页面加载
            if not self.wait_for_page_load(driver):
                print("  ⚠️ 页面加载超时")
                return result

            # 检测登录状态（右上角）
            if not self.is_logged_in(driver):
                print("  ⚠️ 检测到未登录，需要登录")
                result["need_login"] = True
                return result

            # 付费内容：需要点击“使用1次免费额度查看付费内容”才显示链接
            self.unlock_paid_content(driver)

            # 提取标题
            title = self.extract_title(driver)
            result["title"] = title
            if title:
                print(f"  ✓ 标题: {title[:50]}...")

            # 提取页面文本
            page_text = self.extract_page_text(driver)
            
            if not page_text or len(page_text) < 50:
                print("  ⚠️ 无法获取页面内容")
                # 尝试保存页面源码用于调试
                try:
                    page_source = driver.page_source
                    if "pan.baidu.com" in page_source.lower():
                        print("  ℹ️ 页面源码中包含百度网盘相关文本，可能是CSS隐藏")
                except:
                    pass
                return result

            # 从文本中提取信息
            result["baidu_link"] = self.extract_baidu_link(page_text)
            result["extract_code"] = self.extract_code(page_text)
            result["password"] = self.extract_password(page_text)

            # 调试输出
            if result["baidu_link"]:
                print(f"  ✓ 百度链接: {result['baidu_link']}")
            else:
                print("  ✗ 未找到百度链接")
                
            if result["extract_code"]:
                print(f"  ✓ 提取码: {result['extract_code']}")
            else:
                print("  ✗ 未找到提取码")
                
            if result["password"]:
                print(f"  ✓ 解压密码: {result['password']}")
            else:
                print("  ✗ 未找到解压密码")

            # 检查是否成功提取必要信息
            if result["baidu_link"] and result["extract_code"]:
                result["success"] = True
            else:
                # 如果失败，尝试从页面源码中查找
                try:
                    page_source = driver.page_source
                    if result["baidu_link"] is None:
                        source_link = self.extract_baidu_link(page_source)
                        if source_link:
                            result["baidu_link"] = source_link
                            print(f"  ✓ 从源码找到百度链接: {result['baidu_link']}")
                    
                    if result["extract_code"] is None:
                        source_code = self.extract_code(page_source)
                        if source_code:
                            result["extract_code"] = source_code
                            print(f"  ✓ 从源码找到提取码: {result['extract_code']}")
                    
                    if result["baidu_link"] and result["extract_code"]:
                        result["success"] = True
                except Exception as e:
                    print(f"  ⚠️ 从源码提取时出错: {e}")

        except Exception as e:
            print(f"  ✗ 提取内容时出错: {e}")
            import traceback
            traceback.print_exc()

        return result


class BatchProcessor:
    """批量处理器"""

    def __init__(self):
        self.extractor = ContentExtractor()
        self.browser_manager = BrowserManager()
        self.store = ThreadStore()

    def register_links_file(self) -> int:
        """把 links.txt 里还没登记的链接登记到 threads.json（例如手动添加的链接）。返回新增数量"""
        if not LINKS_FILE.exists():
            return 0
        added = 0
        for line in LINKS_FILE.read_text(encoding='utf-8').splitlines():
            url = line.strip()
            if not url:
                continue
            if not thread_id(url):
                print(f"⚠️ 无法识别帖子ID，跳过: {url}")
                continue
            added += self.store.add_link(url)
        if added:
            self.store.save()
        return added

    def wait_for_manual_login(self) -> bool:
        """关闭当前浏览器，弹出可见窗口让用户登录，登录成功后恢复原浏览器模式"""
        print("\n" + "=" * 50)
        print("⚠️ 需要登录足下时光，正在打开浏览器窗口，请在弹出的页面中完成登录")
        print(f"   (最多等待 {LOGIN_TIMEOUT_SECONDS} 秒)")
        print("=" * 50)

        # 同一个 chrome_profile 不能被两个浏览器同时使用，先关闭当前浏览器
        self.browser_manager.close_browser()
        if not self.browser_manager.start_browser(headless=False):
            print("✗ 无法打开登录窗口")
            return False

        driver = self.browser_manager.driver
        logged_in = False
        try:
            safe_get(driver, LOGIN_URL)
            start_ts = time.time()
            while time.time() - start_ts < LOGIN_TIMEOUT_SECONDS:
                time.sleep(2)
                if self.extractor.quick_logged_in(driver):
                    logged_in = True
                    break
        except WebDriverException:
            print("✗ 登录窗口已被关闭")

        if logged_in:
            print("✓ 登录成功，继续处理")
            time.sleep(2)  # 等待cookie写入profile
        else:
            print("✗ 未检测到登录")

        # 恢复为原来的浏览器模式（默认无头）
        self.browser_manager.close_browser()
        if not self.browser_manager.start_browser():
            print("✗ 重新启动浏览器失败")
            return False
        return logged_in

    def record_result(self, tid: str, result: Dict) -> None:
        fields = {k: result.get(k) for k in ("url", "title", "baidu_link", "extract_code", "password")}
        if result["success"]:
            self.store.upsert(tid, **fields, extract_status="ok", extract_error=None, extracted_at=now())
        else:
            missing = [name for key, name in (("baidu_link", "百度链接"), ("extract_code", "提取码"))
                       if not result.get(key)]
            # 失败时不用空值覆盖之前已提取到的字段
            record = self.store.get(tid) or {}
            fields = {k: v for k, v in fields.items() if v or not record.get(k)}
            self.store.upsert(tid, **fields, extract_status="failed",
                              extract_error=("缺少" + "、".join(missing)) if missing else "提取出错")
        self.store.save()

    def process_all_links(self) -> None:
        """处理所有链接"""
        added = self.register_links_file()
        if added:
            print(f"从 links.txt 新登记 {added} 个帖子")

        # 待处理 = threads.json 中已登记但还没成功提取的帖子（失败的会自动重试）
        pending = [(r["tid"], r["url"]) for r in self.store.pending_extraction()]
        if not pending:
            print(f"没有待提取的帖子（已提取 {len(self.store.extracted())} 个）")
            return
        print(f"共 {len(pending)} 个帖子待提取（已提取 {len(self.store.extracted())} 个）")

        success_count = 0
        failed_count = 0
        with self.browser_manager as browser:
            if not browser.start_browser():
                print("启动浏览器失败")
                return

            for i, (tid, url) in enumerate(pending, 1):
                print(f"\n[{i}/{len(pending)}] 处理: {url}")

                result = self.extractor.extract_all_content(browser.driver, url)

                # 未登录：弹出窗口让用户登录，然后重试当前链接
                if result["need_login"]:
                    if not self.wait_for_manual_login():
                        print("✗ 未登录，终止处理。请登录后重新运行脚本")
                        break
                    result = self.extractor.extract_all_content(browser.driver, url)
                    if result["need_login"]:
                        print("✗ 仍然检测到未登录，终止处理")
                        break

                self.record_result(tid, result)
                if result["success"]:
                    success_count += 1
                    print(f"✓ 成功提取: {result.get('title', 'N/A')}")
                else:
                    failed_count += 1
                    print(f"✗ 提取失败: 缺少必要信息")

                if i < len(pending):
                    time.sleep(1)

        print(f"\n{'='*50}")
        print(f"处理完成")
        print(f"{'='*50}")
        print(f"成功: {success_count} 条")
        print(f"失败: {failed_count} 条（下次运行会自动重试）")
        print(f"数据文件: {self.store.path}")
        print(f"阅读用导出: {EXPORT_FILE}")
        print(f"{'='*50}")


def main():
    """主函数"""
    try:
        BatchProcessor().process_all_links()
    except KeyboardInterrupt:
        print("\n用户中断操作")
    except Exception as e:
        print(f"程序运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
