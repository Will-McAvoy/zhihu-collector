from zhihu_collector.utils.file_utils import apply_filename_rule, safe_filename, unique_path


def test_replaces_windows_invalid_characters_and_keeps_chinese_emoji():
    name = safe_filename('如何:评价?测试*文件/名称\\标题"引号<尖>括号|线 中文 😀')
    assert ":" not in name
    assert "?" not in name
    assert "*" not in name
    assert "/" not in name
    assert "\\" not in name
    assert '"' not in name
    assert "<" not in name
    assert ">" not in name
    assert "|" not in name
    assert "中文" in name
    assert "😀" in name


def test_reserved_device_name():
    assert safe_filename("CON").upper().startswith("_CON")
    assert safe_filename("aux.txt").lower().startswith("_aux")
    assert safe_filename("com1").upper().startswith("_COM1")


def test_long_title_is_trimmed():
    name = safe_filename("很长" * 100, max_length=40, extension=".md")
    assert len(name) <= 40
    assert name.endswith(".md")


def test_blank_title_uses_fallback():
    assert safe_filename("   ", fallback="1001") == "1001"


def test_filename_rule_and_unique_path(tmp_path):
    rendered = apply_filename_rule(
        "{index:04d}-{title}",
        index=1,
        title="为什么？",
        content_id="9",
        extension=".md",
    )
    assert rendered.startswith("0001-")
    assert rendered.endswith(".md")
    assert "?" not in rendered
    first = tmp_path / rendered
    first.write_text("x", encoding="utf-8")
    second = unique_path(first)
    assert second != first
    assert second.stem.endswith("_2")
