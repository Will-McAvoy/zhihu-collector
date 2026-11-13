from pathlib import Path
from unittest.mock import MagicMock
import pytest
import zhihu_collector.collectors.service as svc
from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.repairs import retry_saved_failures
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.models import ContentRecord,AuthorInfo
from zhihu_collector.utils.urls import parse_zhihu_url
URL='https://www.zhihu.com/people/audit'
def rec(i,title=None):
    return ContentRecord(zhihu_content_id=str(i),content_type='answer',url=f'https://www.zhihu.com/question/100/answer/{i}',title=title or f'Title{i}',question_id='100',question_title=title or f'Title{i}',html_content=f'<p>BODYMARKER{i}</p>',author=AuthorInfo(name='Audit',url_token='audit',url=URL))
@pytest.fixture
def env(tmp_path):
    db=Database(tmp_path/'audit.db');s=svc.CollectionService(Settings(),db,TaskController())
    yield s,db,tmp_path
    db.close()
def request(root,**kw):
    return svc.CollectRequest(url=URL,mode='answers',output_dir=str(root/'out'),download_images=False,export_docx=False,export_pdf=False,**kw)
def save(db,i,title=None):
    r=rec(i,title);return db.save_content(r,markdown=f'BODYMARKER{i}',html=r.html_content)[1]
def fail(*a,**kw):raise OSError('injected write failure')

@pytest.mark.parametrize('html',[
    '''<div class="AnswerItem" data-zop='{"itemId":123}'><div class="RichText">WRONGANSWER123</div></div>''',
    '''<div class="AnswerItem"><a href="/question/100/answer/123">link</a><div class="RichText">WRONGANSWER123</div></div>'''
])
def test_single_answer_rejects_prefix_identity(env,monkeypatch,html):
    s,db,p=env
    session=MagicMock();session.__enter__.return_value=session
    page=session.new_page.return_value;page.content.return_value=html;page.url=rec(12).url
    monkeypatch.setattr(svc,'BrowserSession',lambda **kw:session)
    # 让真实解析器解析：JSON为空，DOM可识别时只会得到123，不应构造目标12。
    r=request(p);r.url=rec(12).url;r.mode='single_answer'
    try:s.run(r)
    except svc.CollectorError:pass
    assert not db.list_contents(),'目标12不存在，123的正文却被存为12'

def test_old_merged_failure_cannot_overwrite_new_success(env,monkeypatch):
    s,db,p=env;a=save(db,1);b=save(db,2);c=save(db,3)
    r=request(p,export_separate=False,export_merged=True)
    with monkeypatch.context() as m:
        m.setattr(svc,'_write_export_file',fail)
        assert export_local_contents(db,[a,b],p/'out',r,Settings()).failed
    assert export_local_contents(db,[c],p/'out',r,Settings()).status=='success'
    target=p/'out'/'所选内容.md';before=target.read_text(encoding='utf-8')
    assert 'BODYMARKER3' in before and 'BODYMARKER1' not in before
    retry_saved_failures(db,Settings(),p/'out')
    after=target.read_text(encoding='utf-8')
    assert after==before,'旧失败任务把后一次成功导出的C覆盖为A+B'

def test_explicit_separate_type_overrides_filename_guessing(env,monkeypatch):
    s,db,p=env;a=save(db,1,'全部回答');save(db,2)
    r=request(p)
    with monkeypatch.context() as m:
        m.setattr(svc,'_write_export_file',fail)
        svc.write_separate_exports(db.get_content(a),[],p/'out',r,Settings(),None,db)
    item=db.list_failures()[0]
    assert item['export_type']=='separate'
    target=Path(item['target'])
    retry_saved_failures(db,Settings(),p/'out')
    text=target.read_text(encoding='utf-8')
    assert 'BODYMARKER1' in text and 'BODYMARKER2' not in text,'单篇失败重试按文件名扩大为作者全量'

def test_failed_enrichment_has_recoverable_item(env,monkeypatch):
    s,db,p=env;r=rec(1);r.content_incomplete=True
    session=MagicMock();session.__enter__.return_value=session
    session.new_page.return_value.content.return_value='<html><body>No target answer</body></html>'
    monkeypatch.setattr(svc,'BrowserSession',lambda **kw:session)
    monkeypatch.setattr(svc,'records_from_page_sources',lambda **kw:[])
    monkeypatch.setattr(svc,'collect_listing',lambda *a,**kw:kw['on_page']([r],1,1))
    s.run(request(p))
    assert db.latest_task(URL)['error_message']
    assert db.list_failures(),'任务报正文失败，但失败项没有记录，无法按失败回答恢复'

@pytest.mark.parametrize('metadata', ['{"itemId":12}', '{"itemId":"12"}'])
def test_exact_identity_and_body_citations(metadata):
    from bs4 import BeautifulSoup
    from zhihu_collector.parsers.identity import answer_container
    html=f'''<div class="AnswerItem" data-zop='{metadata}'><div class="RichText">正文<a href="/question/100/answer/123">引用</a></div></div>'''
    assert answer_container(BeautifulSoup(html,'lxml'),'12') is not None

@pytest.mark.parametrize('html', [
    '''<div class="AnswerItem" data-zop='{"questionId":12,"itemId":123}'><div class="RichText">别篇</div></div>''',
    '''<div class="AnswerItem"><a href="/question/100/answer/12abc">错误链接</a></div>'''
])
def test_identity_requires_answer_key_and_complete_url(html):
    from bs4 import BeautifulSoup
    from zhihu_collector.parsers.identity import answer_container
    assert answer_container(BeautifulSoup(html,'lxml'),'12') is None

