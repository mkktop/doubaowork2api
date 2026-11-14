from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from doubao_work2api.accounts import Account
from doubao_work2api.config import Settings
from doubao_work2api.errors import UpstreamAuthError, UpstreamChallengeError, UpstreamRateLimitError, UpstreamError
from doubao_work2api.transport import DirectTransport, classify_body_error


def test_query_contains_required_desktop_identity() -> None:
    transport = DirectTransport(Settings())
    query = transport._query(
        Account(
            id="one",
            cookies={"sessionid": "x"},
            device_id="device-1",
            web_id="web-1",
        )
    )
    assert "device_id=device-1" in query
    assert "web_id=web-1" in query
    assert "fp=verify_device-1" in query
    assert "doubao_device_platform=desktop" in query
    assert "web_tab_id=" in query


def test_classify_body_error_ignores_success_codes() -> None:
    classify_body_error({"code": 0})
    classify_body_error({"code": 200})
    classify_body_error({})


@pytest.mark.parametrize(('code', 'error'), [(710022004, UpstreamChallengeError), (710012001, UpstreamAuthError), (710022005, UpstreamRateLimitError), (999999, UpstreamError)])
def test_stream_error_codes_are_classified_without_leaking_metadata(code, error):
    with pytest.raises(error) as raised:
        classify_body_error({'_event': 'STREAM_ERROR', 'error_code': code, 'code': 0, 'error_msg': 'private upstream metadata'})
    assert raised.value.code == str(code)
    assert 'private' not in str(raised.value)


def test_stream_error_without_code_is_not_silently_successful():
    with pytest.raises(UpstreamError) as raised:
        classify_body_error({'_event': 'STREAM_ERROR'})
    assert raised.value.code == 'upstream_stream_error'


async def test_http_200_stream_challenge_raises_before_empty_response(monkeypatch):
    transport = DirectTransport(Settings(upstream_base='https://upstream.test'))
    mock_clients(monkeypatch, transport, lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, text='event: STREAM_ERROR\ndata: {"error_code":710022004,"error_msg":"private"}\n\nevent: SSE_REPLY_END\ndata: {"end_type":3}\n\n'))
    try:
        with pytest.raises(UpstreamChallengeError) as raised:
            await consume(transport, make_account('one'))
        assert raised.value.code == '710022004'
    finally:
        await transport.close()


def make_account(account_id: str) -> Account:
    return Account(id=account_id, cookies={"sessionid": f"fake-{account_id}"},
                   device_id="shared-device", web_id="shared-web")


def mock_clients(monkeypatch, transport: DirectTransport, handler):
    clients = []

    def create(account):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), cookies=account.cookies,
            timeout=httpx.Timeout(transport.settings.request_timeout_seconds, connect=20),
            follow_redirects=True,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(transport, "_create_client", create)
    return clients


async def consume(transport: DirectTransport, account: Account):
    return [event async for event in transport.stream(account, {"synthetic": True})]


