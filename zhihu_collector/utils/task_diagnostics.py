"""Small structured records; never include document bodies or authentication state."""
import json
import logging
from pathlib import Path
import threading
import time

_lock = threading.Lock()
log = logging.getLogger('zhihu_collector.pdf')


def pdf_event(event: str, **fields):
    from zhihu_collector.config import log_dir
    record = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'event': event, **fields}
    with _lock:
        try:
            folder = log_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / 'pdf-events.jsonl'
            if path.exists() and path.stat().st_size > 4_000_000:
                path.replace(folder / 'pdf-events.previous.jsonl')
            with path.open('a', encoding='utf-8') as file:
                file.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        except OSError:
            log.debug('PDF 诊断记录写入失败', exc_info=True)
    if event in {'retry', 'failed'}:
        log.warning('PDF %s：阶段=%s，尝试=%s，耗时=%s 秒，原因=%s',
                    event, fields.get('stage'), fields.get('attempt'), fields.get('elapsed'), fields.get('error'))


def pdf_stage_label(stage: str, done: int = 0, total: int = 0) -> str:
    label = {'starting': '启动排版', 'prepare': '准备图文', 'load': '加载本地图文',
             'assets': '等待字体和图片', 'print': '打印页面', 'merge': '合并页面',
             'write': '写入文档', 'cached': '复用已验证页面', 'recycle': '释放页面资源'}.get(stage, stage)
    return f'{label}（{done}/{total}）' if total else label
