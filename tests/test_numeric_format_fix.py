from pathlib import Path
import ast

p = Path('/mnt/data/offene_positionen_check_TAB2_Zahlenformat_FIX_FINAL_20260910.py')
t = p.read_text(encoding='utf-8')
assert 'def _apply_numeric_formats(' in t
assert 'pattern": \'0.00" %"\'' in t
assert 'pattern": "0.00"' in t
assert '_apply_numeric_formats(sheets, spreadsheet_id, "Offene Positionen+Check", HEADERS)' in t
assert '_apply_numeric_formats(sheets, spreadsheet_id, "Geschlossene Positionen", HISTORY_HEADERS)' in t
ast.parse(t)
print('PASS: gezielter Zahlenformat-Fix vorhanden, Syntax/AST ok.')
