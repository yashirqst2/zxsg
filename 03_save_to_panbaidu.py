r"""
把 02 提取到的百度分享链接转存到自己网盘的目标文件夹（默认 nztd）。

- 数据来自 runtime/state/threads.json；已确认转存成功的链接不会重复处理，失败的下次自动重试。
- 转存结果以百度 /share/transfer 接口的返回为准，不再靠页面提示猜测：
  errno=0 为成功；"文件已转存"（目标文件夹已有同一文件）也视为成功，不会产生重复文件。

Usage:
    python 03_save_to_panbaidu.py
    HEADLESS=false python 03_save_to_panbaidu.py   # 显示浏览器窗口

重新转存已转存过的帖子（比如网盘里的文件被误删了）：
    python 03_save_to_panbaidu.py --redo last                 # 最近一次运行转存的那一批
    python 03_save_to_panbaidu.py --redo 2026-10-09T18:00     # 这个时间之后转存的
    python 03_save_to_panbaidu.py --redo 123318,141112        # 指定帖子 ID
    python 03_save_to_panbaidu.py --redo all                  # 全部
    加 --dry-run 只列出会处理哪些，不实际转存。
    网盘里还在的文件百度会返回"已转存"，不会产生重复文件。
"""

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import WebDriverException

from common import config
from common.browser import create_driver, env_headless, safe_get
from common.store import ThreadStore, archive_base_name, now

TARGET_FOLDER = config.get("save_to_panbaidu.target_folder", "nztd")
PAGE_READY_TIMEOUT = 40      # 分享页出现"保存到网盘"按钮的最长等待
PAGE_LOAD_TIMEOUT = 10       # 单次页面加载超时；超时后停止加载继续使用已加载部分（参考 wckbot）
RELOAD_AFTER = 8             # 页面既没有保存按钮也没有提取码输入框超过这么久，就重新加载
EXTRACT_PENDING_LIMIT = 25   # "提取中..."超过这么久仍没完成才重新加载
TRANSFER_TIMEOUT = 20        # 点击保存后等待转存接口返回的最长时间
THROTTLE_COOLDOWN = 20       # 百度提示操作频繁时的冷却时间
BETWEEN_LINKS_DELAY = 1.5

RETRY_ROUNDS = 3             # 失败的链接最多再重试几轮
RETRY_ROUND_DELAY = 10       # 每轮重试前的等待（秒），逐轮加倍

SHARE_GONE_PHRASES = ("分享的文件已经被取消", "分享已失效", "链接不存在", "分享内容可能因为涉及侵权",
                      "啊哦，你来晚了", "此链接分享内容可能", "已过期", "提取码错误")
THROTTLE_PHRASES = ("频繁", "稍后再试", "系统繁忙")
EXTRACT_PENDING_PHRASE = "提取中"
# 这些错误重试也没用，不进入重试轮
PERMANENT_ERRORS = ("分享已失效", "未登录")

# 参考 wckbot：统计/登录脚本在无头模式下会卡住渲染进程，图片字体也没必要加载
BLOCK_URLS = ["*hm.baidu.com/*", "*tongji.baidu.com/*", "*passport.baidu.com/passApi/js/*",
              "*.woff", "*.woff2", "*.ttf", "*.mp4"]

