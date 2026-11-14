from __future__ import annotations

from doubao_work2api.logs import RequestLog


def test_request_log_keeps_metadata_not_prompt() -> None:
    log = RequestLog(capacity=2)
    entry = log.start(model="deepseek-v4.1-flash", stream=False)
    log.account(entry, "account-1")
    log.success(entry)
    snapshot = log.snapshot()
    assert snapshot[0]["status"] == "success"
    assert snapshot[0]["account_id"] == "account-1"
    assert "prompt" not in snapshot[0]
    assert "content" not in snapshot[0]


def test_request_log_is_bounded() -> None:
    log = RequestLog(capacity=2)
    for index in range(4):
        entry = log.start(model=f"m{index}", stream=False)
        log.failure(entry, "failure")
    snapshot = log.snapshot()
    assert len(snapshot) == 2
    assert [item["model"] for item in snapshot] == ["m3", "m2"]


def test_request_metrics_distinguish_unknown_from_zero():
    log = RequestLog()
    entry = log.start(model="example", stream=True)
    before = entry.public_dict()
    assert before["ttft_ms"] is None
    assert before["total_tokens"] is None
    assert before["tokens_per_second"] is None
    assert before["cached_tokens"] is None and before["credits"] is None
    entry.started_at = 100
    entry.first_token_at = 104
    entry.finished_at = 109
    entry.input_tokens = 1000
    entry.output_tokens = 200
    entry.reasoning_tokens = 0
    entry.usage_estimated = True
    after = entry.public_dict()
    assert after["ttft_ms"] == 4000
    assert after["tokens_per_second"] == 40
    assert after["total_tokens"] == 1200
    assert after["reasoning_tokens"] == 0
    assert after["usage_estimated"] is True
    assert after["credits"] is None and after["estimated_price"] is None
