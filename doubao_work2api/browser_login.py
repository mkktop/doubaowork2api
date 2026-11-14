"""User-driven Chrome/Edge login in an isolated temporary browser profile."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from .qr_login import web_context


def find_browser():
    candidates = [shutil.which(name) for name in ('google-chrome','chromium','chromium-browser','microsoft-edge','chrome','msedge')]
    if sys.platform == 'win32':
        for directory in (os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')):
            if directory:
                candidates.extend(str(Path(directory)/relative) for relative in ('Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe'))
    elif sys.platform == 'darwin':
        candidates.extend(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'])
    return next((value for value in candidates if value and Path(value).is_file()),None)


class BrowserLogin:
    def __init__(self, pool):
        self.pool, self.sessions = pool, {}

    def session(self, identifier, owner):
        session = self.sessions.get(identifier)
        if not session or session['owner'] != owner:
            raise KeyError('浏览器登录会话不存在。')
        return session

    async def start(self, owner):
        if len(self.sessions) >= 2:
            raise ValueError('请先关闭已有浏览器登录窗口。')
        executable = find_browser()
        if not executable:
            raise ValueError('服务所在机器未检测到 Chrome/Edge；无桌面服务器请使用扫码或 JSON 导入。')
        profile = Path(tempfile.mkdtemp(prefix='doubaowork-login-'))
        try:
            process = subprocess.Popen([executable,'--remote-debugging-port=0','--remote-debugging-address=127.0.0.1',
                '--user-data-dir='+str(profile),'--no-first-run','--no-default-browser-check','--new-window',
                'https://www.doubao.com/chat/'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except OSError:
            shutil.rmtree(profile,ignore_errors=True)
            raise ValueError('无法启动独立浏览器，请检查服务端图形环境。') from None
        identifier = secrets.token_urlsafe(24)
        session = {'owner':owner,'profile':profile,'process':process,'expires':time.time()+300,
                   'lock':asyncio.Lock(),'status':'waiting'}
        self.sessions[identifier] = session
        async def expire():
            await asyncio.sleep(300)
            if identifier in self.sessions:
                await self.cancel(identifier,owner)
        session['expiry'] = asyncio.create_task(expire())
        return {'id':identifier,'status':'waiting','expires_at':session['expires'],
                'message':'请在服务所在电脑的独立浏览器中登录豆包，再点击「完成登录并导入」。'}

    async def cdp(self, session, method):
        path = session['profile']/'DevToolsActivePort'
        if not path.exists():
            raise ValueError('浏览器尚未就绪，请稍后再试。')
        port, endpoint, *_ = path.read_text().splitlines()
        if not port.isdigit() or not endpoint.startswith('/devtools/browser/'):
            raise ValueError('独立浏览器调试地址无效。')
        async with connect(f'ws://127.0.0.1:{int(port)}{endpoint}',open_timeout=5,max_size=4*1024*1024) as socket:
            await socket.send(json.dumps({'id':1,'method':method}))
            async def receive():
                async for raw in socket:
                    value = json.loads(raw)
                    if value.get('id') == 1:
                        if 'error' in value:
                            raise ValueError('浏览器拒绝读取登录状态。')
                        return value.get('result',{})
            return await asyncio.wait_for(receive(),10)
        raise ValueError('浏览器连接已关闭。')

    async def finish(self, identifier, owner):
        session = self.session(identifier,owner)
        async with session['lock']:
            if session['expires'] <= time.time():
                raise ValueError('浏览器登录会话已过期，请重新打开。')
            cookie_result = await self.cdp(session,'Storage.getCookies')
            cookies = {row['name']:row['value'] for row in cookie_result.get('cookies',[])
                       if isinstance(row,dict) and (str(row.get('domain','')).lstrip('.')=='doubao.com'
                       or str(row.get('domain','')).endswith('.doubao.com'))}
            if not (cookies.get('sessionid') or cookies.get('sessionid_ss')):
                raise ValueError('尚未取得豆包登录状态，请先在独立浏览器完成登录。')
            version = await self.cdp(session,'Browser.getVersion')
            ua = str(version.get('userAgent') or '')[:512]
            async with httpx.AsyncClient(cookies=cookies,headers={'User-Agent':ua},follow_redirects=False,timeout=20) as client:
                identity = await client.get('https://www.doubao.com/passport/account/info/v2/',params={
                    'aid':'582478','real_aid':'582478','account_sdk_source':'web','sdk_version':'2.2.5'})
                identity.raise_for_status()
                user = identity.json().get('data',{})
                uid = str(user.get('user_id_str') or user.get('user_id') or '')
                if not uid.isdigit() or int(uid) <= 0:
                    raise ValueError('官方尚未确认登录身份，请完成登录后重试。')
                response = await client.get('https://www.doubao.com/chat/')
                response.raise_for_status()
                context = web_context(response.text)
            identity_id = uid+':'+str(user.get('passport_enterprise_user_type') or '0')
            account = await self.pool.upsert({'id':'doubao-'+hashlib.sha256(identity_id.encode()).hexdigest()[:24],
                'identity_id':identity_id,'cookies':cookies,**context,'user_agent':ua,
                'fingerprint':cookies.get('s_v_web_id',''),'name':str(user.get('name') or '浏览器账号')[:128]})
        await self.cancel(identifier,owner)
        return {'status':'confirmed','account_id':account.id}

    async def cancel(self, identifier, owner):
        session = self.session(identifier,owner)
        self.sessions.pop(identifier,None)
        expiry = session.get('expiry')
        if expiry and expiry is not asyncio.current_task():
            expiry.cancel()
        try:
            await self.cdp(session,'Browser.close')
        except Exception:
            if session['process'].poll() is None:
                session['process'].terminate()
        try:
            await asyncio.to_thread(session['process'].wait,3)
        except subprocess.TimeoutExpired:
            session['process'].kill()
        profile = session['profile'].resolve()
        if profile.parent == Path(tempfile.gettempdir()).resolve() and profile.name.startswith('doubaowork-login-'):
            await asyncio.to_thread(shutil.rmtree,profile,True)

    async def close(self):
        for identifier,session in list(self.sessions.items()):
            await self.cancel(identifier,session['owner'])
