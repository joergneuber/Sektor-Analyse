import ast, contextlib, datetime, hashlib, json, math, os, tempfile, time
from collections import defaultdict
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
    GenerateContentConfig = staticmethod(lambda **kwargs: kwargs)

calls = []
token_ratio = 0.55
def token_count(client, model, contents, system_instruction, label):
    calls.append(label)
    total_chars = sum(len(getattr(x, 'text', '') or '') for x in contents)
    return math.ceil(total_chars * token_ratio) + math.ceil(len(system_instruction or '') / 3)

ns = {
    'math': math, 're': __import__('re'), 'types': FakeTypes, '_gemini_tokenzahl': token_count,
    'GEMINI_INPUT_SAFE_BUDGET': 120_000,
    'GEMINI_INPUT_EXCEPTION_BUDGET': 121_200,
    'print': lambda *a, **k: None,
    'os': os, 'json': json, 'hashlib': hashlib, 'datetime': datetime,
    'tempfile': tempfile, 'SimpleNamespace': SimpleNamespace,
    'time': SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic, time=time.time),
    # These module-level production states must exist in isolated AST namespaces.
    '_gemini_503_failed_models_by_request': defaultdict(set),
    '_gemini_503_pool_passes_this_run': defaultdict(int),
    'GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN': 2,
    'GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN': 2,
    'GEMINI_503_POOL_BACKOFF_SECONDS': 0.0,
    'GEMINI_MODELLREIHENFOLGE': ['model-a', 'model-b'],
    'GEMINI_MODELLWECHSEL_JITTER_MAX': 0.0,
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


# Mehr-Part-Gruppe oberhalb des Normalziels muss geteilt werden, selbst wenn
# sie unter der Ausnahmegrenze liegt. Einzel-Parts duerfen die Ausnahme nutzen.
budget_ns = dict(ns)
def budget_token_count(client, model, contents, system_instruction, label):
    if "Arbeitskontext" in label:
        return 100
    return 120_500 if len(contents) > 2 else 119_000
budget_ns['_gemini_tokenzahl'] = budget_token_count
extract_function('_gemini_sichere_daten_gruppen', budget_ns)
budget_groups = list(budget_ns['_gemini_sichere_daten_gruppen'](
    None, 'fake', [FakePart('A'), FakePart('B')], [FakePart('work')], 'sys', 'BUDGET'
))
assert len(budget_groups) == 2, (
    '120,500-token multi-part group was not split at the 120,000 normal budget'
)
assert all(len(group) == 1 for group in budget_groups), 'budget split did not isolate individual parts'

# Bei weiterer Segmentierung muss jeder Teil die Quellenkennung behalten.
header = (
    "\n\n===== QUELLDATEI example.json | CHUNK 1/1 =====\n"
    "Dieser Chunk ist ein vollstaendiger Ausschnitt der unveraenderten Originalquelle. "
    "Keine Daten ergaenzen oder weglassen.\n\n"
)
payload = ("record-001|alpha\n" * 12000) + ("record-002|beta\n" * 12000)
source_part_text = header + payload
token_ratio = 0.80
header_calls = []
def header_token_count(client, model, contents, system_instruction, label):
    header_calls.append(label)
    chars = sum(len(getattr(x, 'text', '') or '') for x in contents)
    return math.ceil(chars * token_ratio)
header_ns = dict(ns)
header_ns['re'] = __import__('re')
header_ns['_gemini_tokenzahl'] = header_token_count
extract_function('_gemini_sichere_daten_gruppen', header_ns)
header_groups = list(header_ns['_gemini_sichere_daten_gruppen'](
    None, 'fake', [FakePart(source_part_text)], [FakePart('work')], 'sys', 'HEADER'
))
header_parts = [part for group in header_groups for part in group]
assert len(header_parts) >= 2, 'header-bearing oversized source was not split'
assert all('===== QUELLDATEI example.json | CHUNK 1/1 =====' in p.text for p in header_parts), (
    'a re-segmented part lost its source header'
)
continuation = (
    "FORTSETZUNG DES GLEICHEN QUELL-CHUNKS; QUELLKOPF ZUR ZUORDNUNG WIEDERHOLT.\n\n"
)
reconstructed_payload = []
for part in header_parts:
    part_text = part.text
    assert part_text.startswith(header), 'source header was not kept at the beginning of every part'
    body = part_text[len(header):]
    if body.startswith(continuation):
        body = body[len(continuation):]
    reconstructed_payload.append(body)
