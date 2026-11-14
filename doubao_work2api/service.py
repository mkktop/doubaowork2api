from __future__ import annotations

import asyncio
import json
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

from .accounts import Account, AccountPool
from .catalog import DEFAULT_MODEL_ID, REASONING_LEVELS, request_model
from .affinity import AccountAffinity
from .errors import (
    NoAvailableAccountError,
    UpstreamAuthError,
    UpstreamChallengeError,
    UpstreamError,
    UpstreamRateLimitError,
)
from .logs import RequestLog, RequestLogEntry
from .models import ChatCompletionRequest
from .models import ChatMessage
from .usage import official_usage
from .native_sessions import NativeSessions
from .routing import RoutingPolicy
from .protocol import (
    chunk_payload,
    completion_id,
    completion_payload,
    UpstreamTextParser,
    sse_data,
    upstream_payload,
    validate_request,
    resolve_reasoning_effort,
)
from .transport import DirectTransport
from .tool_bridge import parse_tool_response
from .image_inputs import load_image, ImageInput
from .image_upload import upload_image, ImageUploadError
from .request_contract import enforce_contract, validate_tool_arguments, estimate_tokens
from .output_budget import OutputBudget


class ChatService:
    def __init__(
        self,
        pool: AccountPool,
        transport: DirectTransport,
        *,
        model_key: str,
        app_id: str,
        workspace_root: Path,
        default_model: str = DEFAULT_MODEL_ID,
        request_log: RequestLog | None = None,
    ) -> None:
        self.pool = pool
        self.transport = transport
        self.model_key = model_key
        self.app_id = app_id
        self.workspace_root = workspace_root
        self.default_model = default_model
        self.request_log = request_log
        self.catalog = None
        self.affinity = AccountAffinity()
        self.native_sessions = NativeSessions()
        self.routing = RoutingPolicy()

    def prepare(self, request):
        if self.catalog is not None:
            request = self.catalog.bind(request)
        return validate_request(request)

    async def probe(
        self, request: ChatCompletionRequest, *, account_id: str,
    ) -> dict[str, Any]:
        """Verify one account from its first answer text, then close the stream."""
        request = self.prepare(request)
        target = self.pool.find(account_id)
        recovery = bool(target and target.enabled and target.failure_type == 'needs_verification')
        if recovery:
            request._verification_probe_id = account_id
        entry = self._start_log(request, "admin-account-test")
        started_at = time.time()
        received = False
        try:
            async with aclosing(self._fragments_with_retry(request, entry, {account_id})) as fragments:
                async for kind, text in fragments:
                    if kind != "content" or not text.strip():
                        continue
                    received = True
                    if recovery:
                        continue
                    account = self.pool.find(account_id)
                    if account is not None:
                        self.pool.mark_success(account, started_at=started_at)
                    self._log_success(entry)
                    return {"model": request.normalized_model, "verified": "first_content"}
            if received and recovery:
                self._log_success(entry)
                return {'model':request.normalized_model,'verified':'complete_recovery'}
            raise UpstreamError("Doubao returned no answer text.", code="empty_response")
        except Exception as exc:
            self._log_failure(entry, str(exc))
            raise
        finally:
            self._log_interrupted(entry)

    async def complete(
        self, request: ChatCompletionRequest, *,
        allowed_ids: set[str] | None = None, key_id: str = "",
    ) -> dict[str, Any]:
        request._affinity_scope = key_id
        request = self.prepare(request)
        log_entry = self._start_log(request, key_id)
        content: list[str] = []
        reasoning: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        finish_reason = "stop"
        converted = self._converted_fragments(request, log_entry, allowed_ids)
        try:
            async for kind, text in converted:
                if kind == "content":
                    content.append(text)
                elif kind == "tool_calls":
                    tool_calls.extend(text)
                elif kind == "finish":
                    finish_reason = text
                else:
                    reasoning.append(text)
            answer = "".join(content)
            if not answer and not tool_calls and finish_reason != "length":
                raise UpstreamError("Doubao returned an empty response.")
            output_text = answer + "".join(reasoning)
            if tool_calls:
                output_text += json.dumps([call["function"] for call in tool_calls], ensure_ascii=False)
            from .context import estimate_request_tokens
            result = completion_payload(
                request_id=completion_id(),
                model=request.normalized_model,
                content=answer,
                reasoning="".join(reasoning),
                prompt_tokens=estimate_request_tokens(request),
                completion_tokens=estimate_tokens(output_text),
            )
            result["choices"][0]["finish_reason"] = finish_reason
            result["usage"]["is_estimated"] = True
            if request._official_usage:
                result['usage'] = request._official_usage
            result["gateway_context"] = getattr(request, "gateway_context", {})
            result['gateway_context']['native_session_reused'] = request._native_reused
            if tool_calls:
                result["choices"][0]["message"]["content"] = answer or None
                result["choices"][0]["message"]["tool_calls"] = tool_calls
                result["choices"][0]["finish_reason"] = "tool_calls"
            self._log_usage(log_entry, request, output_text, "".join(reasoning))
            self._log_success(log_entry)
            return result
        except Exception as exc:
            self._log_failure(log_entry, str(exc))
            raise
        finally:
            await converted.aclose()
            self._log_interrupted(log_entry)

    async def stream(
        self, request: ChatCompletionRequest, *,
        allowed_ids: set[str] | None = None, key_id: str = "",
    ) -> AsyncIterator[str]:
        request._affinity_scope = key_id
        request = self.prepare(request)
        log_entry = self._start_log(request, key_id)
        request_id = completion_id()
        has_tools = False
        finish_reason = "stop"
        visible_output: list[str] = []
        reasoning_output: list[str] = []
        converted = self._converted_fragments(request, log_entry, allowed_ids)
        try:
            yield sse_data(
                chunk_payload(
                    request_id=request_id,
                    model=request.normalized_model,
                    role="assistant",
                )
            )
            async for kind, text in converted:
                if kind == "tool_calls":
                    has_tools = True
                    visible_output.append(json.dumps([call["function"] for call in text], ensure_ascii=False))
                    chunk = chunk_payload(request_id=request_id, model=request.normalized_model)
                    chunk["choices"][0]["delta"] = {
                        "tool_calls": [dict(call, index=index) for index, call in enumerate(text)]
                    }
                    yield sse_data(chunk)
                    continue
                if kind == "finish":
                    finish_reason = text
                    continue
                visible_output.append(text)
                if kind == "reasoning":
                    reasoning_output.append(text)
                field = "content" if kind == "content" else "reasoning"
                yield sse_data(
                    chunk_payload(
                        request_id=request_id,
                        model=request.normalized_model,
                        **{field: text},
                    )
                )
            yield sse_data(
                chunk_payload(
                    request_id=request_id,
                    model=request.normalized_model,
                    finish_reason="tool_calls" if has_tools else finish_reason,
                )
            )
            if request.stream_options and request.stream_options.get("include_usage"):
                from .context import estimate_request_tokens
                prompt_count = estimate_request_tokens(request)
                completion_count = estimate_tokens("".join(visible_output))
                yield sse_data({
                    "id": request_id, "object": "chat.completion.chunk", "model": request.normalized_model,
                    "created": int(time.time()), "choices": [],
                    "usage": request._official_usage or {"prompt_tokens": prompt_count, "completion_tokens": completion_count, "total_tokens": prompt_count + completion_count, "is_estimated": True},
                })
            self._log_usage(log_entry, request, "".join(visible_output), "".join(reasoning_output))
            yield "data: [DONE]\n\n"
            self._log_success(log_entry)
        except Exception as exc:
            self._log_failure(log_entry, str(exc))
            raise
        finally:
            await converted.aclose()
            self._log_interrupted(log_entry)

    async def _converted_fragments(
        self,
        request: ChatCompletionRequest,
        log_entry: RequestLogEntry | None,
        allowed_ids: set[str] | None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Use one response conversion path for JSON and SSE clients.

        Tool responses must be complete before validation. Ordinary text remains
        incremental; tool-enabled requests buffer only their answer text.
        """
        tools = getattr(request, "tools", None)
        tool_choice = getattr(request, "tool_choice", None)
        bridge_enabled = bool(tools) and tool_choice != "none"
        structured = bridge_enabled or bool(request.response_format and request.response_format.get("type") != "text")
        budget = OutputBudget(request)
        buffered: list[str] = []
        has_answer = False
        async with aclosing(self._fragments_with_retry(request, log_entry, allowed_ids)) as fragments:
            async for kind, text in fragments:
                if kind == "content" and text:
                    has_answer = True
                if structured and kind == "content":
                    buffered.append(text)
                else:
                    visible = budget.feed(kind, text)
                    if visible:
                        yield kind, visible
                    if budget.finished:
                        yield "finish", budget.finish_reason
                        return
        calls: list[dict[str, Any]] = []
        if structured:
            raw = "".join(buffered)
            if bridge_enabled:
                content, calls = parse_tool_response(raw, tools, tool_choice, request.parallel_tool_calls)
                validate_tool_arguments(calls, tools)
            else:
                content = raw
            if not calls:
                content = enforce_contract(content or "", request)
            if content:
                visible = budget.feed("content", content) + budget.flush()
                if visible:
                    if request.response_format and budget.finish_reason != "length":
                        visible = enforce_contract(visible, request)
                    yield "content", visible
            if calls:
                call_text = json.dumps([call["function"] for call in calls], ensure_ascii=False)
                if budget.remaining is not None and estimate_tokens(call_text) > budget.remaining:
                    yield "finish", "length"
                    return
                if not budget.finished:
                    yield "tool_calls", calls
            has_answer = bool(content or calls)
        else:
            tail = budget.flush()
            if tail:
                yield "content", tail
        if not has_answer:
            raise UpstreamError("Doubao returned an empty response.", code="empty_response")
        yield "finish", "tool_calls" if calls and not budget.finished else budget.finish_reason

    async def _fragments_with_retry(
        self,
        request: ChatCompletionRequest,
        log_entry: RequestLogEntry | None,
        allowed_ids: set[str] | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        sources = [
            part["image_url"]["url"] for message in request.messages if isinstance(message.content, list)
            for part in message.content if part.get("type") == "image_url"
        ]
        if sources and not any(account.available and (allowed_ids is None or account.id in allowed_ids) for account in self.pool.accounts):
            raise NoAvailableAccountError("No usable Doubao account.")
        images: list[ImageInput] = []
        total_bytes = 0
        for source in sources:
            image = await load_image(source)
            total_bytes += len(image.data)
            if total_bytes > 20 * 1024 * 1024:
                from .protocol import UnsupportedContentError
                raise UnsupportedContentError("Total image data exceeds the 20 MiB request limit.")
            images.append(image)
        attempted: set[str] = set()
        affinity_key = self.affinity.key(request)
        if self.catalog is not None and self.pool.accounts:
            catalog_ids = self.catalog.eligible_ids(request.normalized_model, {account.id for account in self.pool.accounts})
            if catalog_ids is not None:
                allowed_ids = catalog_ids if allowed_ids is None else allowed_ids & catalog_ids
                if not allowed_ids:
                    raise NoAvailableAccountError('当前允许的账号尚未在官方目录中开通此模型，请刷新目录或使用有权限的账号。', code='model_no_available_account')
        last_error: Exception | None = None
        large = self.routing.is_large(request)
        if large:
            eligible = {account.id for account in self.pool.accounts if not self.routing.blocked(account.id)}
            allowed_ids = eligible if allowed_ids is None else allowed_ids & eligible
            if not allowed_ids:
                from .errors import AccountBusyError
                raise AccountBusyError('大请求预算暂时用尽，请稍后重试或调整转发策略。', code='large_request_budget')
        for _ in range(max(1, len(self.pool.accounts))):
            try:
                account = await self.pool.acquire(allowed_ids=allowed_ids, preferred_id=self.affinity.get(affinity_key), verification_id=request._verification_probe_id or None)
            except NoAvailableAccountError:
                if last_error:
                    raise last_error
                raise
            if account.id in attempted:
                await self.pool.release(account)
                break
            if large and self.routing.blocked(account.id):
                await self.pool.release(account)
                allowed_ids.discard(account.id)
                continue
            attempted.add(account.id)
            if large:
                self.routing.admit(account.id)
            request._official_usage = None
            if self.request_log is not None and log_entry is not None:
                self.request_log.account(log_entry, account.id)
            emitted = False
            started_at = time.time()
            try:
                async with aclosing(self._fragments(account, request, images)) as fragments:
                    async for fragment in fragments:
                        emitted = True
                        if log_entry is not None and not log_entry.first_token_at and fragment[0] in {"content", "reasoning"} and fragment[1]:
                            log_entry.first_token_at = time.time()
                        yield fragment
                if not emitted:
                    raise UpstreamError(
                        "Doubao returned an empty response.", code="empty_response"
                    )
                self.pool.mark_success(account, started_at=started_at)
                self.affinity.bind(affinity_key, account.id)
                return
            except UpstreamAuthError as exc:
                self.affinity.forget(affinity_key)
                if emitted:
                    raise
                account.last_error = exc.message
                account.error_code = exc.code
                account.last_error_at = time.time()
                try:
                    await self.pool.set_enabled(account.id, False)
                except Exception:
                    account.enabled = False
                    account.last_error = exc.message
                last_error = exc
            except UpstreamChallengeError as exc:
                self.affinity.forget(affinity_key)
                if emitted:
                    raise
                account.last_error = exc.message
                account.error_code = exc.code
                account.last_error_at = time.time()
                try:
                    await self.pool.set_enabled(account.id, account.enabled)
                except Exception:
                    account.enabled = False
                    account.last_error = exc.message
                last_error = exc
            except UpstreamRateLimitError as exc:
                self.affinity.forget(affinity_key)
                if emitted:
                    raise
                self.pool.mark_cooldown(account, exc.message, error_code=exc.code)
                last_error = exc
            except ImageUploadError:
                # A storage upload failure does not invalidate this account and
                # must not fan out the same private images across the pool.
                raise
            except (UpstreamError, OSError) as exc:
                self.affinity.forget(affinity_key)
                if emitted:
                    raise
                self.pool.mark_cooldown(account, str(exc), error_code=getattr(exc, "code", "network_error"))
                last_error = exc
            finally:
                await self.pool.release(account)
        if last_error:
            raise last_error
        raise NoAvailableAccountError("No usable Doubao account.")

    async def _fragments(
        self, account: Account, request: ChatCompletionRequest,
        images: list[ImageInput] | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        # Each request has its own uploads, workspace, IDs and parser. The pool
        # reserves capacity; a whole-request account lock would ignore that cap.
        uploaded = [
            await upload_image(self.transport, account, image, filename=f"image-{index}.{image.filename.rsplit('.', 1)[-1]}")
            for index, image in enumerate(images or [], 1)
        ]
        workspace = self._workspace(account)
        payload = upstream_payload(
            request,
            model_key=request_model(request).upstream_key,
            workspace=workspace,
            device_name=account.device_name or socket.gethostname().upper(),
            app_id=account.app_id or self.app_id,
            device_id=account.device_id,
            images=uploaded,
        )
        if self.catalog is not None and account.id in self.catalog.bot_ids:
            payload['client_meta']['bot_id'] = self.catalog.bot_ids[account.id]
        parser = UpstreamTextParser()
        affinity_key = self.affinity.key(request)
        signature = json.dumps([request.normalized_model,resolve_reasoning_effort(request),
            request.response_format,getattr(request,'verbosity',None),request.stop,
            request.max_tokens,request.max_completion_tokens],sort_keys=True)
        eligible = self.routing.config['native_sessions'] and not request.tools and not images
        entry, reserved = self.native_sessions.reserve(affinity_key,account,request,signature) if eligible else (None,False)
        request._native_reused = bool(entry)
        if entry:
            from .protocol import render_prompt
            tail = request.messages[entry['count']:]
            payload['client_meta'].update(conversation_id=entry['conversation_id'], last_section_id=entry['section_id'])
            payload['option']['need_create_conversation'] = False
            payload['messages'][0]['content_block'][0]['content']['text_block']['text'] = render_prompt(tail)
            payload['client_meta']['local_conversation_id'] = ''
        conversation_id = entry['conversation_id'] if entry else ''
        section_id = entry['section_id'] if entry else ''
        collected = {'content':[], 'reasoning':[]}
        complete = False
        try:
            async with aclosing(self.transport.stream(account, payload)) as events:
                async for event in events:
                    billing = official_usage(event)
                    if billing:
                        request._official_usage = billing
                    ack = event.get('ack_client_meta')
                    if isinstance(ack,dict):
                        conversation_id = ack.get('conversation_id') or conversation_id
                        section_id = ack.get('section_id') or section_id
                    if event.get('_event') == 'SSE_REPLY_END' and event.get('end_type') in {3,'3'}:
                        complete = True
                    for kind, text in parser.feed(event):
                        if text:
                            collected[kind].append(text)
                            yield kind, text
            if reserved and complete and conversation_id:
                assistant = ChatMessage(role='assistant',content=''.join(collected['content']))
                if collected['reasoning']:
                    assistant.reasoning_content = ''.join(collected['reasoning'])
                self.native_sessions.save(affinity_key,account,signature,
                    request.messages+[assistant],conversation_id,section_id)
        finally:
            if reserved:
                self.native_sessions.release(affinity_key,invalidate=not complete)

    def _workspace(self, account: Account) -> Path:
        safe_id = "".join(
            character
            for character in account.id
            if character.isalnum() or character in {"-", "_"}
        )[:40] or "account"
        workspace = (
            self.workspace_root
            / time.strftime("%Y-%m-%d")
            / f"{safe_id}-{uuid.uuid4().hex[:8]}"
        )
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _start_log(
        self, request: ChatCompletionRequest, key_id: str = ""
    ) -> RequestLogEntry | None:
        if self.request_log is None:
            return None
        return self.request_log.start(
            model=request.normalized_model,
            stream=request.stream,
            meta={"messages": len(request.messages), "key_id": key_id,
                  "reasoning_effort": REASONING_LEVELS[resolve_reasoning_effort(request) - 3]},
        )

    def _log_usage(self, entry: RequestLogEntry | None, request: ChatCompletionRequest,
                   output: str, reasoning: str) -> None:
        if entry is None:
            return
        entry.meta['native_reused'] = request._native_reused
        from .context import estimate_request_tokens
        entry.input_tokens = estimate_request_tokens(request)
        entry.output_tokens = estimate_tokens(output)
        entry.reasoning_tokens = estimate_tokens(reasoning) if reasoning else 0
        entry.usage_estimated = True
        if request._official_usage:
            value = request._official_usage
            entry.input_tokens, entry.output_tokens = value['prompt_tokens'],value['completion_tokens']
            entry.total_tokens = value['total_tokens']
            entry.reasoning_tokens = value.get('completion_tokens_details',{}).get('reasoning_tokens')
            entry.cached_tokens = value.get('prompt_tokens_details',{}).get('cached_tokens')
            entry.usage_estimated = False
            entry.meta['usage_source'] = 'official'

    def _log_success(self, entry: RequestLogEntry | None) -> None:
        if self.request_log is not None and entry is not None:
            self.request_log.success(entry)

    def _log_failure(
        self, entry: RequestLogEntry | None, error: str
    ) -> None:
        if self.request_log is not None and entry is not None:
            self.request_log.failure(entry, error)

    def _log_interrupted(self, entry: RequestLogEntry | None) -> None:
        if (
            self.request_log is not None
            and entry is not None
            and entry.status == "running"
        ):
            self.request_log.failure(entry, "request ended before completion")
