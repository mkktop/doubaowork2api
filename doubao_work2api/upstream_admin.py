"""Account-scoped official metadata requests; no browser or external relay."""
from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from .accounts import Account
from .errors import UpstreamError
from .transport import DirectTransport, USER_AGENT, classify_status, classify_body_error

ROUTES = {
    'launch': '/alice/user/launch',
    'models': '/alice/slot/action_bar_v3/brief_list',
    'quota': '/alice/commerce/sale/subscription/quota/summary/',
    'overview': '/alice/commerce/sale/subscription/overview/',
    'detail': '/alice/ug/campaign/v3/detail/',
    'campaigns': '/alice/ug/campaign/list/',
    'claim': '/alice/ug/campaign/item/reward/claim/',
    'checkin': '/alice/ug/campaign/landing/checkin',
}


async def metadata_request(transport: DirectTransport, account: Account,
                           route: str, body: dict | None = None) -> dict:
    client = await transport._client_for(account)
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
               'User-Agent': account.user_agent or USER_AGENT,
               'Origin': transport.settings.upstream_base,
               'Referer': transport.settings.upstream_base + '/chat/', 'agw-js-conv': 'str'}
    csrf = account.cookies.get('passport_csrf_token') or account.cookies.get('passport_csrf_token_default')
    if csrf:
        headers['x-tt-passport-csrf-token'] = csrf
    try:
        async with client.stream('POST', transport.settings.upstream_base + ROUTES[route] + '?' + transport._query(account),
                                 headers=headers, json=body or {}, follow_redirects=False, timeout=25) as response:
            chunks = []
            length = 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > 4 * 1024 * 1024:
                    raise UpstreamError('上游元数据响应过大。', code='invalid_upstream_metadata')
                chunks.append(chunk)
            classify_status(response.status_code, '')
            if response.is_redirect:
                raise UpstreamError('上游元数据要求重新登录。', code='metadata_redirect')
            try:
                result = json.loads(b''.join(chunks))
            except (ValueError, UnicodeError):
                raise UpstreamError('上游元数据格式已变化。', code='invalid_upstream_metadata') from None
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise UpstreamError('未能连接豆包元数据接口，请稍后重试。', code='metadata_not_sent') from None
    except httpx.RequestError:
        raise UpstreamError('连接豆包元数据接口失败，请稍后重试。', code='metadata_network_error') from None
    if not isinstance(result, dict):
        raise UpstreamError('上游元数据格式已变化。', code='invalid_upstream_metadata')
    # Do not expose raw upstream error text, which can include private metadata.
    classify_body_error({key: result[key] for key in ('error_code', 'code', 'status_code') if key in result})
    return result


def number(value: Any) -> float | None:
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def quota_windows(payload: dict) -> dict:
    """Retain independent allowance windows; never add 5h/7d/month limits."""
    result = []
    seen = set()

    def walk(value: Any, group: str = '', feature: str = '', depth: int = 0):
        if depth > 20:
            return
        if isinstance(value, dict):
            group = str(value.get('feature_group_name') or group)[:120]
            feature = str(value.get('feature_group') or feature)[:160]
            for row in value.get('window_limits', []) if isinstance(value.get('window_limits'), list) else []:
                if not isinstance(row, dict):
                    continue
                total, used = number(row.get('total_amount')), number(row.get('used_amount'))
                kind = str(row.get('window_type') or '')
                end = number(row.get('end_time'))
                key = (group, feature, kind, total, used, end)
                if key in seen:
                    continue
                seen.add(key)
                result.append({'name': group or '官方额度', 'feature': feature, 'window_type': kind,
                    'total': total, 'used': used,
                    'remaining': max(0, total-used) if total is not None and used is not None else None,
                    'percent': number(row.get('used_percent')), 'end_time': end,
                    'unlimited': row.get('unlimited') is True})
            for key, child in value.items():
                if key != 'window_limits':
                    walk(child, group, feature, depth+1)
        elif isinstance(value, list):
            for child in value[:1000]:
                walk(child, group, feature, depth+1)
    walk(payload.get('data', {}))
    return {'windows': result, 'updated_at': time.time(), 'source': 'official', 'request_credits': None}


def daily_candidates(payload: dict, group: str) -> list[dict]:
    """Only explicitly identified daily check-in tasks may be claimed."""
    found = {}

    def walk(value: Any, inherited: str, depth: int = 0):
        if depth > 12 or len(found) > 30:
            return
        if isinstance(value, dict):
            current = value.get('campaign_group_code') or inherited
            daily = value.get('daily_checkin')
            if isinstance(daily, dict) and isinstance(daily.get('checked_in_today'), bool) and value.get('campaign_code'):
                code = str(value['campaign_code'])
                status = number(value.get('task_status'))
                direct = number(value.get('reward_mode')) == 2
                claim = None
                if status == 2 or (direct and status in (1, 4)):
                    if direct:
                        claim = {'task_id': '', 'campaign_group_code': current, 'campaign_item_code': code}
                    elif value.get('task_id'):
                        claim = {'task_id': str(value['task_id'])}
                found[(current, code)] = {'group': current, 'code': code, 'checked': daily['checked_in_today'], 'claim': claim}
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child, current, depth+1)
        elif isinstance(value, list):
            for child in value[:1000]:
                walk(child, inherited, depth+1)
    walk(payload.get('data', {}), group)
    return list(found.values())
