r"""
清理和提取模块
用于清理下载文件和从浏览器提取链接
适配 zuxiashiguang.com/thread 与 forum.php?mod=viewthread 链接提取

清理时只删除 04 已确认解压成功的压缩包，未解压或解压失败的会保留。

使用方法:
start msedge.exe --remote-debugging-port=9222 --user-data-dir="D:\projects\zxsg\runtime\profiles\browser_data_debug"
"""

import sys
import psutil
import requests
import re
import logging
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from common import config
from common.config import DEBUG_PROFILE_DIR, LINKS_FILE
from common.store import ThreadStore, archive_base_name

# 配置常量
TARGET_URL_PATTERNS = (
    "https://www.zuxiashiguang.com/thread",
    "https://www.zuxiashiguang.com/forum",
)
DEBUG_PORTS = [9222, 9223, 9224, 9225, 9990, 9991, 9992, 8080, 8888, 3000]
PORT_SCAN_TIMEOUT = 1


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """设置日志配置"""
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level.upper()))
    logger.handlers.clear()
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger


def print_status(message: str, status: str = "INFO"):
    """打印带状态的消息"""
    status_colors = {
        "INFO": "\033[94m",
        "SUCCESS": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
    }
    reset_color = "\033[0m"
    color = status_colors.get(status.upper(), "")
    print(f"{color}[{status}] {message}{reset_color}")
    logging.info(message)