# 直接调用百度转存接口（参数取自分享页的 yunData），不依赖页面按钮；参考 wckbot
DIRECT_TRANSFER_JS = r"""
const done = arguments[arguments.length - 1];
const targetDir = arguments[0];
const yd = window.yunData || {};
const local = (k) => { try { return window.locals && window.locals.get && window.locals.get(k); } catch (e) { return null; } };
const first = (...vals) => vals.find(v => v !== undefined && v !== null && v !== '');
const files = first(yd.file_list, yd.FILEINFO, local('file_list'), window.metaData && window.metaData.file_list) || [];
const fsIds = [...new Set((Array.isArray(files) ? files : []).map(f => Number(f && f.fs_id)).filter(Boolean))];
const names = (Array.isArray(files) ? files : []).map(f => f && f.server_filename).filter(Boolean);
const shareid = first(yd.shareid, local('shareid'));
const shareUk = first(yd.share_uk, local('share_uk'));
const bdstoken = first(yd.bdstoken, local('bdstoken'));
const sekey = first(window.currentSekey, yd.sekey, local('sekey'));
const missing = Object.entries({shareid, share_uk: shareUk, bdstoken, sekey})
  .filter(([, v]) => !v).map(([k]) => k);
if (!fsIds.length) missing.push('fs_id');
if (missing.length) { done({attempted: false, reason: '缺少参数: ' + missing.join(', ')}); return; }
const params = new URLSearchParams({shareid: String(shareid), from: String(shareUk), sekey: String(sekey),
  channel: 'chunlei', web: '1', app_id: '250528', bdstoken: String(bdstoken), clienttype: '0'});
fetch('/share/transfer?' + params, {
  method: 'POST', credentials: 'include',
  headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'},
  body: new URLSearchParams({fsidlist: JSON.stringify(fsIds), path: targetDir}).toString()
}).then(r => r.text().then(t => done({attempted: true, status: r.status, body: t, names})))
  .catch(e => done({attempted: true, status: 0, body: '', error: String(e), names}));
"""

# 拦截页面发出的转存请求，把接口返回记录到 window.__zxsgTransfer
TRANSFER_HOOK_JS = r"""
if (!window.__zxsgHooked) {
  window.__zxsgHooked = true;
  var isTransfer = function (u) { return /share\/transfer|api\/transfer/.test(u || ''); };
  var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__zxsgUrl = u; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    var xhr = this;
    xhr.addEventListener('loadend', function () {
      if (isTransfer(xhr.__zxsgUrl)) window.__zxsgTransfer.push({status: xhr.status, body: xhr.responseText});
    });
    return send.apply(this, arguments);
  };
  if (window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function (u) {
      var url = (u && u.url) || u;
      return origFetch.apply(this, arguments).then(function (resp) {
        if (isTransfer(url)) resp.clone().text().then(function (t) { window.__zxsgTransfer.push({status: resp.status, body: t}); });
        return resp;
      });
    };
  }
}
window.__zxsgTransfer = [];
"""

PAGE_STATE_JS = r"""
var visible = function (el) { return el && el.offsetParent; };
var byText = function (sel, re) {
  return Array.from(document.querySelectorAll(sel)).find(function (e) { return visible(e) && re.test(e.innerText || ''); });
};
// 百度在做新旧两版分享页的 A/B 测试：旧版按钮是 <a class="g-button bottom_save_btn">，
// 新版是 <button class="wp-base-button save_btn bottom_save_btn">，两种都要认
var save = document.querySelector('a.bottom_save_btn, button.bottom_save_btn');
if (!visible(save)) save = byText('a.g-button, button', /保存到网盘/);
// 提取码页的按钮是 <div id="submitBtn">提取文件</div>，不是 a/button
var extract = document.querySelector('#submitBtn');
if (!visible(extract)) extract = byText('#submitBtn, a.g-button, button', /^\s*提取文件\s*$/);
var pathEl = document.querySelector('.save-path');
return {
  save: !!save,
  extract: !!extract,
  savePath: pathEl ? pathEl.innerText.trim() : '',
  body: document.body ? document.body.innerText.slice(0, 2000) : ''
};
"""


def share_key(link: str) -> str:
    """分享链接的标识：https://pan.baidu.com/s/1AbC → AbC（share/init?surl= 用的也是它）"""
    match = re.search(r"/s/1([\w-]+)", link)
    return match.group(1) if match else link


def build_share_url(link: str, code: str) -> str:
    if "pwd=" in link:
        return link
    return f"{link}{'&' if '?' in link else '?'}pwd={code}"


