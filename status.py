"""
查看每个帖子进行到哪一步：哪些在等待处理、哪些失败了（附原因）。

使用方法:
python status.py          # 汇总 + 列出未完成的帖子
python status.py --all    # 同时列出已全部完成的帖子
"""

import sys
from typing import Dict, List, Optional, Tuple

from common.config import LINKS_FILE
from common.store import ThreadStore, thread_id

# (步骤号, 名称, 状态字段, 错误字段)
STEPS = [
    (2, "提取内容", "extract_status", "extract_error"),
    (3, "转存网盘", "save_status", "save_error"),
    (4, "解压", "unzip_status", "unzip_error"),
    (5, "重命名", "rename_status", "rename_error"),
]
STEP_HINTS = {
    2: "运行 02_extract_content.py",
    3: "运行 03_save_to_panbaidu.py",
    4: "在百度网盘客户端下载后运行 04_open_and_decrypt_files.py",
    5: "运行 05_rename_mp4_files.py",
}


def current_step(record: dict) -> Optional[Tuple[int, str, Optional[str]]]:
    """返回 (卡在第几步, 状态, 错误原因)；全部完成返回 None。"""
    if record.get("rename_status") == "ok":
        return None
    for number, _, status_field, error_field in STEPS:
        status = record.get(status_field)
        if status != "ok":
            return number, status or "pending", record.get(error_field)
    return None


def pending_links(store: ThreadStore) -> List[str]:
    """links.txt 中还没登记到 threads.json 的链接（如手动添加的；02 运行时会自动登记）"""
    if not LINKS_FILE.exists():
        return []
    links = []
    for url in LINKS_FILE.read_text(encoding="utf-8").splitlines():
        url = url.strip()
        tid = thread_id(url) if url else None
        if tid and store.get(tid) is None and url not in links:
            links.append(url)
    return links


def main() -> None:
    show_all = "--all" in sys.argv
    store = ThreadStore()
    new_links = pending_links(store)

    groups: Dict[int, List[Tuple[dict, str, Optional[str]]]] = {n: [] for n, *_ in STEPS}
    done = []
    for record in store:
        step = current_step(record)
        if step is None:
            done.append(record)
        else:
            number, status, error = step
            groups[number].append((record, status, error))

    print("=" * 60)
    print("📊 处理进度")
    print("=" * 60)
    print(f"  links.txt 中未登记的链接: {len(new_links)}")
    for number, name, *_ in STEPS:
        items = groups[number]
        failed = sum(1 for _, status, _ in items if status == "failed")
        detail = f"（其中失败 {failed}）" if failed else ""
        print(f"  {number}. 等待{name}: {len(items)}{detail}")
    print(f"  ✅ 全部完成: {len(done)}")
    print(f"  合计帖子: {len(store.records)}")

    if new_links:
        print(f"\n── links.txt 中未登记的链接 → {STEP_HINTS[2]}")
        for url in new_links:
            print(f"  ⏳ {url}")

    for number, name, *_ in STEPS:
        items = groups[number]
        if not items:
            continue
        print(f"\n── 等待{name} ({len(items)}) → {STEP_HINTS[number]}")
        for record, status, error in items:
            label = record.get("title") or record.get("url") or record["tid"]
            file_info = f" [{record['file_name']}]" if record.get("file_name") else ""
            icon = "❌" if status == "failed" else "⏳"
            print(f"  {icon} {label}{file_info}")
            if status == "failed" and error:
                print(f"      原因: {error}")
            elif status == "not_found":
                print(f"      原因: {error or '未找到对应视频文件'}")

    if show_all and done:
        print(f"\n── 全部完成 ({len(done)})")
        for record in done:
            renamed = "、".join(record.get("renamed_to") or [])
            print(f"  ✅ {record.get('title')}" + (f" → {renamed}" if renamed else ""))


if __name__ == "__main__":
    main()
