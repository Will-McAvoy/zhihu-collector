import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from pypdf import PdfReader, PdfWriter

from zhihu_collector.browser import owned_temp as temp


def test_runtime_cache_respects_override_portable_and_isolation(tmp_path, monkeypatch):
    import zhihu_collector.config as config
    monkeypatch.setattr(config, 'user_data_root', lambda: tmp_path / 'isolated')
    monkeypatch.delenv('ZHIHU_COLLECTOR_RUNTIME_DIR', raising=False)
    assert config.runtime_cache_dir() == tmp_path / 'isolated/data'
    assert config.runtime_cache_dir(tmp_path / 'browser-isolated') == tmp_path / 'browser-isolated'
    monkeypatch.setenv('ZHIHU_COLLECTOR_RUNTIME_DIR', str(tmp_path / 'scratch'))
    assert config.runtime_cache_dir() == tmp_path / 'scratch'
    monkeypatch.delenv('ZHIHU_COLLECTOR_RUNTIME_DIR')
    monkeypatch.setattr(config, 'user_data_root', config.app_dir)
    monkeypatch.setattr(config, 'is_portable_mode', lambda: True)
    assert config.runtime_cache_dir() == config.data_dir().resolve()


def test_runtime_cache_default_and_unwritable_fallback(tmp_path, monkeypatch):
    import zhihu_collector.config as config
    monkeypatch.delenv('ZHIHU_COLLECTOR_RUNTIME_DIR', raising=False)
    monkeypatch.setattr(config, 'user_data_root', lambda: tmp_path / 'app')
    monkeypatch.setattr(config, 'app_dir', lambda: tmp_path / 'app')
    monkeypatch.setattr(config, 'is_portable_mode', lambda: False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    assert config.runtime_cache_dir() == tmp_path / 'local/ZhihuCollector/runtime-cache'
    monkeypatch.setattr(config, '_is_directory_writable', lambda path: False)
    assert config.runtime_cache_dir() == tmp_path / 'app/data/runtime-cache'


def test_shared_worker_lease_survives_parent_release(tmp_path):
    work=temp.ManagedTemp(tmp_path/'cache','pdf')
    path=Path(work.name)
    (path/'payload').write_text('keep while child is active')
    ready=tmp_path/'ready';finish=tmp_path/'finish'
    code='''from pathlib import Path
import sys,time
from zhihu_collector.browser.owned_temp import attach
lease=attach(Path(sys.argv[1]),'pdf')
Path(sys.argv[2]).touch()
while not Path(sys.argv[3]).exists():time.sleep(.02)
lease.close()
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(path),str(ready),str(finish)])
    try:
        end=time.monotonic()+6
        while not ready.exists() and time.monotonic()<end:time.sleep(.02)
        assert ready.exists()
        work.cleanup();assert temp.wait_for_cleanup(3)
        assert path.is_dir() and not temp.cleanup_once(path.parent,path.name,'pdf')
    finally:
        finish.touch();child.wait(timeout=5)
    temp.sweep(path.parent,'pdf');assert temp.wait_for_cleanup(4)
    assert not path.exists()


def test_locked_cache_preserves_ownership_and_is_recovered_later(tmp_path):
    if os.name!='nt':pytest.skip('Windows file-sharing behavior')
    import ctypes
    from ctypes import wintypes
    work=temp.ManagedTemp(tmp_path/'cache','browser');path=Path(work.name)
    locked=path/'journal.baj';locked.write_bytes(b'cache')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(locked),0x80000000,0,None,3,0,None)
    assert handle!=ctypes.c_void_p(-1).value
    try:
        work.cleanup();assert temp.wait_for_cleanup(4)
        assert path.exists() and (path.parent/f'.{path.name}.owner.json').exists()
    finally:kernel.CloseHandle(handle)
    temp.sweep(path.parent,'browser');assert temp.wait_for_cleanup(4)
    assert not path.exists()


def test_cleanup_does_not_remove_unmarked_or_outside_directories(tmp_path):
    root=tmp_path/'cache';root.mkdir()
    innocent=root/'session-user';innocent.mkdir();(innocent/'keep').write_text('user')
    outside=tmp_path/'outside';outside.mkdir();(outside/'keep').write_text('user')
    assert not temp.cleanup_once(root,innocent.name,'browser')
    assert not temp.cleanup_once(root,'../outside','browser')
    temp.sweep(root,'browser');assert temp.wait_for_cleanup(3)
    assert (innocent/'keep').read_text()=='user' and (outside/'keep').read_text()=='user'


def test_new_tasks_can_scan_while_old_temporary_directories_are_deleted(tmp_path):
    root=tmp_path/'cache'
    for _ in range(100):
        work=temp.ManagedTemp(root,'browser')
        (Path(work.name)/'cache-data').write_bytes(b'cache')
        work.cleanup()
        temp.sweep(root,'browser')
    assert temp.wait_for_cleanup(10)
    temp.sweep(root,'browser');assert temp.wait_for_cleanup(5)
    assert not list(root.glob('session-*')) and not list(root.glob('.*.owner.json'))


def test_fragment_fingerprint_detects_image_edits_and_pdf_corruption(tmp_path):
    from zhihu_collector.exporters import pdf_cache as cache
    image=tmp_path/'picture.png';image.write_bytes(b'original image bytes')
    html='<html><img src="picture.png"></html>'
    first=cache.render_key(html,cache.asset_hashes(html,tmp_path),{'format':'A4'})
    stat=image.stat();image.write_bytes(b'modified image bytes');os.utime(image,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    second=cache.render_key(html,cache.asset_hashes(html,tmp_path),{'format':'A4'})
    assert first!=second
    pdf=tmp_path/'source.pdf'
    with PdfWriter() as writer:
        writer.add_blank_page(100,100);writer.write(pdf)
    cache.save(tmp_path/'cache',second,pdf)
    assert cache.restore(tmp_path/'cache',second,tmp_path/'copy.pdf')
    stored=tmp_path/'cache'/f'{second}.pdf'
    data=stored.read_bytes();stored.write_bytes(data[:40]+b'X'+data[41:])
    assert not cache.restore(tmp_path/'cache',second,tmp_path/'bad.pdf')
    assert not (tmp_path/'bad.pdf').exists()


def test_renderer_crash_retries_only_unfinished_fragments(tmp_path,monkeypatch):
    import zhihu_collector.config as config
    from zhihu_collector.exporters.markdown_exporter import render_merged_markdown
    from zhihu_collector.exporters.pdf_exporter import PdfExporter
    from zhihu_collector.exporters import pdf_process as module
    monkeypatch.setattr(config,'user_data_root',lambda:tmp_path/'runtime')
    code='''import sys,time,os
from pathlib import Path
import zhihu_collector.config as config
config.user_data_root=lambda:Path(sys.argv[1])/'runtime'
protocol=sys.stdout
sys.stdout=sys.stderr
from zhihu_collector.exporters.pdf_exporter import PdfExporter
from zhihu_collector.exporters.pdf_process import worker_main
sys.stdout=protocol
original=PdfExporter._print
count=0
def interrupted(page,options,before_print=None):
 global count
 count+=1
 if count==3:
  if before_print:before_print()
  os._exit(37)
 else:original(page,options,before_print)
PdfExporter._print=staticmethod(interrupted)
raise SystemExit(worker_main(sys.argv[1]))
'''
    real=module.subprocess.Popen
    def start(args,**kwargs):
        if '--pdf-worker' in args:args=[sys.executable,'-u','-c',code,args[-1]]
        return real(args,**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',start)
    stages=[]
    exporter=PdfExporter()
    def observe(stage,part,total):
        stages.append((stage,part))
    exporter.on_stage=observe
    markdown=render_merged_markdown(title='Recovery',author='QA',include_meta=True,
        pieces=[{'anchor':f'item-{i}','title':f'Answer {i}','body':f'Unique body {i}'} for i in range(1,4)])
    try:path=exporter.write(markdown,tmp_path/'recovered.pdf',title='Recovery')
    finally:exporter.close()
    reader=PdfReader(path)
    assert len(reader.pages)==4 and len(reader.named_destinations)==3
    assert all(f'Unique body {i}' in ''.join(p.extract_text() for p in reader.pages) for i in range(1,4))
    assert ('cached',1) in stages and ('cached',2) in stages
    diagnostics=[json.loads(line) for line in (tmp_path/'runtime/logs/pdf-events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(row['event']=='retry' and row['stage']=='print' and row['part']==3 for row in diagnostics)
    assert diagnostics[-1]['event']=='completed' and diagnostics[-1]['attempt']==2


def test_more_than_twenty_documents_recycles_page_without_browser_restart(tmp_path,monkeypatch):
    import zhihu_collector.config as config
    from zhihu_collector.exporters.pdf_exporter import PdfExporter
    monkeypatch.setattr(config,'user_data_root',lambda:tmp_path/'runtime')
    stages=[];pids=set()
    exporter=PdfExporter(on_stage=lambda stage,part,total:stages.append(stage))
    try:
        for index in range(21):
            path=exporter.write(f'# Page {index}\n\nUnique content {index}',tmp_path/f'{index}.pdf',title=f'Page {index}')
            pids.add(exporter._process.process.pid)
            assert f'Unique content {index}' in PdfReader(path).pages[0].extract_text()
    finally:exporter.close()
    assert len(pids)==1 and stages.count('starting')==1
    assert stages.count('recycle')==1


def test_browser_retains_descendants_before_closing_parent():
    from unittest.mock import MagicMock
    from zhihu_collector.browser.playwright_manager import BrowserSession
    calls=[];session=BrowserSession()
    owner=MagicMock();session._owner=owner
    session.browser=MagicMock();session.context=MagicMock()
    owner.capture.side_effect=lambda:calls.append('capture')
    session.browser.close.side_effect=lambda:calls.append('close')
    session.close()
    assert calls[:2]==['capture','close']


def test_pdf_pause_does_not_consume_idle_timeout(tmp_path,monkeypatch):
    import io
    import queue
    import threading
    from types import SimpleNamespace
    import zhihu_collector.config as config
    from zhihu_collector.collectors.base import TaskController
    from zhihu_collector.exporters.pdf_process import PdfProcess
    monkeypatch.setattr(config,'user_data_root',lambda:tmp_path/'runtime')
    controller=TaskController();controller.pause()
    renderer=PdfProcess(controller);starts=[]
    def start():
        starts.append(True)
        renderer.process=SimpleNamespace(pid=1,stdin=io.StringIO(),wait=lambda **kwargs:0)
        renderer.replies=queue.Queue();renderer.replies.put({'ok':True})
    monkeypatch.setattr(renderer,'_start',start)
    monkeypatch.setattr(renderer,'_stop',lambda:setattr(renderer,'process',None))
    monkeypatch.setattr(renderer,'IDLE_TIMEOUT',.1)
    timer=threading.Timer(.35,controller.resume);timer.start()
    try:renderer.write('# pause',tmp_path/'out.pdf','pause',False)
    finally:timer.join();renderer.close()
    assert len(starts)==1


def test_productive_large_pdf_is_not_cut_off_at_fifteen_minutes(tmp_path,monkeypatch):
    import io
    import queue
    from types import SimpleNamespace
    import zhihu_collector.config as config
    from zhihu_collector.exporters import pdf_process as module
    monkeypatch.setattr(config,'user_data_root',lambda:tmp_path/'runtime')
    ticks=[0]
    def now():ticks[0]+=1;return ticks[0]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=now))
    renderer=module.PdfProcess();starts=[]
    def start():
        starts.append(True)
        renderer.process=SimpleNamespace(pid=1,stdin=io.StringIO(),wait=lambda **kwargs:0)
        renderer.replies=queue.Queue()
        for index in range(1000):renderer.replies.put({'stage':'print','part':index,'total':1000})
        renderer.replies.put({'ok':True})
    monkeypatch.setattr(renderer,'_start',start)
    monkeypatch.setattr(renderer,'_stop',lambda:setattr(renderer,'process',None))
    try:renderer.write('# answer {#item-1}\n'*1000,tmp_path/'out.pdf','long',False)
    finally:renderer.close()
    assert len(starts)==1 and ticks[0]>900


@pytest.mark.parametrize('merged',[False,True])
def test_stopping_pdf_export_is_cancelled_not_a_format_failure(tmp_path,monkeypatch,merged):
    from zhihu_collector.collectors import service
    from zhihu_collector.collectors.offline import export_local_contents
    from zhihu_collector.config import Settings
    from zhihu_collector.database.database import Database
    from zhihu_collector.errors import TaskStopped
    from zhihu_collector.models import ContentRecord
    def stopped(*args,**kwargs):raise TaskStopped()
    monkeypatch.setattr(service,'_write_export_file',stopped)
    db=Database(tmp_path/'sandbox.db')
    try:
        _,cid=db.save_content(ContentRecord('31','answer','https://www.zhihu.com/question/9/answer/31',html_content='<p>Body</p>'),
                              markdown='Body',html='<p>Body</p>')
        request=service.CollectRequest(url='offline',export_markdown=False,export_docx=False,export_pdf=True,
                                      export_separate=not merged,export_merged=merged)
        result=export_local_contents(db,[cid],tmp_path/'output',request,Settings())
        assert result.status=='cancelled' and result.failed==0
        assert not db.list_failures()
    finally:db.close()