assert ''.join(reconstructed_payload) == payload, 'source payload was duplicated, lost, or reordered'

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
    send_log = []
    def simulated_generate(client, model, contents, config, tokens, label):
        send_log.append(label)
        if label.endswith('Split 1'):
            return SimpleNamespace(text='SUCCESSFUL_SPLIT_1', candidates=[])
        raise RuntimeError('503 UNAVAILABLE high demand')
    resume_ns['_gemini_generate_content_quota_safe'] = simulated_generate
    extract_function('_gemini_split_resume_laden', resume_ns)
    extract_function('_gemini_split_resume_speichern', resume_ns)
    extract_function('_gemini_split_resume_fehler_sichern', resume_ns)
    extract_function('_gemini_request_part_fingerprint', resume_ns)
    extract_function('_gemini_cache_antwort', resume_ns)
    source_a, source_b = FakePart('source-a'), FakePart('source-b')
    request_context = {
        'contents': [source_a, source_b],
        'context_names': ['a.txt', 'b.txt'],
        'system_instruction': 'system',
        'resume_fingerprint': 'same-context',
    }
    # This branch deliberately stubs the quota-safe sender. Therefore it tests
    # split failure persistence/cache resume only; terminal retry-cap behavior
    # is tested below with the real production quota-safe sender.
    for _attempt in range(2):
        try:
            resume_ns['_gemini_cache_antwort'](None, 'model-a', request_context, [FakePart('work')], 'system')
            raise AssertionError('simulated failing split unexpectedly succeeded')
        except RuntimeError as exc:
            assert '503 UNAVAILABLE' in str(exc), str(exc)
            assert 'GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL' not in str(exc), (
                'stubbed sender must not be used to assert production terminal-cap behavior'
            )
        resume_ns['_gemini_request_split_cache'] = {}
    assert send_log == ['GenerateContent Split 1', 'GenerateContent Split 2', 'GenerateContent Split 2'], send_log
    saved = json.loads(Path(resume_ns['GEMINI_SPLIT_RESUME_DATEI']).read_text(encoding='utf-8'))
    ctx_state = saved['contexts']['same-context']
    assert len(ctx_state['completed']) == 1 and ctx_state['failed']['attempt_count'] == 2

    # Next run: persistent resume reuses completed split 1 and retries only split 2.
    resume_ns['_GEMINI_RUN_ID'] = 'next-test-run'
    resume_ns['_gemini_request_split_cache'] = {}
    def succeed_only_pending_split(client, model, contents, config, tokens, label):
        send_log.append('NEXT_RUN:' + label)
        return SimpleNamespace(text='SUCCESSFUL_SPLIT_2_ON_RESUME', candidates=[])
    resume_ns['_gemini_generate_content_quota_safe'] = succeed_only_pending_split
    resume_ns['_gemini_synthese'] = lambda client, model, texts, system, label: '|'.join(texts)
    resumed = resume_ns['_gemini_cache_antwort'](
        None, 'model-a', request_context, [FakePart('work')], 'system'
    )
    assert resumed.text == 'SUCCESSFUL_SPLIT_1|SUCCESSFUL_SPLIT_2_ON_RESUME', resumed.text
    assert send_log[-1] == 'NEXT_RUN:GenerateContent Split 2', send_log
    assert 'NEXT_RUN:GenerateContent Split 1' not in send_log, send_log

    # Production-function integration: actual quota-safe sender -> 503 ->
    # persisted FAILED state -> model switch -> terminal cap -> next-run resume.
    actual_ns = dict(resume_ns)
    actual_ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume_actual_503.json')
    actual_ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_actual_503_done')
    actual_ns['_GEMINI_RUN_ID'] = 'actual-run-1'
    actual_ns['GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN'] = 2
    actual_ns['_gemini_503_failures_this_run'] = {}
    actual_ns['_gemini_last_503_request_info'] = {}
    actual_ns['hashlib'] = hashlib
    actual_ns['json'] = json
    extract_function('_gemini_503_request_fingerprint', actual_ns)
    extract_function('_gemini_generate_content_einzelversuch', actual_ns)
    actual_ns['_gemini_request_split_cache'] = {}
    actual_ns['_gemini_active_modell'] = 'model-a'
    actual_ns['_gemini_last_request_model'] = 'model-a'
    actual_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (
        200_000 if label == 'GenerateContent' else 1_000
    )
    actual_ns['_gemini_quota_waehlen'] = lambda preferred, tokens, **kwargs: (preferred, 0)
    actual_ns['_gemini_quota_registriere_sendung'] = lambda model, tokens: {'model': model, 'tokens': tokens}
    actual_ns['_gemini_quota_rollback_sendung'] = lambda reservation: True
    actual_sends = []
    class Actual503Models:
        def generate_content(self, model, contents, config):
            payload = ' '.join(getattr(part, 'text', '') or '' for part in contents)
            actual_sends.append((model, 'source-a' if 'source-a' in payload else 'source-b'))
            if 'source-b' in payload and model in {'model-a', 'model-b'}:
                raise RuntimeError('503 UNAVAILABLE high demand')
            return SimpleNamespace(
                text='SUCCESSFUL_SPLIT_1' if 'source-a' in payload else 'SUCCESSFUL_SPLIT_2',
                candidates=[],
            )
    class Actual503Client:
        models = Actual503Models()
    extract_function('_gemini_split_resume_laden', actual_ns)
    extract_function('_gemini_split_resume_speichern', actual_ns)
    extract_function('_gemini_split_resume_fehler_sichern', actual_ns)
    extract_function('_gemini_request_part_fingerprint', actual_ns)
    extract_function('_gemini_generate_content_quota_safe', actual_ns)
    extract_function('_gemini_cache_antwort', actual_ns)
    actual_ns['_gemini_synthese'] = lambda client, model, texts, system, label: '|'.join(texts)
    actual_client = Actual503Client()
    # Each model gets at most two real sends for this request. The third
    # attempt on model-b must be blocked before generate_content is called.
    for model, expected_terminal in [
        ('model-a', False), ('model-b', False), ('model-b', False), ('model-b', True)
    ]:
        actual_ns['_gemini_active_modell'] = model
        actual_ns['_gemini_last_request_model'] = model
        try:
            actual_ns['_gemini_cache_antwort'](
                actual_client, model, request_context, [FakePart('work')], 'system'
            )
            raise AssertionError('actual 503 sender unexpectedly succeeded before terminal cap')
        except RuntimeError as exc:
            if expected_terminal:
                assert 'GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL' in str(exc), str(exc)
            else:
                assert '503 UNAVAILABLE' in str(exc), str(exc)
    assert actual_sends == [
        ('model-a', 'source-a'), ('model-a', 'source-b'),
        ('model-b', 'source-b'), ('model-b', 'source-b')
    ], actual_sends
    assert len([call for call in actual_sends if call[1] == 'source-b']) == 3, actual_sends
    extract_function('_gemini_503_naechstes_modell', actual_ns)
    extract_function('_gemini_503_aeussere_entscheidung', actual_ns)
    extract_function('analysiere_api_fehler', actual_ns)
    assert actual_ns['_gemini_503_naechstes_modell']('model-a', ['model-a', 'model-b', 'model-c'], {'model-a'}) == 'model-b'
    assert actual_ns['_gemini_503_naechstes_modell']('model-b', ['model-a', 'model-b', 'model-c'], {'model-a', 'model-b'}) == 'model-c'
    assert actual_ns['_gemini_503_naechstes_modell']('model-c', ['model-a', 'model-b', 'model-c'], {'model-a', 'model-b', 'model-c'}) is None
    assert actual_ns['_gemini_503_aeussere_entscheidung']('503 UNAVAILABLE high demand', 'model-a', ['model-a', 'model-b', 'model-c'], {'model-a'}) == ('switch', 'model-b')
    assert actual_ns['_gemini_503_aeussere_entscheidung']('GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL: limit reached 503', 'model-b', ['model-a', 'model-b', 'model-c'], {'model-a', 'model-b'}) == ('terminal', None)
    assert actual_ns['_gemini_503_aeussere_entscheidung']('503 UNAVAILABLE high demand', 'model-c', ['model-a', 'model-b', 'model-c'], {'model-a', 'model-b', 'model-c'}) == ('terminal', None)
    assert actual_ns['analysiere_api_fehler']('GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL: limit reached 503')[0:3] == (True, None, 'modellpool_temporaer')
    actual_ns['_GEMINI_RUN_ID'] = 'actual-run-2'
    actual_ns['_gemini_503_failures_this_run'] = {}
    actual_ns['_gemini_last_503_request_info'] = {}
    actual_ns['_gemini_active_modell'] = 'model-c'
    actual_ns['_gemini_last_request_model'] = 'model-c'
    actual_ns['_gemini_request_split_cache'] = {}
    actual_result = actual_ns['_gemini_cache_antwort'](
        actual_client, 'model-c', request_context, [FakePart('work')], 'system'
    )
    assert actual_result.text == 'SUCCESSFUL_SPLIT_1|SUCCESSFUL_SPLIT_2', actual_result.text
    assert actual_sends[-1] == ('model-c', 'source-b'), actual_sends
    assert ('model-c', 'source-a') not in actual_sends, actual_sends
    print('PASS: production quota-safe sender + actual 503 failures + two-model terminal cap + next-run resume')

    # The low-level production sender blocks any third API call BEFORE reaching the provider.
    precheck_ns = dict(actual_ns)
    precheck_ns['_gemini_503_failures_this_run'] = {}
    precheck_ns['_gemini_last_503_request_info'] = {}
    precheck_ns['_GEMINI_RUN_ID'] = 'precheck-run'
    precheck_ns['GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN'] = 2
    extract_function('_gemini_503_request_fingerprint', precheck_ns)
    extract_function('_gemini_generate_content_einzelversuch', precheck_ns)
    precheck_calls = []
    class Always503Models:
        def generate_content(self, model, contents, config):
            precheck_calls.append(model)
            raise RuntimeError('503 UNAVAILABLE high demand')
    class Always503Client:
        models = Always503Models()
    for expected_terminal in (False, False, True):
        try:
            precheck_ns['_gemini_generate_content_einzelversuch'](
                Always503Client(), 'model-x', [FakePart('same synthesis prompt')], {},
                'SYNTHESIS', 'system'
            )
            raise AssertionError('503 attempt unexpectedly succeeded')
        except RuntimeError as exc:
            if expected_terminal:
                assert 'GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL' in str(exc), str(exc)
            else:
                assert '503 UNAVAILABLE' in str(exc), str(exc)
    assert precheck_calls == ['model-x', 'model-x'], precheck_calls
    print('PASS: third identical 503 request is blocked before API call; shared cap applies to synthesis requests too')

    # End-to-end synthesis failure: successful source splits persist; synthesis
    # 503 attempts are counted and persisted by the same production resume gate.
    synth_ns = dict(actual_ns)
    synth_ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume_synthesis_503.json')
    synth_ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_synthesis_503_done')
    synth_ns['_GEMINI_RUN_ID'] = 'synthesis-same-run'
    synth_ns['_gemini_503_failures_this_run'] = {}
    synth_ns['_gemini_last_503_request_info'] = {}
    synth_ns['_gemini_request_split_cache'] = {}
    synth_ns['_gemini_active_modell'] = 'model-a'
    synth_ns['_gemini_last_request_model'] = 'model-a'
    synth_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (200_000 if label == 'GenerateContent' else 1_000)
    synth_ns['_gemini_request_plan'] = lambda client, model, cache, data, work, system, label: [[data[0]], [data[1]]]
    synth_sends = []
    class Synthesis503Models:
        def generate_content(self, model, contents, config):
            payload = ' '.join(getattr(part, 'text', '') or '' for part in contents)
            if 'synth-prompt:' in payload:
                synth_sends.append(('SYNTHESIS', model))
                raise RuntimeError('503 UNAVAILABLE high demand')
            src = 'source-a' if 'source-a' in payload else 'source-b'
            synth_sends.append((src, model))
            return SimpleNamespace(text='OK_' + src, candidates=[])
    class Synthesis503Client:
        models = Synthesis503Models()
    synth_client = Synthesis503Client()
    extract_function('_gemini_503_request_fingerprint', synth_ns)
    extract_function('_gemini_generate_content_einzelversuch', synth_ns)
    extract_function('_gemini_generate_content_quota_safe', synth_ns)
    extract_function('_gemini_split_resume_laden', synth_ns)
    extract_function('_gemini_split_resume_speichern', synth_ns)
    extract_function('_gemini_split_resume_fehler_sichern', synth_ns)
    extract_function('_gemini_request_part_fingerprint', synth_ns)
    extract_function('_gemini_cache_antwort', synth_ns)
    def fail_synthesis_with_real_sender(client, model, texts, system, label):
        prompt = FakePart('synth-prompt:' + '|'.join(texts))
        response = synth_ns['_gemini_generate_content_quota_safe'](
            client, model, [prompt], {'system_instruction': system}, 1_000, 'SYNTHESIS'
        )
        return response.text
    synth_ns['_gemini_synthese'] = fail_synthesis_with_real_sender
    synth_ns['_gemini_request_split_cache'] = {}
    synth_ns['analysiere_api_fehler'] = lambda error: (False, None, 'ueberlast' if '503' in str(error) else 'sonstiges')
    synth_ns['ist_ablehnung'] = lambda value: False
    synth_context = {
        'contents': [FakePart('source-a'), FakePart('source-b')],
        'context_names': ['a.txt', 'b.txt'], 'system_instruction': 'system',
        'resume_fingerprint': 'synthesis-context',
    }
    for terminal_expected in (False, False, True):
        synth_ns['_gemini_request_split_cache'] = {}
        try:
            synth_ns['_gemini_cache_antwort'](
                synth_client, 'model-a', synth_context, [FakePart('work')], 'system'
            )
            raise AssertionError('synthesis 503 unexpectedly produced a complete response')
        except RuntimeError as exc:
            if terminal_expected:
                assert 'GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL' in str(exc), str(exc)
            else:
                assert '503 UNAVAILABLE' in str(exc), str(exc)
    assert [x for x in synth_sends if x[0] == 'SYNTHESIS'] == [('SYNTHESIS', 'model-a'), ('SYNTHESIS', 'model-a')], synth_sends
    synth_saved = json.loads(Path(synth_ns['GEMINI_SPLIT_RESUME_DATEI']).read_text(encoding='utf-8'))
    synth_failed = synth_saved['contexts']['synthesis-context']['failed']
    assert synth_failed['stage'] == 'SYNTHESIS' and synth_failed['attempt_count'] == 2, synth_failed
    assert len(synth_saved['contexts']['synthesis-context']['completed']) == 2, synth_saved
    print('PASS: synthesis 503 uses persistent request counter; terminal after two real failures; successful splits retained')

    # INTEGRATION: real synthesis sender + production outer retry action + terminal fallback.
    # The first synthesis 503 switches model-a -> model-b. The second real 503 is
    # persisted as terminal, the outer action stops, and the production fallback
    # status file is written. Successful source splits must not be resent.
    outer_ns = dict(synth_ns)
    outer_ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume_outer_integration.json')
    outer_ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_outer_integration_done')
    outer_ns['_GEMINI_RUN_ID'] = 'outer-integration-run'
    outer_ns['_gemini_503_failures_this_run'] = {}
    outer_ns['_gemini_last_503_request_info'] = {}
    outer_ns['_gemini_request_split_cache'] = {}
    outer_ns['_gemini_active_modell'] = 'model-a'
    outer_ns['_gemini_last_request_model'] = 'model-a'
    outer_ns['_gemini_failed_models'] = set()
    outer_ns['GEMINI_MODELLREIHENFOLGE'] = ['model-a', 'model-b']
    outer_ns['GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN'] = 2
    outer_ns['analysiere_api_fehler'] = lambda error: (
        False, None, 'ueberlast' if '503' in str(error) else 'sonstiges'
    )
    outer_sends = []
    class Outer503Models:
        def generate_content(self, model, contents, config):
            payload = ' '.join(getattr(part, 'text', '') or '' for part in contents)
            if 'synth-prompt:' in payload:
                outer_sends.append(('SYNTHESIS', model))
                raise RuntimeError('503 UNAVAILABLE high demand')
            source = 'source-a' if 'source-a' in payload else 'source-b'
            outer_sends.append((source, model))
            return SimpleNamespace(text='OK_' + source, candidates=[])
    class Outer503Client:
        models = Outer503Models()
    outer_client = Outer503Client()
    extract_function('_gemini_503_request_fingerprint', outer_ns)
    extract_function('_gemini_generate_content_einzelversuch', outer_ns)
    extract_function('_gemini_generate_content_quota_safe', outer_ns)
    extract_function('_gemini_split_resume_laden', outer_ns)
    extract_function('_gemini_split_resume_speichern', outer_ns)
    extract_function('_gemini_split_resume_fehler_sichern', outer_ns)
    extract_function('_gemini_request_part_fingerprint', outer_ns)
    extract_function('_gemini_cache_antwort', outer_ns)
    outer_ns['_gemini_request_plan'] = lambda client, model, cache, data, work, system, label: [[data[0]], [data[1]]]
    def outer_real_sender_synthesis(client, model, texts, system, label):
        prompt = FakePart('synth-prompt:' + '|'.join(texts))
        response = outer_ns['_gemini_generate_content_quota_safe'](
            client, model, [prompt], {'system_instruction': system}, 1_000, 'SYNTHESIS'
        )
        return response.text
    outer_ns['_gemini_synthese'] = outer_real_sender_synthesis
    extract_function('_gemini_503_naechstes_modell', outer_ns)
    extract_function('_gemini_503_aeussere_entscheidung', outer_ns)
    extract_function('_gemini_503_aeussere_retry_aktion', outer_ns)
    extract_function('_gemini_fallback_status_datei', outer_ns)
    outer_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (
        200_000 if label == 'GenerateContent' else 1_000
    )
    outer_ns['_gemini_quota_waehlen'] = lambda preferred, tokens, **kwargs: (preferred, 0)
    outer_ns['_gemini_quota_registriere_sendung'] = lambda model, tokens: {'model': model, 'tokens': tokens}
    outer_ns['_gemini_quota_rollback_sendung'] = lambda reservation: True
    outer_ns['GEMINI_INPUT_SAFE_BUDGET'] = 120_000
    outer_ns['GEMINI_INPUT_EXCEPTION_BUDGET'] = 121_200
    outer_ns['GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS'] = 60
    outer_ns['GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS'] = 5
    outer_ns['types'] = FakeTypes
    outer_context = {
        'contents': [FakePart('source-a'), FakePart('source-b')],
        'context_names': ['a.txt', 'b.txt'], 'system_instruction': 'system',
        'resume_fingerprint': 'outer-integration-context',
    }
    outer_terminal_error = None
    for outer_attempt in range(2):
        try:
            outer_ns['_gemini_cache_antwort'](
                outer_client, outer_ns['_gemini_active_modell'], outer_context,
                [FakePart('work')], 'system'
            )
            raise AssertionError('integrated synthesis 503 unexpectedly succeeded')
        except RuntimeError as exc:
            action, candidate = outer_ns['_gemini_503_aeussere_retry_aktion'](
                str(exc), outer_ns['_gemini_active_modell'],
                outer_ns['GEMINI_MODELLREIHENFOLGE'], outer_ns['_gemini_failed_models'],
            )
            if outer_attempt == 0:
                assert '503 UNAVAILABLE' in str(exc), str(exc)
                assert (action, candidate) == ('switch', 'model-b'), (action, candidate)
                assert outer_ns['_gemini_active_modell'] == 'model-b'
            else:
                assert '503 UNAVAILABLE' in str(exc), str(exc)
                assert (action, candidate) == ('terminal', None), (action, candidate)
                outer_terminal_error = str(exc)
                break
    assert outer_terminal_error, 'outer loop did not reach terminal fallback'
    previous_cwd = os.getcwd()
    try:
        os.chdir(td)
        outer_ns['_gemini_fallback_status_datei']('write', outer_terminal_error)
        status_path = next(Path(td).glob('Gemini_Fallback_Status_*.txt'))
        status_text = status_path.read_text(encoding='utf-8')
    finally:
        os.chdir(previous_cwd)
    assert 'GEMINI_STATUS=TECHNISCHER_FALLBACK' in status_text, status_text
    assert 'GEMINI_SPLIT_STATUS=FAILED' in status_text, status_text
    assert outer_sends == [
        ('source-a', 'model-a'), ('source-b', 'model-a'),
        ('SYNTHESIS', 'model-a'), ('SYNTHESIS', 'model-b'),
    ], outer_sends
    outer_saved = json.loads(Path(outer_ns['GEMINI_SPLIT_RESUME_DATEI']).read_text(encoding='utf-8'))
    outer_failed = outer_saved['contexts']['outer-integration-context']['failed']
    assert outer_failed['stage'] == 'SYNTHESIS' and outer_failed['attempt_count'] == 2, outer_failed
    assert len(outer_saved['contexts']['outer-integration-context']['completed']) == 2, outer_saved
    print('PASS: integrated outer retry switches model A->B, stops after second synthesis 503, writes technical fallback status, and preserves completed source splits')

    # TRUE OUTER-HANDLER INTEGRATION: execute the actual production except-handler
    # inside a real while/try loop, not a test-authored retry loop. The try body
    # calls the real production _gemini_cache_antwort sender; the exception
    # handler below is extracted verbatim from gemini_auswertung_starten().
    production_start = next(
        n for n in TREE.body
        if isinstance(n, ast.FunctionDef) and n.name == 'gemini_auswertung_starten'
    )
    production_outer_try = next(
        n for n in ast.walk(production_start)
        if isinstance(n, ast.Try)
        and any(h.name == 'e' and h.body
                and isinstance(h.body[0], ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'fehlertext'
                        for t in h.body[0].targets)
                for h in n.handlers)
    )
    production_handler = next(
        h for h in production_outer_try.handlers
        if h.name == 'e' and h.body
        and isinstance(h.body[0], ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'fehlertext'
                for t in h.body[0].targets)
    )
    true_outer_ns = dict(outer_ns)
    true_outer_ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume_true_outer.json')
    true_outer_ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_true_outer_done')
    true_outer_ns['_GEMINI_RUN_ID'] = 'true-outer-production-handler-run'
    true_outer_ns['_gemini_503_failures_this_run'] = {}
    true_outer_ns['_gemini_last_503_request_info'] = {}
    true_outer_ns['_gemini_request_split_cache'] = {}
    true_outer_ns['_gemini_active_modell'] = 'model-a'
    true_outer_ns['_gemini_last_request_model'] = 'model-a'
    true_outer_ns['_gemini_failed_models'] = set()
    true_outer_ns['GEMINI_MODELLREIHENFOLGE'] = ['model-a', 'model-b']
    true_outer_ns['GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN'] = 2
    true_outer_ns['GEMINI_MODELLWECHSEL_JITTER_MAX'] = 0
    true_outer_ns['MAX_VERSUCHE'] = 4
    true_outer_ns['WARTEZEIT_SEKUNDEN'] = 0
    true_outer_ns['random'] = SimpleNamespace(uniform=lambda a, b: 0)
    true_outer_ns['time'] = SimpleNamespace(sleep=lambda seconds: None)
    true_outer_ns['print'] = lambda *a, **k: None
    true_outer_ns['analysiere_api_fehler'] = lambda error: (
        False, None, 'ueberlast' if '503' in str(error) else 'sonstiges'
    )
    true_outer_ns['_gemini_stufen_cache'] = {}
    true_outer_ns['_gemini_cache_name'] = None
    true_outer_ns['_gemini_last_request_model'] = 'model-a'
    true_outer_ns['_gemini_fallback_status_datei'] = outer_ns['_gemini_fallback_status_datei']
    # Re-extract into the new namespace so function globals (counters, model
    # state and resume paths) are the same objects as the production handler.
    for _function_name in (
        '_gemini_503_request_fingerprint', '_gemini_generate_content_einzelversuch',
        '_gemini_generate_content_quota_safe', '_gemini_split_resume_laden',
        '_gemini_split_resume_speichern', '_gemini_split_resume_fehler_sichern',
        '_gemini_request_part_fingerprint', '_gemini_cache_antwort',
        '_gemini_503_naechstes_modell', '_gemini_503_aeussere_entscheidung',
        '_gemini_503_aeussere_retry_aktion', '_gemini_fallback_status_datei',
    ):
        extract_function(_function_name, true_outer_ns)
    true_outer_ns['_gemini_request_plan'] = lambda client, model, cache, data, work, system, label: [[data[0]], [data[1]]]
    def true_outer_real_sender_synthesis(client, model, texts, system, label):
        prompt = FakePart('synth-prompt:' + '|'.join(texts))
        response = true_outer_ns['_gemini_generate_content_quota_safe'](
            client, model, [prompt], {'system_instruction': system}, 1_000, 'SYNTHESIS'
        )
        return response.text
    true_outer_ns['_gemini_synthese'] = true_outer_real_sender_synthesis
    # The exact production handler includes `continue` and `break`; placing it
    # in this test harness preserves those control-flow semantics unchanged.
    call_sender = ast.Call(
        func=ast.Name(id='_gemini_cache_antwort', ctx=ast.Load()),
        args=[
            ast.Name(id='outer_client', ctx=ast.Load()),
            ast.Name(id='aktuelles_modell', ctx=ast.Load()),
            ast.Name(id='outer_context', ctx=ast.Load()),
            ast.List(elts=[ast.Call(func=ast.Name(id='FakePart', ctx=ast.Load()),
                                    args=[ast.Constant(value='work')], keywords=[])], ctx=ast.Load()),
            ast.Constant(value='system'),
        ], keywords=[],
    )
    synthetic_try = ast.Try(
        body=[ast.Expr(value=call_sender)],
        handlers=[production_handler], orelse=[], finalbody=[],
    )
    # The except-handler is nested in gemini_auswertung_starten(). Preserve
    # that function's module-global bindings in this synthetic integration wrapper.
    production_global_names = sorted({
        name
        for node in ast.walk(production_start)
        if isinstance(node, ast.Global)
        for name in node.names
    })
    true_outer_global_stmt = ast.Global(names=production_global_names)
    true_outer_function = ast.FunctionDef(
        name='_run_actual_production_outer_handler', args=ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
        ),
        body=[
            true_outer_global_stmt,
            ast.Assign(targets=[ast.Name(id='versuch', ctx=ast.Store())], value=ast.Constant(value=0)),
            ast.Assign(targets=[ast.Name(id='versuch_nummer', ctx=ast.Store())], value=ast.Constant(value=0)),
            ast.Assign(targets=[ast.Name(id='letzte_antwort', ctx=ast.Store())], value=ast.Constant(value=None)),
            ast.Assign(targets=[ast.Name(id='hochgeladene_teile', ctx=ast.Store())], value=ast.Constant(value=None)),
            ast.Assign(targets=[ast.Name(id='modell_index', ctx=ast.Store())], value=ast.Constant(value=0)),
            ast.Assign(targets=[ast.Name(id='aktuelles_modell', ctx=ast.Store())], value=ast.Constant(value='model-a')),
            ast.Assign(targets=[ast.Name(id='technische_retry_wiederverwenden', ctx=ast.Store())], value=ast.Constant(value=False)),
            ast.While(
                test=ast.Constant(value=True),
                body=[
                    ast.AugAssign(target=ast.Name(id='versuch', ctx=ast.Store()), op=ast.Add(), value=ast.Constant(value=1)),
                    ast.Assign(
                        targets=[ast.Name(id='versuch_nummer', ctx=ast.Store())],
                        value=ast.BinOp(
                            left=ast.BinOp(
                                left=ast.BinOp(left=ast.Name(id='versuch', ctx=ast.Load()), op=ast.Sub(), right=ast.Constant(value=1)),
                                op=ast.Mod(), right=ast.Name(id='MAX_VERSUCHE', ctx=ast.Load())
                            ), op=ast.Add(), right=ast.Constant(value=1)
                        ),
                    ),
                    synthetic_try,
                    ast.Expr(value=ast.Call(func=ast.Name(id='AssertionError', ctx=ast.Load()), args=[ast.Constant(value='production outer handler returned without continue/break')], keywords=[])),
                ], orelse=[],
            ),
            ast.Expr(value=ast.Call(
                func=ast.Name(id='_gemini_fallback_status_datei', ctx=ast.Load()),
                args=[ast.Constant(value='write'), ast.Name(id='letzte_antwort', ctx=ast.Load())], keywords=[]
            )),
            ast.Return(value=ast.Name(id='letzte_antwort', ctx=ast.Load())),
        ], decorator_list=[],
    )
    true_outer_module = ast.fix_missing_locations(ast.Module(body=[true_outer_function], type_ignores=[]))
    exec(compile(true_outer_module, '<actual-production-outer-handler>', 'exec'), true_outer_ns)
    true_outer_context = {
        'contents': [FakePart('source-a'), FakePart('source-b')],
        'context_names': ['a.txt', 'b.txt'], 'system_instruction': 'system',
        'resume_fingerprint': 'true-outer-production-context',
    }
    true_outer_ns['FakePart'] = FakePart
    true_outer_ns['outer_client'] = outer_client
    true_outer_ns['outer_context'] = true_outer_context
    true_outer_sends_before = len(outer_sends)
    previous_cwd = os.getcwd()
    try:
        os.chdir(td)
        true_outer_result = true_outer_ns['_run_actual_production_outer_handler']()
        true_outer_status = next(Path(td).glob('Gemini_Fallback_Status_*.txt')).read_text(encoding='utf-8')
    finally:
        os.chdir(previous_cwd)
    assert true_outer_result.startswith('[Technischer Fehler] 503 UNAVAILABLE'), true_outer_result
    assert 'GEMINI_STATUS=TECHNISCHER_FALLBACK' in true_outer_status, true_outer_status
    assert 'GEMINI_SPLIT_STATUS=FAILED' in true_outer_status, true_outer_status
    assert outer_sends[true_outer_sends_before:] == [
        ('source-a', 'model-a'), ('source-b', 'model-a'),
        ('SYNTHESIS', 'model-a'), ('SYNTHESIS', 'model-b'),
        ('SYNTHESIS', 'model-a'), ('SYNTHESIS', 'model-b')
    ], outer_sends[true_outer_sends_before:]
    true_outer_saved = json.loads(Path(true_outer_ns['GEMINI_SPLIT_RESUME_DATEI']).read_text(encoding='utf-8'))
    true_outer_failed = true_outer_saved['contexts']['true-outer-production-context']['failed']
    assert true_outer_failed['stage'] == 'SYNTHESIS' and true_outer_failed['attempt_count'] == 4, true_outer_failed
    assert len(true_outer_saved['contexts']['true-outer-production-context']['completed']) == 2, true_outer_saved
    print('PASS: verbatim production outer except-handler executes real sender, switches model A->B, breaks on terminal 503, and writes fallback status')

    # The same two-attempt cap also applies to a request that fits into one call.
    direct_ns = dict(resume_ns)
    direct_ns['GEMINI_SPLIT_RESUME_DATEI'] = str(Path(td) / '.resume_direct.json')
    direct_ns['GEMINI_SPLIT_RESUME_DONE_DATEI'] = str(Path(td) / '.resume_direct_done')
    direct_ns['_gemini_request_split_cache'] = {}
    direct_sends = []
    def simulated_direct_failure(client, model, contents, config, tokens, label):
        direct_sends.append(label)
        raise RuntimeError('503 UNAVAILABLE high demand')
    direct_ns['_gemini_generate_content_quota_safe'] = simulated_direct_failure
    direct_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: 1_000
    direct_ns['analysiere_api_fehler'] = lambda error: (False, None, 'ueberlast' if '503' in str(error) else 'sonstiges')
    extract_function('_gemini_split_resume_laden', direct_ns)
    extract_function('_gemini_split_resume_speichern', direct_ns)
    extract_function('_gemini_split_resume_fehler_sichern', direct_ns)
    extract_function('_gemini_request_part_fingerprint', direct_ns)
    extract_function('_gemini_cache_antwort', direct_ns)
    direct_context = {
        'contents': [FakePart('small-source')],
        'context_names': ['small.txt'],
        'system_instruction': 'system',
        'resume_fingerprint': 'direct-context',
    }
    # This stub intentionally bypasses the production quota-safe sender.
    # Test the cache/resume failure path here; terminal send-cap behavior is
    # verified above with the real production sender.
    for _attempt in range(2):
        try:
            direct_ns['_gemini_cache_antwort'](None, 'model-a', direct_context, [FakePart('work')], 'system')
            raise AssertionError('simulated direct 503 unexpectedly succeeded')
        except RuntimeError as exc:
            assert '503 UNAVAILABLE' in str(exc), str(exc)
            assert 'GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL' not in str(exc), str(exc)
    assert len(direct_sends) == 2, direct_sends


