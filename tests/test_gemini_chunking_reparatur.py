
    
  
import ast, contextlib, datetime, hashlib, json, math, os, re, tempfile
from pathlib import Path
from types import SimpleNamespace

SOURCE_PATH = Path(__file__).resolve().parents[1] / 'gemini_auswertung.py'
SOURCE = SOURCE_PATH.read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)

def extract_function(name, namespace):
    node = next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<extracted>', 'exec'), namespace)

class FakePart:
    def __init__(self, text): self.text = text
    @classmethod
    def from_text(cls, text): return cls(text)
class FakeTypes:
    Part = FakePart

calls = []
token_ratio = 0.55
def token_count(client, model, contents, system_instruction, label):
    calls.append(label)
    total_chars = sum(len(getattr(x, 'text', '') or '') for x in contents)
    return math.ceil(total_chars * token_ratio) + math.ceil(len(system_instruction or '') / 3)

ns = {
    'math': math, 'types': FakeTypes, '_gemini_tokenzahl': token_count,
    'GEMINI_INPUT_SAFE_BUDGET': 120_000,
    'GEMINI_INPUT_EXCEPTION_BUDGET': 121_200,
    'print': lambda *a, **k: None,
    'os': os, 'json': json, 'hashlib': hashlib, 'datetime': datetime, 're': re,
    'tempfile': tempfile, 'SimpleNamespace': SimpleNamespace,
}
extract_function('_gemini_sichere_daten_gruppen', ns)

# 80 parts: no per-part preflight; group-level checks only.
parts = [FakePart((f'part-{i:03d}|' + 'x' * 19990)) for i in range(80)]
original = ''.join(p.text for p in parts)
calls.clear()
groups = list(ns['_gemini_sichere_daten_gruppen'](None, 'fake', parts, [FakePart('work' * 200)], 'sys' * 20, 'TEST80'))
reconstructed = ''.join(p.text for group in groups for p in group)
assert reconstructed == original, '80-Part-Chunking lost/reordered source content'
assert len(calls) < 80, f'too many CountTokens calls: {len(calls)}'
assert all(token_count(None, 'fake', list(g)+[FakePart('work'*200)], 'sys'*20, 'verify') <= 121200 for g in groups)
grouping_call_count = len(calls)

# One oversized text part: lossless recursive splitting and real newline preference.
text = 'a' * 99990 + '\n' + 'b' * 100009
token_ratio = 0.80
calls.clear()
large_groups = list(ns['_gemini_sichere_daten_gruppen'](None, 'fake', [FakePart(text)], [FakePart('work')], 'sys', 'LARGE'))
assert ''.join(p.text for group in large_groups for p in group) == text, 'oversized split lost source text'
assert len(large_groups) >= 2, 'oversized text part was not split'

# Resume JSON: atomic write/read, failed status, and multiple contexts survive.
with tempfile.TemporaryDirectory() as td:
    ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume.json')
    ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_done')
    ns['_GEMINI_SPLIT_RESUME_VERSION'] = 2
    extract_function('_gemini_split_resume_laden', ns)
    extract_function('_gemini_split_resume_speichern', ns)
    extract_function('_gemini_quellkontext_fingerprint', ns)
    ns['_gemini_split_resume_speichern']('ctx-A', {'key-A': 'answer-A'}, {'status': 'FAILED', 'split_index': 2})
    ns['_gemini_split_resume_speichern']('ctx-B', {'key-B': 'answer-B'}, None)
    a = ns['_gemini_split_resume_laden']('ctx-A')
    b = ns['_gemini_split_resume_laden']('ctx-B')
    assert a['completed'].get('key-A') == 'answer-A' and a['failed']['split_index'] == 2
    assert b['completed'].get('key-B') == 'answer-B'
    assert ns['_gemini_split_resume_laden']('ctx-C')['completed'] == {}
    source_file = Path(td) / 'source.json'
    source_file.write_text('{\"v\":1}', encoding='utf-8')
    fingerprint_1 = ns['_gemini_quellkontext_fingerprint'](['source.json'], {'source.json': str(source_file)})
    source_file.write_text('{\"v\":2}', encoding='utf-8')
    fingerprint_2 = ns['_gemini_quellkontext_fingerprint'](['source.json'], {'source.json': str(source_file)})
    assert fingerprint_1 != fingerprint_2, 'changed source file did not invalidate persistent resume cache'
    Path(ns['GEMINI_SPLIT_RESUME_DONE_DATEI']).write_text('done', encoding='utf-8')
    assert ns['_gemini_split_resume_laden']('ctx-A')['completed'] == {}
    assert not Path(ns['GEMINI_SPLIT_RESUME_DONE_DATEI']).exists()

    # Simulate two 503s on the same split in one run: stop after the second,
    # persist the failure, and reuse the already successful first split.
    resume_ns = dict(ns)
    resume_ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume_503.json')
    resume_ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_503_done')
    resume_ns['_GEMINI_RUN_ID'] = 'same-test-run'
    resume_ns['GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN'] = 2
    resume_ns['GEMINI_INPUT_SAFE_BUDGET'] = 120_000
    resume_ns['GEMINI_INPUT_EXCEPTION_BUDGET'] = 121_200
    resume_ns['_gemini_request_split_cache'] = {}
    resume_ns['_gemini_last_request_model'] = 'model-a'
    resume_ns['_gemini_active_modell'] = 'model-a'
    resume_ns['analysiere_api_fehler'] = lambda error: (False, None, 'ueberlast' if '503' in str(error) else 'sonstiges')
    resume_ns['ist_ablehnung'] = lambda value: not value or 'ablehnung' in value.lower()
    resume_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: 200_000 if label == 'GenerateContent' else 1_000
    resume_ns['_gemini_request_plan'] = lambda client, model, cache, data, work, system, label: [[data[0]], [data[1]]]

