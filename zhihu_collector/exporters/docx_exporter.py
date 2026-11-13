"""把 Markdown 写成使用标题样式的 Word 文档。"""

from __future__ import annotations

import re
from pathlib import Path

import markdown
from bs4 import BeautifulSoup, NavigableString
from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# CommonMark 围栏：三个以上反引号或波浪线，info string 第一个词是语言（可含 + # - 等）。
_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})\s*(.*)$")
_HEADING_ANCHOR_RE = re.compile(r"\s*\{#([^}\s]+)\}\s*$")
_BOOKMARK_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def markdown_to_docx(
    markdown_text: str,
    destination: Path,
    *,
    base_dir: Path | None = None,
    frontmatter: bool | None = None,
) -> Path:
    """frontmatter 由调用方说明本次文本是否带程序生成的元数据头；None 时只剥离严格匹配的元数据块。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    _set_default_font(document)
    _add_blocks(document, markdown_text, base_dir or destination.parent, frontmatter=frontmatter)
    document.save(destination)
    return destination


def add_toc_field(document: Document) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    _append_field(run, r' TOC \o "1-2" \h \z \u ')
    hint = paragraph.add_run("（在 Word 中右键目录，选择“更新域”以刷新页码）")
    hint.italic = True
    hint.font.color.rgb = RGBColor(0x66, 0x66, 0x66)


def _add_blocks(document: Document, markdown_text: str, base_dir: Path, *, frontmatter: bool | None = None) -> None:
    from zhihu_collector.exporters.markdown_exporter import split_frontmatter

    # 只剥离确实存在且完整的元数据头。正文可以用分隔线开头，不能因此被吞掉。
    _meta, body = split_frontmatter(markdown_text, present=frontmatter)
    lines = body.split("\n")
    index = 0
    document.part._zc_bookmarks = _plan_bookmarks(lines)
    document.part._zc_bookmark_ids = 0
    paragraph_buffer: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        text = " ".join(part.strip() for part in paragraph_buffer if part.strip())
        paragraph_buffer.clear()
        if text:
            _add_rich_paragraph(document.add_paragraph(), text, base_dir)

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        fence = _fence_open(line)
        if fence:
            flush_paragraph()
            marker, language = fence
            index += 1
            code: list[str] = []
            while index < len(lines) and not _closes_fence(lines[index], marker):
                code.append(lines[index])
                index += 1
            index += 1
            _add_code(document, "\n".join(code), language)
            continue
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        if stripped == "---":
            flush_paragraph()
            document.add_paragraph().add_run("—" * 16)
            index += 1
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            anchor = _HEADING_ANCHOR_RE.search(heading.group(2))
            title = _HEADING_ANCHOR_RE.sub("", heading.group(2)).strip()
            paragraph = document.add_heading(level=len(heading.group(1)))
            _add_rich_paragraph(paragraph, title or "未命名", base_dir)
            if anchor:
                _add_bookmark(paragraph, document.part._zc_bookmarks.get(anchor.group(1), ""))
            index += 1
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            quote = document.add_paragraph()
            quote.paragraph_format.left_indent = Inches(0.3)
            _add_rich_paragraph(quote, stripped.lstrip(">").strip(), base_dir)
            index += 1
            continue
        image = _IMAGE_RE.fullmatch(stripped)
        if image:
            flush_paragraph()
            _add_image(document, image.group(2), base_dir, image.group(1))
            index += 1
            continue
        if ("|" in stripped) and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            flush_paragraph()
            header_cells = _split_table_row(stripped)
            index += 2
            rows_data = [header_cells]
            while index < len(lines):
                next_line = lines[index].strip()
                if not next_line or not ("|" in next_line):
                    break
                rows_data.append(_split_table_row(next_line))
                index += 1
            _add_table(document, rows_data, base_dir)
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        number = re.match(r"^\d+\.\s+(.*)$", stripped)
        if bullet or number:
            flush_paragraph()
            style = "List Bullet" if bullet else "List Number"
            text = (bullet or number).group(1)
            _add_rich_paragraph(document.add_paragraph(style=style), text, base_dir)
            index += 1
            continue
        paragraph_buffer.append(stripped)
        index += 1
    flush_paragraph()


def _is_table_separator(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return len(cells) > 0 and all(re.match(r"^:?-+:?$", c) for c in cells if c)


def _split_table_row(line: str) -> list[str]:
    """按未转义、且不在行内代码里的竖线分列；单元格中的 \\| 还原为字面竖线。"""
    raw = line.strip()
    cells: list[str] = []
    current: list[str] = []
    index = 0
    code_ticks = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw) and raw[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if char == "`":
            run = len(raw[index:]) - len(raw[index:].lstrip("`"))
            if code_ticks == 0:
                code_ticks = run
            elif run == code_ticks:
                code_ticks = 0
            current.append("`" * run)
            index += run
            continue
        if char == "|" and code_ticks == 0:
            cells.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    trailing = "".join(current)
    if trailing.strip() or not raw.endswith("|") or raw.endswith("\\|"):
        cells.append(trailing)
    if raw.startswith("|") and cells:
        cells = cells[1:]
    return [cell.strip() for cell in cells]


def _set_cell_background(cell, fill_hex="F2F2F2"):
    try:
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls

        shading_elm = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
        cell._tc.get_or_add_tcPr().append(shading_elm)
    except Exception:
        pass


def _add_table(document: Document, rows_data: list[list[str]], base_dir: Path) -> None:
    if not rows_data:
        return
    num_cols = max(len(row) for row in rows_data)
    table = document.add_table(rows=len(rows_data), cols=num_cols)
    try:
        table.style = "Table Grid"
    except Exception:
        pass
    for row_idx, row in enumerate(rows_data):
        is_header = row_idx == 0
        for col_idx in range(num_cols):
            cell_text = row[col_idx] if col_idx < len(row) else ""
            cell = table.cell(row_idx, col_idx)
            cell.paragraphs[0].text = ""
            p = cell.paragraphs[0]
            _add_rich_paragraph(p, cell_text, base_dir)
            if is_header:
                _set_cell_background(cell, "F2F2F2")
                for run in p.runs:
                    run.bold = True


def _add_rich_paragraph(paragraph, text: str, base_dir: Path) -> None:
    # 先解析 Markdown：转义符只在普通文本中解码，代码中的反斜杠保留。
    # 不能先全局替换转义，否则字面星号会变成强调，路径及代码也会失真。
    fragment = BeautifulSoup(markdown.markdown(text), "html.parser")

    def append(node, *, bold=False, italic=False, code=False):
        if isinstance(node, NavigableString):
            run = paragraph.add_run(str(node))
            if bold:
                run.bold = True
            if italic:
                run.italic = True
            if code:
                run.font.name = "Consolas"
            return
        if node.name in {"script", "style"}:
            return
        if node.name == "img":
            _add_image_run(paragraph, node.get("src", ""), base_dir, node.get("alt", ""))
            return
        if node.name == "a":
            _add_hyperlink(paragraph, node.get("href", ""), node.get_text(), bold=bold, italic=italic)
            return
        if node.name == "br":
            paragraph.add_run().add_break()
            return
        for child in node.children:
            append(child, bold=bold or node.name in {"strong", "b"},
                   italic=italic or node.name in {"em", "i"}, code=code or node.name == "code")

    for node in fragment.children:
        if isinstance(node, NavigableString) and not node.strip():
            continue
        append(node)


def _add_code(document: Document, code: str, language: str) -> None:
    if language:
        label = document.add_paragraph()
        run = label.add_run(language)
        run.italic = True
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    run = paragraph.add_run(code or "")
    run.font.name = "Consolas"
    run.font.size = Pt(10)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")


def _add_image(document: Document, src: str, base_dir: Path, alt: str) -> None:
    path = _resolve_image(src, base_dir)
    if path is None:
        document.add_paragraph(alt or src)
        return
    document.add_picture(str(path), width=Inches(5.6))


def _add_image_run(paragraph, src: str, base_dir: Path, alt: str) -> None:
    path = _resolve_image(src, base_dir)
    if path is None:
        paragraph.add_run(alt or src)
        return
    run = paragraph.add_run()
    run.add_picture(str(path), width=Inches(5.6))


def _resolve_image(src: str, base_dir: Path) -> Path | None:
    if not src or src.startswith("http://") or src.startswith("https://"):
        return None
    candidate = (base_dir / src).resolve()
    if candidate.is_file():
        return candidate
    return None


def _add_hyperlink(paragraph, url: str, text: str, *, bold: bool = False, italic: bool = False) -> None:
    part = paragraph.part
    hyperlink = OxmlElement("w:hyperlink")
    if url.startswith("#"):
        # 文档内部跳转指向书签，不能登记成外部关系。
        bookmarks = getattr(part, "_zc_bookmarks", None) or {}
        hyperlink.set(qn("w:anchor"), bookmarks.get(url[1:]) or _bookmark_name(url[1:]))
    else:
        relationship = part.relate_to(
            url,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True,
        )
        hyperlink.set(qn("r:id"), relationship)
    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1772F6")
    run_properties.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_properties.append(underline)
    if bold:
        run_properties.append(OxmlElement("w:b"))
    if italic:
        run_properties.append(OxmlElement("w:i"))
    run.append(run_properties)
    text_element = OxmlElement("w:t")
    text_element.text = text
    run.append(text_element)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _fence_open(line: str) -> tuple[str, str] | None:
    if len(line) - len(line.lstrip(" ")) > 3:
        return None
    match = _FENCE_OPEN_RE.match(line.strip())
    if match is None:
        return None
    marker, info = match.group(1), match.group(2).strip()
    if marker.startswith("`") and "`" in info:
        return None
    return marker, (info.split()[0] if info else "")


def _closes_fence(line: str, marker: str) -> bool:
    stripped = line.strip()
    if not stripped or len(line) - len(line.lstrip(" ")) > 3:
        return False
    run = len(stripped) - len(stripped.lstrip(marker[0]))
    return run >= len(marker) and not stripped[run:].strip()


def _bookmark_name(anchor: str) -> str:
    name = _BOOKMARK_UNSAFE.sub("_", anchor or "")
    if not name or not name[0].isalpha():
        name = "a" + name
    return name[:40]


def _plan_bookmarks(lines: list[str]) -> dict[str, str]:
    """先收集全部标题锚点，目录链接和标题使用同一个合法且唯一的书签名。"""
    planned: dict[str, str] = {}
    used: set[str] = set()
    in_fence = ""
    for line in lines:
        if in_fence:
            if _closes_fence(line, in_fence):
                in_fence = ""
            continue
        fence = _fence_open(line)
        if fence:
            in_fence = fence[0]
            continue
        heading = re.match(r"^#{1,3}\s+(.*)$", line.strip())
        anchor = _HEADING_ANCHOR_RE.search(heading.group(1)) if heading else None
        if anchor is None or anchor.group(1) in planned:
            continue
        base = _bookmark_name(anchor.group(1))
        name = base
        number = 2
        while name.lower() in used:
            suffix = f"_{number}"
            name = base[: 40 - len(suffix)] + suffix
            number += 1
        used.add(name.lower())
        planned[anchor.group(1)] = name
    return planned


def _add_bookmark(paragraph, name: str) -> None:
    if not name:
        return
    part = paragraph.part
    bookmark_id = int(getattr(part, "_zc_bookmark_ids", 0))
    part._zc_bookmark_ids = bookmark_id + 1
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.insert(1 if paragraph._p.pPr is not None else 0, start)
    paragraph._p.append(end)


def _append_field(run, instruction: str) -> None:
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    middle = run._r.makeelement(qn("w:r"), {})
    middle.append(instr)
    run._r.addnext(middle)
    third = middle.makeelement(qn("w:r"), {})
    third.append(separate)
    middle.addnext(third)
    fourth = third.makeelement(qn("w:r"), {})
    fourth.append(end)
    third.addnext(fourth)


def _set_default_font(document: Document) -> None:
    style = document.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