# Gesamtrequest mit Quellen-Parts bei 120,500 Tokens muss ebenfalls in den
# Split-Plan statt direkt durch die Ausnahmegrenze gesendet werden.
route_ns = dict(ns)
route_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (
    120_500 if label == 'GenerateContent' else 119_000
)
route_ns['_gemini_split_resume_laden'] = lambda fingerprint: {'completed': {}, 'failed': None}
route_ns['_gemini_split_resume_speichern'] = lambda *args, **kwargs: None
route_ns['_gemini_request_part_fingerprint'] = lambda *args, **kwargs: f'part-{args[3]}'
route_ns['_gemini_request_plan'] = lambda *args, **kwargs: [[FakePart('source-a')], [FakePart('source-b')]]
route_ns['_gemini_generate_content_quota_safe'] = lambda *args, **kwargs: SimpleNamespace(text='ok', candidates=[])
route_ns['_gemini_request_split_cache'] = {}
route_ns['_gemini_last_request_model'] = 'model-a'
route_ns['_gemini_active_modell'] = 'model-a'
route_ns['ist_ablehnung'] = lambda text: False
route_ns['_gemini_synthese'] = lambda *args, **kwargs: SimpleNamespace(text='synth', candidates=[])
route_ns['GEMINI_INPUT_SAFE_BUDGET'] = 120_000
route_ns['GEMINI_INPUT_EXCEPTION_BUDGET'] = 121_200
extract_function('_gemini_cache_antwort', route_ns)
route_cache = {
    'contents': [FakePart('source-a'), FakePart('source-b')],
    'context_names': ['a.txt', 'b.txt'],
    'system_instruction': 'system',
    'resume_fingerprint': 'route-context',
}
route_response = route_ns['_gemini_cache_antwort'](
    None, 'model-a', route_cache, [FakePart('work')], 'system'
)
assert getattr(route_response.text, 'text', None) == 'synth', 'over-budget multi-source request did not follow split path'
print('PASS: 120,500-token multi-source request uses split plan, not direct exception path')

