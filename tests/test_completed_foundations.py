import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from doubao_work2api.accounts import AccountPool
from doubao_work2api.app import create_app
from doubao_work2api.config import Settings
from doubao_work2api.errors import AccountBusyError, UpstreamChallengeError
from doubao_work2api.logs import RequestLog
from doubao_work2api.models import ChatCompletionRequest
from doubao_work2api.protocol import UpstreamTextParser
from doubao_work2api.service import ChatService
from doubao_work2api.transport import DirectTransport
from doubao_work2api.usage import official_usage
from doubao_work2api.vault import AccountVault


def block(text, identifier='block'):
    return {'block_type':10000,'block_id':identifier,'content':{'text_block':{'text':text}}}


def test_snapshot_delta_reconciliation_and_message_scoping():
    parser = UpstreamTextParser()
    events = [
        {'_event':'STREAM_MSG_NOTIFY','meta':{'message_id':'m1'},'content':{'content_block':[block('')]}},
        {'_event':'CHUNK_DELTA','text':'ab'},
        {'_event':'STREAM_MSG_NOTIFY','content':{'content_block':[block('ab')]}},
        {'_event':'STREAM_CHUNK','patch_op':[{'patch_type':1,'patch_value':{'content_block':[block('c')]}}]},
        {'_event':'CHUNK_DELTA','text':'c'},
        {'_event':'FULL_MSG_NOTIFY','message':{'user_type':2,'message_id':'m1','content':json.dumps([block('abc')])}},
        {'_event':'FULL_MSG_NOTIFY','message':{'user_type':1,'message_id':'user','content':json.dumps([block('PRIVATE USER TEXT')])}},
        {'_event':'FULL_MSG_NOTIFY','message':{'user_type':2,'message_id':'m2','content':json.dumps([block('second')])}},
    ]
    assert ''.join(text for event in events for kind,text in parser.feed(event)) == 'abcsecond'


@pytest.mark.parametrize('container', ['direct','msg_finish_attr','answer_finish_attr'])
def test_official_usage_requires_real_nonnegative_counts(container):
    body = {'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
        'prompt_tokens_details':{'cached_tokens':80},'completion_tokens_details':{'reasoning_tokens':5}}}
    event = body if container=='direct' else {container:body}
    value = official_usage(event)
    assert value['total_tokens']==120 and value['is_estimated'] is False
    assert value['prompt_tokens_details']['cached_tokens']==80
    assert official_usage({'context_window_usage':{'messages':100,'total_window_size':300000}}) is None
    assert official_usage({'usage':{'prompt_tokens':True,'completion_tokens':1,'total_tokens':2}}) is None


async def make_service(tmp_path, events=None):
    pool = AccountPool(tmp_path/'accounts')
    await pool.upsert({'id':'one','cookies':{'sessionid':'synthetic-secret'},'device_id':'d','web_id':'w'})
    sent = []
    class Transport:
        async def stream(self, account, payload):
            sent.append((account.id,payload))
            for event in events or [
                {'_event':'SSE_ACK','ack_client_meta':{'conversation_id':'native-one','section_id':'section-one'}},
                {'_event':'CHUNK_DELTA','text':'OK'},
                {'_event':'SSE_REPLY_END','end_type':3},
            ]:
                yield event
    service = ChatService(pool,Transport(),model_key='unused',app_id='test',workspace_root=tmp_path,
        request_log=RequestLog(path=tmp_path/'requests.sqlite3'))
    return service, sent


@pytest.mark.parametrize('stream', [False,True])
async def test_official_usage_reaches_api_and_persistent_log(tmp_path,stream):
    events = [{'_event':'CHUNK_DELTA','text':'OK'}, {'_event':'SSE_REPLY_END','end_type':3,
        'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
        'prompt_tokens_details':{'cached_tokens':75},'completion_tokens_details':{'reasoning_tokens':5}}}]
    service,_ = await make_service(tmp_path,events)
    request = ChatCompletionRequest(messages=[{'role':'user','content':'private prompt'}],stream=stream,stream_options={'include_usage':True})
    if stream:
        chunks = [item async for item in service.stream(request,key_id='key-a')]
        usage = next(json.loads(item[5:])['usage'] for item in chunks if item.startswith('data: {') and '"usage"' in item)
    else:
        usage = (await service.complete(request,key_id='key-a'))['usage']
    assert usage['total_tokens']==120 and usage['is_estimated'] is False
    restored = RequestLog(path=tmp_path/'requests.sqlite3')
    row = restored.snapshot()[0]
    assert row['input_tokens']==100 and row['cached_tokens']==75 and row['usage_estimated'] is False
    assert row['reasoning_tokens']==5
    assert restored.analytics(1)['by_key'][0]['id']=='key-a'
    assert 'private prompt' not in (tmp_path/'requests.sqlite3').read_bytes().decode(errors='ignore')


