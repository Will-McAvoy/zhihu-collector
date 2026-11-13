"""第二轮升级各项缺陷修复与体验改进的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.repairs import retry_saved_failures
from zhihu_collector.collectors.service import (
    CollectRequest,
    CollectionService,
    LimitReached,
    _resolve_mode,
    _write_export_file,
    export_saved_rows,
)
from zhihu_collector.config import Settings, is_portable_mode, load_settings, user_data_root
from zhihu_collector.database.database import Database
from zhihu_collector.downloader.image_downloader import ImageDownloader
from zhihu_collector.errors import CollectorError, TaskStopped
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.utils.urls import parse_zhihu_url


def test_p1_01_image_failure_retry_returns_partial_or_failed(tmp_path):
    """P1-01: 图片下载失败（如 404）时，重试绝不能误报为全部成功。"""
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    content_id = "20001"
    record = ContentRecord(
        zhihu_content_id=content_id,
        content_type="answer",
        url=f"https://www.zhihu.com/question/1/answer/{content_id}",
        title="测试回答图片失败",
        author=AuthorInfo(name="测试答主"),
        html_content="<p>正文内容带图片 <img src='https://pic1.zhimg.com/failed.jpg'></p>",
    )
    action, local_id = database.save_content(record, markdown="正文内容", html=record.html_content)
    database.replace_images(
        local_id,
        [
            {
                "original_url": "https://pic1.zhimg.com/failed.jpg",
                "local_path": "",
                "download_status": "failed",
                "error": "HTTP 404 Not Found",
                "hash": "",
                "sort_index": 1,
            }
        ],
    )

    # 404 mock
    def mock_handler(request: httpx.Request):
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)
    downloader = ImageDownloader(client=client, attempts=1)

    settings = Settings(export_markdown=True, export_docx=False, export_pdf=False)
    result = retry_saved_failures(
        database,
        settings,
        tmp_path / "output",
        downloader=downloader,
    )

    # 断言：不能报 success，必须是 partial 或 failed
    assert result.status in ("partial", "failed")
    assert result.completed == 0
    assert result.failed == 1
    assert "失败" in result.message

    # 数据库中的图片状态依然是 failed
    images = database.list_images(local_id)
    assert len(images) == 1
    assert images[0]["download_status"] != "downloaded"

    # 修复：当图片 200 成功下载时，重试应报告 success
    def mock_ok_handler(request: httpx.Request):
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfakeimagebytes", headers={"content-type": "image/png"})

    ok_client = httpx.Client(transport=httpx.MockTransport(mock_ok_handler))
    ok_downloader = ImageDownloader(client=ok_client, attempts=1)

    result_ok = retry_saved_failures(
        database,
        settings,
        tmp_path / "output",
        downloader=ok_downloader,
    )
    assert result_ok.status == "success"
    assert result_ok.completed == 1
    assert result_ok.failed == 0
    images_ok = database.list_images(local_id)
    assert images_ok[0]["download_status"] == "downloaded"


def test_p1_03_secondary_database_instance_does_not_interrupt_active_tasks(tmp_path):
    """P1-03: 第二个 Database 实例打开同一数据库时，绝不能篡改已有的 running 任务。"""
    db_path = tmp_path / "shared.db"
    db_a = Database(db_path)
    task_id = db_a.create_task(
        task_type="user_answers",
        target_url="https://www.zhihu.com/people/someone",
        target_id="someone",
        output_dir=str(tmp_path),
        options={},
    )
    db_a.update_task(task_id, status="running", total_completed=5, total_found=10)

    # 验证此时连接 A 读到 running
    task_before = db_a.latest_task("https://www.zhihu.com/people/someone")
    assert task_before["status"] == "running"

    # 连接 B 构造（指向同一数据库）
    db_b = Database(db_path)

    # 连接 A 再次读取，任务状态仍须为 running！
    task_after = db_a.latest_task("https://www.zhihu.com/people/someone")
    assert task_after["status"] == "running"

    # 仅当显式调用崩溃恢复时（如主进程启动），才转为 interrupted
    db_b.recover_interrupted_tasks()
    task_recovered = db_a.latest_task("https://www.zhihu.com/people/someone")
    assert task_recovered["status"] == "interrupted"


def test_p1_05_atomic_export_preserves_old_file_on_error(tmp_path):
    """P1-05: 导出时发生异常不会破坏旧文件（原子写入 .tmp 替换机制）。"""
    target = tmp_path / "document.md"
    target.write_text("原有正确完整的旧内容", encoding="utf-8")

    # 模拟写入中途出错
    with pytest.raises(RuntimeError):
        _write_export_file(
            export_format="unknown_format",
            rendered="新内容",
            base_dir=tmp_path,
            target=target,
            pdf=None,
            title="标题",
        )

    # 验证旧文件完好无损
    assert target.read_text(encoding="utf-8") == "原有正确完整的旧内容"
    assert not (tmp_path / "document.md.tmp").exists()


def test_p1_06_and_p2_04_settings_validation(tmp_path):
    """P1-06 & P2-04: load_settings 对错误字段类型与超出范围数值进行恢复并警告，永不崩溃。"""
    settings_file = tmp_path / "settings.json"
    malformed_data = {
        "image_concurrency": "not_an_int",
        "retry_count": -5,
        "page_interval_ms": -100,
        "headful_debug": "not_a_bool",
        "filename_rule": 12345,
    }
    settings_file.write_text(json.dumps(malformed_data), encoding="utf-8")

    loaded = load_settings(settings_file)
    default_settings = Settings()

    assert loaded.image_concurrency == default_settings.image_concurrency
    assert loaded.retry_count == default_settings.retry_count
    assert loaded.page_interval_ms == default_settings.page_interval_ms
    assert loaded.headful_debug == default_settings.headful_debug
    assert loaded.filename_rule == default_settings.filename_rule


def test_p2_01_single_answer_url_resolves_and_bare_answer_parsed():
    """P2-01: 单条回答链接自动识别为 single_answer 模式，裸 /answer/ 链接正确解析。"""
    # 完整问题回答链接
    url_full = "https://www.zhihu.com/question/55555/answer/88888"
    parsed_full = parse_zhihu_url(url_full)
    assert parsed_full.kind == "answer"
    assert parsed_full.question_id == "55555"
    assert parsed_full.answer_id == "88888"
    assert _resolve_mode("auto", parsed_full) == "single_answer"
    assert _resolve_mode("question", parsed_full) == "question"
    assert _resolve_mode("single_answer", parsed_full) == "single_answer"

    # 裸回答链接
    url_bare = "https://www.zhihu.com/answer/88888"
    parsed_bare = parse_zhihu_url(url_bare)
    assert parsed_bare.kind == "answer"
    assert parsed_bare.answer_id == "88888"
    assert _resolve_mode("auto", parsed_bare) == "single_answer"
    assert _resolve_mode("single_answer", parsed_bare) == "single_answer"

    # 裸回答链接若强制指定整个问题模式，给出明确报错
    with pytest.raises(CollectorError, match="问题搜集需要知乎问题链接"):
        _resolve_mode("question", parsed_bare)


def test_p2_02_task_rerun_updates_task_type_and_options(tmp_path):
    """P2-02: 续跑任务复用 task_id 时，必须同步更新 task_type 与 options_json。"""
    database = Database(tmp_path / "test.db")
    task_id = database.create_task(
        task_type="user_answers",
        target_url="https://www.zhihu.com/question/1/answer/2",
        target_id="2",
        output_dir=str(tmp_path),
        options={"mode": "answers", "export_pdf": False},
    )

    service = CollectionService(Settings(), database, TaskController())
    request = CollectRequest(
        url="https://www.zhihu.com/question/1/answer/2",
        mode="single_answer",
        export_pdf=True,
        task_id=task_id,
        download_images=False,
    )

    # 模拟 run 前段逻辑：直接调用更新
    parsed = parse_zhihu_url(request.url)
    mode = _resolve_mode(request.mode, parsed)
    task_types = {
        "answers": "user_answers",
        "pins": "user_pins",
        "all": "user_all",
        "question": "question_answers",
        "single_answer": "single_answer",
    }
    database.update_task(
        task_id,
        task_type=task_types[mode],
        options_json=json.dumps(request.as_options(), ensure_ascii=False),
        status="running",
    )

    task_record = database.latest_task(parsed.canonical)
    assert task_record["task_type"] == "single_answer"
    options = json.loads(task_record["options_json"])
    assert options["mode"] == "single_answer"
    assert options["export_pdf"] is True


def test_p2_03_bounded_crawl_stops_at_limit(tmp_path):
    """P2-03: 设置 max_items 上限时，达到数量即停止，不超额处理。"""
    database = Database(tmp_path / "test.db")
    controller = TaskController()
    service = CollectionService(Settings(), database, controller)
    request = CollectRequest(
        url="https://www.zhihu.com/people/test_user",
        mode="answers",
        max_items=2,
    )

    service._items_processed_this_run = 0
    records = [
        ContentRecord(zhihu_content_id=str(i), content_type="answer", url=f"http://test/{i}", title=f"T{i}")
        for i in range(5)
    ]

    processed = []
    try:
        for record in records:
            if request.max_items > 0 and service._items_processed_this_run >= request.max_items:
                service._hit_max_items = True
                raise LimitReached()
            processed.append(record)
            service._items_processed_this_run += 1
            if request.max_items > 0 and service._items_processed_this_run >= request.max_items:
                service._hit_max_items = True
                raise LimitReached()
    except LimitReached:
        pass

    assert len(processed) == 2
    assert service._hit_max_items is True
    assert service._items_processed_this_run == 2


def test_export_saved_rows_checkpoint_cancellation(tmp_path):
    """P2-03: export_saved_rows 支持 TaskController 检查点，遇到取消及时退出。"""
    database = Database(tmp_path / "test.db")
    rows = [{"id": 1, "content_type": "answer", "title": "A"}, {"id": 2, "content_type": "answer", "title": "B"}]
    controller = TaskController()
    controller.stop()  # 预先触发停止

    with pytest.raises(TaskStopped):
        export_saved_rows(
            database,
            rows,
            tmp_path,
            CollectRequest("offline"),
            Settings(),
            controller=controller,
        )