# Model-switch integration: an exact fallback-model count above 121,200 causes
# only the affected group to be losslessly resegmented into smaller requests.
resegment_ns = dict(route_ns)
resegment_ns['_gemini_request_split_cache'] = {}
resegment_ns['_gemini_active_modell'] = 'model-a'
resegment_ns['_gemini_last_request_model'] = 'model-a'
resegment_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (
    200_000 if label == 'GenerateContent' else 1_000
)
resegment_ns['_gemini_request_plan'] = lambda *args, **kwargs: [[FakePart('oversized-source-group')]]
resegment_ns['_gemini_sichere_daten_gruppen'] = lambda client, model, data, work, system, label: (
    [[FakePart('source-left')], [FakePart('source-right')]]
    if 'Quota-Fallback-Nachsegmentierung' in label else [[FakePart('oversized-source-group')]]
)
resegment_sends = []
class FakeModels:
    def generate_content(self, model, contents, config):
        resegment_sends.append(f'{model}:sent')
        payload = ' '.join(getattr(part, 'text', '') or '' for part in contents)
        return SimpleNamespace(
            text='left' if 'source-left' in payload else 'right', candidates=[]
        )
class FakeClient:
    models = FakeModels()
resegment_ns['_gemini_quota_waehlen'] = lambda preferred, tokens, **kwargs: (
    ('model-b', 0) if preferred == 'model-a' else (preferred, 0)
)
resegment_ns['_gemini_quota_registriere_sendung'] = lambda model, tokens: {'model': model, 'tokens': tokens}
resegment_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (
    200_000 if label == 'GenerateContent' else
    121_300 if 'Quota-Fallback' in label and any(
        'oversized-source-group' in (getattr(part, 'text', '') or '') for part in contents
    ) else 1_000
)
resegment_ns['GEMINI_INPUT_SAFE_BUDGET'] = 120_000
resegment_ns['GEMINI_INPUT_EXCEPTION_BUDGET'] = 121_200
resegment_ns['_gemini_503_failures_this_run'] = {}
resegment_ns['_gemini_last_503_request_info'] = {}
resegment_ns['GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN'] = 2
resegment_ns['_GEMINI_RUN_ID'] = 'resegment-run'
resegment_ns['hashlib'] = hashlib
resegment_ns['json'] = json
extract_function('_gemini_503_request_fingerprint', resegment_ns)
extract_function('_gemini_generate_content_einzelversuch', resegment_ns)
extract_function('_gemini_generate_content_quota_safe', resegment_ns)
resegment_ns['_gemini_generate_content_quota_safe'] = resegment_ns['_gemini_generate_content_quota_safe']
resegment_ns['_gemini_split_resume_laden'] = lambda fingerprint: {'completed': {}, 'failed': None}
resegment_ns['_gemini_split_resume_speichern'] = lambda *args, **kwargs: None
resegment_ns['_gemini_split_resume_fehler_sichern'] = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError('oversized fallback group should be resegmented, not marked failed')
)
resegment_ns['_gemini_synthese'] = lambda *args, **kwargs: 'left+right'
resegment_ns['ist_ablehnung'] = lambda value: False
resegment_ns['_gemini_request_part_fingerprint'] = lambda *args, **kwargs: f'part-{args[3]}'
resegment_ns['GEMINI_INPUT_SAFE_BUDGET'] = 120_000
resegment_ns['GEMINI_INPUT_EXCEPTION_BUDGET'] = 121_200
extract_function('_gemini_cache_antwort', resegment_ns)
resegment_cache = {
    'contents': [FakePart('source-original')], 'context_names': ['source.txt'],
    'system_instruction': 'system', 'resume_fingerprint': 'resegment-context',
}
resegment_response = resegment_ns['_gemini_cache_antwort'](
    FakeClient(), 'model-a', resegment_cache, [FakePart('work')], 'system'
)
assert resegment_response.text == 'left+right', resegment_response.text
assert len(resegment_sends) == 2, resegment_sends
assert 'GEMINI-QUOTA-FALLBACK-RESEGMENTIERT' in SOURCE
print('PASS: actual quota-safe model switch detects 121,300 tokens, resegments only affected group, and sends both smaller groups')

