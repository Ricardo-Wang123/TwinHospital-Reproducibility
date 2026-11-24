"""Bounded lexical JSON closure normalization, never clinical content repair."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json


POLICY = {
    'version':'terminal_json_delimiters_and_string_whitespace_v2',
    'required_finish_reason':'stop',
    'top_level':'single_object',
    'max_terminal_delimiters':2,
    'allowed_operations':['append_missing_terminal_closers','remove_extra_terminal_closers',
                          'escape_literal_string_whitespace'],
    'edit_keys_values_strings_commas':False,
    'string_whitespace_encoding_only':['LF','CR','TAB'],
    'decoded_string_characters_changed':False,
    'retry_model':False,
    'reject_duplicate_keys':True,
    'reject_nonfinite_constants':True,
}
LEGACY_POLICY = {k: deepcopy(v) for k, v in POLICY.items()
                 if k not in {'string_whitespace_encoding_only', 'decoded_string_characters_changed'}}
LEGACY_POLICY['version'] = 'terminal_json_delimiters_v1'
LEGACY_POLICY['allowed_operations'] = ['append_missing_terminal_closers','remove_extra_terminal_closers']


class DuplicateJSONKeyError(ValueError):
    """A completed object is ambiguous; no duplicate value is selected."""
    def __init__(self, doc):
        self.msg = 'duplicate JSON object key'
        self.doc = doc
        super().__init__(self.msg)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _loads(text):
    def object_pairs(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise DuplicateJSONKeyError(text)
            value[key]=item
        return value
    def constant(_):
        raise ValueError('nonfinite JSON constant')
    return json.loads(text,object_pairs_hook=object_pairs,parse_constant=constant)


def _escape_string_whitespace(text):
    """Encode literal LF/CR/TAB inside quoted strings, preserving their values."""
    inside, escaped, count, output = False, False, 0, []
    for ch in text:
        if inside:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                inside = False
            elif ch in '\n\r\t':
                output.append({'\n': '\\n', '\r': '\\r', '\t': '\\t'}[ch])
                count += 1
                continue
        elif ch == '"':
            inside = True
        output.append(ch)
    return ''.join(output), count


def parse_json_response(raw_text, *, finish_reason, policy):
    """Parse one object, optionally fixing only 1–2 terminal brackets.

    Every other malformed response stays a failure. The returned metadata has no
    source text. On failure it is attached to the exception as parse_metadata.
    """
    if policy not in (POLICY, LEGACY_POLICY):
        raise ValueError('JSON parsing policy must match its frozen definition')
    meta = {'policy':deepcopy(policy), 'finish_reason':finish_reason,
        'original_text_sha256':_hash(raw_text), 'original_parse_error':None,
        'operations':[], 'normalized_text_sha256':None, 'normalization_performed':False}

    def fail(error):
        error.parse_metadata = deepcopy(meta)
        raise error

    if finish_reason != 'stop':
        fail(ValueError('JSON normalization requires finish_reason=stop'))
    text = raw_text.strip()
    # Preserve the existing whole-response fence handling. This never extracts
    # a fragment from prose or from multiple responses.
    if text.startswith('```') and text.endswith('```'):
        lines = text.splitlines()
        if lines[0].strip().lower() not in {'```','```json'} or lines[-1].strip() != '```':
            fail(ValueError('unrecognized surrounding JSON fence'))
        text = '\n'.join(lines[1:-1]).strip()
    meta['parser_input_sha256'] = _hash(text)
    try:
        value = _loads(text)
    except json.JSONDecodeError as original:
        meta['original_parse_error'] = {'type':'JSONDecodeError','message':original.msg,
            'line':original.lineno,'column':original.colno,'position':original.pos}
        if policy == POLICY and original.msg.startswith('Invalid control character'):
            candidate, count = _escape_string_whitespace(text)
            if not count:
                fail(original)
            try:
                value = _loads(candidate)
            except ValueError as error:
                # No second repair and no duplicate-key value selection.
                fail(error)
            if not isinstance(value, dict):
                fail(ValueError('model response must be a single JSON object'))
            meta['operations'] = [{'operation': 'escape_literal_string_whitespace', 'count': count}]
            meta['normalization_performed'] = True
            meta['normalized_text_sha256'] = _hash(candidate)
            return value, meta
        if not text.startswith('{'):
            fail(original)
        stack, inside, escaped, end = [], False, False, None
        for index, ch in enumerate(text):
            if inside:
                if escaped: escaped=False
                elif ch=='\\': escaped=True
                elif ch=='"': inside=False
                continue
            if ch=='"':inside=True
            elif ch in '{[':stack.append(ch)
            elif ch in '}]':
                if not stack or stack[-1] != {'}':'{',']':'['}[ch]:
                    fail(original)
                stack.pop()
                if not stack:
                    end=index+1
                    break
        if inside or escaped:
            fail(original)
        if end is not None:
            suffix=text[end:]
            closers=''.join(ch for ch in suffix if not ch.isspace())
            if not 1<=len(closers)<=2 or any(ch not in '}]' for ch in closers):
                fail(original)
            candidate=text[:end]
            operation={'operation':'remove_extra_terminal_closers','characters':closers,'count':len(closers)}
        else:
            if not 1<=len(stack)<=2:
                fail(original)
            closers=''.join({'{':'}','[':']'}[ch] for ch in reversed(stack))
            candidate=text+closers
            operation={'operation':'append_missing_terminal_closers','characters':closers,'count':len(closers)}
        try:
            value=_loads(candidate)
        except ValueError:
            fail(original)
        if not isinstance(value,dict):
            fail(ValueError('model response must be a single JSON object'))
        # Candidate is exactly the original bytes plus/minus terminal closers.
        # No key, value, string, comma or clinical disposition is inferred.
        meta['operations']=[operation]
        meta['normalization_performed']=True
        text=candidate
    except ValueError as error:
        meta['original_parse_error']={'type':type(error).__name__,'message':str(error),
            'line':None,'column':None,'position':None}
        fail(error)
    if not isinstance(value,dict):
        fail(ValueError('model response must be a single JSON object'))
    meta['normalized_text_sha256']=_hash(text)
    return value, meta
