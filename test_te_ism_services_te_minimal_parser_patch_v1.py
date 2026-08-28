from te_ism_services_te_minimal_parser_patch_v1 import _te_parse_services_components_from_html

html = """
United States ISM Services PMI Non Manufacturing PMI in the United States
increased to 54.10 points in July 2026.
ISM Services Business Activity 59.10 points Jul 2026
ISM Services New Orders 57.20 points Jul 2026
ISM Services Employment 47.40 points Jul 2026
ISM Services Prices 70.30 points Jul 2026
2026-08-05
"""
x = _te_parse_services_components_from_html(html, "Jul 2026")
assert x is not None, x
assert x["pmi"] == 54.10
assert x["business_activity"] == 59.10
assert x["new_orders"] == 57.20
assert x["employment"] == 47.40
assert x["prices"] == 70.30
assert x["release_date"] == "2026-08-05"

bad = """
ISM Services Business Activity 59.10 points Jul 2026
ISM Services New Orders 57.20 points Jul 2026
ISM Services Employment 47.40 points Jul 2026
ISM Services Prices 70.30 points Jul 2026
2026-08-05
"""
assert _te_parse_services_components_from_html(bad, "Jul 2026") is None
print("RESULT=GREEN_MINIMAL_TE_SERVICES_PARSER_REGRESSION")
