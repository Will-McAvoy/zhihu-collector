"""用无头 Chromium 把 Markdown 转成的 HTML 打印成 PDF，并保留标题大纲。"""

from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote

import markdown
from playwright.sync_api import Error as PlaywrightError
from zhihu_collector.browser.guarded import BrowserOperationTimeout

from zhihu_collector.exporters.markdown_exporter import split_frontmatter

_CSS = """
body { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; line-height: 1.65; color: #222; overflow-wrap: anywhere; }
h1, h2, h3 { line-height: 1.3; }
img { max-width: 100%; max-height: 650px; object-fit: contain; page-break-inside: avoid; break-inside: avoid; }
pre { background: #f4f4f4; padding: 10px; white-space: pre-wrap; }
blockquote { border-left: 3px solid #ccc; margin-left: 0; padding-left: 12px; color: #444; }
article { break-before: page; }
article:first-of-type { break-before: auto; }
a { color: #1772f6; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; }
th, td { border: 1px solid #d3d3d3; padding: 8px 12px; text-align: left; }
th { background-color: #f7f7f7; font-weight: bold; }
tr:nth-child(even) { background-color: #fafafa; }
.meta-info { font-size: 12px; color: #8590a6; margin-top: 4px; margin-bottom: 16px; line-height: 1.5; }
.meta-info span { margin-right: 14px; display: inline-block; }
"""


