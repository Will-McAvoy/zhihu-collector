import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from zhihu_collector.browser.guarded import bounded, BrowserOperationTimeout
from zhihu_collector.browser.network_listener import NetworkListener
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.config import Settings
from zhihu_collector.errors import TaskStopped


def response(url='https://www.zhihu.com/api/v4/collections/12/items', status=200):
    return SimpleNamespace(url=url, status=status, headers={'content-type':'application/json'},
        request=object(), json=MagicMock(return_value={'data':[], 'paging':{'is_end':True}}))


def test_headers_and_finished_callbacks_never_read_body():
    listener=NetworkListener()
    row=response()
    listener._on_response(row)
    assert listener.snapshot()==[]
    row.json.assert_not_called()
    listener._on_finished(row.request)
    row.json.assert_not_called()
    assert len(listener.consume())==1
    row.json.assert_called_once()


def test_error_headers_still_raise_login_even_when_body_never_finishes():
    from zhihu_collector.browser.page_guard import inspect_page
    from zhihu_collector.errors import LoginExpiredError
    listener=NetworkListener()
    row=response(status=401)
    listener._on_response(row)
    with pytest.raises(LoginExpiredError):
        inspect_page(SimpleNamespace(url='https://www.zhihu.com/collection/12'),found_content=2,
                     network_errors=listener.consume_errors())
    row.json.assert_not_called()


def test_drain_does_not_recursively_parse_callbacks_from_another_request():
    listener=NetworkListener()
    a,b=response(),response()
    def read_a():
        listener._on_response(b)
        listener._on_finished(b.request)
        b.json.assert_not_called()
        return {'data':[]}
    a.json.side_effect=read_a
    listener._on_response(a);listener._on_finished(a.request)
    assert len(listener.consume())==1
    b.json.assert_not_called()
    assert len(listener.consume())==1
    b.json.assert_called_once()


def test_unrelated_metadata_does_not_block_capture():
    listener=NetworkListener()
    row=response('https://www.zhihu.com/api/v4/answers/31/relationship')
    listener._on_response(row);listener._on_finished(row.request)
    assert listener.consume()==[]
    row.json.assert_not_called()


def test_next_button_required_for_new_collection_page():
    from zhihu_collector.collectors.collection_answers import _advance_page
    page=MagicMock()
    page.locator.return_value.count.return_value=1
    button=page.locator.return_value.first
    button.is_visible.return_value=True
    button.is_enabled.return_value=True
    listener=NetworkListener()
    row=response()
    button.click.side_effect=lambda **kw:(listener._on_response(row),listener._on_finished(row.request))
    _advance_page(page,listener,TaskController(),Settings())
    button.click.assert_called_once()
    page.evaluate.assert_not_called()
    assert listener.snapshot()[0].data['paging']['is_end'] is True


@pytest.mark.parametrize('stop',[False,True])
def test_watchdog_terminates_only_owner_and_preserves_cancellation(stop):
    event=threading.Event()
    controller=TaskController()
    owner=SimpleNamespace(terminate=MagicMock(side_effect=event.set))
    session=SimpleNamespace(controller=controller,_owner=owner)
    if stop:
        threading.Timer(.05,controller.stop).start()
    started=time.monotonic()
    with pytest.raises(TaskStopped if stop else BrowserOperationTimeout):
        bounded(session,'blocked test operation',lambda:event.wait(3),.15)
    assert time.monotonic()-started < 1
    owner.terminate.assert_called_once()


def test_page_recreated_after_pdf_failure(tmp_path):
    from zhihu_collector.exporters.pdf_exporter import PdfExporter
    session=MagicMock()
    first,second=MagicMock(),MagicMock()
    first.goto.side_effect=PlaywrightTimeout('render timed out')
    session.new_page.side_effect=[first,second]
    PdfExporter(session).write('# 可恢复的内容',tmp_path/'a.pdf',title='测试')
    first.close.assert_called_once()
    second.pdf.assert_called_once()
    assert second.goto.call_args.kwargs['wait_until']=='domcontentloaded'