@pytest.mark.parametrize("terminal", [
    'event: SSE_REPLY_END\ndata: {"end_type":3}\n\n',
    'data: [DONE]\n\n',
])
async def test_final_answer_closes_stream_without_waiting_for_eof(monkeypatch, terminal):
    class HeldOpenStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'event: CHUNK_DELTA\ndata: {"text":"O"}\n\n'
            # A message boundary must not discard the rest of the answer.
            yield b'event: SSE_REPLY_END\ndata: {"end_type":1}\n\n'
            yield b'event: CHUNK_DELTA\ndata: {"text":"K"}\n\n'
            yield terminal.encode()
            raise AssertionError("Read past final marker into the keepalive period")

        async def aclose(self):
            self.closed = True

    stream = HeldOpenStream()
    transport = DirectTransport(Settings(upstream_base="https://upstream.test"))
    mock_clients(monkeypatch, transport, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    try:
        events = await consume(transport, make_account("one"))
        assert [event["text"] for event in events if "text" in event] == ["O", "K"]
        assert stream.closed
    finally:
        await transport.close()


async def test_stream_delivers_split_utf8_event_before_remaining_body(monkeypatch):
    release_tail = asyncio.Event()
    closed = asyncio.Event()

    class IncrementalStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            encoded = 'event: CHUNK_DELTA\ndata: {"text":"你好"}\n\n'.encode()
            split = encoded.index('你'.encode()) + 1
            yield encoded[:split]
            yield encoded[split:]
            await release_tail.wait()
            yield b'event: SSE_REPLY_END\ndata: {"end_type":3}\n\n'
            raise AssertionError('Read beyond the final answer')

        async def aclose(self):
            closed.set()

    transport = DirectTransport(Settings(upstream_base='https://upstream.test'))
    mock_clients(monkeypatch, transport, lambda request: httpx.Response(
        200, headers={'content-type': 'text/event-stream'}, stream=IncrementalStream()))
    events = transport.stream(make_account('one'), {'synthetic': True})
    try:
        first = await asyncio.wait_for(anext(events), timeout=1)
        assert first == {'text': '你好', '_event': 'CHUNK_DELTA'}
        assert not release_tail.is_set()
        release_tail.set()
        assert [event async for event in events] == [{'_event': 'SSE_REPLY_END', 'end_type': 3}]
        assert closed.is_set()
    finally:
        release_tail.set()
        await events.aclose()
        await transport.close()


async def test_accounts_keep_independent_cookie_jars_and_response_cookies(monkeypatch) -> None:
    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0)  # Exercise simultaneous account streams as well.
        return httpx.Response(200, headers={
            "content-type": "text/event-stream",
            "set-cookie": f"server_cookie={request.headers['x-request-id']}; Path=/",
        }, text='event: STREAM_CHUNK\ndata: {"code":0,"value":"ok"}\n\ndata: [DONE]\n\n')

    transport = DirectTransport(Settings(upstream_base="https://upstream.test"))
    clients = mock_clients(monkeypatch, transport, handler)
    one, two = make_account("one"), make_account("two")
    try:
        events = await asyncio.gather(consume(transport, one), consume(transport, two))
        assert all(result == [{"code": 0, "value": "ok", "_event": "STREAM_CHUNK"}] for result in events)
        assert len(clients) == 2
        assert clients[0].cookies is not clients[1].cookies
        assert clients[0]._transport is not clients[1]._transport
        assert requests[0].headers["cookie"] == "sessionid=fake-one"
        assert requests[1].headers["cookie"] == "sessionid=fake-two"
        one_cookie = requests[0].headers["x-request-id"]
        two_cookie = requests[1].headers["x-request-id"]
        await consume(transport, one)
        await consume(transport, two)
        assert len(clients) == 2
        assert f"server_cookie={one_cookie}" in requests[2].headers["cookie"]
        assert two_cookie not in requests[2].headers["cookie"]
        assert f"server_cookie={two_cookie}" in requests[3].headers["cookie"]
        assert one_cookie not in requests[3].headers["cookie"]
        assert one.cookies == {"sessionid": "fake-one"}
        assert two.cookies == {"sessionid": "fake-two"}
    finally:
        await transport.close()
    assert all(client.is_closed for client in clients)


async def test_real_client_connection_pools_are_isolated_without_requests() -> None:
    transport = DirectTransport(Settings())
    try:
        first = await transport._client_for(make_account("one"))
        second = await transport._client_for(make_account("two"))
        assert first is not second
        assert first._transport is not second._transport
        assert first._transport._pool is not second._transport._pool
    finally:
        await transport.close()


async def test_answer_end_still_reads_usage_and_reply_end(monkeypatch):
    wire = 'event: CHUNK_DELTA\r\ndata: {"text":"OK"}\r\n\r\nevent: SSE_REPLY_END\ndata: {"end_type":2}\n\nevent: SSE_REPLY_END\ndata: {"end_type":3,"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
    transport = DirectTransport(Settings(upstream_base='https://upstream.test'))
    mock_clients(monkeypatch,transport,lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},text=wire))
    try:
        values = await consume(transport,make_account('one'))
        assert values[-1]['end_type']==3 and values[-1]['usage']['total_tokens']==12
    finally:
        await transport.close()


async def test_answer_end_without_reply_end_uses_bounded_grace(monkeypatch):
    class HeartbeatOnly(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: CHUNK_DELTA\ndata: {"text":"OK"}\n\nevent: SSE_REPLY_END\ndata: {"end_type":2}\n\n'
            await asyncio.Event().wait()
        async def aclose(self):
            pass
    transport = DirectTransport(Settings(upstream_base='https://upstream.test'))
    mock_clients(monkeypatch,transport,lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=HeartbeatOnly()))
    try:
        values = await asyncio.wait_for(consume(transport,make_account('one')),2)
        assert values[-1]['end_type']==2
    finally:
        await transport.close()


