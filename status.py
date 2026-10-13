"""
查看每个帖子进行到哪一步：哪些在等待处理、哪些失败了（附原因）。

使用方法:
python status.py          # 汇总 + 列出未完成的帖子
python status.py --all    # 同时列出已全部完成的帖子
"""

import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import config
from common.config import LINKS_FILE
from common.store import ThreadStore, archive_base_name, clean_filename, thread_id

NOT_FOUND_WARN_AFTER = 2   # 05 连续这么多次找不到视频时，给出诊断

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


def file_name_conflicts(records) -> Dict[str, List[dict]]:
    """
    不同的分享链接却记录了同一个网盘文件名 → {tid: [冲突的其它记录]}。
    正常情况下不可能出现，说明 03 转存错了分享（例如页面卡在上一个分享上）。
    """
    by_name = defaultdict(list)
    for r in records:
        if r.get("file_name"):
            by_name[r["file_name"].lower()].append(r)
    conflicts = {}
    for group in by_name.values():
        if len({r.get("baidu_link") for r in group}) > 1:
            for r in group:
                conflicts[r["tid"]] = [o for o in group if o["tid"] != r["tid"]]
    return conflicts


def diagnose_not_found(record: dict, conflicts: Dict[str, List[dict]],
                       download_dir: Path, mp4_dir: Path) -> str:
    """05 一直找不到视频时，推测原因并给出处理办法"""
    file_name = record["file_name"]
    if record["tid"] in conflicts:
        other = conflicts[record["tid"]][0]
        return (f"网盘文件名 {file_name} 和另一个帖子【{other.get('title')}】重复，很可能转存错了分享"
                f" → python 03_save_to_panbaidu.py --redo {record['tid']}")
    if download_dir.exists():
        archives = [p.name for p in download_dir.rglob("*")
                    if p.is_file() and archive_base_name(p.name).lower() == file_name.lower()]
        if archives:
            return f"压缩包 {archives[0]} 还在下载目录，没有解压成功 → 运行 04 查看错误"
    if (mp4_dir / f"{clean_filename(record.get('title') or '')}.mp4").exists():
        return f"{mp4_dir} 里已经有这个标题的视频，可能之前手动改过名"
    return (f"下载目录和视频目录里都没有 {file_name}：可能还没在百度网盘客户端下载，"
            f"或解压出来的文件名和 {file_name} 不一样")


def main() -> None:
    show_all = "--all" in sys.argv
    store = ThreadStore()
    new_links = pending_links(store)
    conflicts = file_name_conflicts(store)
    download_dir = Path(config.get("paths.baidu_download"))
    mp4_dir = Path(config.get("paths.mp4_dir"))

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

    if conflicts:
        print(f"\n── ⚠️ 数据异常：不同分享链接记录了同一个网盘文件名 ({len(conflicts)})")
        for tid in conflicts:
            record = store.get(tid)
            print(f"  ⚠️ [{tid}] {record.get('title')} [{record.get('file_name')}]  转存于 {record.get('saved_at') or '-'}")
        print("      其中只有一个是对的；转存错的那个用 03_save_to_panbaidu.py --redo <帖子ID> 重新转存")

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
                count = record.get("not_found_count") or 1
                if count >= NOT_FOUND_WARN_AFTER:
                    print(f"      ⚠️ 已连续 {count} 次未找到视频，"
                          f"{diagnose_not_found(record, conflicts, download_dir, mp4_dir)}")
                else:
                    print(f"      原因: {error or '未找到对应视频文件'}")

    if show_all and done:
        print(f"\n── 全部完成 ({len(done)})")
        for record in done:
            renamed = "、".join(record.get("renamed_to") or [])
            print(f"  ✅ {record.get('title')}" + (f" → {renamed}" if renamed else ""))


if __name__ == "__main__":
    main()