# Direct request that fits on the first model but exceeds the exception budget
# after model switch must fall through into the source-group planner.
direct_resegment_ns = dict(route_ns)
direct_resegment_ns['_gemini_request_split_cache'] = {}
direct_resegment_ns['_gemini_tokenzahl'] = lambda client, model, contents, system, label: (
    119_000 if label == 'GenerateContent' else 1_000
)
direct_resegment_sends = []
def direct_switch_then_send(client, model, contents, config, tokens, label):
    direct_resegment_sends.append(label)
    if len(direct_resegment_sends) == 1:
        raise RuntimeError('GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_FALLBACK: Modell=model-b | 121,300 > 121,200;RESEGMENTIEREN')
    return SimpleNamespace(text='direct-left' if len(direct_resegment_sends) == 2 else 'direct-right', candidates=[])
direct_resegment_ns['_gemini_generate_content_quota_safe'] = direct_switch_then_send
direct_resegment_ns['_gemini_split_resume_laden'] = lambda fingerprint: {'completed': {}, 'failed': None}
direct_resegment_ns['_gemini_split_resume_speichern'] = lambda *args, **kwargs: None
direct_resegment_ns['_gemini_split_resume_fehler_sichern'] = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError('oversized direct request should enter split planner, not be marked failed')
)
direct_resegment_ns['_gemini_synthese'] = lambda *args, **kwargs: 'direct-left+direct-right'
extract_function('_gemini_cache_antwort', direct_resegment_ns)
direct_resegment_response = direct_resegment_ns['_gemini_cache_antwort'](
    None, 'model-a', resegment_cache, [FakePart('work')], 'system'
)
assert direct_resegment_response.text == 'direct-left+direct-right', direct_resegment_response.text
assert len(direct_resegment_sends) == 3, direct_resegment_sends
print('PASS: direct request that grows on fallback model enters lossless split plan')

