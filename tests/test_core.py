"""不需要浏览器的核心逻辑测试：python -m pytest"""

import importlib.util
from pathlib import Path

import pytest

from common import store as store_mod
from common.store import ThreadStore, archive_base_name, thread_id

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    """根目录脚本以数字开头，不能直接 import，用 importlib 按路径加载。"""
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url, tid", [
    ("https://www.zuxiashiguang.com/thread-157370-1-3.html", "157370"),
    ("https://www.zuxiashiguang.com/forum.php?mod=viewthread&tid=158172&highlight=x", "158172"),
    ("https://www.zuxiashiguang.com/forum.php?mod=forumdisplay&fid=2", None),
])
def test_thread_id(url, tid):
    assert thread_id(url) == tid


@pytest.mark.parametrize("name, base", [
    ("TJSP-102196.7z", "TJSP-102196"),
    ("TLZB-1.7z.001", "TLZB-1"),
    ("a.part2.rar", "a"),
    ("b.zip", "b"),
])
def test_archive_base_name(name, base):
    assert archive_base_name(name) == base


def test_concurrent_saves_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """两个脚本各自持有旧数据时，后保存的一方不能覆盖先保存一方写入的字段"""
    monkeypatch.setattr(store_mod, "EXPORT_FILE", tmp_path / "export.txt")
    path = tmp_path / "threads.json"
    seed = ThreadStore(path)
    seed.upsert("1", title="T", extract_status="ok", baidu_link="L", extract_code="ABCD")
    seed.save()

    saver, unzipper = ThreadStore(path), ThreadStore(path)   # 同时启动的 03 和 04
    saver.upsert("1", save_status="ok")
    saver.save()
    unzipper.upsert("1", unzip_status="ok")
    unzipper.save()

    record = ThreadStore(path).get("1")
    assert record["save_status"] == "ok" and record["unzip_status"] == "ok"
    assert record["title"] == "T"
    assert not (tmp_path / "threads.json.lock").exists()


class TestExtractCode:
    extractor = load_script("02_extract_content.py").ContentExtractor()

    def test_labelled_code(self):
        text = "链接: https://pan.baidu.com/s/1abc 提取码: ab12 解压密码：www.x.com"
        assert self.extractor.extract_code(text) == "AB12"
        assert self.extractor.extract_password(text) == "www.x.com"

    def test_code_from_pwd_param(self):
        assert self.extractor.extract_code("https://pan.baidu.com/s/1abc?pwd=Zx9q") == "ZX9Q"

    def test_password_is_not_taken_as_code(self):
        assert self.extractor.extract_code("解压密码: abcd 链接 https://pan.baidu.com/s/1abc") is None


class TestTransferResponse:
    parse = staticmethod(load_script("03_save_to_panbaidu.py").BaiduPanSaver.parse_transfer_response)

    def test_success(self):
        body = '{"errno":0,"extra":{"list":[{"from":"/TJSP-1.7z","to":"/nztd/TJSP-1.7z"}]},"show_msg":""}'
        assert self.parse({"status": 200, "body": body}) == (True, "转存成功", "TJSP-1")

    def test_already_saved_counts_as_success(self):
        body = '{"errno":4,"duplicated":{"list":[{"path":"/nztd/TJSP-1.7z"}]},"show_msg":"文件已转存"}'
        ok, _, name = self.parse({"status": 200, "body": body})
        assert ok and name == "TJSP-1"

    def test_failure(self):
        ok, message, _ = self.parse({"status": 200, "body": '{"errno":-9,"show_msg":"分享已失效"}'})
        assert not ok and "分享已失效" in message


def test_find_videos_matches_exact_name_and_subfolder(tmp_path, monkeypatch):
    rename = load_script("05_rename_mp4_files.py")
    for name in ["TJSP-1.mp4", "TJSP-1_2.mp4", "TJSP-10.mp4", "TJSP-1.txt"]:
        (tmp_path / name).write_text("x")
    (tmp_path / "TJSP-2").mkdir()
    (tmp_path / "TJSP-2" / "1.mp4").write_text("x")

    assert [p.name for p in rename.find_videos(tmp_path, "TJSP-1")] == ["TJSP-1.mp4", "TJSP-1_2.mp4"]
    assert [p.name for p in rename.find_videos(tmp_path, "TJSP-2")] == ["1.mp4"]


@pytest.mark.parametrize("record, expected", [
    ({"extract_status": "failed", "extract_error": "缺少提取码"}, (2, "failed", "缺少提取码")),
    ({"extract_status": "ok"}, (3, "pending", None)),
    ({"extract_status": "ok", "save_status": "ok"}, (4, "pending", None)),
    ({"extract_status": "ok", "save_status": "ok", "unzip_status": "ok", "rename_status": "not_found"},
     (5, "not_found", None)),
    ({"extract_status": "ok", "save_status": "ok", "unzip_status": "ok", "rename_status": "ok"}, None),
])
def test_status_current_step(record, expected):
    import status
    assert status.current_step(record) == expected


def test_add_link_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "EXPORT_FILE", tmp_path / "export.txt")
    s = ThreadStore(tmp_path / "threads.json")
    assert s.add_link("https://www.zuxiashiguang.com/thread-1-1-1.html")
    assert [r["tid"] for r in s.pending_extraction()] == ["1"]
    s.upsert("1", extract_status="ok", title="T")
    assert not s.add_link("https://www.zuxiashiguang.com/forum.php?mod=viewthread&tid=1")
    assert s.get("1")["extract_status"] == "ok" and s.pending_extraction() == []
