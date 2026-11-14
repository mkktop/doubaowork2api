"""Bounded native conversation reuse with exact, scoped history validation."""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict


def history_hash(messages):
    documents = [message.model_dump(exclude_none=True) for message in messages]
    for document in documents:
        if document.get('reasoning') == document.get('reasoning_content'):
            document.pop('reasoning',None)
    return hashlib.sha256(json.dumps(documents,
        ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()).hexdigest()


class NativeSessions:
    def __init__(self, ttl=1800, limit=512):
        self.ttl, self.limit = ttl, limit
        self.entries = OrderedDict()
        self.active = set()

    def reserve(self, key, account, request, signature):
        if not key or key in self.active:
            return None, False
        self.active.add(key)
        entry = self.entries.get(key)
        revision = hashlib.sha256(json.dumps([account.cookies, account.device_id, account.web_id],
            sort_keys=True).encode()).hexdigest()
        if entry and entry['expires'] > time.monotonic() and entry['account'] == account.id and entry['revision'] == revision and entry['signature'] == signature:
            count = entry['count']
            if len(request.messages) > count and history_hash(request.messages[:count]) == entry['history']:
                return entry, True
        self.entries.pop(key, None)
        return None, True

    def save(self, key, account, signature, messages, conversation_id, section_id=''):
        if not key or not conversation_id:
            return
        revision = hashlib.sha256(json.dumps([account.cookies,account.device_id,account.web_id],sort_keys=True).encode()).hexdigest()
        self.entries[key] = {'account':account.id, 'revision':revision, 'signature':signature,
            'history':history_hash(messages), 'count':len(messages), 'conversation_id':conversation_id,
            'section_id':section_id, 'expires':time.monotonic()+self.ttl}
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)

    def release(self, key, *, invalidate=False):
        self.active.discard(key)
        if invalidate:
            self.entries.pop(key, None)
