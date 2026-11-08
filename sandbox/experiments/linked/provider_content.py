"""Separate an explicitly delimited provider reasoning prefix from final JSON.

No reasoning text leaves the private transport evidence. This is a channel
boundary adaptation, not extraction of a convenient command from prose.
"""
from copy import deepcopy
import hashlib

POLICY = {
    'version': 'glm_explicit_final_boundary_v1',
    'model_family': 'glm',
    'required_finish_reason': 'stop',
    'separator': '</think>',
    'required_separator_count': 1,
    'final_shape': 'whole_json_object_or_whole_json_fence',
    'edit_final_content': False,
    'infer_missing_final_content': False,
    'retry_model': False,
}


def separate_final_content(content, *, model, finish_reason, policy):
    if policy != POLICY:
        raise ValueError('Content boundary policy must match its frozen definition')
    meta = {'policy': deepcopy(policy), 'applied': False,
        'original_content_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'final_content_sha256': None, 'removed_prefix_characters': 0}
    text = content.strip()
    if not model.startswith('glm') or text.startswith(('{', '```')) or '</think>' not in text:
        return content, meta
    if finish_reason != 'stop' or text.count('</think>') != 1:
        raise ValueError('Ambiguous or incomplete provider final boundary')
    prefix, final = text.split('</think>', 1)
    if '<think>' in prefix and (prefix.count('<think>') != 1 or not prefix.startswith('<think>')):
        raise ValueError('Nested provider reasoning boundaries are ambiguous')
    final = final.strip()
    if '<think>' in final or '</think>' in final:
        raise ValueError('Unexpected reasoning delimiter in final response')
    if final.startswith('```'):
        lines = final.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {'```', '```json'} or lines[-1].strip() != '```':
            raise ValueError('Final response lacks a whole JSON fence')
    elif not final.startswith('{'):
        raise ValueError('No explicit final JSON object after reasoning delimiter')
    meta.update(applied=True, final_content_sha256=hashlib.sha256(final.encode()).hexdigest(),
        removed_prefix_characters=len(content)-len(final))
    return final, meta