class BaiduPanSaver:
    def __init__(self, target_folder: str = TARGET_FOLDER):
        self.target_folder = target_folder
        self.driver: Optional[webdriver.Chrome] = None

    def start(self) -> None:
        # 刚关闭浏览器马上重开，偶尔会报 "chrome not reachable"，稍等再试
        for attempt in range(3):
            try:
                self.driver = create_driver(headless=env_headless(True), load_images=False, block_urls=BLOCK_URLS)
                break
            except WebDriverException:
                if attempt == 2:
                    raise
                time.sleep(3)
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def restart(self) -> None:
        self.close()
        self.start()

    def is_logged_in(self) -> bool:
        safe_get(self.driver, "https://pan.baidu.com/disk/main")
        return any(c.get("name") == "BDUSS" for c in self.driver.get_cookies())

    # ---------- 分享页 ----------
    def page_state(self) -> dict:
        try:
            return self.driver.execute_script(PAGE_STATE_JS) or {}
        except WebDriverException:
            return {}

    def submit_extract_code(self, code: str) -> None:
        """share/init 提取码页：无头模式下百度不会自动提交链接里的 pwd，需要填写并点击"提取文件"。"""
        self.driver.execute_script(r"""
            var code = arguments[0];
            var input = document.querySelector('#accessCode');
            if (!input || !input.offsetParent)
              input = Array.from(document.querySelectorAll('input')).find(function (e) { return e.offsetParent; });
            if (input) {
              var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              setter.call(input, code);
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.dispatchEvent(new Event('change', {bubbles: true}));
            }
            var btn = document.querySelector('#submitBtn');
            if (!btn || !btn.offsetParent) btn = Array.from(document.querySelectorAll('a.g-button, button, a'))
              .find(function (e) { return e.offsetParent && /提取文件/.test(e.innerText); });
            if (btn) btn.click();""", code)

    def on_target_share(self, url: str) -> bool:
        """
        浏览器当前是否在目标分享页上（/s/1xxx 或 share/init?surl=xxx）。
        页面加载卡住时，浏览器可能还停在上一个分享页；不检查就会把上一个分享再转存一次，
        还会把上一个文件的名字记到当前帖子上。
        """
        return share_key(url) in self.driver.current_url

    def wait_for_share_page(self, code: str, url: str) -> Tuple[bool, str]:
        """等待目标分享页出现"保存到网盘"按钮；遇到提取码页则提交提取码。返回 (是否就绪, 错误说明)"""
        deadline = time.time() + PAGE_READY_TIMEOUT
        last_submit = 0.0
        last_load = time.time()
        pending_since: Optional[float] = None
        while time.time() < deadline:
            if not self.on_target_share(url):
                # 还停在别的页面（通常是上一个分享），重新打开目标链接
                if time.time() - last_load > 2:
                    safe_get(self.driver, url)
                    last_load = time.time()
                time.sleep(0.5)
                continue
            state = self.page_state()
            body = state.get("body", "")
            gone = next((p for p in SHARE_GONE_PHRASES if p in body), None)
            if gone:
                return False, f"分享已失效：{gone}"
            if state.get("save"):
                return True, ""
            if "passport.baidu.com" in self.driver.current_url:
                return False, "未登录百度网盘"
            on_init_page = "share/init" in self.driver.current_url
            if state.get("extract"):
                if EXTRACT_PENDING_PHRASE in body:
                    # "提取中"时不要重复点击、也不要刷新，否则会打断提取（wckbot 的经验）；
                    # 只有卡在提取中太久才重新加载
                    pending_since = pending_since or time.time()
                    if time.time() - pending_since > EXTRACT_PENDING_LIMIT:
                        print(f"  ⚠️ 提取超过 {EXTRACT_PENDING_LIMIT} 秒仍未完成，重新加载")
                        safe_get(self.driver, url)
                        last_load, pending_since = time.time(), None
                else:
                    pending_since = None
                    if time.time() - last_submit > 6:
                        self.submit_extract_code(code)
                        last_submit = time.time()
            elif time.time() - last_load > (RELOAD_AFTER * 2 if on_init_page else RELOAD_AFTER):
                # 页面偶尔会卡住不渲染，与其干等不如重新加载（加载目标链接，不是当前地址）
                safe_get(self.driver, url)
                last_load = time.time()
            time.sleep(0.5)
        if not self.on_target_share(url):
            return False, "浏览器没能打开目标分享页（一直停在其它页面）"
        if not self.page_state().get("body"):
            return False, "分享页内容未加载（页面卡住）"
        return False, f"{PAGE_READY_TIMEOUT}秒内未出现“保存到网盘”按钮"

    def get_file_name(self) -> Optional[str]:
        """分享页标题形如 "TJSP-102196.7z_免费高速下载|百度网盘-分享无限制" """
        match = re.match(r"(.+?)_免费高速下载", self.driver.title or "")
        return archive_base_name(match.group(1).strip()) if match else None

    def ensure_target_folder(self) -> bool:
        """确认保存路径是目标文件夹，不是则打开文件夹树选择。"""
        path = self.page_state().get("savePath", "")
        if path.rstrip("/").endswith("/" + self.target_folder):
            return True
        print(f"  当前保存路径: {path or '未知'}，切换到 {self.target_folder}")
        opened = self.driver.execute_script(r"""
            var icon = document.querySelector('.bottom-save-path-icon');
            if (!icon) return false;
            icon.click();
            return true;""")
        if not opened:
            return False
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.5)
            done = self.driver.execute_script(r"""
                var name = arguments[0];
                var node = Array.from(document.querySelectorAll('.treeview-txt'))
                  .find(function (e) { return e.offsetParent && e.innerText.trim() === name; });
                if (!node) return false;
                node.click();
                var ok = Array.from(document.querySelectorAll('a.g-button, button'))
                  .find(function (e) { return e.offsetParent && /确定/.test(e.innerText); });
                if (ok) ok.click();
                return !!ok;""", self.target_folder)
            if done:
                time.sleep(0.5)
                path = self.page_state().get("savePath", "")
                return path.rstrip("/").endswith("/" + self.target_folder)
        return False

    # ---------- 转存 ----------
    def direct_transfer(self) -> Optional[Tuple[bool, str, Optional[str]]]:
        """直接调用转存接口。页面缺少参数时返回 None（改用点击按钮）。"""
        try:
            result = self.driver.execute_async_script(DIRECT_TRANSFER_JS, "/" + self.target_folder.strip("/"))
        except WebDriverException as e:
            print(f"  ⚠️ 直接转存出错，改用点击按钮: {e.msg or e}")
            return None
        if not result or not result.get("attempted"):
            print(f"  ⚠️ 无法直接转存（{(result or {}).get('reason', '未知原因')}），改用点击按钮")
            return None
        if result.get("error"):
            return False, f"转存请求失败: {result['error']}", None
        ok, message, file_name = self.parse_transfer_response(result)
        names = result.get("names") or []
        return ok, message, file_name or (archive_base_name(names[0]) if names else None)

    def click_save_and_wait(self) -> Tuple[Optional[bool], str, Optional[str]]:
        """
        点击保存并等待转存接口返回。
        返回 (结果, 说明, 网盘中的文件名)；结果为 None 表示没拿到接口返回（无法确认）。
        """
        self.driver.execute_script(TRANSFER_HOOK_JS)
        self.driver.execute_script(r"""
            var btn = document.querySelector('a.bottom_save_btn, button.bottom_save_btn');
            if (!btn || !btn.offsetParent) btn = Array.from(document.querySelectorAll('a.g-button, button'))
              .find(function (e) { return e.offsetParent && /保存到网盘/.test(e.innerText); });
            btn.click();""")

        deadline = time.time() + TRANSFER_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.5)
            responses = self.driver.execute_script("return window.__zxsgTransfer || []")
            if responses:
                return self.parse_transfer_response(responses[-1])
        return None, f"点击保存后 {TRANSFER_TIMEOUT} 秒内没有收到转存结果", None

    @staticmethod
    def parse_transfer_response(response: dict) -> Tuple[bool, str, Optional[str]]:
        try:
            data = json.loads(response.get("body") or "{}")
        except ValueError:
            return False, f"转存接口返回无法解析 (HTTP {response.get('status')})", None

        errno = data.get("errno")
        msg = data.get("show_msg") or data.get("errmsg") or ""
        items = (data.get("extra") or {}).get("list") or (data.get("duplicated") or {}).get("list") or []
        names = [Path(item.get("from") or item.get("path") or "").name for item in items]
        file_name = archive_base_name(names[0]) if names and names[0] else None

        if errno == 0:
            return True, "转存成功", file_name
        if data.get("duplicated") or "已转存" in msg or "已有相同" in msg:
            return True, "目标文件夹已有该文件（之前已转存）", file_name
        return False, f"{msg or '转存失败'} (errno={errno})", file_name

    def save_entry(self, record: dict) -> Tuple[bool, str, Optional[str]]:
        """处理一条分享链接。返回 (是否成功, 说明, 文件名)"""
        url = build_share_url(record["baidu_link"], record["extract_code"])
        print(f"  URL: {url}")

        ready, error = False, "页面打开失败"
        for attempt in range(2):
            if safe_get(self.driver, url):
                ready, error = self.wait_for_share_page(record["extract_code"], url)
                if ready or error.startswith(PERMANENT_ERRORS):
                    break
            if attempt == 0:
                print(f"  ⚠️ {error}，重启浏览器重试一次...")
                # 渲染进程卡死后 driver 可能不可用，重启浏览器再试
                self.restart()
        if not ready:
            return False, error, None

        file_name = self.get_file_name()
        message = ""
        for attempt in range(2):
            # 转存前再确认一次还在目标分享页上，避免转存成别的分享
            if not self.on_target_share(url):
                return False, "转存前浏览器已不在目标分享页", file_name
            result = self.direct_transfer()
            if result is None:
                # 退回到点击页面上的"保存到网盘"按钮
                if not self.ensure_target_folder():
                    return False, f"无法选择目标文件夹 {self.target_folder}", file_name
                result = self.click_save_and_wait()
            ok, message, transfer_name = result
            file_name = transfer_name or file_name
            if ok is False and attempt == 0 and any(p in message for p in THROTTLE_PHRASES):
                print(f"  ⚠️ {message}，冷却 {THROTTLE_COOLDOWN} 秒后重试")
                time.sleep(THROTTLE_COOLDOWN)
                continue
            return bool(ok), message, file_name
        return False, message, file_name


