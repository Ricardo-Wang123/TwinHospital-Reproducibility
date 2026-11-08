from copy import deepcopy
import unittest
from sandbox.experiments.linked.provider_content import POLICY, separate_final_content
from sandbox.experiments.linked.json_boundary import POLICY as JSON_POLICY, parse_json_response


class ContentBoundaryTests(unittest.TestCase):
    def separate(self, text, **kwargs):
        return separate_final_content(text, model=kwargs.get('model','glm-5.2'),
            finish_reason=kwargs.get('finish','stop'), policy=POLICY)

    def test_explicit_boundary_preserves_final_object_and_never_returns_prefix(self):
        final='```json\n{"action":"defer","reason":"unknown"}\n```'
        for prefix in ('PRIVATE FIXTURE ONLY', '<think>PRIVATE FIXTURE ONLY'):
            out, meta = self.separate(prefix+'</think>\n'+final)
            self.assertEqual(out, final)
            self.assertTrue(meta['applied'])
            self.assertNotIn('PRIVATE', str(meta))
            parsed, _ = parse_json_response(out, finish_reason='stop', policy=JSON_POLICY)
            self.assertEqual(parsed['action'], 'defer')

    def test_valid_json_value_containing_delimiter_is_untouched(self):
        text='{"message":"Literal </think> is data"}'
        out, meta = self.separate(text)
        self.assertEqual(out, text); self.assertFalse(meta['applied'])

    def test_no_implicit_command_extraction_or_unmarked_prose_removal(self):
        text='ordinary prose {"action":"approve"}'
        out, meta = self.separate(text)
        self.assertEqual(out, text); self.assertFalse(meta['applied'])
        with self.assertRaises(ValueError):
            parse_json_response(out, finish_reason='stop', policy=JSON_POLICY)

    def test_missing_ambiguous_and_truncated_finals_are_rejected(self):
        for text, finish in [('x</think>no final','stop'),
                ('x</think>{}</think>{}','stop'), ('x</think>{}','length'),
                ('<think>x<think>y</think>{}','stop')]:
            with self.assertRaises(ValueError):self.separate(text, finish=finish)

    def test_only_declared_model_family_is_changed(self):
        text='x</think>{"ok":true}'
        out, meta = self.separate(text, model='deepseek-v4-flash')
        self.assertEqual(out, text); self.assertFalse(meta['applied'])


if __name__=='__main__':unittest.main()