# A technical fallback must not overwrite an existing daily Auswertung file.
extract_function('speichere_ergebnis', ns)
ns['datetime'] = datetime
ns['os'] = os
ns['Path'] = Path
with tempfile.TemporaryDirectory() as td:
    previous_cwd = os.getcwd()
    try:
        os.chdir(td)
        daily = Path(f'Auswertung({datetime.date.today().isoformat()}).txt')
        daily.write_text('BESTEHENDE TAGESAUSWERTUNG', encoding='utf-8')
        result = ns['speichere_ergebnis']('[GEMINI_TECHNISCHER_FALLBACK]\nkeine Auswertung')
        assert result is None
        assert daily.read_text(encoding='utf-8') == 'BESTEHENDE TAGESAUSWERTUNG'
    finally:
        os.chdir(previous_cwd)

# Critical invariants in the final source.
assert 'GEMINI_INPUT_SAFE_BUDGET = 120_000' in SOURCE
assert 'GEMINI_INPUT_EXCEPTION_BUDGET = 121_200' in SOURCE
assert 'GEMINI_SOURCE_CHUNK_MAX_CHARS = 20_000' in SOURCE
assert 'GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL' in SOURCE
assert 'GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN = 2' in SOURCE
assert SOURCE.count('_gemini_503_aeussere_retry_aktion(') >= 3, 'outer retry/terminal branches are not wired to the tested production helper'
assert 'if attempts >= GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN:' in SOURCE
assert 'os.replace(temp_output, ausgabe_datei)' in SOURCE
assert 'GEMINI_SPLIT_RESUME_DONE_DATEI' in SOURCE
assert 'body.rfind("\\n"' in SOURCE
assert 'body.find("\\n"' in SOURCE
assert 'GEMINI_INPUT_SAFE_BUDGET' in SOURCE[SOURCE.index('def _gemini_sichere_daten_gruppen'):SOURCE.index('def _gemini_quota_prune')]
assert 'direct_request_allowed' in SOURCE and 'and not daten_teile' in SOURCE
# Exhausted pool may enter exactly the guarded second pass; after that it
# must break instead of creating an unbounded retry cycle.
outer_anchor = '# Nach dem ersten vollstaendigen Pooldurchlauf'
outer_start = SOURCE.index(outer_anchor)
outer_end = SOURCE.index('else:\n                wartezeit = (empfohlene_wartezeit', outer_start)
outer = SOURCE[outer_start:outer_end]
assert 'pool_passes < (GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN - 1)' in outer
assert 'time.sleep(wartezeit)' in outer
assert 'break' in outer and 'versuch_zyklus += 1' not in outer
workflow_path = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'main.yml'
if workflow_path.exists():
    workflow = workflow_path.read_text(encoding='utf-8')
    assert 'Gemini Split Resume wiederherstellen' in workflow
    assert 'Gemini Split Resume nach Lauf sichern' in workflow
    assert '${{ github.run_attempt }}' in workflow, 'resume cache key omits run_attempt'
    assert 'Gemini Chunking/Resume Regressionstest' in workflow
    assert 'Warnung bei nicht gespeichertem Gemini Resume' in workflow
    assert "steps.gemini-split-resume-save.outcome == 'failure'" in workflow

