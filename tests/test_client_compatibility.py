import copy
import json

import pytest

from doubao_work2api.models import ChatCompletionRequest
from doubao_work2api.protocol import RequestValidationError, upstream_payload, validate_request
from doubao_work2api.responses import ResponsesConversionError, responses_to_chat


CLIENT_HINTS = {
    'temperature':0.7, 'top_p':0.95, 'frequency_penalty':0.1, 'presence_penalty':0.2,
    'seed':42, 'logit_bias':{'123':-10}, 'logprobs':True, 'top_logprobs':5,
    'service_tier':'auto', 'safety_identifier':'client-user',
    'prompt_cache_key':'client-cache', 'prompt_cache_retention':'24h',
    'prompt_cache_options':{'type':'ephemeral'},
}
OPTIONAL_INCLUDES = [
    'message.output_text.logprobs', 'message.input_image.image_url',
    'web_search_call.action.sources', 'file_search_call.results',
    'code_interpreter_call.outputs', 'computer_call_output.output.image_url',
]


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
@pytest.mark.parametrize('name,value', CLIENT_HINTS.items())
def test_client_hints_leave_converted_request_unchanged(protocol, name, value):
    if protocol == 'chat':
        payload = {'messages':[{'role':'user','content':'hello'}]}
        baseline = ChatCompletionRequest(**payload)
        convert = lambda data: ChatCompletionRequest(**data)
    else:
        payload = {'input':'hello'}
        convert = lambda data: responses_to_chat(data, 'deepseek-v4.1-flash')
        baseline = convert(payload)
    payload[name] = value
    original = copy.deepcopy(payload)
    result = convert(payload)
    assert result.model_dump() == baseline.model_dump()
    assert payload == original


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
def test_combined_hints_do_not_change_prompt_reasoning_or_upstream_controls(tmp_path, protocol):
    options = {'include_usage':True,'include_obfuscation':False,'continuous_usage_stats':True}
    if protocol == 'chat':
        request = ChatCompletionRequest(messages=[{'role':'user','content':'hello'}],
            reasoning_effort='low', n=1, stream_options=options, **CLIENT_HINTS)
        assert request.stream_options == {'include_usage':True}
    else:
        request = responses_to_chat({'input':'hello', 'reasoning':{'effort':'low','summary':'auto'},
            'n':1, 'modalities':['text'], 'stream_options':options,
            'include':OPTIONAL_INCLUDES + ['reasoning.encrypted_content'], **CLIENT_HINTS}, 'deepseek-v4.1-flash')
        assert request.include == ['reasoning.encrypted_content']
    request = validate_request(request)
    body = upstream_payload(request, model_key='person-deepseek-v4-1-flash-261017',
        workspace=tmp_path, app_id='test', device_id='d', device_name='test')
    assert body['option']['model_config']['reasoning_effort'] == 3
    encoded = json.dumps(body)
    assert 'hello' in encoded
    for name in CLIENT_HINTS:
        assert f'"{name}"' not in encoded
    assert options == {'include_usage':True,'include_obfuscation':False,'continuous_usage_stats':True}


@pytest.mark.parametrize('payload', [
    {'n':2}, {'n':True}, {'background':True},
    {'conversation':'conv_unsupported'}, {'context_management':[{'type':'compaction'}]},
    {'max_tool_calls':1}, {'moderation':{'enabled':True}},
    {'modalities':['audio']}, {'tools':[{'type':'web_search'}]},
    {'stream_options':{'include_usage':'yes'}},
    {'stream_options':{'unimplemented_option':True}},
    {'reasoning':{'effort':'ultra'}},
    {'text':{'format':{'type':'unimplemented_format'}}},
    {'unknown_future_feature':True},
])
def test_real_features_and_invalid_requests_are_still_rejected(payload):
    with pytest.raises(ResponsesConversionError):
        responses_to_chat({'input':'hello', **CLIENT_HINTS, **payload}, 'deepseek-v4.1-flash')


def test_chat_stream_options_keep_usage_validation():
    request = ChatCompletionRequest(messages=[{'role':'user','content':'hello'}],
        stream_options={'include_usage':'yes','include_obfuscation':False})
    with pytest.raises(RequestValidationError):
        validate_request(request)
