"""Client hints accepted without claiming unsupported upstream controls."""
from __future__ import annotations

from typing import Any


# Keep one policy for Chat and Responses. These fields neither change the
# converted conversation nor enable a feature. Never forward them to upstream.
IGNORED_REQUEST_PARAMETERS = frozenset({
    'temperature', 'top_p', 'frequency_penalty', 'presence_penalty',
    'seed', 'logit_bias', 'logprobs', 'top_logprobs',
    'service_tier', 'safety_identifier',
    'prompt_cache_key', 'prompt_cache_retention', 'prompt_cache_options',
})
IGNORED_STREAM_OPTIONS = frozenset({'include_obfuscation', 'continuous_usage_stats'})
IGNORED_RESPONSE_INCLUDES = frozenset({
    'message.output_text.logprobs', 'message.input_image.image_url',
    'web_search_call.action.sources', 'file_search_call.results',
    'code_interpreter_call.outputs', 'computer_call_output.output.image_url',
})


def without_client_hints(value: Any) -> Any:
    """Copy a request before filtering, preserving caller data and real options."""
    if not isinstance(value, dict):
        return value
    result = {key: item for key, item in value.items() if key not in IGNORED_REQUEST_PARAMETERS}
    options = result.get('stream_options')
    if isinstance(options, dict):
        result['stream_options'] = {key: item for key, item in options.items() if key not in IGNORED_STREAM_OPTIONS}
    return result
