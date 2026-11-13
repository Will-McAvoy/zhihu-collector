from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from zhihu_collector.browser import auth_manager as auth


@pytest.mark.parametrize('path,profile,cookies,expected',[
    ('/signin',True,[{'name':'z_c0','value':'stale'}],False),
    ('/unhuman',True,[{'name':'z_c0','value':'stale'}],False),
    ('/',False,[{'name':'z_c0','value':'stale'}],False),
    ('/',True,[],False),
    ('/',True,[{'name':'z_c0','value':'valid'}],True),
])
def test_validate_requires_web_confirmation(tmp_path,monkeypatch,path,profile,cookies,expected):
    state=tmp_path/'state.json';state.write_text('{}')
    page=SimpleNamespace(url='https://www.zhihu.com'+path,
        wait_for_timeout=lambda ms:None,evaluate=lambda script:profile)
    session=MagicMock();session.__enter__.return_value=session
    session.new_page.return_value=page;session.context.cookies.return_value=cookies
    monkeypatch.setattr(auth,'BrowserSession',lambda **kw:session)
    monkeypatch.setattr(auth,'navigation_once',lambda *args:None)
    monkeypatch.setattr(auth,'_read_logged_in_name',lambda page:'')
    assert auth.AuthManager(state).validate().logged_in is expected


def test_interactive_login_does_not_save_rejected_cookie(tmp_path,monkeypatch):
    state=tmp_path/'state.json';state.write_text('{}')
    page=SimpleNamespace(url='https://www.zhihu.com/signin',wait_for_timeout=lambda ms:None)
    session=MagicMock();session.__enter__.return_value=session
    session.new_page.return_value=page
    session.context.cookies.return_value=[{'name':'z_c0','value':'stale'}]
    monkeypatch.setattr(auth,'BrowserSession',lambda **kw:session)
    monkeypatch.setattr(auth,'navigation_once',lambda *args:None)
    result=auth.AuthManager(state).login_interactive(timeout_sec=.01)
    assert not result.logged_in
    session.save_storage_state.assert_not_called()