def format_time_duration(seconds: float) -> str:
    """格式化时间持续时间"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}分钟"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}小时"


class Timer:
    """简单的计时器类"""
    def __init__(self):
        self.start_time = None
        self.end_time = None
    
    def start(self):
        self.start_time = time.time()
        self.end_time = None
    
    def stop(self):
        if self.start_time is not None:
            self.end_time = time.time()
    
    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time is not None else time.time()
        return end - self.start_time
    
    def elapsed_formatted(self) -> str:
        return format_time_duration(self.elapsed())


class BrowserDebugPortManager:
    """浏览器调试端口管理器"""

    def __init__(self):
        self.logger = setup_logging("INFO")
        self.timeout = PORT_SCAN_TIMEOUT

    def verify_debug_port(self, port: int) -> bool:
        """验证调试端口是否可用"""
        try:
            response = requests.get(f"http://localhost:{port}/json", timeout=self.timeout)
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False
        except Exception as e:
            self.logger.debug(f"验证端口 {port} 时出错: {e}")
            return False

    def get_browser_processes(self) -> List[dict]:
        """获取浏览器进程信息"""
        browser_processes = []
        browser_names = ['msedge.exe', 'chrome.exe', 'chromium.exe']

        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                proc_name = proc.info.get('name', '').lower()
                if any(browser in proc_name for browser in browser_names):
                    browser_processes.append({
                        'pid': proc.info['pid'],
                        'name': proc.info['name'],
                        'cmdline': proc.info.get('cmdline', [])
                    })
        except Exception as e:
            self.logger.error(f"获取浏览器进程失败: {e}")

        return browser_processes

    def extract_port_from_cmdline(self, cmdline: List[str]) -> Optional[int]:
        """从命令行参数中提取调试端口"""
        if not cmdline:
            return None

        cmdline_str = ' '.join(cmdline)
        match = re.search(r'--remote-debugging-port=(\d+)', cmdline_str)

        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None

        return None

    def get_port_from_processes(self) -> Optional[int]:
        """从浏览器进程获取调试端口"""
        browser_processes = self.get_browser_processes()

        if not browser_processes:
            return None

        for proc in browser_processes:
            port = self.extract_port_from_cmdline(proc['cmdline'])
            if port and self.verify_debug_port(port):
                return port

        return None

    def scan_configured_ports(self) -> Optional[int]:
        """扫描配置的端口列表"""
        for port in DEBUG_PORTS:
            if self.verify_debug_port(port):
                return port
        return None

    def check_browser_listening_ports(self) -> List[int]:
        """检查浏览器进程正在监听的端口"""
        listening_ports = []

        try:
            browser_processes = self.get_browser_processes()

            for proc_info in browser_processes:
                try:
                    proc = psutil.Process(proc_info['pid'])
                    connections = proc.net_connections(kind='inet')

                    for conn in connections:
                        if conn.status == 'LISTEN' and conn.laddr:
                            port = conn.laddr.port
                            if 9000 <= port <= 9999 or port in [8080, 8888, 3000]:
                                listening_ports.append(port)

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        except Exception as e:
            self.logger.debug(f"检查监听端口时出错: {e}")

        return list(set(listening_ports))

    def find_debug_port(self) -> Optional[int]:
        """查找可用的调试端口"""
        # 1. 优先从进程参数获取
        port = self.get_port_from_processes()
        if port:
            return port

        # 2. 检查浏览器正在监听的端口
        listening_ports = self.check_browser_listening_ports()
        if listening_ports:
            for port in listening_ports:
                if self.verify_debug_port(port):
                    return port

        # 3. 扫描配置的端口列表
        port = self.scan_configured_ports()
        if port:
            return port

        # 4. 如果都失败了，提供用户指导
        print("❌ 未找到浏览器调试端口")
        print("💡 请使用以下命令启动浏览器:")
        print(f"   msedge.exe --remote-debugging-port=9222 --user-data-dir=\"{DEBUG_PROFILE_DIR}\"")
        print("   或 chrome.exe --remote-debugging-port=9222")

        return None


class LinkExtractorManager:
    """链接提取管理器"""

    def __init__(self):
        self.logger = setup_logging("INFO")
        self.target_patterns = TARGET_URL_PATTERNS

    def is_valid_url(self, url: str) -> bool:
        """验证URL是否有效"""
        if not url or not isinstance(url, str):
            return False

        if not url.startswith(('http://', 'https://')):
            return False

        try:
            parsed = urlparse(url)
            return bool(parsed.netloc and parsed.scheme)
        except Exception:
            return False

    def extract_tabs_info(self, debug_port: int) -> List[dict]:
        """从浏览器获取标签页信息"""
        try:
            response = requests.get(f"http://localhost:{debug_port}/json", timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(f"获取浏览器标签页信息失败: {e}")
            return []
        except Exception as e:
            self.logger.error(f"解析浏览器响应失败: {e}")
            return []

    def filter_target_links(self, tabs: List[dict]) -> List[str]:
        """过滤目标链接 - 提取包含 zuxiashiguang.com/thread 或 /forum 的链接"""
        links = []
        processed_urls = set()  # 避免重复链接

        for tab in tabs:
            url = tab.get('url', '')

            if not self.is_valid_url(url):
                continue

            # 检查是否包含目标URL模式
            if not any(pattern in url for pattern in self.target_patterns):
                continue

            # 避免重复
            if url in processed_urls:
                continue

            processed_urls.add(url)
            links.append(url)

        return links

    def extract_links_from_browser(self, debug_port: int) -> List[str]:
        """从浏览器提取链接"""
        tabs = self.extract_tabs_info(debug_port)
        if not tabs:
            print("❌ 未获取到浏览器标签页信息")
            return []

        links = self.filter_target_links(tabs)
        return links

    def save_links_to_file(self, links: List[str], output_file: Path) -> bool:
        """保存链接到文件"""
        if not links:
            return True

        try:
            # 确保输出目录存在
            output_file.parent.mkdir(parents=True, exist_ok=True)

            # 保存链接
            with open(output_file, 'w', encoding='utf-8') as f:
                for link in links:
                    f.write(link + '\n')

            return True

        except Exception as e:
            print(f"❌ 保存链接失败: {e}")
            return False

    def extract_and_save_links(self, debug_port: int) -> int:
        """提取并保存链接"""
        links = self.extract_links_from_browser(debug_port)

        # 登记到 threads.json：只新增、不覆盖，没处理完的链接不会因为再次运行 01 而丢失
        store = ThreadStore()
        added = sum(store.add_link(link) for link in links)
        if added:
            store.save()
        print_status(f"新登记 {added} 个待处理帖子（其余 {len(links) - added} 个之前已登记）", "INFO")

        # links.txt 只是本次抓取结果的记录
        if self.save_links_to_file(links, LINKS_FILE):
            return len(links)

        return 0


class CleanupAndExtractManager:
    """清理和提取主管理器"""

    def __init__(self):
        self.logger = setup_logging("INFO")
        self.timer = Timer()
        self.port_manager = BrowserDebugPortManager()
        self.link_extractor = LinkExtractorManager()
        self.download_dir = Path(config.get("paths.baidu_download"))
        self.archive_extensions = ['.7z', '.zip', '.rar', '.tar', '.gz', '.bz2']

    def cleanup_archive_files(self) -> int:
        """删除下载目录中的压缩文件"""
        try:
            if not self.download_dir.exists():
                print_status(f"下载目录不存在: {self.download_dir}", "WARNING")
                return 0
            
            deleted_count = 0
            deleted_files = []
            kept_files = []
            store = ThreadStore()

            print_status(f"扫描目录: {self.download_dir}", "INFO")

            for file_path in self.download_dir.rglob('*'):
                if not file_path.is_file():
                    continue
                name = file_path.name.lower()
                is_archive = (file_path.suffix.lower() in self.archive_extensions
                              or re.search(r'\.(7z|zip|rar)\.\d{3}$', name))
                if not is_archive:
                    continue
                # 只删除 04 已确认解压成功的压缩包（含其分卷）
                record = store.find_by_file_name(archive_base_name(file_path.name))
                if not record or record.get("unzip_status") != "ok":
                    kept_files.append(file_path.name)
                    continue
                try:
                    file_size = file_path.stat().st_size / (1024 * 1024)  # MB
                    file_path.unlink()
                    deleted_files.append(f"{file_path.name} ({file_size:.2f} MB)")
                    deleted_count += 1
                except Exception as e:
                    print_status(f"删除文件失败 {file_path.name}: {e}", "ERROR")

            if kept_files:
                print_status(f"保留 {len(kept_files)} 个未确认解压成功的压缩包:", "WARNING")
                for name in kept_files:
                    print(f"  - {name}")

            if deleted_count > 0:
                print_status(f"已删除 {deleted_count} 个压缩文件:", "SUCCESS")
                for file_info in deleted_files:
                    print(f"  - {file_info}")
            else:
                print_status("没有找到需要删除的压缩文件", "INFO")
            
            return deleted_count
        
        except Exception as e:
            print_status(f"清理压缩文件时出错: {e}", "ERROR")
            self.logger.exception("详细错误信息:")
            return 0

    def run_link_extraction(self) -> int:
        """运行链接提取"""
        try:
            # 查找调试端口
            debug_port = self.port_manager.find_debug_port()
            if not debug_port:
                print_status("未找到浏览器调试端口，跳过链接提取", "WARNING")
                return 0

            print_status(f"找到浏览器调试端口: {debug_port}", "SUCCESS")

            # 提取并保存链接
            links_count = self.link_extractor.extract_and_save_links(debug_port)
            return links_count

        except Exception as e:
            print(f"❌ 链接提取过程中出错: {e}")
            self.logger.exception("详细错误信息:")
            return 0

    def run(self):
        """运行完整的清理和提取流程"""
        self.timer.start()

        print("🧹 清理和链接提取工具")
        print(f"📁 下载目录: {self.download_dir}")
        print(f"🎯 目标URL模式: {', '.join(TARGET_URL_PATTERNS)}")
        print(f"📄 输出文件: {LINKS_FILE}")

        try:
            # 1. 清理压缩文件
            print("\n🧹 开始清理压缩文件...")
            deleted_count = self.cleanup_archive_files()
            
            # 2. 链接提取
            print("\n🔗 开始链接提取...")
            links_count = self.run_link_extraction()

            # 总结
            self.timer.stop()
            print(f"\n{'='*50}")
            print("📊 执行结果")
            print(f"{'='*50}")
            print(f"🗑️  删除压缩文件: {deleted_count} 个")
            print(f"🔗 提取链接: {links_count} 个")
            if links_count > 0:
                print(f"📄 保存位置: {LINKS_FILE}")
            print(f"⏱️  总耗时: {self.timer.elapsed_formatted()}")
            print(f"{'='*50}")

            return deleted_count, links_count

        except KeyboardInterrupt:
            print("\n⚠️  用户中断操作")
            return 0, 0
        except Exception as e:
            print(f"\n❌ 执行过程中出现未预期的错误: {e}")
            self.logger.exception("详细错误信息:")
            return 0, 0


def main():
    """主函数"""
    try:
        manager = CleanupAndExtractManager()
        manager.run()
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()