def _inline_local_images(html_text: str, base_dir: Path) -> str:
    def replace_src(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith("http://") or src.startswith("https://") or src.startswith("data:"):
            return match.group(0)
        try:
            local_path = (base_dir / src).resolve()
            if local_path.is_file():
                mime, _ = mimetypes.guess_type(str(local_path))
                if not mime:
                    mime = "image/jpeg"
                data_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")
                return f'src="data:{mime};base64,{data_b64}"'
        except Exception:
            pass
        return match.group(0)

    return re.sub(r'src="([^"]+)"', replace_src, html_text)


def _extract_frontmatter(markdown_text: str, present: bool | None = None) -> tuple[dict[str, str], str]:
    # 只剥离确实存在且完整的元数据头；没有闭合或不是程序字段时保留原文。
    return split_frontmatter(markdown_text, present=present)


_TABLE_CELL_CODE = re.compile(r"(<t[dh]\b[^>]*>)(.*?)(</t[dh]>)", re.DOTALL)


def _unescape_table_code_pipes(body: str) -> str:
    """GFM 要求表格行内代码里的竖线也写成 \\|；Python-Markdown 会原样保留反斜杠。"""
    def fix_cell(match: re.Match) -> str:
        inner = re.sub(
            r"(<code>)(.*?)(</code>)",
            lambda code: code.group(1) + code.group(2).replace("\\|", "|") + code.group(3),
            match.group(2),
            flags=re.DOTALL,
        )
        return match.group(1) + inner + match.group(3)

    return _TABLE_CELL_CODE.sub(fix_cell, body)


def _format_meta_html(meta: dict[str, str]) -> str:
    if not meta:
        return ""
    items = []
    if "created_at" in meta:
        items.append(f"<span>发布时间：{html.escape(meta['created_at'])}</span>")
    if "updated_at" in meta and meta["updated_at"] != meta.get("created_at"):
        items.append(f"<span>更新时间：{html.escape(meta['updated_at'])}</span>")
    if "vote_count" in meta:
        items.append(f"<span>赞同数：{html.escape(meta['vote_count'])}</span>")
    if "comment_count" in meta:
        items.append(f"<span>评论数：{html.escape(meta['comment_count'])}</span>")
    if "exported_at" in meta:
        items.append(f"<span>导出时间：{html.escape(meta['exported_at'])}</span>")
    if "count" in meta:
        items.append(f"<span>收录篇数：{html.escape(meta['count'])} 篇</span>")
    if not items:
        return ""
    return f'<div class="meta-info">{" ".join(items)}</div>'


def markdown_to_html(markdown_text: str, *, title: str, base_dir: Path, frontmatter: bool | None = None, inline_images: bool = True) -> str:
    meta, body_text = _extract_frontmatter(markdown_text, frontmatter)
    body = markdown.markdown(
        body_text,
        extensions=["extra", "sane_lists", "tables", "fenced_code", "toc"],
    )
    body = _unescape_table_code_pipes(body)
    meta_html = _format_meta_html(meta)
    if meta_html:
        if "</h1>" in body:
            body = body.replace("</h1>", f"</h1>\n{meta_html}", 1)
        else:
            body = meta_html + "\n" + body
    base = base_dir.resolve().as_uri() + "/"
    rendered = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<base href="{html.escape(base)}">
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""
    return _inline_local_images(rendered, base_dir) if inline_images else rendered


class PdfExporter:
    def __init__(self, session=None, *, controller=None, on_progress=None, on_stage=None, cache_dir=None):
        self.session = session
        self._owned_session = None
        self._page = None
        self.controller = controller
        self.on_progress = on_progress
        self.on_stage = on_stage
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._render_count = 0
        self._process = None

    def write(self, markdown_text: str, destination: Path, *, title: str, frontmatter: bool | None = None) -> Path:
        if self.session is None:
            from zhihu_collector.exporters.pdf_process import PdfProcess
            if self._process is None:
                self._process = PdfProcess(self.controller, self.on_progress, self.on_stage)
            return self._process.write(markdown_text, destination, title, frontmatter)
        destination.parent.mkdir(parents=True, exist_ok=True)
        html_text = markdown_to_html(markdown_text, title=title, base_dir=destination.parent,
                                     frontmatter=frontmatter, inline_images=False)
        from zhihu_collector.exporters import pdf_cache
        source_parts = _merged_print_parts(html_text)
        assets = pdf_cache.asset_hashes(html_text, destination.parent) if self.cache_dir is not None else {}
        pdf_kwargs = {
            "path": str(destination),
            "format": "A4",
            "print_background": True,
            "outline": True,
            "margin": {"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
        }
        for attempt in range(2):
            page = self._ensure_page()
            try:
                # A local document can read local images directly. Avoid base64 copies
                # of every image and a huge HTML string through the browser RPC pipe.
                from zhihu_collector.browser.owned_temp import ManagedTemp
                from zhihu_collector.config import runtime_cache_dir
                root = self.cache_dir.parent/'print-work' if self.cache_dir is not None else runtime_cache_dir()/'pdf-print'
                work = ManagedTemp(root, 'print')
                try:
                    directory = Path(work.name)
                    self._stage('prepare', 0, len(source_parts))
                    printable = _print_sized_images(html_text, destination.parent, directory)
                    parts = _merged_print_parts(printable)
                    paths = []
                    for index, (_, text) in enumerate(parts):
                        document = directory / f"print-{index}.html"
                        document.write_text(text, encoding="utf-8")
                        target = directory / f"part-{index}.pdf" if len(parts) > 1 else destination
                        key = pdf_cache.render_key(source_parts[index][1], assets, pdf_kwargs)
                        cached = self.cache_dir is not None and pdf_cache.restore(self.cache_dir, key, target)
                        if cached:
                            self._stage('cached', index+1, len(parts))
                        else:
                            if self._render_count >= 20:
                                self._stage('recycle', index+1, len(parts))
                                replacement = self.session.new_page()
                                self.session.run_guarded('释放 PDF 页面', page.close, 5)
                                self._page = replacement
                                self._render_count = 0
                                page = replacement
                            self._stage('load', index+1, len(parts))
                            page.goto(document.as_uri(), wait_until="domcontentloaded", timeout=30000)
                            self._stage('assets', index+1, len(parts))
                            self._print(page, dict(pdf_kwargs, path=str(target)),
                                        before_print=lambda:self._stage('print',index+1,len(parts)))
                            if self.cache_dir is not None:
                                pdf_cache.save(self.cache_dir, key, target)
                            # Leave the document before asynchronous temp cleanup.
                            page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
                            self._render_count += 1
                        from bs4 import BeautifulSoup
                        heading = BeautifulSoup(text, "html.parser").find("h1")
                        label = heading.get_text(" ", strip=True) if heading else title
                        paths.append((parts[index][0], target, label))
                        if self.on_progress:
                            self.on_progress(index + 1, len(parts))
                    if len(parts) > 1:
                        _join_print_parts(paths, destination, on_stage=self._stage)
                finally:
                    work.cleanup()
                break
            except (PlaywrightError, TimeoutError):
                self._page = None
                try:
                    page.close()
                except Exception:
                    pass
                if attempt:
                    raise
        return destination

    @staticmethod
    def _print(page, pdf_kwargs, before_print=None):
        # Wait only for print assets, not every network activity in this context.
        page.evaluate("""async () => {
            const ready = Promise.all([
                document.fonts.ready,
                ...Array.from(document.images, img => img.decode().catch(() => {}))
            ]);
            await Promise.race([ready, new Promise((_, reject) =>
                setTimeout(() => reject(new Error('PDF_ASSET_TIMEOUT')), 8000))]);
        }""")
        if before_print:
            before_print()
        try:
            page.pdf(**pdf_kwargs)
        except TypeError:
            pdf_kwargs.pop("outline", None)
            page.pdf(**pdf_kwargs)

    def close(self) -> None:
        if self._process is not None:
            self._process.close()
            self._process = None
        if self._owned_session is not None:
            self._owned_session.close()
            self._owned_session = None
        self._page = None

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        if self.session is None:
            from zhihu_collector.browser.playwright_manager import BrowserSession

            self._owned_session = BrowserSession(headless=True)
            self._owned_session.start()
            self.session = self._owned_session
        self._page = self.session.new_page()
        return self._page

    def _stage(self, stage, part=0, total=0):
        if self.on_stage:
            self.on_stage(stage,part,total)


def _print_sized_images(html_text: str, base_dir: Path, work: Path) -> str:
    """Bound raster decode memory for large merged PDFs; originals stay untouched."""
    from bs4 import BeautifulSoup
    from PySide6.QtCore import QSize, Qt, QUrl
    from PySide6.QtGui import QImageReader
    soup = BeautifulSoup(html_text, "html.parser")
    cache = {}
    for image in soup.find_all("img", src=True):
        src = image["src"]
        if src.startswith(("http://", "https://")):
            image.replace_with(soup.new_string("[图片未下载，请通过原文链接查看]"))
            continue
        if src.startswith("data:"):
            continue
        path = Path(QUrl(src).toLocalFile()) if src.startswith("file:") else (base_dir / unquote(src)).resolve()
        key = str(path)
        if key in cache:
            image["src"] = cache[key]
            continue
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        if bytes(reader.format()) in {b"svg", b"svgz"} or (size.width() <= 1600 and size.height() <= 2400):
            image["src"] = path.as_uri()
            continue
        reader.setScaledSize(size.scaled(QSize(1600, 2400), Qt.AspectRatioMode.KeepAspectRatio))
        scaled = reader.read()
        if scaled.isNull():
            raise RuntimeError(f"PDF 无法读取本地图片：{path.name}")
        suffix = ".png" if scaled.hasAlphaChannel() else ".jpg"
        target = work / f"image-{len(cache)}{suffix}"
        if not scaled.save(str(target), "PNG" if suffix == ".png" else "JPG", 90):
            raise RuntimeError("PDF 临时图片写入失败")
        cache[key] = image["src"] = target.as_uri()
    return str(soup)


def _merged_print_parts(html_text: str) -> list[tuple[str, str]]:
    """Split only our merged-answer anchors, preserving the cover and TOC."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    body = soup.body
    if body is None:
        return [("", html_text)]
    anchors = {link.get("href", "")[1:] for link in body.select('a[href^="#item-"]')}
    headings = [tag for tag in body.find_all("h1", recursive=False)
                if tag.get("id") in anchors and re.fullmatch(r"item-\d+", tag.get("id", ""))]
    if len(headings) < 2:
        return [("", html_text)]
    # Chromium omits links whose anchors are absent from this print segment.
    # A reserved URI keeps the link rectangles; joining replaces it with a PDF
    # destination. These links are never requested by the renderer.
    for link in body.select('a[href^="#item-"]'):
        link["href"] = "https://zhihu-collector.invalid/print-anchor" + link["href"]
    marker_ids = {id(tag): tag["id"] for tag in headings}
    groups = []
    anchor = ""
    nodes = []
    for node in list(body.contents):
        if id(node) in marker_ids:
            groups.append((anchor, "".join(nodes)))
            anchor, nodes = marker_ids[id(node)], []
        nodes.append(str(node))
    groups.append((anchor, "".join(nodes)))
    body.clear()
    template = str(soup)
    return [(anchor, template.replace("<body></body>", f"<body>{nodes}</body>")) for anchor, nodes in groups]


def _join_print_parts(parts: list[tuple[str, Path, str]], destination: Path, on_stage=None) -> None:
    """Join bounded print jobs and restore links from the TOC to each answer."""
    from urllib.parse import urlsplit
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, TextStringObject
    with PdfWriter() as writer:
        anchors = {}
        for anchor, path, title in parts:
            if on_stage:
                on_stage('merge',len(anchors),len(parts))
            start = len(writer.pages)
            writer.append(PdfReader(path), outline_item=title, import_outline=True)
            if anchor:
                writer.add_named_destination(anchor, start)
                anchors[anchor] = start
        for page in writer.pages:
            for ref in page.get("/Annots", []):
                annotation = ref.get_object()
                action = annotation.get("/A")
                if not action:
                    continue
                action = action.get_object()
                target = urlsplit(str(action.get("/URI", "")))
                internal = (target.scheme == "file" or
                            (target.hostname == "zhihu-collector.invalid" and target.path == "/print-anchor"))
                if internal and target.fragment in anchors:
                    annotation.pop("/A", None)
                    annotation[NameObject("/Dest")] = TextStringObject(target.fragment)
        if on_stage:
            on_stage('write',len(parts),len(parts))
        writer.write(destination)
