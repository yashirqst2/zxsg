"""
解压下载目录中的压缩包。

- 每个压缩包按文件名（03 记录的网盘文件名）找到对应帖子，优先使用该帖子的解压密码；
  找不到或密码不对时，再依次尝试其它帖子出现过的密码和 config.json 中的 fallback_passwords。
- 解压结果记录到 data/threads.json（01 只会清理已成功解压的压缩包）。
- 失败时输出 7z 的错误信息。
"""

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from common import config
from common.store import ThreadStore, archive_base_name, now

SOURCE_DIR = Path(config.get("paths.baidu_download"))
EXTRACT_TO = Path(config.get("paths.mp4_dir"))
SEVEN_ZIP = config.get("extractor.seven_zip_path")
EXTENSIONS = [e.lower() for e in config.get("extractor.file_extensions", [".7z", ".zip", ".rar"])]
DELETE_AFTER_EXTRACT = config.get("extractor.delete_after_extract", False)
FALLBACK_PASSWORDS = config.get("extractor.fallback_passwords", [])


def run_7z(args: List[str]) -> Tuple[bool, str]:
    result = subprocess.run([SEVEN_ZIP, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    output = (result.stderr or "") + (result.stdout or "")
    errors = [line.strip() for line in output.splitlines()
              if line.strip().startswith(("ERROR", "Wrong password", "Can not open", "Headers Error",
                                          "Data Error", "CRC Failed", "Unexpected end"))]
    return result.returncode == 0, "; ".join(dict.fromkeys(errors)) or f"7z 退出码 {result.returncode}"


def is_first_volume(path: Path) -> bool:
    """分卷压缩只处理第一卷（.7z.001 / .part1.rar），其它分卷由 7z 自动读取。"""
    name = path.name.lower()
    if name.endswith(".rar") and ".part" in name:
        return name.rsplit(".part", 1)[1].split(".")[0].lstrip("0") == "1"
    return True


def find_archives() -> List[Path]:
    archives = []
    for path in sorted(SOURCE_DIR.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if any(name.endswith(ext) for ext in EXTENSIONS) and is_first_volume(path):
            archives.append(path)
        elif name.endswith(".001") and any(name[:-4].endswith(ext) for ext in EXTENSIONS):
            archives.append(path)
    return archives


def candidate_passwords(record: Optional[dict], store: ThreadStore) -> List[Optional[str]]:
    candidates: List[Optional[str]] = []
    if record and record.get("password"):
        candidates.append(record["password"])
    candidates += [r.get("password") for r in store if r.get("password")]
    candidates += FALLBACK_PASSWORDS
    candidates.append(None)  # 也可能没有密码
    return list(dict.fromkeys(candidates))


def extract(archive: Path, passwords: List[Optional[str]]) -> Tuple[bool, str, Optional[str]]:
    """
    先用第一个候选密码直接解压；失败再用 7z t 逐个测试其它密码，找到正确的再解压。
    返回 (是否成功, 错误信息, 使用的密码)
    """
    def args(cmd: str, pw: Optional[str]) -> List[str]:
        # 始终传 -p，避免 7z 在没有密码时等待键盘输入
        base = [cmd, f"-p{pw or ''}", str(archive)]
        return base + ([f"-o{EXTRACT_TO}", "-y"] if cmd == "x" else [])

    ok, error = run_7z(args("x", passwords[0]))
    if ok:
        return True, "", passwords[0]
    first_error = error
    for pw in passwords[1:]:
        tested, _ = run_7z(args("t", pw))
        if tested:
            ok, error = run_7z(args("x", pw))
            return ok, error, pw
    return False, first_error, None


def main() -> None:
    if not SOURCE_DIR.exists():
        print(f"错误: 源目录不存在: {SOURCE_DIR}")
        return
    if not Path(SEVEN_ZIP).exists():
        print(f"错误: 7z.exe 不存在: {SEVEN_ZIP}（请在 config.json 的 extractor.seven_zip_path 中设置）")
        return
    EXTRACT_TO.mkdir(parents=True, exist_ok=True)

    archives = find_archives()
    if not archives:
        print("没有找到需要解压的文件")
        return
    print(f"找到 {len(archives)} 个压缩文件，解压到 {EXTRACT_TO}")

    store = ThreadStore()
    failed = []
    success = 0
    for i, archive in enumerate(archives, 1):
        base = archive_base_name(archive.name)
        record = store.find_by_file_name(base)
        title = f"（{record['title']}）" if record and record.get("title") else "（未找到对应帖子）"
        print(f"[{i}/{len(archives)}] 正在解压: {archive.name} {title}")

        ok, error, used_pw = extract(archive, candidate_passwords(record, store))
        if ok:
            success += 1
            note = "" if not record or used_pw == record.get("password") else f"，实际密码: {used_pw or '无'}"
            print(f"  ✅ 成功{note}")
            if DELETE_AFTER_EXTRACT:
                try:
                    os.remove(archive)
                except OSError as e:
                    print(f"  ⚠️ 删除压缩包失败: {e}")
        else:
            failed.append((archive.name, error))
            print(f"  ❌ 失败: {error}")

        if record:
            store.upsert(record["tid"], unzip_status="ok" if ok else "failed",
                         unzip_error=None if ok else error, **({"unzipped_at": now()} if ok else {}))
            store.save()

    print(f"\n解压完成: {success}/{len(archives)} 个成功")
    if failed:
        print(f"失败 {len(failed)} 个:")
        for name, error in failed:
            print(f"  - {name}: {error}")


if __name__ == "__main__":
    main()
