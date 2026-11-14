"""Per-gateway model discovery, with a local metadata cache and built-in fallback."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from .catalog import MODEL_CATALOG, ModelSpec, normalize_model, REASONING_LEVELS
from .errors import UnknownModelError, UpstreamError
from .private_json import save_private_json
from .upstream_admin import metadata_request, number


def parse_models(payload: dict, *, only_available=False) -> list[ModelSpec]:
    known = {spec.id: spec for spec in MODEL_CATALOG}
    result = {}

    def visit(value, depth=0):
        if depth > 20:
            return
        if isinstance(value, list):
            for child in value[:1000]:
                visit(child, depth+1)
        elif isinstance(value, dict):
            config = value.get('active_switch_conf')
            if isinstance(config, dict):
                use_v2 = config.get('use_menu_conf_v2', bool(config.get('menu_conf_v2')))
                visit(config.get('menu_conf_v2' if use_v2 else 'menu_conf', {}), depth+1)
                return
            key = value.get('model_item_key')
            modes = value.get('conversation_mode_list')
            subscription = value.get('subscribe_config')
            if only_available and isinstance(subscription, dict) and subscription.get('need_upgrade') is True:
                return
            if (isinstance(key, str) and re.fullmatch(r'[\w.+:-]{1,160}', key)
                    and isinstance(value.get('name'), str) and value['name']
                    and value.get('agent_mode') in (None, 1, '1')
                    and (not modes or isinstance(modes, list) and bool({'3', 'office', 'creation', 'work'} & set(map(str, modes))))):
                model_id = normalize_model(key)
                base = known.get(model_id)
                extra = value.get('model_extra_params') or {}
                if isinstance(extra, str):
                    try:
                        extra = json.loads(extra)
                    except ValueError:
                        extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                profile = extra.get('memory_profile') or value.get('memory_profile') or (base.memory_profile if base else None)
                window = number(extra.get('total_window_size') or value.get('context_window') or value.get('total_window_size'))
                if window is None and base:
                    window = base.context_window
                if window is None and profile in {'256k', '300k', '300k_230'}:
                    window = 256000 if profile == '256k' else 300000
                tags = [tag.get('text', '') for tag in value.get('tag_list', []) if isinstance(tag, dict) and isinstance(tag.get('text'), str)]
                price_tags = [text for text in tags if re.search(r'\d+(?:\.\d+)?x|免费', text, re.I)]
                price = value.get('price_desc') or value.get('price_text') or ' · '.join(price_tags) or value.get('subtitle') or (base.price if base else '')
                effort = value.get('reasoning_effort_config') or {}
                if not isinstance(effort, dict):
                    effort = {}
                effort_rows = effort.get('option_list') or value.get('reasoning_effort_list') or extra.get('reasoning_effort_list')
                levels = []
                if isinstance(effort_rows, list):
                    for row in effort_rows:
                        raw = row.get('level', row.get('value')) if isinstance(row, dict) else row
                        if str(raw) in ('3', '4', '5', '6', '7'):
                            levels.append(REASONING_LEVELS[int(raw)-3])
                        elif raw in REASONING_LEVELS:
                            levels.append(raw)
                levels = tuple(dict.fromkeys(levels)) or (base.reasoning_levels if base else ())
                default = effort.get('default_level')
                if str(default) in ('3', '4', '5', '6', '7'):
                    default = REASONING_LEVELS[int(default)-3]
                else:
                    default = base.default_reasoning_effort if base else None
                if default not in levels:
                    default = levels[-1] if levels else None
                image = value.get('image_input')
                if not isinstance(image, bool):
                    image = base.image_input if base else None
                result[model_id] = ModelSpec(id=model_id, upstream_key=key,
                    name=str(value.get('name') or (base.name if base else key))[:160],
                    price=str(price)[:160], context_window=int(window) if window and window <= 10000000 else None,
                    memory_profile=profile if isinstance(profile, str) else None,
                    reasoning_levels=levels, default_reasoning_effort=default, image_input=image,
                    description='由官方模型目录发现。')
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child, depth+1)
    visit(payload.get('data', {}))
    return list(result.values())


class LiveCatalog:
    def __init__(self, data_dir: Path):
        self.path = data_dir / 'model-catalog.json'
        self._models = {spec.id: spec for spec in MODEL_CATALOG}
        self.account_models = {}
        self.bot_ids = {}
        self.updated_at = 0
        self.source = 'builtin'
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                for row in saved['models']:
                    if not isinstance(row.get('name'), str) or row.get('name') == row.get('id') and str(row.get('id', '')).isdigit():
                        continue
                    spec = ModelSpec(**{**row, 'reasoning_levels': tuple(row.get('reasoning_levels', ()))})
                    self._models[spec.id] = spec
                self.account_models = saved.get('account_models', {})
                self.updated_at = saved['updated_at']
                self.source = 'cache'
            except (ValueError, TypeError, KeyError):
                pass

    @property
    def models(self):
        return tuple(self._models.values())

    def find(self, model):
        return self._models.get(normalize_model(model))

    def bind(self, request):
        spec = self.find(request.normalized_model)
        if spec is None:
            raise UnknownModelError(request.normalized_model)
        request._model_spec = spec
        return request

    def status(self):
        return {'source': self.source, 'updated_at': self.updated_at, 'count': len(self._models)}

    async def refresh(self, transport, account):
        launch = await metadata_request(transport, account, 'launch')
        bot_id = str(launch.get('data', {}).get('assistant_bot_id') or '7338286299411103781')
        response = await metadata_request(transport, account, 'models', {'language_code': 'zh', 'bot_id': bot_id})
        found = parse_models(response)
        if not found:
            raise UpstreamError('官方模型目录为空或格式已变化，保留现有清单。', code='empty_catalog')
        updated = {**self._models, **{spec.id: spec for spec in found}}
        scopes = {**self.account_models, account.id: [spec.id for spec in parse_models(response, only_available=True)]}
        timestamp = time.time()
        save_private_json(self.path, {'models': [asdict(spec) for spec in updated.values()],
                                     'updated_at': timestamp, 'account_models': scopes})
        self._models, self.account_models, self.updated_at = updated, scopes, timestamp
        self.bot_ids[account.id] = bot_id
        self.source = 'official'
        return self.status()

    def eligible_ids(self, model, account_ids=None):
        # Previously unobserved models must only go to accounts that list them.
        if any(spec.id == model for spec in MODEL_CATALOG):
            return {account_id for account_id in account_ids if account_id not in self.account_models or model in self.account_models[account_id]} if account_ids is not None else None
        return {account_id for account_id, models in self.account_models.items() if model in models}
