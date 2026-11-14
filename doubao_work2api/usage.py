"""Read optional official billing fields without inferring missing charges."""
from __future__ import annotations


def official_usage(event):
    for source in (event, event.get('msg_finish_attr'), event.get('answer_finish_attr')):
        if not isinstance(source, dict):
            continue
        value = source.get('usage')
        if not isinstance(value, dict):
            continue
        prompt = value.get('prompt_tokens', value.get('input_tokens'))
        output = value.get('completion_tokens', value.get('output_tokens'))
        total = value.get('total_tokens')
        if not all(type(number) is int and number >= 0 for number in (prompt, output, total)):
            continue
        result = {'prompt_tokens':prompt, 'completion_tokens':output, 'total_tokens':total,
                  'is_estimated':False, 'estimated':False}
        for name, alias in (('prompt_tokens_details','input_tokens_details'),
                            ('completion_tokens_details','output_tokens_details')):
            details = value.get(name, value.get(alias))
            if isinstance(details, dict):
                safe = {key:number for key,number in details.items()
                        if key in {'cached_tokens','reasoning_tokens'} and type(number) is int and number >= 0}
                if safe:
                    result[name] = safe
        return result
    return None