def test_progress_not_complete_while_more_answers_expected():
    from zhihu_collector.collectors.service import CollectionService
    service=CollectionService(Settings(),MagicMock(),TaskController())
    service._new_saved=34
    service.progress.declared_total=91
    service._apply_percent(34,34,34)
    assert 37 < service.progress.percent < 38
    service.progress.integrity='complete'
    service._apply_percent(91,91,91)
    assert service.progress.percent==100


def test_real_browser_stuck_rpc_is_bounded(tmp_path,monkeypatch):
    from zhihu_collector.browser import playwright_manager as module
    monkeypatch.setattr(module,'data_dir',lambda:tmp_path)
    started=time.monotonic()
    with module.BrowserSession() as session:
        page=session.new_page()
        with pytest.raises(BrowserOperationTimeout):
            session.run_guarded('永不结束的页面脚本',lambda:page.evaluate('() => new Promise(() => {})'),.5)
    assert time.monotonic()-started < 15


def test_dead_dispatcher_never_spins_waiting_for_unstarted_task():
    import asyncio
    from playwright.sync_api import Error
    from zhihu_collector.browser.sync_guard import safe_sync
    loop=asyncio.new_event_loop()
    fake=SimpleNamespace(_loop=loop,_dispatcher_fiber=SimpleNamespace(dead=True))
    try:
        with pytest.raises(Error,match='调度已退出'):
            safe_sync(fake,asyncio.sleep(100))
    finally:
        loop.close()


def test_detail_tab_is_activated_before_navigation(monkeypatch):
    from zhihu_collector.collectors import service as module
    from zhihu_collector.models import ContentRecord
    service=module.CollectionService(Settings(),MagicMock(),TaskController())
    page=MagicMock()
    service._detail_page=page
    service._detail_listener=MagicMock()
    original=ContentRecord('31','answer','https://www.zhihu.com/question/9/answer/31',content_incomplete=True)
    complete=ContentRecord('31','answer',original.url,html_content='<p>完整正文</p>')
    monkeypatch.setattr(service,'_read_answer_html',lambda *args:'<html>ready</html>')
    monkeypatch.setattr(module,'records_from_page_sources',lambda **kwargs:[complete])
    service._enrich(MagicMock(),original)
    names=[c[0] for c in page.mock_calls]
    assert names.index('bring_to_front')<names.index('goto')
    assert not original.content_incomplete


def test_retired_session_does_not_cascade_failures_to_later_records(tmp_path,monkeypatch):
    from zhihu_collector.collectors import service as module
    from zhihu_collector.database.database import Database
    from zhihu_collector.errors import CollectorError
    from zhihu_collector.models import ContentRecord
    browser=MagicMock();browser.__enter__.return_value=browser
    monkeypatch.setattr(module,'BrowserSession',lambda **kwargs:browser)
    rows=[ContentRecord(str(i),'answer',f'https://www.zhihu.com/question/9/answer/{i}',content_incomplete=True) for i in (31,32)]
    monkeypatch.setattr(module,'collect_listing',lambda *args,**kwargs:kwargs['on_page'](rows,2,2))
    enrich=MagicMock(side_effect=BrowserOperationTimeout('会话已超时回收'))
    monkeypatch.setattr(module.CollectionService,'_enrich',enrich)
    db=Database(tmp_path/'sandbox.db')
    try:
        service=module.CollectionService(Settings(),db,TaskController())
        with pytest.raises(CollectorError,match='会话已超时'):
            service.run(module.CollectRequest(url='https://www.zhihu.com/people/sample',mode='answers',
                execution_intent='library',download_images=False))
        assert enrich.call_count==1
        assert [x['zhihu_content_id'] for x in db.list_failures() if x['kind']=='body']==['31']
    finally:
        db.close()


