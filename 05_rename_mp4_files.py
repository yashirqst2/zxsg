"""
MP4文件重命名模块
按 data/threads.json 中记录的网盘文件名（如 TJSP-107447）找到解压出的视频，重命名为帖子标题。

- 只匹配文件名完全相同，或"文件名 + 分隔符"开头的视频（如 TJSP-107447_1.mp4），
  不会把 TJSP-1074470.mp4 之类的文件误认为 TJSP-107447。
- 每个文件的处理结果都会打印出来，并写入 runtime/reports/rename_report_<时间>.txt。

使用方法:
python 05_rename_mp4_files.py            # 执行重命名
python 05_rename_mp4_files.py --dry-run  # 只预览，不改文件
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from common import config
from common.config import REPORTS_DIR
from common.store import ThreadStore, now

MP4_DIRECTORY = Path(config.get("paths.mp4_dir"))
VIDEO_EXTENSIONS = {e.lower() for e in config.get("rename.video_extensions", [".mp4"])}
# 每次运行生成一份带时间的报告，不覆盖之前的
REPORT_FILE = REPORTS_DIR / f"rename_report_{datetime.now():%Y%m%d_%H%M%S}.txt"


def clean_filename(filename: str) -> str:
    """清理文件名中的非法字符"""
    cleaned = re.sub(r'[\\/:*?"<>|]', '_', filename or "")
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip().rstrip('.')


def find_videos(directory: Path, file_name: str) -> List[Path]:
    """
    找出属于 file_name 的视频：
    - 目录下主名完全相同，或以 file_name 加非字母数字分隔符开头的视频；
    - 压缩包解压出的同名子文件夹（如 TJSP-105555/1.mp4、2.mp4）里的视频。
    """
    pattern = re.compile(re.escape(file_name) + r"(?:$|[^0-9A-Za-z])", re.IGNORECASE)
    videos = [p for p in directory.iterdir()
              if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and pattern.match(p.stem)]
    folder = directory / file_name
    if folder.is_dir():
        videos += [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
    return sorted(videos)


def unique_path(directory: Path, name: str, ext: str) -> Path:
    path = directory / f"{name}{ext}"
    counter = 1
    while path.exists():
        path = directory / f"{name}_{counter}{ext}"
        counter += 1
    return path


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    print("🎯 MP4文件重命名工具" + ("（预览模式，不会修改文件）" if dry_run else ""))
    print("=" * 50)

    if not MP4_DIRECTORY.exists():
        print(f"❌ 目录不存在: {MP4_DIRECTORY}")
        return

    store = ThreadStore()
    records = [r for r in store if r.get("file_name") and r.get("title")
               and r.get("rename_status") != "ok"]
    if not records:
        print("⚠️ 没有需要重命名的记录（需要 03 已记录文件名）")
        return
    print(f"📁 目录: {MP4_DIRECTORY}")
    print(f"共 {len(records)} 条待处理记录\n")

    renamed, not_found, failed = [], [], []
    for i, record in enumerate(records, 1):
        file_name, title = record["file_name"], record["title"]
        print(f"[{i}/{len(records)}] {file_name} → {title}")

        new_base = clean_filename(title)
        if not new_base:
            failed.append((file_name, "(无)", "标题清理后为空"))
            print("  ❌ 标题无效")
            continue

        videos = find_videos(MP4_DIRECTORY, file_name)
        if not videos:
            not_found.append((file_name, title))
            print("  ⚠️ 未找到对应视频文件")
            if not dry_run:
                store.upsert(record["tid"], rename_status="not_found", rename_error="未找到对应视频文件")
            continue

        new_names, errors = [], []
        for n, video in enumerate(videos, 1):
            # 一个压缩包里有多个视频时依次编号
            base = new_base if len(videos) == 1 else f"{new_base}_{n}"
            # 子文件夹里的视频会移到目录根部
            target = unique_path(MP4_DIRECTORY, base, video.suffix)
            if dry_run:
                print(f"  🔍 {video.relative_to(MP4_DIRECTORY)} → {target.name}")
                continue
            try:
                video.rename(target)
                new_names.append(target.name)
                source = str(video.relative_to(MP4_DIRECTORY))
                renamed.append((source, target.name))
                print(f"  ✅ {source} → {target.name}")
            except OSError as e:
                errors.append(f"{video.name}: {e}")
                failed.append((video.name, target.name, str(e)))
                print(f"  ❌ {video.name} → {target.name} 失败: {e}")

        if not dry_run:
            store.upsert(record["tid"], rename_status="failed" if errors else "ok",
                         renamed_to=new_names or None, rename_error="; ".join(errors) or None,
                         **({} if errors else {"renamed_at": now()}))
    if not dry_run:
        store.save()

    print("\n" + "=" * 50)
    print("📊 重命名完成" + ("（预览）" if dry_run else ""))
    print("=" * 50)
    print(f"✅ 成功: {len(renamed)} 个文件")
    print(f"⚠️ 未找到: {len(not_found)} 条")
    print(f"❌ 失败: {len(failed)} 个")
    if dry_run:
        return

    lines = [f"重命名报告  {datetime.now():%Y-%m-%d %H:%M:%S}", f"目录: {MP4_DIRECTORY}", ""]
    lines.append(f"== 成功 ({len(renamed)}) ==")
    lines += [f"{old} → {new}" for old, new in renamed]
    lines.append(f"\n== 未找到对应视频 ({len(not_found)}) ==")
    lines += [f"{name}（{title}）" for name, title in not_found]
    lines.append(f"\n== 重命名失败 ({len(failed)}) ==")
    lines += [f"{old} → {new}：{err}" for old, new, err in failed]
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"📄 报告已写入: {REPORT_FILE}")


if __name__ == "__main__":
    main()