def test_body_failure_survives_restart_and_recovers(env,monkeypatch):
    from zhihu_collector.collectors.repairs import retry_body_failures
    s,db,p=env;r=rec(1);r.content_incomplete=True
    session=MagicMock();session.__enter__.return_value=session
    session.new_page.return_value.content.return_value='<html><body>No answer</body></html>'
    monkeypatch.setattr(svc,'BrowserSession',lambda **kw:session)
    monkeypatch.setattr(svc,'records_from_page_sources',lambda **kw:[])
    monkeypatch.setattr(svc,'collect_listing',lambda *a,**kw:kw['on_page']([r],1,1))
    s.run(request(p))
    s2=svc.CollectionService(Settings(),db,TaskController());s2.run(request(p))
    assert len([x for x in db.list_failures() if x['kind']=='body'])==1
    db.close()
    reopened=Database(p/'audit.db')
    try:
        assert reopened.list_failures()[0]['detail']==r.url
        full=rec(1)
        monkeypatch.setattr(svc,'records_from_page_sources',lambda **kw:[full])
        result=retry_body_failures(reopened,Settings())
        assert result.status=='success'
        assert len(reopened.list_contents())==1
        assert 'BODYMARKER1' in reopened.list_contents()[0]['html_content']
        assert reopened.list_failures()==[]
    finally:reopened.close()

def test_snapshot_retry_cannot_override_new_revision(env,monkeypatch):
    from zhihu_collector.collectors.repairs import _retry_merged_export
    s,db,p=env;a=save(db,1);b=save(db,2)
    r=request(p,export_separate=False,export_merged=True)
    with monkeypatch.context() as m:
        m.setattr(svc,'_write_export_file',fail)
        export_local_contents(db,[a],p/'out',r,Settings())
    snapshot=db.list_failures()[0]
    export_local_contents(db,[b],p/'out',r,Settings())
    target=p/'out'/'所选内容.md';before=target.read_bytes()
    assert _retry_merged_export(db,snapshot,Settings(),None,True)
    assert target.read_bytes()==before

def test_export_targets_case_and_relative_paths_are_one_identity(env,monkeypatch):
    import os
    s,db,p=env;a=save(db,1);b=save(db,2)
    monkeypatch.chdir(p)
    db.record_export(a,export_format='markdown',output_path='OUT/Book.md',content_hash='',status='failed')
    db.record_export(b,export_format='markdown',output_path=str(p/'out'/'book.md'),content_hash='ok',status='success')
    assert db.list_failures()==[]

def test_legacy_migration_creates_valid_backup_and_no_guess(env,monkeypatch):
    import sqlite3
    from zhihu_collector.database.migrations import MIGRATIONS
    s,db,p=env
    path=p/'old.db'
    c=sqlite3.connect(path)
    for v,script in MIGRATIONS:
        if v>4:break
        c.executescript(script)
        c.execute('INSERT INTO schema_migrations VALUES (?,?)',(v,'2026-01-01'))
        c.commit()
    c.close()
    upgraded=Database(path)
    try:
        backups=list(p.glob('old.db.pre-v4-*.bak'));assert len(backups)==1
        with sqlite3.connect(backups[0]) as bc:
            assert bc.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert bc.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==4
        assert upgraded._conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] >= 5
    finally:upgraded.close()

def test_missing_member_cannot_silently_shrink_merge(env,monkeypatch):
    s,db,p=env;a=save(db,1);b=save(db,2)
    r=request(p,export_separate=False,export_merged=True)
    target=p/'out'/'所选内容.md';target.parent.mkdir();target.write_text('OLD SAFE',encoding='utf-8')
    with monkeypatch.context() as m:
        m.setattr(svc,'_write_export_file',fail)
        export_local_contents(db,[a,b],p/'out',r,Settings())
    db._conn.execute('DELETE FROM contents WHERE id=?',(b,));db._conn.commit()
    result=retry_saved_failures(db,Settings(),p/'out')
    assert result.status=='partial'
    assert target.read_text(encoding='utf-8')=='OLD SAFE'

@pytest.mark.parametrize('title', ['全部想法', '任意-回答', '所选内容'])
def test_other_titles_do_not_change_separate_scope(env, monkeypatch, title):
    s, db, p = env
    a = save(db, 1, title)
    save(db, 2)
    with monkeypatch.context() as m:
        m.setattr(svc, '_write_export_file', fail)
        svc.write_separate_exports(db.get_content(a), [], p/'out', request(p), Settings(), None, db)
    target = Path(db.list_failures()[0]['target'])
    result = retry_saved_failures(db, Settings(), p/'out')
    assert result.status == 'success'
    text = target.read_text(encoding='utf-8')
    assert 'BODYMARKER1' in text and 'BODYMARKER2' not in text


def test_unknown_legacy_type_refuses_to_overwrite(env):
    s, db, p = env
    a = save(db, 1, '全部回答')
    save(db, 2)
    target = p/'legacy.md'
    target.write_text('EXISTING SAFE FILE', encoding='utf-8')
    db.record_export(a, export_format='markdown', output_path=str(target), content_hash='', status='failed')
    db._conn.execute("UPDATE exports SET type_explicit=0, scope_json='' WHERE content_id=?", (a,))
    db._conn.commit()
    assert db.list_failures()[0]['export_type'] == 'legacy'
    result = retry_saved_failures(db, Settings(), p)
    assert result.status == 'partial'
    assert target.read_text(encoding='utf-8') == 'EXISTING SAFE FILE'
    assert '重新选择' in db.list_failures()[0]['reason']
