"""Private render fragments, fingerprinted by HTML, settings and image bytes."""
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import unquote
from uuid import uuid4

from bs4 import BeautifulSoup
from PySide6.QtCore import QUrl
from zhihu_collector.utils.artifacts import artifact_problem


def digest(path: Path) -> str:
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def asset_hashes(html: str, base: Path) -> dict[str, str]:
    result = {}
    for image in BeautifulSoup(html, 'html.parser').find_all('img', src=True):
        src = image['src']
        if src.startswith(('http:', 'https:', 'data:')) or src in result:
            continue
        path = Path(QUrl(src).toLocalFile()) if src.startswith('file:') else (base/unquote(src)).resolve()
        result[src] = digest(path) if path.is_file() else 'missing'
    return result


def render_key(html: str, assets: dict[str, str], options: dict) -> str:
    used = {image['src'] for image in BeautifulSoup(html, 'html.parser').find_all('img', src=True)}
    options = {k:v for k,v in options.items() if k != 'path'}
    payload = json.dumps({'renderer':2, 'html':html, 'assets':{k:v for k,v in assets.items() if k in used},
                          'options':options}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def restore(cache: Path, key: str, target: Path) -> bool:
    path, meta = cache/f'{key}.pdf', cache/f'{key}.json'
    try:
        if meta.stat().st_size > 512 or artifact_problem(path, 'pdf'):
            return False
        record = json.loads(meta.read_text(encoding='utf-8'))
        if record.get('key') != key or record.get('sha256') != digest(path):
            return False
        shutil.copyfile(path, target)
        return True
    except (OSError, ValueError):
        return False


def save(cache: Path, key: str, source: Path):
    cache.mkdir(parents=True, exist_ok=True)
    if artifact_problem(source, 'pdf'):
        raise ValueError('拒绝缓存不完整的 PDF 页面')
    temporary = cache/f'{uuid4().hex}.partial'
    metadata = temporary.with_suffix('.metadata')
    try:
        shutil.copyfile(source, temporary)
        metadata.write_text(json.dumps({'key':key,'sha256':digest(temporary)}), encoding='utf-8')
        temporary.replace(cache/f'{key}.pdf')
        metadata.replace(cache/f'{key}.json')
    finally:
        temporary.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
