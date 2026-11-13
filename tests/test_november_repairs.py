"""Regressions for failure paths changed during the November acceptance pass."""

from pathlib import Path

import pytest

from zhihu_collector import config
from zhihu_collector.browser.network_listener import NetworkListener
from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.repairs import retry_saved_failures
from zhihu_collector.collectors.service import CollectRequest
from zhihu_collector.database.database import Database
from zhihu_collector.downloader.image_downloader import ImageAsset
from zhihu_collector.errors import LoginExpiredError
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.utils.list_target import ListTarget, choose_paging, response_matches_list


def test_settings_replace_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    config.save_settings(config.Settings(output_dir="old"), target)
    original = target.read_bytes()

    def fail_replace(source, destination):
        assert Path(source).parent == target.parent and Path(destination) == target
        raise OSError("injected replace failure")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        config.save_settings(config.Settings(output_dir="new"), target)
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_pin_moments_response_and_errors_are_bound_to_requested_member():
    target = ListTarget("member_pins", "sample")
    url = "https://www.zhihu.com/api/v4/v2/pins/sample/moments?limit=2"

    class Response:
        status = 200
        headers = {"content-type": "application/json"}
        def __init__(self, url, data):
            self.url = url
            self.data = data
        def json(self):
            return self.data

    listener = NetworkListener()
    listener._capture(Response(url, {"data": [{"id": 42, "type": "pin"}], "paging": {"is_end": True}}))
    listener._capture(Response(url.replace("/sample/", "/sample-2/"), {"data": [], "paging": {"is_end": False}}))
    listener._capture(Response(url.replace("www.zhihu.com", "notzhihu.com"), {"data": [], "paging": {"is_end": False}}))
    assert len(listener.snapshot()) == 2
    paging, _ = choose_paging(listener.consume(), target, None)
    assert paging["is_end"]
    listener._capture(Response(url, {"message": "请先登录"}))
    errors = [e for e in listener.consume_errors() if response_matches_list(e["url"], target)]
    class Page:
        url = "https://www.zhihu.com/people/sample/pins"
    with pytest.raises(LoginExpiredError):
        inspect_page(Page(), found_content=1, network_errors=errors)


def test_restoring_merged_image_leaves_user_edited_copy_untouched(tmp_path):
    url = "https://picx.zhimg.com/repair.jpg"
    html = f'<p>正文</p><img src="{url}">'
    database = Database(tmp_path / "store.db")
    try:
        record = ContentRecord(
            zhihu_content_id="42", content_type="answer",
            url="https://www.zhihu.com/question/1/answer/42", title="内容",
            author=AuthorInfo(name="作者"), html_content=html,
        )
        _, content_id = database.save_content(record, markdown="正文", html=html)
        database.replace_images(content_id, [{"original_url": url, "download_status": "failed", "sort_index": 1}])
        request = CollectRequest(
            url=record.url, export_separate=False, export_merged=True, export_pdf=False,
            export_docx=False, save_metadata=False,
        )
        for directory in ("editable", "original"):
            result = export_local_contents(database, [content_id], tmp_path/directory, request, config.Settings())
            assert result.status == "success"
        modified = tmp_path / "editable" / "所选内容.md"
        modified.write_text(modified.read_text(encoding="utf-8") + "\n用户手工添加\n", encoding="utf-8")
        edited_bytes = modified.read_bytes()
        image_file = tmp_path / "repair.jpg"
        image_file.write_bytes(b"fake nonempty image asset")
        class Downloader:
            def download_all(self, urls, directory, content_id, **kwargs):
                return [ImageAsset(original_url=url, local_path=str(image_file), download_status="downloaded", sort_index=1)]
        result = retry_saved_failures(database, config.Settings(), tmp_path, downloader=Downloader())
        assert result.status == "success"
        assert modified.read_bytes() == edited_bytes
        original = tmp_path / "original" / "所选内容.md"
        text = original.read_text(encoding="utf-8")
        assert url not in text and "repair.jpg" in text
        assert not text.startswith("---")
        assert len(list(tmp_path.rglob("*.md"))) == 2
    finally:
        database.close()


def test_legacy_artifact_health_rejects_truncated_pdf(tmp_path):
    path = tmp_path / "legacy.pdf"
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<")
    assert "截断" in Database.recorded_artifact_problem({}, path, "pdf")

def test_cross_volume_export_copies_images_into_archive(tmp_path, monkeypatch):
    import os
    import shutil
    from zhihu_collector.exporters.markdown_exporter import localize_images

    source = tmp_path / "outside" / "picture.png"
    source.parent.mkdir()
    source.write_bytes(b"image bytes")
    archive = tmp_path / "archive"
    url = "https://picx.zhimg.com/picture.png"
    original = os.path.relpath

    def relpath(path, start):
        if Path(path) == source:
            raise ValueError("path is on another Windows drive")
        return original(path, start)

    monkeypatch.setattr(os.path, "relpath", relpath)
    text = localize_images(f"![]({url})", [{
        "original_url": url, "local_path": str(source), "download_status": "downloaded",
    }], archive / "book.md")
    assert text == "![](images/picture.png)"
    moved = tmp_path / "moved"
    shutil.move(archive, moved)
    assert (moved / "images" / "picture.png").read_bytes() == source.read_bytes()

def test_pin_html_excerpt_does_not_leak_into_title():
    from zhihu_collector.parsers.pin_parser import parse_pin_object

    pin = parse_pin_object({
        "id": "42", "type": "pin", "excerpt": "第一行<br><p>第二行 &amp; 续</p>",
        "content_html": "<p>正文</p>",
    })
    assert pin.title == "第一行 第二行 & 续"


def test_corrupt_settings_are_preserved_once_before_using_defaults(tmp_path):
    path = tmp_path / "settings.json"
    raw = bytes([255, 254, 128])
    path.write_bytes(raw)
    assert config.load_settings(path) == config.Settings()
    assert config.load_settings(path) == config.Settings()
    backups = list(tmp_path.glob("settings.json.corrupt-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
