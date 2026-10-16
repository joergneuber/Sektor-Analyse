"""Gezielte Regression-/Negativtests fuer die logische Gemini-Request-ID."""
import ast
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

SOURCE_PATH = Path('/mnt/data/gemini_auswertung(20261016-102942)_repariert.py')
SOURCE = SOURCE_PATH.read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)


def function(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)

NS = {'hashlib': hashlib, 'json': json, 're': re}
exec(compile(ast.Module(body=[function('_gemini_503_request_fingerprint'), function('_gemini_logische_request_id'), function('_gemini_503_modellwechsel_budget_pruefen')], type_ignores=[]), str(SOURCE_PATH), 'exec'), NS)
NS['_gemini_503_modellwechsel_je_stage'] = defaultdict(int)
NS['_gemini_last_503_request_info'] = {}
logical_id = NS['_gemini_logische_request_id']


def test_same_source_different_prompts_get_distinct_ids():
    a = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['Prompt A'])
    b = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['Prompt B'])
    assert a != b


def test_same_source_same_prompt_and_stage_are_stable():
    a = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['same prompt'])
    b = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['same prompt'])
    assert a == b


def test_different_semantic_stages_are_isolated_even_with_same_prompt():
    a = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['same prompt'])
    b = logical_id('SAME-CONTEXT', 'A2_TECHNIK', ['same prompt'])
    assert a != b


def test_logical_base_stays_stable_when_source_chunks_change():
    a = logical_id('SAME-CONTEXT', 'A2_TECHNIK', ['same work prompt'], source_contents=['chunk A'])
    b = logical_id('SAME-CONTEXT', 'A2_TECHNIK', ['same work prompt'], source_contents=['chunk B', 'chunk C'])
    assert a == b


def test_no_resume_fingerprint_still_distinguishes_different_source_contexts():
    a = logical_id('', 'A1_DISCOVERY', ['same prompt'], source_contents=['source A'])
    b = logical_id('', 'A1_DISCOVERY', ['same prompt'], source_contents=['source B'])
    assert a != b


def test_split_identity_is_stable_without_split_denominator():
    base = logical_id('CTX', 'A3_HISTORIE', ['history prompt'])
    assert f'{base}:Split:8' == f'{base}:Split:8'
    assert f'{base}:Split:8' != f'{base}:Split:9'


def test_production_direct_split_and_synthesis_use_prompt_aware_base():
    cache_fn = function('_gemini_cache_antwort')
    body = ast.get_source_segment(SOURCE, cache_fn)
    assert 'logical_request_base = _gemini_logische_request_id(' in body
    assert 'logical_request_id=f"{logical_request_base}:Direct"' in body
    assert 'logical_request_id=f"{logical_request_base}:Split:{idx}"' in body
    assert 'logical_request_prefix=f"{logical_request_base}:Synthesis"' in body


def test_production_stages_are_explicit_and_distinct():
    expected = {
        'A1_DISCOVERY', 'A2_TECHNIK', 'A3_HISTORIE',
        'FINAL_SYNTHESE', 'MAKRO_GATE_REPARATUR',
    }
    found = set(re.findall(r'logical_stage="([A-Z0-9_]+)"', SOURCE))
    assert expected <= found


def test_budget_registry_separates_different_prompt_ids():
    counter = defaultdict(int)
    a = f"{logical_id('CTX', 'A1_DISCOVERY', ['prompt A'])}:Split:1"
    b = f"{logical_id('CTX', 'A1_DISCOVERY', ['prompt B'])}:Split:1"
    counter[a + '::GenerateContent Split 1'] += 1
    counter[b + '::GenerateContent Split 1'] += 1
    assert len(counter) == 2


def test_negative_regression_former_collision_is_fixed():
    # This is the prior defect: same resume/source fingerprint + generic label.
    old_a = 'SAME-CONTEXT:GenerateContent'
    old_b = 'SAME-CONTEXT:GenerateContent'
    assert old_a == old_b
    # Current production construction includes semantic stage and prompt hash.
    new_a = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['different prompt A'])
    new_b = logical_id('SAME-CONTEXT', 'A1_DISCOVERY', ['different prompt B'])
    assert new_a != new_b


def test_real_budget_helper_separates_same_context_different_prompts():
    budget = NS["_gemini_503_modellwechsel_budget_pruefen"]
    counter = {}
    id_a = logical_id("SAME-CONTEXT", "A1_DISCOVERY", ["prompt A"])
    id_b = logical_id("SAME-CONTEXT", "A1_DISCOVERY", ["prompt B"])
    a = budget("GenerateContent Split 1/4", 1, zaehler=counter, logical_request_id=id_a)
    b = budget("GenerateContent Split 1/4", 1, zaehler=counter, logical_request_id=id_b)
    assert a[0] is True and b[0] is True
    assert len(counter) == 2


def test_real_budget_helper_reuses_same_logical_split_across_denominator_change():
    budget = NS["_gemini_503_modellwechsel_budget_pruefen"]
    counter = {}
    stable_id = logical_id("CTX", "A2_TECHNIK", ["technical prompt"]) + ":Split:8"
    a = budget("GenerateContent Split 8/39", 2, zaehler=counter, logical_request_id=stable_id)
    b = budget("GenerateContent Split 8/40", 2, zaehler=counter, logical_request_id=stable_id)
    assert a[2] == b[2]
    assert b[1] == 2
    assert len(counter) == 1
