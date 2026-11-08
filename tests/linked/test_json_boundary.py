from copy import deepcopy
import hashlib
import json
import unittest
from sandbox.experiments.linked.json_boundary import POLICY, LEGACY_POLICY, DuplicateJSONKeyError, parse_json_response


class JsonBoundaryTests(unittest.TestCase):
    def parse(self,text,finish_reason='stop'):
        return parse_json_response(text,finish_reason=finish_reason,policy=deepcopy(POLICY))

    def test_single_missing_or_extra_closer_keeps_defer_and_text(self):
        original={'action':'adjudication','decision':'defer','reason':'Synthetic uncertainty; do not approve.'}
        full=json.dumps(original)
        for raw,operation in [(full[:-1],'append_missing_terminal_closers'),(full+'}','remove_extra_terminal_closers')]:
            with self.subTest(operation=operation):
                result,meta=self.parse(raw)
                self.assertEqual(result,original)
                self.assertEqual(meta['operations'],[{'operation':operation,'characters':'}','count':1}])
                self.assertEqual(meta['original_text_sha256'],hashlib.sha256(raw.encode()).hexdigest())
                self.assertEqual(meta['normalized_text_sha256'],hashlib.sha256(full.encode()).hexdigest())
                self.assertNotIn('Synthetic',json.dumps(meta))

    def test_at_most_two_terminal_delimiters(self):
        value,meta=self.parse('{"content":{"text":"synthetic"')
        self.assertEqual(value,{'content':{'text':'synthetic'}})
        self.assertEqual(meta['operations'][0]['count'],2)
        value,meta=self.parse('{"a":[1,2')
        self.assertEqual(value,{'a':[1,2]})
        self.assertEqual(meta['operations'][0]['characters'],']}')
        self.assertEqual(self.parse('{"a":1}}]')[0],{'a':1})
        for raw in ['{"a":{"b":{"c":1','{"a":1}}}}']:
            with self.assertRaises(json.JSONDecodeError):self.parse(raw)

    def test_brackets_inside_strings_are_not_structural(self):
        original={'text':'literal { ] } \\" quote \" and slash \\','decision':'reject'}
        raw=json.dumps(original,ensure_ascii=False)
        self.assertEqual(self.parse(raw[:-1])[0],original)
        self.assertEqual(self.parse('```json\n'+raw+'\n```')[0],original)

    def test_rejects_semantic_edits_second_objects_prose_and_incomplete_strings(self):
        malformed=['{"a":1 "b":2}', '{"a":1,', '{"a":', '{"a":"unfinished',
            '{"a":"unfinished\\', '{"a":[1}', '{"a":1} {"b":2}',
            '{"a":1} trailing words', 'Answer: {"a":1}', '[1,2]',
            '```python\n{"a":1}\n```']
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ValueError):self.parse(raw)

    def test_no_length_or_unknown_finish_reason_is_repaired(self):
        for finish in ['length','tool_calls',None]:
            with self.subTest(finish=finish), self.assertRaises(ValueError) as context:self.parse('{"a":1',finish)
            self.assertEqual(context.exception.parse_metadata['operations'],[])
        value,meta=self.parse('{"decision":"reject"}')
        self.assertEqual(value['decision'],'reject')
        self.assertFalse(meta['normalization_performed'])
        self.assertIsNone(meta['original_parse_error'])

    def test_failure_preserves_original_parse_error_without_editing(self):
        raw='{"a":1 "b":2}'
        with self.assertRaises(json.JSONDecodeError) as context:self.parse(raw)
        meta=context.exception.parse_metadata
        self.assertEqual(meta['original_parse_error']['position'],7)
        self.assertEqual(meta['operations'],[])
        self.assertIsNone(meta['normalized_text_sha256'])

    def test_duplicate_keys_and_nonfinite_values_cannot_silently_change_decisions(self):
        cases=['{"decision":"reject","decision":"approve"}',
            '{"content":{"text":"a","text":"b"}}', '{"dose":NaN}',
            '{"dose":Infinity}', '{"dose":-Infinity}',
            '{"decision":"reject","decision":"approve"']
        for raw in cases:
            with self.subTest(raw=raw),self.assertRaises(ValueError):self.parse(raw)

    def test_duplicate_key_failure_has_typed_provenance_without_selecting_a_value(self):
        raw='{"decision":"reject","decision":"approve"}'
        with self.assertRaises(DuplicateJSONKeyError) as context:
            self.parse(raw)
        error=context.exception
        self.assertEqual(error.doc,raw)
        self.assertEqual(error.parse_metadata['original_parse_error']['type'],'DuplicateJSONKeyError')
        self.assertFalse(error.parse_metadata['normalization_performed'])
        self.assertIsNone(error.parse_metadata['normalized_text_sha256'])
        self.assertNotIn('approve',json.dumps(error.parse_metadata))

    def test_literal_note_line_breaks_preserve_the_decoded_text_and_decision(self):
        raw = '{"decision":"reject","content":{"text":"first\nsecond\r\nthird\tend"}}'
        value, meta = self.parse(raw)
        self.assertEqual(value['content']['text'], 'first\nsecond\r\nthird\tend')
        self.assertEqual(value['decision'], 'reject')
        self.assertEqual(meta['operations'], [{'operation':'escape_literal_string_whitespace','count':4}])
        self.assertTrue(meta['normalization_performed'])

    def test_literal_whitespace_does_not_allow_other_controls_duplicates_or_truncation(self):
        for raw in ['{"text":"a\x00b"}', '{"text":"a\nb","text":"other"}',
                    '{"text":"a\nb"', '{"text":"a\nb"} {"decision":"approve"}',
                    '{"text":"a\\\nb"}']:
            with self.subTest(raw=repr(raw)), self.assertRaises(ValueError):
                self.parse(raw)

    def test_legacy_policy_does_not_silently_adopt_new_normalization(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_json_response('{"text":"a\nb"}',finish_reason='stop',policy=deepcopy(LEGACY_POLICY))


if __name__=='__main__':unittest.main()
