import json
from pathlib import Path

import httpx
import pytest

from doubao_work2api.accounts import AccountPool
from doubao_work2api.browser_login import BrowserLogin


class FakeProcess:
    def poll(self): return 0
    def wait(self, timeout=None): return 0
    def terminate(self): pass
    def kill(self): pass


async def test_browser_login_is_isolated_owner_scoped_verified_and_closed(tmp_path,monkeypatch):
    from doubao_work2api import browser_login as module
    calls=[]
    monkeypatch.setattr(module,'find_browser',lambda:'synthetic-browser')
    monkeypatch.setattr(module.subprocess,'Popen',lambda args,**kwargs:(calls.append(args) or FakeProcess()))
    pool=AccountPool(tmp_path/'accounts')
    login=BrowserLogin(pool)
    session=await login.start('owner-a')
    identifier=session['id']
    profile=login.sessions[identifier]['profile']
    assert any(value.startswith('--user-data-dir=') for value in calls[0])
    assert '--remote-debugging-address=127.0.0.1' in calls[0]
    assert 'synthetic-secret' not in json.dumps(session)
    with pytest.raises(KeyError):
        await login.finish(identifier,'owner-b')
    closed=[]
    async def cdp(session,method):
        if method=='Storage.getCookies':
            return {'cookies':[{'domain':'.doubao.com','name':'sessionid','value':'synthetic-secret'},
                {'domain':'.other.test','name':'external','value':'never-import'}]}
        if method=='Browser.getVersion': return {'userAgent':'Synthetic Chrome/147'}
        closed.append(method); return {}
    monkeypatch.setattr(login,'cdp',cdp)
    router={'loaderData':{'chat_layout':{'chat_layout':{'launchCore':{'web_id':'123456789'},'aid':'582478','pc_version':'2.0'}}}}
    def handler(request):
        assert request.url.host=='www.doubao.com'
        assert 'never-import' not in request.headers.get('cookie','')
        if 'account/info' in request.url.path:
            return httpx.Response(200,json={'data':{'user_id_str':'123','name':'Browser Test'}})
        return httpx.Response(200,text='<script>window._ROUTER_DATA='+json.dumps(router)+';</script>')
    original_client=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original_client(transport=httpx.MockTransport(handler),**kwargs))
    result=await login.finish(identifier,'owner-a')
    assert result['status']=='confirmed' and closed==['Browser.close']
    assert not profile.exists() and not login.sessions
    account=pool.accounts[0]
    assert account.client_profile=='web' and account.device_id=='123456789'
    assert account.cookies=={'sessionid':'synthetic-secret'}
    assert 'synthetic-secret' not in account.source_path.read_text()


async def test_browser_cancel_and_no_browser_do_not_import_accounts(tmp_path,monkeypatch):
    from doubao_work2api import browser_login as module
    login=BrowserLogin(AccountPool(tmp_path/'accounts'))
    monkeypatch.setattr(module,'find_browser',lambda:None)
    with pytest.raises(ValueError):
        await login.start('owner')
    assert not login.sessions and not login.pool.accounts
