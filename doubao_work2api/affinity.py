"""Bounded account affinity. Never stores prompts or native conversations."""
from __future__ import annotations
import hashlib
import json
import time
from collections import OrderedDict


class AccountAffinity:
    def __init__(self, ttl=7200, limit=2048):
        self.ttl, self.limit = ttl, limit
        self.entries = OrderedDict()

    def key(self, request):
        scope = request._affinity_scope
        if not scope:
            return ''
        hint = request._conversation_hint or request.session_id or request.conversation_id
        metadata = getattr(request, 'metadata', None)
        if not hint and isinstance(metadata, dict):
            hint = metadata.get('conversation_id') or metadata.get('session_id')
        if not hint:
            first = next((message.content for message in request.messages if message.role == 'user'), '')
            hint = json.dumps([request.user, first], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(json.dumps([scope, request.normalized_model, hint], ensure_ascii=False).encode()).hexdigest()

    def get(self, key):
        item = self.entries.get(key)
        if not item:
            return None
        account_id, expiry = item
        if expiry <= time.monotonic():
            self.entries.pop(key, None)
            return None
        self.entries.move_to_end(key)
        return account_id

    def bind(self, key, account_id):
        if not key:
            return
        self.entries[key] = (account_id, time.monotonic()+self.ttl)
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)

    def forget(self, key):
        self.entries.pop(key, None)
