from fastapi.testclient import TestClient

from doubao_work2api.app import create_app
from doubao_work2api.catalog import DEFAULT_MODEL_ID, MODEL_CATALOG, normalize_model
from doubao_work2api.config import Settings
from doubao_work2api.models import ChatMessage
from doubao_work2api.protocol import completion_payload
from doubao_work2api.response_store import ResponseStore
from doubao_work2api.security import SecurityStore


def test_all_public_model_ids_match_native_keys():
    assert all(spec.id == spec.upstream_key for spec in MODEL_CATALOG)
    for alias in ("deepseek-v4.1-flash", "deepseek-v4-1-flash", "deepseek-v4.1-flash-261017", DEFAULT_MODEL_ID):
        assert normalize_model(alias) == DEFAULT_MODEL_ID


def test_legacy_default_key_whitelist_and_chat_responses_keep_working(tmp_path):
    settings = Settings(api_key="synthetic-native-test", admin_password="synthetic-admin", default_model="deepseek-v4.1-flash", data_dir=tmp_path / "data", accounts_dir=tmp_path / "accounts")
    security = SecurityStore(settings)
    key = security.list_keys()[0]
    security.update_key(key["id"], {"models": ["deepseek-v4.1-flash"]})
    app = create_app(settings)
    seen = []
    async def complete(request, **options):
        seen.append(request.normalized_model)
        return completion_payload(request_id="chatcmpl_test", model=request.normalized_model, content="OK")
    app.state.service.complete = complete
    headers = {"Authorization": "Bearer synthetic-native-test"}
    with TestClient(app) as client:
        assert client.post("/admin/login", json={"password": "synthetic-admin"}).status_code == 200
        config = client.get("/admin/config").json()
        assert config["default_model"] == config["runtime"]["default_model"] == DEFAULT_MODEL_ID
        assert client.get("/admin/keys").json()["data"][0]["models"] == [DEFAULT_MODEL_ID]
        listed = client.get("/v1/models", headers=headers).json()["data"]
        assert [item["id"] for item in listed] == [DEFAULT_MODEL_ID]
        for model in (None, "deepseek-v4.1-flash", DEFAULT_MODEL_ID):
            chat = {"messages": [{"role": "user", "content": "synthetic"}]}
            responses = {"input": "synthetic", "store": False}
            if model is not None:
                chat["model"] = responses["model"] = model
            for path, payload in (("/v1/chat/completions", chat), ("/v1/responses", responses)):
                result = client.post(path, headers=headers, json=payload)
                assert result.status_code == 200, result.text
                assert result.json()["model"] == DEFAULT_MODEL_ID
        denied = client.post("/v1/responses", headers=headers, json={"model": "seed-lite-7b", "input": "synthetic"})
        assert denied.status_code == 403
    assert seen == [DEFAULT_MODEL_ID] * 6
    # Present canonical names without rewriting the authenticated Key's policy,
    # which keeps previously issued replay tokens bound to the same owner.
    assert app.state.security.authenticate_api(settings.api_key)["models"] == ["deepseek-v4.1-flash"]


def test_replay_tokens_bound_to_legacy_alias_accept_native_model(tmp_path):
    store = ResponseStore(tmp_path)
    history = [ChatMessage(role="user", content="synthetic")]
    token = store.encode("owner", "deepseek-v4.1-flash", history, kind="compaction")
    decoded = store.decode("owner", DEFAULT_MODEL_ID, {"type": "compaction", "encrypted_content": token})
    assert decoded[0].content == "synthetic"
