"""
帖子数据存储：runtime/state/threads.json，以帖子 tid 为键，记录每个帖子在各步骤中的数据与状态。

多个脚本可以同时运行（例如一边转存一边解压）：保存时会加锁、重新读取磁盘上的最新内容，
只合并本进程修改过的字段，不会用旧数据覆盖其它脚本写入的结果。

记录字段：
  tid, url, title, baidu_link, extract_code, password      ← 02 提取
  extract_status ("ok"/"failed"), extract_error
  file_name                                                 ← 03 从分享页读取（不含扩展名）
  save_status ("ok"/"failed"), save_error
  unzip_status ("ok"/"failed"), unzip_error                 ← 04
  rename_status ("ok"/"failed"), renamed_to, rename_error   ← 05
"""

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set
from urllib.parse import parse_qs, urlparse

from common.config import REPORTS_DIR, STATE_DIR

STORE_FILE = STATE_DIR / "threads.json"
# 供人查看的导出文件（由 store 自动生成，不再作为步骤间的数据来源）
EXPORT_FILE = REPORTS_DIR / "extracted_contents.txt"


def thread_id(url: str) -> Optional[str]:
    """从 thread-123-1-1.html 或 forum.php?mod=viewthread&tid=123 中取出 tid。"""
    match = re.search(r"thread-(\d+)-", url)
    if match:
        return match.group(1)
    tid = parse_qs(urlparse(url).query).get("tid")
    return tid[0] if tid else None


def archive_base_name(file_name: str) -> str:
    """压缩包/分卷文件名 → 基础名：a.7z / a.7z.001 / a.part1.rar / a.zip → a"""
    name = Path(file_name).name
    name = re.sub(r"\.(7z|zip|rar)\.\d{3}$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.part\d+\.rar$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.(7z|zip|rar|tar|gz|bz2)$", "", name, flags=re.IGNORECASE)
    return name


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ThreadStore:
    def __init__(self, path: Path = STORE_FILE):
        self.path = path
        self.records: Dict[str, dict] = self._read()
        self._changes: Dict[str, dict] = {}   # 本进程通过 upsert 修改的字段
        self._added: Set[str] = set()         # 本进程通过 add_link 新登记的帖子

    # ---------- 读写 ----------
    def _read(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    @contextmanager
    def _lock(self, timeout: float = 30):
        """用独占创建的锁文件防止两个进程同时写 threads.json"""
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.time() + timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                # 锁文件超过 2 分钟视为上次异常退出遗留，直接清除
                try:
                    if time.time() - lock.stat().st_mtime > 120:
                        lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"等待 {lock} 超时，可能有其它脚本正在写入")
                time.sleep(0.1)
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def save(self) -> None:
        """重新读取磁盘上的最新数据，合并本进程的修改后原子写入。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            latest = self._read()
            for key in self._added:
                latest.setdefault(key, self.records[key])
            for key, fields in self._changes.items():
                latest.setdefault(key, {"tid": key}).update(fields)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)  # 原子替换，中途中断也不会写坏文件
        self.records = latest
        self._changes.clear()
        self._added.clear()
        self.export_text()

    def get(self, key: str) -> Optional[dict]:
        return self.records.get(key)

    def upsert(self, key: str, **fields) -> dict:
        fields["updated_at"] = now()
        record = self.records.setdefault(key, {"tid": key})
        record.update(fields)
        self._changes.setdefault(key, {}).update(fields)
        return record

    def add_link(self, url: str) -> bool:
        """登记一个待提取的帖子链接；已登记过的帖子不会重复添加，也不会覆盖其状态。返回是否为新增。"""
        tid = thread_id(url)
        if not tid or tid in self.records:
            return False
        self.records[tid] = {"tid": tid, "url": url, "extract_status": None, "added_at": now()}
        self._added.add(tid)
        return True

    def pending_extraction(self) -> List[dict]:
        """已登记但还没成功提取的帖子（02 的待处理列表）"""
        return [r for r in self.records.values() if r.get("url") and r.get("extract_status") != "ok"]

    def __iter__(self) -> Iterator[dict]:
        return iter(self.records.values())

    def find_by_file_name(self, base_name: str) -> Optional[dict]:
        for record in self.records.values():
            if record.get("file_name") and record["file_name"].lower() == base_name.lower():
                return record
        return None

    def extracted(self) -> List[dict]:
        return [r for r in self.records.values() if r.get("extract_status") == "ok"]

    # ---------- 导出 ----------
    def export_text(self) -> None:
        """生成便于阅读的 extracted_contents.txt（只读视图）。"""
        lines = []
        for r in self.extracted():
            lines.append(f"标题：{r.get('title') or ''}")
            lines.append(f"链接: {r['baidu_link']}")
            lines.append(f"提取码: {r['extract_code']}")
            if r.get("password"):
                lines.append(f"解压密码：{r['password']}")
            if r.get("file_name"):
                lines.append(f"文件名：{r['file_name']}")
            lines.append("")
        EXPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        EXPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