print('PASS: 80-Part grouping lossless; CountTokens calls=', grouping_call_count)
print('PASS: multi-part groups above 120,000 tokens are split; singleton exception is isolated')
print('PASS: every re-segmented source part retains source header and payload reconstructs exactly')
print('PASS: oversized part split lossless; groups=', len(large_groups))
print('PASS: persistent resume status, source invalidation, and multi-context retention')
print('PASS: simulated 503 cap at two attempts; persistent resume reuses completed split and retries only failed split')
print('PASS: technical fallback leaves existing daily output unchanged')
print('PASS: 120k/121.2k/20k limits; 503 terminal behavior; atomic output; newline split')


# Targeted regression: registry absent, then outer retry creates request-a;
# quota-safe production logic must safely initialize a distinct request-b entry.
target_ns = {
    'GEMINI_MODELLREIHENFOLGE': ['model-a', 'model-b'],
    '_gemini_last_503_request_info': {'request_fingerprint': 'req-test-a'},
    '_gemini_active_modell': 'model-a',
    '_gemini_last_request_model': 'model-a',
    '_gemini_failed_models': set(),
    'analysiere_api_fehler': lambda msg: (
        False, None, 'ueberlast' if '503' in str(msg) else 'sonstiges'
    ),
}
extract_function('_gemini_503_naechstes_modell', target_ns)
extract_function('_gemini_503_aeussere_entscheidung', target_ns)
extract_function('_gemini_503_aeussere_retry_aktion', target_ns)
target_result = target_ns['_gemini_503_aeussere_retry_aktion']('503 UNAVAILABLE', 'model-a')
assert target_result == ('switch', 'model-b'), target_result
registry = target_ns['_gemini_503_failed_models_by_request']
assert 'model-a' in registry['req-test-a']
assert target_ns['_gemini_failed_models'] is registry['req-test-a']

