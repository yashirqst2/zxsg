"""
按顺序运行整条流程，某一步失败时停止。

  python run_pipeline.py              # 运行 1~5 步
  python run_pipeline.py --from 3     # 从第 3 步开始
  python run_pipeline.py --only 5     # 只运行第 5 步
  python run_pipeline.py --login      # 先检查足下时光和百度网盘的登录状态

步骤：
  1 清理已解压的压缩包 + 从调试浏览器提取帖子链接（需先用 --remote-debugging-port=9222 启动浏览器）
  2 提取帖子内容（标题/百度链接/提取码/解压密码）
  3 转存到百度网盘
  4 解压（需先在百度网盘客户端下载完成）
  5 重命名视频
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGIN_STEPS = [
    ("登录检查：足下时光", ROOT / "check_zxsg_login.py"),
    ("登录检查：百度网盘", ROOT / "check_baidu_login.py"),
]
STEPS = {
    1: ("清理压缩包 + 提取链接", ROOT / "01_cleanup_and_extract.py"),
    2: ("提取帖子内容", ROOT / "02_extract_content.py"),
    3: ("转存到百度网盘", ROOT / "03_save_to_panbaidu.py"),
    4: ("解压", ROOT / "04_open_and_decrypt_files.py"),
    5: ("重命名视频", ROOT / "05_rename_mp4_files.py"),
}


def run(title: str, script: Path) -> bool:
    print(f"\n{'#' * 60}\n# {title}\n{'#' * 60}")
    return subprocess.run([sys.executable, str(script)], cwd=script.parent).returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="zxsg 下载流程")
    parser.add_argument("--from", dest="start", type=int, default=1, choices=STEPS)
    parser.add_argument("--to", dest="end", type=int, default=5, choices=STEPS)
    parser.add_argument("--only", type=int, choices=STEPS)
    parser.add_argument("--login", action="store_true", help="先运行登录检查")
    args = parser.parse_args()

    steps = [args.only] if args.only else range(args.start, args.end + 1)
    plan = (LOGIN_STEPS if args.login else []) + [STEPS[n] for n in steps]
    for title, script in plan:
        if not run(title, script):
            print(f"\n❌ “{title}”失败，流程已停止")
            sys.exit(1)
        if title == STEPS[3][0] and 4 in steps:
            input("\n⏸️ 请在百度网盘客户端把转存的文件下载完成后，按回车继续解压...")
    print("\n✅ 全部完成")


if __name__ == "__main__":
    main()