async def test_native_reuse_requires_exact_history_key_model_account_and_completion(tmp_path):
    service,sent = await make_service(tmp_path)
    messages = [{'role':'user','content':'hello'}]
    await service.complete(ChatCompletionRequest(messages=messages,session_id='s'),key_id='key-a')
    history = messages+[{'role':'assistant','content':'OK'},{'role':'user','content':'next'}]
    await service.complete(ChatCompletionRequest(messages=history,session_id='s'),key_id='key-a')
    second = sent[1][1]
    assert second['client_meta']['conversation_id']=='native-one'
    assert second['client_meta']['last_section_id']=='section-one'
    assert 'hello' not in second['messages'][0]['content_block'][0]['content']['text_block']['text']
    await service.complete(ChatCompletionRequest(messages=history,session_id='s'),key_id='key-b')
    assert sent[2][1]['option']['need_create_conversation'] is True
    edited = messages+[{'role':'assistant','content':'CHANGED'},{'role':'user','content':'next'}]
    await service.complete(ChatCompletionRequest(messages=edited,session_id='s'),key_id='key-a')
    assert sent[3][1]['option']['need_create_conversation'] is True


async def test_large_requests_are_spread_then_explicitly_limited(tmp_path):
    service,sent = await make_service(tmp_path)
    await service.pool.upsert({'id':'two','cookies':{'sessionid':'second'},'device_id':'d2','web_id':'w2'})
    service.routing.configure({'large_request_bytes':1,'large_request_budget':1})
    for _ in range(2):
        await service.complete(ChatCompletionRequest(messages=[{'role':'user','content':'hello'}]))
    assert [item[0] for item in sent]==['one','two']
    with pytest.raises(AccountBusyError) as raised:
        await service.complete(ChatCompletionRequest(messages=[{'role':'user','content':'hello'}]))
    assert raised.value.code=='large_request_budget'


async def test_legacy_plaintext_migrates_atomically_and_export_stays_portable(tmp_path):
    document = {'id':'one','cookies':{'sessionid':'SYNTHETIC_PRIVATE_COOKIE'},'device_id':'d','web_id':'w'}
    path = tmp_path/'legacy.json'
    path.write_text(json.dumps(document))
    pool = AccountPool(tmp_path)
    assert pool.load()==1
    assert 'SYNTHETIC_PRIVATE_COOKIE' not in path.read_text()
    assert pool.export_accounts(['one'])[0]['cookies']==document['cookies']
    ciphertext = path.read_bytes()
    (tmp_path/'.account-vault.key').unlink()
    with pytest.raises(ValueError):
        AccountPool(tmp_path).load()
    assert path.read_bytes()==ciphertext and not (tmp_path/'.account-vault.key').exists()


def test_vault_tamper_and_wrong_key_are_rejected(tmp_path):
    vault = AccountVault(tmp_path)
    encoded = vault.encode({'cookies':{'sessionid':'private'}})
    encoded['ciphertext']='AAAA'
    with pytest.raises(ValueError):
        vault.decode(encoded)


def test_unfinished_logs_restore_as_interrupted_and_filters_survive_restart(tmp_path):
    log = RequestLog(path=tmp_path/'log.sqlite3')
    first=log.start(model='m1',stream=True,meta={'key_id':'k1'})
    log.account(first,'a1')
    second=log.start(model='m2',stream=False,meta={'key_id':'k2'})
    log.success(second)
    restored=RequestLog(path=tmp_path/'log.sqlite3')
    assert restored.snapshot(model='m1',key_id='k1',account_id='a1')[0]['status']=='interrupted'
    assert len(restored.analytics(7)['by_model'])==2


async def test_verification_is_distinct_and_only_targeted_complete_retest_restores(tmp_path):
    service,_=await make_service(tmp_path)
    account=service.pool.find('one')
    account.error_code='710022004'; account.last_error='verify'
    assert account.public_dict()['state']=='needs_verification' and not account.available
    result=await service.probe(ChatCompletionRequest(messages=[{'role':'user','content':'OK'}]),account_id='one')
    assert result['verified']=='complete_recovery'
    assert account.available and not account.error_code


def test_new_admin_routes_are_separate_from_api_keys(tmp_path):
    app=create_app(Settings(api_key='api',admin_password='admin',data_dir=tmp_path/'data',accounts_dir=tmp_path/'accounts'))
    with TestClient(app) as client:
        for path in ['/admin/routing','/admin/analytics']:
            assert client.get(path,headers={'Authorization':'Bearer api'}).status_code==401
        assert client.post('/admin/accounts/browser').status_code==401
        assert client.post('/admin/models/test',json={'model':'deepseek-v4.1-flash'}).status_code==401
        client.post('/admin/login',json={'password':'admin'})
        assert client.put('/admin/routing',json={'large_request_budget':2}).status_code==200
        assert client.put('/admin/routing',json={'large_request_budget':-1}).status_code==400
        assert client.get('/admin/analytics?days=31').status_code==400
