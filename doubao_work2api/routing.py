"""Local routing settings and bounded large-request admission windows."""
from __future__ import annotations

import json
import time
from collections import deque

from .private_json import save_private_json


class RoutingPolicy:
    DEFAULTS = {'native_sessions':True, 'large_request_bytes':131072,
                'large_request_budget':6, 'large_request_window':120}

    def __init__(self, data_dir=None):
        self.path = data_dir / 'routing.json' if data_dir else None
        self.config = dict(self.DEFAULTS)
        self.windows = {}
        if self.path and self.path.exists():
            self.config = self.validate(json.loads(self.path.read_text(encoding='utf-8')))

    def validate(self, patch):
        if not isinstance(patch, dict) or patch.keys() - self.DEFAULTS.keys():
            raise ValueError('转发策略字段无效。')
        result = {**self.config, **patch}
        if type(result['native_sessions']) is not bool:
            raise ValueError('原生会话开关必须为布尔值。')
        for field, minimum, maximum in [('large_request_bytes',0,16777216),
                ('large_request_budget',0,1000),('large_request_window',1,86400)]:
            if type(result[field]) is not int or not minimum <= result[field] <= maximum:
                raise ValueError(f'{field} 超出允许范围。')
        return result

    def configure(self, patch):
        result = self.validate(patch)
        if self.path:
            save_private_json(self.path, result)
        self.config = result
        self.windows.clear()
        return dict(result)

    def is_large(self, request):
        threshold = self.config['large_request_bytes']
        return bool(threshold and self.config['large_request_budget'] and
            len(request.model_dump_json(exclude_none=True).encode()) >= threshold)

    def blocked(self, account_id):
        now = time.monotonic()
        window = self.windows.setdefault(account_id, deque())
        while window and window[0] <= now - self.config['large_request_window']:
            window.popleft()
        return bool(self.config['large_request_budget'] and len(window) >= self.config['large_request_budget'])

    def admit(self, account_id):
        self.windows.setdefault(account_id, deque()).append(time.monotonic())
        if len(self.windows) > 10000:
            cutoff = time.monotonic()-self.config['large_request_window']
            self.windows = {key:value for key,value in self.windows.items() if value and value[-1] > cutoff}