def process(saver: BaiduPanSaver, store: ThreadStore, record: dict, label: str) -> Tuple[bool, str]:
    """转存一条并记录结果"""
    print(f"\n{label} {record.get('title') or '无标题'}")
    try:
        ok, message, file_name = saver.save_entry(record)
    except WebDriverException as e:
        ok, message, file_name = False, f"浏览器出错: {e.msg or e}", None
        saver.restart()

    # 不同分享链接不可能是同一个网盘文件：出现这种情况说明转存的是别的分享，不能当成功
    owner = store.find_by_file_name(file_name) if ok and file_name else None
    if owner and owner["tid"] != record["tid"] and owner.get("baidu_link") != record.get("baidu_link"):
        ok, message = False, f"转存到的文件 {file_name} 属于另一个帖子（{owner.get('title')}），可能转存错了分享"
        file_name = None

    fields = {"save_status": "ok" if ok else "failed", "save_error": None if ok else message}
    if file_name:
        fields["file_name"] = file_name
    if ok:
        fields["saved_at"] = now()
    store.upsert(record["tid"], **fields)
    store.save()

    name_info = f" [{file_name}]" if file_name else ""
    print(f"  {'✅' if ok else '❌'} {message}{name_info}")
    return ok, message


LAST_RUN_GAP_MINUTES = 30   # --redo last：转存时间间隔不超过这么久的算同一次运行


