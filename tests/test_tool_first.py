"""Offline tests: the tool-first delegation wording is added once, can be switched off, and touches nothing else."""
import importlib.util, os, json, unittest
from pathlib import Path
SOURCE = Path(os.environ.get('BRIDGE_UNDER_TEST') or Path(__file__).resolve().parents[1] / 'bridge/live_bridge.py')
spec = importlib.util.spec_from_file_location('bridge_tf', SOURCE)
assert spec is not None and spec.loader is not None
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)

def setup():
    return {'setup': {'model': 'models/gemini-3.8-live',
            'tools': [{'functionDeclarations': [{'name': 'delegate_to_hermes', 'description': 'Ask Hermes.'}]}],
            'systemInstruction': {'parts': [{'text': 'You are Penny.'}]}}}

def delegate(out):
    return [d for t in out['setup']['tools'] for d in t['functionDeclarations'] if d['name'] == 'delegate_to_hermes'][0]

class ToolFirst(unittest.TestCase):
    def test_added_to_tool_and_prompt(self):
        out = b.prepare_setup(setup(), 'gemini-3.8-live')
        self.assertIn('MUST be called FIRST', delegate(out)['description'])
        self.assertIn('FINAL RULE', out['setup']['systemInstruction']['parts'][-1]['text'])
        self.assertTrue(delegate(out)['description'].startswith('Ask Hermes.'))

    def test_idempotent(self):
        once = b.prepare_setup(setup(), 'gemini-3.8-live')
        twice = b.prepare_setup(once, 'gemini-3.8-live')
        self.assertEqual(delegate(twice)['description'].count('MUST be called FIRST'), 1)
        self.assertEqual(twice['setup']['systemInstruction']['parts'][-1]['text'].count('FINAL RULE'), 1)

    def test_input_not_mutated(self):
        s = setup(); before = json.dumps(s); b.prepare_setup(s, 'gemini-3.8-live')
        self.assertEqual(json.dumps(s), before)

    def test_switch_off(self):
        orig = b.TOOL_FIRST; b.TOOL_FIRST = False
        try:
            out = b.prepare_setup(setup(), 'gemini-3.8-live')
        finally:
            b.TOOL_FIRST = orig
        self.assertNotIn('MUST be called FIRST', delegate(out)['description'])
        self.assertNotIn('FINAL RULE', out['setup']['systemInstruction']['parts'][-1]['text'])

if __name__ == '__main__':
    unittest.main()