# Exercise the ACTUAL production quota-safe function (not a mock of it) with a
# fake provider boundary. A second fingerprint absent from the existing plain
# dict must be initialized, not raise KeyError. Existing request-a state survives.
quota_ns = dict(target_ns)
quota_ns['_gemini_503_failed_models_by_request'] = registry
quota_ns['_gemini_last_503_request_info'] = {}
quota_ns['_gemini_active_modell'] = 'model-b'
quota_ns['_gemini_last_request_model'] = 'model-b'
quota_ns['GEMINI_INPUT_EXCEPTION_BUDGET'] = 121200
quota_ns['GEMINI_INPUT_SAFE_BUDGET'] = 120000
quota_ns['GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS'] = 60
quota_ns['GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS'] = 5
quota_ns['types'] = FakeTypes
quota_ns['time'] = SimpleNamespace(sleep=lambda seconds: None)
quota_ns['print'] = lambda *a, **k: None
quota_ns['_gemini_503_request_fingerprint'] = lambda contents, system: 'req-test-b'
quota_ns['_gemini_quota_waehlen'] = lambda preferred, tokens, **kwargs: (preferred, 0)
quota_ns['_gemini_quota_registriere_sendung'] = lambda model, tokens: {'model': model, 'tokens': tokens}
quota_ns['_gemini_generate_content_einzelversuch'] = lambda *args, **kwargs: SimpleNamespace(text='OK', candidates=[])
quota_ns['_gemini_tokenzahl'] = lambda *args, **kwargs: 1000
extract_function('_gemini_generate_content_quota_safe', quota_ns)
# Also test the quota-safe function when the registry itself is absent, not
# merely when one request key is absent from an existing plain dictionary.
quota_missing_ns = dict(quota_ns)
quota_missing_ns.pop('_gemini_503_failed_models_by_request', None)
quota_missing_ns['_gemini_503_request_fingerprint'] = lambda contents, system: 'req-created-from-none'
extract_function('_gemini_generate_content_quota_safe', quota_missing_ns)
missing_registry_response = quota_missing_ns['_gemini_generate_content_quota_safe'](
    None, 'model-b', [FakePart('missing-registry-payload')], {}, 1000, 'MISSING-REGISTRY'
)
assert missing_registry_response.text == 'OK', missing_registry_response.text
assert quota_missing_ns['_gemini_503_failed_models_by_request']['req-created-from-none'] == set()
quota_response = quota_ns['_gemini_generate_content_quota_safe'](
    None, 'model-b', [FakePart('request-b-payload')], {}, 1000, 'TARGET-REGRESSION'
)
assert quota_response.text == 'OK', quota_response.text
registry = quota_ns['_gemini_503_failed_models_by_request']
assert 'req-test-a' in registry and registry['req-test-a'] == {'model-a'}, registry
assert 'req-test-b' in registry and isinstance(registry['req-test-b'], set), registry
assert quota_ns['_gemini_failed_models'] is registry['req-test-b']
assert registry['req-test-a'] is not registry['req-test-b']
print('PASS: missing registry and missing second request key are safely initialized by both production functions; existing request state preserved')


# Explicit registry value-type policy:
# - a string is one model name, never an iterable of characters;
# - collections must contain only non-empty model-name strings;
# - malformed registry roots/entries raise TypeError rather than silently
#   erasing state or constructing a misleading blocklist.
string_outer_ns = {
    'GEMINI_MODELLREIHENFOLGE': ['model-a', 'model-b'],
    '_gemini_last_503_request_info': {'request_fingerprint': 'req-string-outer'},
    '_gemini_active_modell': 'model-a',
    '_gemini_last_request_model': 'model-a',
    '_gemini_failed_models': set(),
    '_gemini_503_failed_models_by_request': {'req-string-outer': 'model-a'},
    'analysiere_api_fehler': lambda msg: (
        False, None, 'ueberlast' if '503' in str(msg) else 'sonstiges'
    ),
}
extract_function('_gemini_503_naechstes_modell', string_outer_ns)
extract_function('_gemini_503_aeussere_entscheidung', string_outer_ns)
extract_function('_gemini_503_aeussere_retry_aktion', string_outer_ns)
assert string_outer_ns['_gemini_503_aeussere_retry_aktion'](
    '503 UNAVAILABLE', 'model-a'
) == ('switch', 'model-b')
assert string_outer_ns['_gemini_503_failed_models_by_request']['req-string-outer'] == {'model-a'}

string_quota_ns = dict(quota_ns)
string_quota_ns['_gemini_503_failed_models_by_request'] = {'req-string-quota': 'model-a'}
string_quota_ns['_gemini_503_request_fingerprint'] = lambda contents, system: 'req-string-quota'
extract_function('_gemini_generate_content_quota_safe', string_quota_ns)
string_response = string_quota_ns['_gemini_generate_content_quota_safe'](
    None, 'model-b', [FakePart('string-registry-payload')], {}, 1000, 'STRING-REGISTRY'
)
assert string_response.text == 'OK'
assert string_quota_ns['_gemini_503_failed_models_by_request']['req-string-quota'] == {'model-a'}
assert string_quota_ns['_gemini_failed_models'] == {'model-a'}

def assert_type_error(label, call):
    try:
        call()
    except TypeError:
        return
    raise AssertionError(f'{label}: malformed registry state was silently accepted')

bad_entry_outer_ns = dict(string_outer_ns)
bad_entry_outer_ns['_gemini_last_503_request_info'] = {'request_fingerprint': 'req-bad-entry'}
bad_entry_outer_ns['_gemini_503_failed_models_by_request'] = {'req-bad-entry': 17}
extract_function('_gemini_503_aeussere_retry_aktion', bad_entry_outer_ns)
assert_type_error(
    'outer retry invalid registry entry',
    lambda: bad_entry_outer_ns['_gemini_503_aeussere_retry_aktion']('503 UNAVAILABLE', 'model-a')
)

bad_entry_quota_ns = dict(quota_ns)
bad_entry_quota_ns['_gemini_503_failed_models_by_request'] = {'req-bad-entry-quota': 17}
bad_entry_quota_ns['_gemini_503_request_fingerprint'] = lambda contents, system: 'req-bad-entry-quota'
extract_function('_gemini_generate_content_quota_safe', bad_entry_quota_ns)
assert_type_error(
    'quota-safe invalid registry entry',
    lambda: bad_entry_quota_ns['_gemini_generate_content_quota_safe'](
        None, 'model-b', [FakePart('bad-registry-payload')], {}, 1000, 'BAD-REGISTRY'
    )
)

bad_root_outer_ns = dict(string_outer_ns)
bad_root_outer_ns['_gemini_last_503_request_info'] = {'request_fingerprint': 'req-bad-root'}
bad_root_outer_ns['_gemini_503_failed_models_by_request'] = ['not-a-dict']
extract_function('_gemini_503_aeussere_retry_aktion', bad_root_outer_ns)
assert_type_error(
    'outer retry invalid registry root',
    lambda: bad_root_outer_ns['_gemini_503_aeussere_retry_aktion']('503 UNAVAILABLE', 'model-a')
)

bad_root_quota_ns = dict(quota_ns)
bad_root_quota_ns['_gemini_503_failed_models_by_request'] = ['not-a-dict']
bad_root_quota_ns['_gemini_503_request_fingerprint'] = lambda contents, system: 'req-bad-root-quota'
extract_function('_gemini_generate_content_quota_safe', bad_root_quota_ns)
assert_type_error(
    'quota-safe invalid registry root',
    lambda: bad_root_quota_ns['_gemini_generate_content_quota_safe'](
        None, 'model-b', [FakePart('bad-root-payload')], {}, 1000, 'BAD-ROOT'
    )
)

bad_collection_ns = dict(string_quota_ns)
bad_collection_ns['_gemini_503_failed_models_by_request'] = {'req-string-quota': ['model-a', 7]}
extract_function('_gemini_generate_content_quota_safe', bad_collection_ns)
assert_type_error(
    'quota-safe mixed-type collection',
    lambda: bad_collection_ns['_gemini_generate_content_quota_safe'](
        None, 'model-b', [FakePart('mixed-registry-payload')], {}, 1000, 'MIXED-REGISTRY'
    )
)
print('PASS: registry strings normalize as one model; malformed roots/entries/collections raise TypeError')