def select_redo(records: List[dict], spec: str) -> List[dict]:
    """
    按 --redo 参数选出要重新转存的帖子（只在已转存成功的帖子里选）：
    all / last / 时间（2026-10-09T18:00 或 "2026-10-09 18:00"，选此后转存的）/ 逗号分隔的帖子 ID
    """
    saved = [r for r in records if r.get("save_status") == "ok"]
    spec = spec.strip()
    if spec == "all":
        return saved
    if spec == "last":
        timed = sorted((r for r in saved if r.get("saved_at")), key=lambda r: r["saved_at"], reverse=True)
        batch = timed[:1]
        for prev, cur in zip(timed, timed[1:]):
            gap = datetime.fromisoformat(prev["saved_at"]) - datetime.fromisoformat(cur["saved_at"])
            if gap.total_seconds() > LAST_RUN_GAP_MINUTES * 60:
                break
            batch.append(cur)
        return batch
    if re.match(r"\d{4}-\d{2}-\d{2}", spec):
        since = datetime.fromisoformat(spec.replace(" ", "T"))
        return [r for r in saved if r.get("saved_at") and datetime.fromisoformat(r["saved_at"]) >= since]
    tids = {t.strip() for t in spec.split(",") if t.strip()}
    return [r for r in saved if r["tid"] in tids]