async def test_missing_reply_end_does_not_turn_truncated_text_into_success(monkeypatch):
    transport=DirectTransport(Settings(upstream_base='https://upstream.test'))
    mock_clients(monkeypatch,transport,lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},
        text='event: CHUNK_DELTA\ndata: {"text":"incomplete"}\n\n'))
    try:
        with pytest.raises(UpstreamError) as raised:
            await consume(transport,make_account('one'))
        assert raised.value.code=='upstream_incomplete_reply'
    finally:
        await transport.close()


async def test_deleted_account_transport_disposes_only_selected_cookie_jar() -> None:
    transport = DirectTransport(Settings())
    try:
        first = await transport._client_for(make_account("one"))
        second = await transport._client_for(make_account("two"))
        await transport.forget_accounts(["one", "missing"])
        assert first.is_closed and not second.is_closed
        assert "one" not in transport._clients
        assert await transport._client_for(make_account("two")) is second
    finally:
        await transport.close()


async def test_credential_revision_replaces_only_affected_client(monkeypatch) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={
            "content-type": "text/event-stream", "set-cookie": "old_jar=private; Path=/",
        }, text='data: {"value":"ok"}\n\ndata: [DONE]\n\n')

    transport = DirectTransport(Settings(upstream_base="https://upstream.test"))
    clients = mock_clients(monkeypatch, transport, handler)
    one, two = make_account("one"), make_account("two")
    try:
        await consume(transport, one)
        await consume(transport, two)
        one.name = "Metadata edit"
        await consume(transport, one)
        assert len(clients) == 2
        one.cookies["sessionid"] = "fake-updated"
        await consume(transport, one)
        assert len(clients) == 3
        assert clients[0].is_closed
        assert not clients[1].is_closed
        assert requests[-1].headers["cookie"] == "sessionid=fake-updated"
        await consume(transport, two)
        assert len(clients) == 3
        assert await transport._client_for(replace(one)) is clients[2]
        one.device_id = "new-device"
        await consume(transport, one)
        assert clients[2].is_closed
        assert len(clients) == 4
    finally:
        await transport.close()


async def test_concurrent_client_lookup_reuses_same_revision(monkeypatch) -> None:
    transport = DirectTransport(Settings())
    clients = mock_clients(monkeypatch, transport, lambda request: httpx.Response(200))
    try:
        account = make_account("one")
        found = await asyncio.gather(*(transport._client_for(account) for _ in range(10)))
        assert len(clients) == 1
        assert all(client is clients[0] for client in found)
    finally:
        await transport.close()


async def test_configure_closes_every_client_and_uses_new_settings(monkeypatch) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")

    settings = Settings(upstream_base="https://old.test")
    transport = DirectTransport(settings)
    clients = mock_clients(monkeypatch, transport, handler)
    one, two = make_account("one"), make_account("two")
    try:
        await consume(transport, one)
        await consume(transport, two)
        configured = replace(settings, upstream_base="https://new.test", request_timeout_seconds=12,
                             app_id="changed-app", pc_version="changed-version")
        await transport.configure(configured)
        assert all(client.is_closed for client in clients)
        assert not transport._clients
        assert transport.settings is configured
        await consume(transport, one)
        assert requests[-1].url.host == "new.test"
        assert requests[-1].url.params["aid"] == "changed-app"
        assert requests[-1].url.params["pc_version"] == "changed-version"
        assert clients[-1].timeout.read == 12
        assert requests[-1].headers["origin"] == "https://new.test"
    finally:
        await transport.close()
    assert all(client.is_closed for client in clients)
    assert not transport._clients
    await transport.close()  # Idempotent shutdown.


async def test_close_attempts_all_clients_even_if_one_fails(monkeypatch) -> None:
    transport = DirectTransport(Settings())
    clients = mock_clients(monkeypatch, transport, lambda request: httpx.Response(200))
    await transport._client_for(make_account("one"))
    await transport._client_for(make_account("two"))
    original_close = clients[0].aclose

    async def fail_close():
        raise OSError("synthetic close failure")

    monkeypatch.setattr(clients[0], "aclose", fail_close)
    with pytest.raises(OSError, match="synthetic"):
        await transport.configure(replace(transport.settings, app_id="new"))
    assert clients[1].is_closed
    assert transport.settings.app_id != "new"
    monkeypatch.setattr(clients[0], "aclose", original_close)
    await transport.close()
    assert clients[0].is_closed
