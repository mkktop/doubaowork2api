from fastapi.testclient import TestClient

from doubao_work2api.app import create_app
from doubao_work2api.config import Settings
from doubao_work2api.errors import UpstreamAuthError


def setup(tmp_path):
    app = create_app(Settings(api_key="probe-key", admin_password="probe-admin",
                              accounts_dir=tmp_path / "accounts", data_dir=tmp_path / "data"))
    calls = []

    class Transport:
        async def stream(self, account, payload):
            calls.append(account.id)
            yield {"_event": "CHUNK_DELTA", "text": "OK"}

    app.state.service.transport = Transport()
    return app, calls


def seed(client):
    assert client.post("/admin/login", json={"password": "probe-admin"}).status_code == 200
    for account_id in ("one", "two"):
        assert client.post("/admin/accounts/import", json={"account": {
            "id": account_id, "device_id": "device-" + account_id, "web_id": "web",
            "cookies": {"sessionid": "private-cookie-" + account_id},
        }}).status_code == 200


def test_probe_requires_admin_but_no_api_key(tmp_path):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        assert client.post("/admin/accounts/one/test", json={}, headers={"x-api-key": "probe-key"}).status_code == 401
        seed(client)
        assert client.post("/admin/accounts/one/test").status_code == 200
        assert client.post("/admin/accounts/one/test", json={}, headers={"x-api-key": "probe-key", "Origin": "https://evil.example"}).status_code == 403
        assert calls == ["one"]
        assert client.get("/v1/models").status_code == 401


def test_probe_targets_exact_account_with_small_budget_and_logs(tmp_path):
    app, calls = setup(tmp_path)
    original = app.state.service.probe
    async def capture(request, **options):
        assert request.max_completion_tokens == 64
        assert request.reasoning_effort == "low"
        assert request.messages[0].content == "只回复 OK"
        return await original(request, **options)
    app.state.service.probe = capture
    with TestClient(app) as client:
        seed(client)
        client.put("/admin/config", json={"default_model": "seed-lite-7b"})
        result = client.post("/admin/accounts/two/test")
        assert result.status_code == 200
        assert result.json()["ok"] and result.json()["account_id"] == "two"
        assert result.json()["model"] == "seed-lite-7b"
        assert calls == ["two"]
        assert "private-cookie" not in result.text
        log = client.get("/admin/logs").json()["data"][0]
        assert log["account_id"] == "two" and log["status"] == "success" and log["meta"]["key_id"] == "admin-account-test"
        assert app.state.pool.find("two").in_flight == 0


def test_probe_uses_fixed_request_and_preserves_public_api_auth(tmp_path):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        seed(client)
        result = client.post("/admin/accounts/one/test", json={"model": app.state.settings.default_model, "messages": [{"role": "user", "content": "ignored"}]})
        assert result.status_code == 200
        assert result.json()["model"] == app.state.settings.default_model
        assert client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]}).status_code == 401
        assert calls == ["one"]


def test_probe_failure_never_falls_back_and_releases_capacity(tmp_path):
    app, calls = setup(tmp_path)

    class BrokenTransport:
        async def stream(self, account, payload):
            calls.append(account.id)
            raise UpstreamAuthError("expired")
            yield

    app.state.service.transport = BrokenTransport()
    with TestClient(app) as client:
        seed(client)
        assert client.post("/admin/accounts/one/test", json={}).status_code == 401
        assert calls == ["one"]
        assert app.state.pool.find("one").in_flight == 0
        assert client.post("/admin/accounts/two/test", json={}).status_code == 401
        assert calls == ["one", "two"]
        assert client.get("/admin/logs").json()["data"][0]["status"] == "error"


def test_probe_rejects_missing_disabled_and_cooling_accounts(tmp_path):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        seed(client)
        assert client.post("/admin/accounts/missing/test", json={}).status_code == 404
        assert client.patch("/admin/accounts/one", json={"enabled": False}).status_code == 200
        assert client.post("/admin/accounts/one/test", json={}).status_code == 503
        app.state.pool.mark_cooldown(app.state.pool.find("two"), "test cooldown")
        assert client.post("/admin/accounts/two/test").status_code == 503
        assert not calls


def test_probe_duplicate_is_blocked_and_capacity_recovers(tmp_path):
    import asyncio
    import httpx
    app, calls = setup(tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()
    class WaitingTransport:
        async def stream(self, account, payload):
            calls.append(account.id)
            started.set()
            await finish.wait()
            yield {"_event": "CHUNK_DELTA", "text": "OK"}
    app.state.service.transport = WaitingTransport()
    with TestClient(app) as client:
        seed(client)
        cookies = dict(client.cookies)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies=cookies) as client:
            task = asyncio.create_task(client.post("/admin/accounts/one/test"))
            await asyncio.wait_for(started.wait(), timeout=5)
            try:
                duplicate = await client.post("/admin/accounts/one/test")
                assert duplicate.status_code == 429
                assert calls == ["one"]
            finally:
                finish.set()
                await task
            assert (await client.post("/admin/accounts/one/test")).status_code == 200
    asyncio.run(run())


def test_probe_closes_after_answer_without_waiting_for_completion(tmp_path):
    app, calls = setup(tmp_path)
    closed = []

    class HeldOpenTransport:
        async def stream(self, account, payload):
            calls.append(account.id)
            try:
                yield {"_event": "CHUNK_DELTA", "_thinking": True, "text": "thinking"}
                yield {"_event": "SSE_HEARTBEAT"}
                yield {"_event": "CHUNK_DELTA", "_thinking": False, "text": "OK"}
                raise AssertionError("Account test waited beyond the first answer")
            finally:
                closed.append(account.id)

    app.state.service.transport = HeldOpenTransport()
    with TestClient(app) as client:
        seed(client)
        result = client.post("/admin/accounts/one/test")
        assert result.status_code == 200
        assert result.json()["verified"] == "first_content"
        assert calls == closed == ["one"]
        assert app.state.pool.find("one").in_flight == 0
        assert client.get("/admin/logs").json()["data"][0]["status"] == "success"


def test_probe_does_not_accept_reasoning_only_as_success(tmp_path):
    app, calls = setup(tmp_path)

    class ReasoningOnlyTransport:
        async def stream(self, account, payload):
            yield {"_event": "CHUNK_DELTA", "_thinking": True, "text": "thinking"}
            yield {"_event": "SSE_HEARTBEAT"}

    app.state.service.transport = ReasoningOnlyTransport()
    with TestClient(app) as client:
        seed(client)
        result = client.post("/admin/accounts/one/test")
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "empty_response"
        assert app.state.pool.find("one").in_flight == 0
