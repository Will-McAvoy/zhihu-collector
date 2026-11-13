"""第6轮功能升级回归测试套件。

涵盖：
1. 常用采集配置（Preset）：加载、增删改查、白名单、原子写入、格式校验、类型校验、损坏自动恢复。
2. 导出范围与确认（ExportPlan）：计划生成、冲突策略（新副本、覆盖、跳过）、跨页勾选。
3. 文章阅读预览增强（PreviewRenderer）：XSS与危险标签过滤、本地图片与缺失占位、样式渲染。
4. 收藏夹归档（Collection）：链接识别、API与DOM条目解析、非回答独立计数、去重、Schema V6 存储与快照、结果报告。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.collection_answers import CollectionManifest
from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.service import CollectRequest, CollectionService
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.export_plan import ExportPlan, resolve_file_conflict
from zhihu_collector.gui.preview_renderer import render_article_preview, sanitize_preview_html
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.collection_parser import (
    extract_collection_title,
    parse_collection_items,
    parse_collection_items_detailed,
    payload_matches_collection,
)
from zhihu_collector.presets import Preset, PresetManager, get_default_presets
from zhihu_collector.utils.urls import parse_zhihu_url, public_collection_url


# ==============================================================================
# 1. 常用采集配置测试
# ==============================================================================


def test_presets_defaults_and_crud(tmp_path: Path):
    preset_file = tmp_path / "presets.json"
    manager = PresetManager(preset_file)
    assert len(manager.presets) == 3
    assert manager.last_selected_id == "daily_archive"
    assert preset_file.is_file()

    # 新建并保存自定义配置
    custom = Preset(
        id="custom_test",
        name="我的归档测试",
        export_markdown=True,
        export_docx=True,
        export_pdf=False,
        export_separate=True,
        export_merged=False,
        download_images=True,
        save_metadata=True,
        max_items=20,
    )
    manager.save_or_update(custom)
    assert len(manager.presets) == 4
    assert manager.last_selected_id == "custom_test"

    # 重启重新加载
    reloaded = PresetManager(preset_file)
    assert len(reloaded.presets) == 4
    found = reloaded.get_preset("custom_test")
    assert found is not None
    assert found.name == "我的归档测试"
    assert found.max_items == 20

    # 修改重命名
    found.name = "我的归档改名"
    reloaded.save_or_update(found)
    assert reloaded.get_preset("custom_test").name == "我的归档改名"

    # 删除自定义配置
    deleted = reloaded.delete_preset("custom_test")
    assert deleted is True
    assert len(reloaded.presets) == 3
    assert reloaded.get_preset("custom_test") is None


def test_presets_validation_and_types(tmp_path: Path):
    preset_file = tmp_path / "presets.json"
    manager = PresetManager(preset_file)

    # 1. 格式全不选
    invalid1 = Preset(id="inv1", name="无格式", export_markdown=False, export_docx=False, export_pdf=False)
    with pytest.raises(ValueError, match="至少需要选择一种导出格式"):
        manager.save_or_update(invalid1)

    # 2. 输出方式全不选
    invalid2 = Preset(id="inv2", name="无方式", export_separate=False, export_merged=False)
    with pytest.raises(ValueError, match="至少需要选择一种输出方式"):
        manager.save_or_update(invalid2)

    # 3. 负数上限
    invalid3 = Preset(id="inv3", name="负数", max_items=-5)
    with pytest.raises(ValueError, match="篇数上限必须为非负整数"):
        manager.save_or_update(invalid3)

    # 4. bool 作为整数防范（Python 中 bool 是 int 的子类）
    invalid4 = Preset(id="inv4", name="布尔篇数", max_items=True)
    with pytest.raises(ValueError, match="篇数上限必须为非负整数"):
        manager.save_or_update(invalid4)


def test_presets_corruption_recovery(tmp_path: Path):
    preset_file = tmp_path / "presets.json"
    preset_file.write_text("{ corrupt json: 123", encoding="utf-8")

    manager = PresetManager(preset_file)
    # 应自动恢复默认模板
    assert len(manager.presets) == 3
    assert manager.last_selected_id == "daily_archive"
    # 原损坏文件被重命名保留
    corrupted_files = list(tmp_path.glob("presets.json.corrupted-*"))
    assert len(corrupted_files) == 1
    assert "{ corrupt json" in corrupted_files[0].read_text(encoding="utf-8")


# ==============================================================================
# 2. 导出范围与冲突处理测试
# ==============================================================================


def test_export_plan_and_conflict_resolution(tmp_path: Path):
    plan = ExportPlan(
        scope_kind="offline_exact",
        source_title="测试导出计划",
        member_ids=["101", "102"],
        export_markdown=True,
        export_docx=True,
        export_pdf=False,
        output_root=str(tmp_path),
        conflict_policy="new_copy",
    )
    plan.validate()
    assert plan.is_exact is True
    assert plan.exact_count == 2

    # 测试冲突策略
    file1 = tmp_path / "article.md"
    file1.write_text("原有内容", encoding="utf-8")

    # 1. new_copy：生成副本
    candidate, skip = resolve_file_conflict(file1, conflict_policy="new_copy")
    assert skip is False
    assert candidate.name == "article_副本1.md"

    # 2. skip：跳过
    candidate_skip, skip2 = resolve_file_conflict(file1, conflict_policy="skip")
    assert skip2 is True

    # 3. overwrite：覆盖现有
    candidate_ow, skip3 = resolve_file_conflict(file1, conflict_policy="overwrite")
    assert skip3 is False
    assert candidate_ow.name == "article.md"


def test_offline_export_conflict_policy(tmp_path: Path):
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 插入两条测试回答
    rec1 = ContentRecord(
        zhihu_content_id="111",
        content_type="answer",
        url="https://www.zhihu.com/question/1/answer/111",
        title="第一篇回答",
        html_content="<p>正文一</p>",
    )
    rec2 = ContentRecord(
        zhihu_content_id="222",
        content_type="answer",
        url="https://www.zhihu.com/question/1/answer/222",
        title="第二篇回答",
        html_content="<p>正文二</p>",
    )
    _, cid1 = database.save_content(rec1, markdown="正文一", html="<p>正文一</p>")
    _, cid2 = database.save_content(rec2, markdown="正文二", html="<p>正文二</p>")

    settings = Settings()

    # 首次导出合并电子书
    req1 = CollectRequest(
        url="offline",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        export_separate=False,
        export_merged=True,
        output_dir=str(output_dir),
        conflict_policy="new_copy",
    )
    res1 = export_local_contents(database, [cid1, cid2], output_dir, req1, settings)
    assert res1.status == "success"
    merged_file = output_dir / "所选内容.md"
    assert merged_file.is_file()

    # 第二次导出：策略为 new_copy，应生成 所选内容_副本1.md，原文件不被静默覆盖
    req2 = CollectRequest(
        url="offline",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        export_separate=False,
        export_merged=True,
        output_dir=str(output_dir),
        conflict_policy="new_copy",
    )
    res2 = export_local_contents(database, [cid1, cid2], output_dir, req2, settings)
    assert res2.status == "success"
    copy_file = output_dir / "所选内容_副本1.md"
    assert copy_file.is_file()
    assert merged_file.is_file()


# ==============================================================================
# 3. 文章阅读预览渲染测试
# ==============================================================================


def test_preview_renderer_safety_and_images(tmp_path: Path):
    # 模拟包含脚本、iframe、事件属性的恶意正文
    malicious_html = """
    <h1>正常标题</h1>
    <script>alert('xss');</script>
    <iframe src="http://evil.com"></iframe>
    <img src="https://pic1.zhimg.com/downloaded.jpg" onload="alert(1)" onerror="alert(2)">
    <img src="https://pic1.zhimg.com/missing.jpg">
    <a href="javascript:alert('bad')">恶意链接</a>
    <a href="https://www.zhihu.com/question/123">知乎正常链接</a>
    <p>正文内容测试</p>
    """

    local_img_file = tmp_path / "img1.jpg"
    local_img_file.write_bytes(b"image bytes")

    images = [
        {
            "original_url": "https://pic1.zhimg.com/downloaded.jpg",
            "local_path": str(local_img_file),
            "download_status": "downloaded",
        },
        {
            "original_url": "https://pic1.zhimg.com/missing.jpg",
            "local_path": "",
            "download_status": "failed",
        },
    ]

    row = {
        "title": "测试文章",
        "author_name": "张三",
        "html_content": malicious_html,
        "markdown_content": "",
        "updated_time": "2026-09-27",
        "created_time": "2026-09-27",
        "url": "https://www.zhihu.com/question/1/answer/111",
    }

    rendered_html = render_article_preview(row, images, base_font_size=16)

    # 验证恶意内容彻底被过滤
    assert "<script" not in rendered_html
    assert "alert(" not in rendered_html
    assert "<iframe" not in rendered_html
    assert "onload" not in rendered_html
    assert "onerror" not in rendered_html
    assert "javascript:" not in rendered_html

    # 验证正常内容存在
    assert "正常标题" in rendered_html
    assert "正文内容测试" in rendered_html
    assert "https://www.zhihu.com/question/123" in rendered_html

    # 验证本地图片协议与缺失占位
    assert local_img_file.as_posix() in rendered_html
    assert "【图片未下载或本地文件缺失】" in rendered_html


def test_preview_renderer_rejects_paths_outside_allowed_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    row = {
        "html_content": '<img src="https://pic1.zhimg.com/outside.jpg">',
        "markdown_content": "",
    }
    images = [{"original_url": "https://pic1.zhimg.com/outside.jpg", "local_path": str(outside)}]
    rendered = render_article_preview(row, images, allowed_roots=[allowed])
    assert outside.as_posix() not in rendered
    assert "【图片未下载或本地文件缺失】" in rendered


def test_collection_parser_keeps_short_and_image_only_answers_complete():
    payload = {
        "collection": {"id": 12, "title": "短内容"},
        "data": [
            {"content": {"type": "answer", "id": 1, "question": {"id": 2, "title": "短回答"}, "content": "<p>好</p>"}},
            {"content": {"type": "answer", "id": 2, "question": {"id": 3, "title": "图片回答"}, "content": '<img src="https://pic1.zhimg.com/a.jpg">'}},
        ],
    }
    result = parse_collection_items_detailed([payload], target_collection_id="12")
    assert [item.zhihu_content_id for item in result.answers] == ["1", "2"]
    assert all(item.content_incomplete is False for item in result.answers)


def test_collection_filtering_is_strict_and_unknown_types_are_separate():
    target_payload = {"collection": {"id": 12}, "data": []}
    other_payload = {"collection": {"id": 123}, "data": []}
    assert payload_matches_collection(target_payload, "https://www.zhihu.com/api/v4/collections/12/items", "12") is True
    assert payload_matches_collection(other_payload, "https://www.zhihu.com/api/v4/collections/123/items", "12") is False

    mixed = {
        "collection": {"id": 12},
        "data": [
            {"content": {"type": "article", "id": 1}},
            {"content": {"type": "mystery", "id": 2}},
            {"content": {"id": 3}},
        ],
    }
    result = parse_collection_items_detailed([mixed], target_collection_id="12")
    assert result.non_answer_count == 1
    assert result.unknown_type_count == 2


# ==============================================================================
# 4. 收藏夹功能测试（URL解析、列表解析、Schema V6及归档闭环）
# ==============================================================================


def test_collection_url_parsing():
    # 正常链接
    p1 = parse_zhihu_url("https://www.zhihu.com/collection/51234567")
    assert p1.kind == "collection"
    assert p1.collection_id == "51234567"
    assert p1.canonical == "https://www.zhihu.com/collection/51234567"
    assert p1.collection_page_url == "https://www.zhihu.com/collection/51234567"

    # 末尾斜杠与参数
    p2 = parse_zhihu_url("https://www.zhihu.com/collection/998877/?page=2")
    assert p2.kind == "collection"
    assert p2.collection_id == "998877"

    # 纯域名
    p3 = parse_zhihu_url("zhihu.com/collection/12345")
    assert p3.kind == "collection"
    assert p3.collection_id == "12345"

    # 错误链接（非数字后缀或外部域名）
    p_err1 = parse_zhihu_url("https://www.zhihu.com/collection/abc")
    assert p_err1.kind == "unknown"

    p_err2 = parse_zhihu_url("https://example.com/collection/12345")
    assert p_err2.kind == "unknown"


def test_collection_items_parsing_and_filtering():
    # 构造知乎收藏夹接口的典型响应数据（包含回答、文章、视频等混合条目）
    mock_payload = {
        "collection": {"id": 12345, "title": "经典架构精选"},
        "paging": {"is_end": True, "totals": 5},
        "data": [
            {
                "content": {
                    "type": "answer",
                    "id": 1001,
                    "question": {"id": 888, "title": "如何设计高并发系统？"},
                    "author": {"id": "u1", "name": "架构师小王", "url_token": "wang"},
                    "content": "<p>高并发系统的第一原则是解耦...</p>",
                    "excerpt": "高并发系统的第一原则...",
                    "voteup_count": 256,
                    "comment_count": 32,
                }
            },
            {
                # 非回答条目（文章）：应独立统计
                "content": {
                    "type": "article",
                    "id": 2002,
                    "title": "专栏文章：Redis底层设计",
                }
            },
            {
                # 非回答条目（视频）：应独立统计
                "content": {
                    "type": "zvideo",
                    "id": 3003,
                    "title": "视频讲解",
                }
            },
            {
                # 第二篇回答
                "content": {
                    "type": "answer",
                    "id": 1002,
                    "question": {"id": 889, "title": "分布式事务怎么做？"},
                    "author": {"id": "u2", "name": "李四", "url_token": "lisi"},
                    "content": "<p>两阶段提交与Saga模式对比...</p>",
                }
            },
            {
                # 重复回答条目：应独立统计
                "content": {
                    "type": "answer",
                    "id": 1001,
                    "question": {"id": 888, "title": "如何设计高并发系统？"},
                }
            },
        ],
    }

    title = extract_collection_title([mock_payload], "")
    assert title == "经典架构精选"

    answers, non_ans, dups = parse_collection_items([mock_payload], target_collection_id="12345")
    # 只提取回答
    assert len(answers) == 2
    assert answers[0].zhihu_content_id == "1001"
    assert answers[0].question_title == "如何设计高并发系统？"
    assert answers[0].author.name == "架构师小王"
    assert answers[1].zhihu_content_id == "1002"

    # 非回答统计与重复统计准确
    assert non_ans == 2  # 1 article + 1 zvideo
    assert dups == 1  # 1 duplicate answer


def test_collection_database_schema_v6_and_filtering(tmp_path: Path):
    db_path = tmp_path / "v6_test.db"
    database = Database(db_path)

    # 1. 保存收藏夹信息
    coll_id = "556677"
    database.save_collection(coll_id, title="全栈开发宝典", url="https://www.zhihu.com/collection/556677")
    coll = database.get_collection(coll_id)
    assert coll is not None
    assert coll["title"] == "全栈开发宝典"

    # 2. 保存成员与快照
    members = [
        {"answer_id": "8001", "sort_order": 1, "item_status": "discovered"},
        {"answer_id": "8002", "sort_order": 2, "item_status": "discovered"},
    ]
    database.save_collection_members(coll_id, members)

    snap_id = database.create_collection_snapshot(
        task_id=1,
        collection_id=coll_id,
        member_ids=["8001", "8002"],
        total_declared=2,
        discovered_answers=2,
        non_answer_count=0,
        duplicate_count=0,
        integrity_status="complete",
        finish_reason="已到达末尾",
    )
    assert snap_id > 0
    snapshot = database.get_collection_snapshot(task_id=1)
    assert snapshot["integrity_status"] == "complete"
    assert json.loads(snapshot["member_ids_json"]) == ["8001", "8002"]

    # 3. 内容入库时自动关联收藏夹成员 content_id
    rec1 = ContentRecord(
        zhihu_content_id="8001",
        content_type="answer",
        url="https://www.zhihu.com/answer/8001",
        title="Python 技巧",
        html_content="<p>代码简洁</p>",
    )
    rec_other = ContentRecord(
        zhihu_content_id="9999",
        content_type="answer",
        url="https://www.zhihu.com/answer/9999",
        title="不属于该收藏夹的内容",
        html_content="<p>其他内容</p>",
    )
    _, cid1 = database.save_content(rec1, markdown="代码简洁", html="<p>代码简洁</p>")
    _, cid_other = database.save_content(rec_other, markdown="其他内容", html="<p>其他内容</p>")

    # 验证 collection_members 表已自动关联 content_id
    stored_members = database.get_collection_members(coll_id)
    assert len(stored_members) == 2
    linked_m1 = next(m for m in stored_members if m["answer_id"] == "8001")
    assert linked_m1["content_id"] == cid1

    # 4. 内容库按收藏夹筛选
    all_rows, total_all = database.search_contents()
    assert total_all >= 2

    coll_rows, total_coll = database.search_contents(collection_id=coll_id)
    assert total_coll == 1
    assert coll_rows[0]["zhihu_content_id"] == "8001"