def test_print_images_bound_decode_size_without_changing_original(tmp_path):
    import hashlib
    from bs4 import BeautifulSoup
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage
    from zhihu_collector.exporters.pdf_exporter import _print_sized_images
    original=tmp_path/'高清图.png'
    image=QImage(3200,4800,QImage.Format.Format_RGB32)
    image.fill(0xff229955)
    assert image.save(str(original))
    digest=hashlib.sha256(original.read_bytes()).hexdigest()
    work=tmp_path/'work';work.mkdir()
    rendered=_print_sized_images('<img src="高清图.png"><img src="高清图.png">',tmp_path,work)
    images=BeautifulSoup(rendered,'html.parser').find_all('img')
    assert images[0]['src']==images[1]['src']
    output=QImage(QUrl(images[0]['src']).toLocalFile())
    assert output.width()==1600 and output.height()==2400
    assert hashlib.sha256(original.read_bytes()).hexdigest()==digest


def test_cancelled_session_reclaims_owner_without_waiting_for_tabs():
    from zhihu_collector.browser.playwright_manager import BrowserSession
    controller=TaskController();controller.stop()
    session=BrowserSession(controller=controller)
    context,browser=MagicMock(),MagicMock()
    session.context,session.browser=context,browser
    session._owner=MagicMock()
    owner=session._owner
    session.close()
    context.close.assert_not_called()
    browser.close.assert_not_called()
    owner.terminate.assert_called()
    owner.release.assert_called_once()


def test_real_merged_pdf_preserves_all_answers_and_toc_destinations(tmp_path,monkeypatch):
    import zhihu_collector.config as config
    from zhihu_collector.exporters.markdown_exporter import render_merged_markdown
    from zhihu_collector.exporters.pdf_exporter import PdfExporter
    from pypdf import PdfReader
    monkeypatch.setattr(config,'user_data_root',lambda:tmp_path/'runtime')
    pieces=[{'anchor':f'item-{i}','title':f'Answer {i}','body':f'Unique body {i}.'} for i in range(1,4)]
    text=render_merged_markdown(title='Merged test',author='test',pieces=pieces,include_meta=True)
    progress=[]
    pdf=PdfExporter(on_progress=lambda done,total:progress.append((done,total)))
    try:
        path=pdf.write(text,tmp_path/'merged.pdf',title='Merged test')
    finally:
        pdf.close()
    reader=PdfReader(path)
    assert len(reader.pages)==4
    text='\n'.join(page.extract_text() for page in reader.pages)
    assert all(f'Unique body {i}.' in text for i in range(1,4))
    assert set(reader.named_destinations)>={'item-1','item-2','item-3'}
    links=[ref.get_object() for page in reader.pages for ref in page.get('/Annots',[])]
    assert {str(a.get('/Dest')) for a in links}>={'item-1','item-2','item-3'}
    assert reader.outline
    assert progress==[(i,4) for i in range(1,5)]


@pytest.mark.parametrize('kind',['export','retry'])
def test_parent_close_keeps_database_open_until_child_worker_ends(tmp_path,monkeypatch,kind):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
    import zhihu_collector.config as config
    from zhihu_collector.gui import main_window as module
    from zhihu_collector.gui.library_window import OfflineExportDialog
    from zhihu_collector.gui.failures_window import FailuresWindow
    monkeypatch.setattr(config,'user_data_root',lambda:tmp_path/'runtime')
    monkeypatch.setattr(module,'load_settings',lambda:Settings(database_path=str(tmp_path/'sandbox.db')))
    app=QApplication.instance() or QApplication([])
    window=module.MainWindow()
    child=(OfflineExportDialog(window.database,window.settings,[],parent=window) if kind=='export'
           else FailuresWindow(window.database,window.settings,parent=window))
    finish=threading.Event()
    class Working(QThread):
        def run(self):
            finish.wait(3)
    worker=Working();child.worker=worker
    window.show();child.show();worker.start();app.processEvents()
    try:
        window.close()
        assert child.controller.stopped
        assert window.isVisible() and worker.isRunning()
        assert window.database._conn.execute('select 1').fetchone()[0]==1
    finally:
        finish.set();worker.wait(2000)
        time.sleep(.12);app.processEvents()
        child.close();window.close();app.processEvents()
    assert not window.isVisible()
