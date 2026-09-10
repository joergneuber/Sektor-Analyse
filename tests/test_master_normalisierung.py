import ast
import re
from pathlib import Path

SRC = Path(__file__).parents[1].joinpath('gemini_auswertung.py').read_text(encoding='utf-8')
tree = ast.parse(SRC)
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'normalisiere_ausgabe')
ns = {
    're': re,
    'RuntimeError': RuntimeError,
    '_normalisiere_7_4_numerische_ausgabe': lambda x: x,
    '_normalisiere_positionsname': lambda x: re.sub(r'[^a-z0-9äöüß]+', ' ', str(x).lower()).strip(),
    '_normalisiere_ticker': lambda x: re.sub(r'[^a-z0-9.=-]+', '', str(x).lower().strip()),
    '_positionsfeld_schluessel': lambda x: re.sub(r'[^a-z0-9]+', '', str(x).lower()),
    '_finde_quellposition': lambda key, expected: expected.get(key),
}
exec(compile(ast.Module(body=[node], type_ignores=[]), '<test>', 'exec'), ns)

expected = {
    ('amundi msci new energy ucits etf dist', 'nrj.pa', '3804', '03022026'): {
        'name': 'Amundi MSCI New Energy UCITS ETF Dist', 'ticker': 'NRJ.PA',
        'entry': '38,04€', 'date': '03.02.2026', 'market': 'EU', 'direction': 'Long',
        'source': 'Langfrist', 'technical': {'Technische_Zielzone': '45,00€'}},
    ('ishares core msci world ucits etf usd acc', 'eunl.de', '10804', '02022022'): {
        'name': 'iShares Core MSCI World UCITS ETF USD (Acc)', 'ticker': 'EUNL.DE',
        'entry': '108,04€', 'date': '02.02.2022', 'market': 'EU', 'direction': 'Long',
        'source': 'Langfrist', 'technical': {'Technische_Zielzone': '135,00€'}},
}

text = '''7. OFFENE POSITIONEN

Amundi MSCI New Energy UCITS ETF Dist (NRJ.PA) | Markt: EU
Einstieg: 38,04€ (03.02.2026)

Amundi MSCI New Energy UCITS ETF Dist (NRJ.PA) | Markt: EU
Einstieg: 38,04€ (03.02.2026)

8. SONSTIGES
foo
'''
out = ns['normalisiere_ausgabe'](text, expected)
assert out.count('Amundi MSCI New Energy UCITS ETF Dist (NRJ.PA) | Markt: EU') == 1
assert out.count('iShares Core MSCI World UCITS ETF USD (Acc) (EUNL.DE) | Markt: EU') == 1
assert 'Technische Zielzone: 135,00€' in out
print('MASTER_NORMALISIERUNG_TEST: PASS')