def main() -> None:
    parser = argparse.ArgumentParser(description="转存百度分享链接到网盘")
    parser.add_argument("--redo", metavar="范围",
                        help="重新转存已转存过的帖子：last / all / 时间 / 帖子ID（逗号分隔）")
    parser.add_argument("--dry-run", action="store_true", help="只列出要处理的帖子，不实际转存")
    parser.add_argument("-r", "--retry", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.retry:
        print("ℹ️ --retry 已不再需要：每次运行都会自动处理所有未成功的链接")

    store = ThreadStore()
    pending = [r for r in store.extracted() if r.get("save_status") != "ok"]
    done = len(store.extracted()) - len(pending)
    if args.redo:
        redo = select_redo(store.extracted(), args.redo)
        if not redo:
            print(f"--redo {args.redo} 没有匹配到已转存的帖子")
            return
        print(f"重新转存 {len(redo)} 条（--redo {args.redo}）")
        pending = redo + pending
    if not pending:
        print(f"没有需要转存的链接（已转存 {done} 条）")
        return
    print(f"待转存 {len(pending)} 条（已转存 {done} 条），目标文件夹: {TARGET_FOLDER}")
    if args.dry_run:
        for r in pending:
            print(f"  - [{r['tid']}] {r.get('title')}  {r.get('file_name') or ''}  转存于 {r.get('saved_at') or '-'}")
        return

    saver = BaiduPanSaver()
    saver.start()
    success = 0
    failed: List[Tuple[dict, str]] = []
    try:
        if not saver.is_logged_in():
            print("❌ 未登录百度网盘，请先运行 check_baidu_login.py 登录")
            return

        batch = pending
        for round_no in range(RETRY_ROUNDS + 1):
            if round_no:
                delay = RETRY_ROUND_DELAY * 2 ** (round_no - 1)
                print(f"\n🔁 第 {round_no} 轮重试：{len(batch)} 条，{delay} 秒后开始")
                time.sleep(delay)
                saver.restart()  # 换一个干净的浏览器会话
            failed = []
            for i, record in enumerate(batch, 1):
                ok, message = process(saver, store, record, f"[{i}/{len(batch)}]")
                if ok:
                    success += 1
                else:
                    failed.append((record, message))
                if i < len(batch):
                    time.sleep(BETWEEN_LINKS_DELAY)
            # 分享失效、未登录这类错误重试也没用
            batch = [r for r, message in failed if not message.startswith(PERMANENT_ERRORS)]
            if not batch:
                break
    finally:
        saver.close()

    print("\n========== 完成 ==========")
    print(f"成功: {success}")
    print(f"失败: {len(failed)}")
    for record, message in failed:
        print(f"  - {record.get('title') or record['baidu_link']}: {message}")
    if failed:
        print("\n💡 再次运行本脚本会自动重试失败的链接；运行 status.py 可查看所有未完成的帖子")


if __name__ == "__main__":
    main()
