# zxsg

从足下时光（zuxiashiguang.com）帖子提取百度网盘链接 → 转存到网盘 → 解压 → 按帖子标题重命名视频。

## 目录结构

```
zxsg/
├── 01_cleanup_and_extract.py      # 清理已解压的压缩包 + 从调试浏览器标签页提取帖子链接
├── 02_extract_content.py          # 打开帖子，提取标题/百度链接/提取码/解压密码（自动使用免费额度解锁）
├── 03_save_to_panbaidu.py         # 转存到百度网盘目标文件夹（以转存接口返回为准）
├── 04_open_and_decrypt_files.py   # 用各帖子自己的密码解压
├── 05_rename_mp4_files.py         # 视频重命名为帖子标题，生成报告
├── check_zxsg_login.py            # 检查/登录足下时光
├── check_baidu_login.py           # 检查/登录百度网盘（扫码）
├── run_pipeline.py                # 按顺序运行整条流程
├── status.py                      # 查看每个帖子卡在哪一步、失败原因
├── common/                        # 各脚本共用：配置、浏览器、数据存储
├── tests/                         # python -m pytest
├── config.example.json            # 配置模板（复制为 config.json 后修改）
└── runtime/                       # 运行时文件（不纳入 git）
    ├── input/links.txt            # 01 最近一次抓到的链接（仅作记录；也可手动加链接，02 会自动登记）
    ├── state/threads.json         # 所有帖子的数据与各步骤状态（核心数据）
    ├── reports/                   # extracted_contents.txt、rename_report_<时间>.txt
    └── profiles/                  # chrome_profile（自动化浏览器登录状态）、browser_data_debug
```

## 首次使用

```powershell
pip install -r requirements.txt
copy config.example.json config.json   # 按需修改路径、7z 位置、备用密码
python check_zxsg_login.py
python check_baidu_login.py
```

## 日常运行

1. 用调试端口启动 Edge，打开要下载的帖子：
   ```powershell
   start msedge.exe --remote-debugging-port=9222 --user-data-dir="D:\projects\zxsg\runtime\profiles\browser_data_debug"
   ```
2. 运行流程（第 3 步后会暂停，等你在百度网盘客户端下载完成再继续）：
   ```powershell
   python run_pipeline.py              # 1~5 步
   python run_pipeline.py --from 4     # 从第 4 步开始
   python run_pipeline.py --only 5     # 只运行第 5 步
   ```
   也可以直接单独运行任意一步，例如 `python 05_rename_mp4_files.py --dry-run`。

随时运行 `python status.py` 查看进度（`--all` 同时列出已完成的）。

每一步都会跳过已完成的帖子、自动重试失败的帖子，可以放心重复运行。
设置环境变量 `HEADLESS=false` 可显示浏览器窗口。
