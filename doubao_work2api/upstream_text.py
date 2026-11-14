"""Message-scoped reconciliation of upstream text snapshots and deltas."""
from __future__ import annotations

import json

from .errors import UpstreamError


class UpstreamTextParser:
    def __init__(self):
        self.message = 'default'
        self.active = None
        self.reasoning_ids = set()
        self.text_kinds = {}
        self.views = {}
        self.emitted = {}
        self.last_text_kind = 'content'

    def _select(self, identity):
        if identity is not None and str(identity) != self.message:
            self.message, self.active = str(identity), None
            self.reasoning_ids.clear()
            self.last_text_kind = 'content'

    def _text(self, key, kind, text, source, append):
        if not isinstance(text, str) or not text:
            return
        previous = self.emitted.get((key,kind), '')
        source_key = (key,kind,source)
        prior = self.views.get(source_key)
        if append:
            if prior is None:
                prior = '' if previous.startswith(text) else previous
            value = prior + text
        else:
            value = text
        self.views[source_key] = value
        if value.startswith(previous):
            delta = value[len(previous):]
            self.emitted[key,kind] = value
            if delta:
                yield kind, delta
        elif not previous.startswith(value):
            raise UpstreamError('上游修订了已输出文本，无法保真追加；请重新请求。', code='upstream_text_revision')

    def _blocks(self, blocks, append, source):
        from .protocol import _text_block_kind
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if isinstance(block, dict) and block.get('block_type') == 10040 and isinstance(block.get('block_id'),str):
                self.reasoning_ids.add(block['block_id'])
        for block in blocks:
            if not isinstance(block,dict) or block.get('block_type') != 10000:
                continue
            info = block.get('content',{}).get('text_block',{}) if isinstance(block.get('content'),dict) else {}
            if not isinstance(info,dict):
                continue
            key = self.message, str(block.get('block_id') or 'default')
            kind = _text_block_kind(block,self.reasoning_ids)
            if any(isinstance(info.get(name),str) and 'deep_think' in info[name].lower() for name in ('icon_url','icon_url_dark')):
                kind = 'reasoning'
            self.active, self.last_text_kind = key, kind
            self.text_kinds[key] = kind
            yield from self._text(key,kind,info.get('text'),source,append)

    def feed(self, payload):
        from .protocol import extract_text_fragments
        event = payload.get('_event','')
        meta = payload.get('meta')
        self._select(payload.get('message_id') or (meta.get('message_id') if isinstance(meta,dict) else None))
        if event == 'FULL_MSG_NOTIFY':
            message = payload.get('message',{})
            if not isinstance(message,dict) or str(message.get('user_type',2)) != '2':
                return
            self._select(message.get('message_id'))
            blocks = message.get('content_block')
            if blocks is None and isinstance(message.get('content'),str):
                try:
                    blocks = json.loads(message['content'])
                except ValueError:
                    raise UpstreamError('上游完整消息不是有效 JSON。',code='invalid_upstream_snapshot') from None
            yield from self._blocks(blocks,False,'blocks')
            return
        if event == 'CHUNK_DELTA' and isinstance(payload.get('text'),str):
            key = (self.message,str(payload['block_id'])) if payload.get('block_id') else self.active or (self.message,'default')
            kind = 'reasoning' if payload.get('_thinking') is True else 'content' if payload.get('_thinking') is False else self.text_kinds.get(key,self.last_text_kind)
            yield from self._text(key,kind,payload['text'],'delta',True)
            return
        handled = False
        content = payload.get('content')
        if isinstance(content,dict) and isinstance(content.get('content_block'),list):
            handled = True
            yield from self._blocks(content['content_block'],False,'blocks')
        for patch in payload.get('patch_op',[]) if isinstance(payload.get('patch_op',[]),list) else []:
            if not isinstance(patch,dict) or not isinstance(patch.get('patch_value'),dict):
                continue
            blocks = patch['patch_value'].get('content_block')
            if isinstance(blocks,list):
                handled = True
                yield from self._blocks(blocks,patch.get('patch_type') in {1,'1'},'blocks')
        if not handled:
            yield from extract_text_fragments(payload,reasoning_ids=self.reasoning_ids)
