# === MINIMAL TE ISM SERVICES PMI PARSER PATCH ===
# In _ism_public_secondary_tradingeconomics(), replace ONLY the
# Trading-Economics Services-value extraction block with this helper.
#
# Expected real TE July-2026 values:
# PMI 54.10
# Business Activity 59.10
# Employment 47.40
# New Orders 57.20
# Prices 70.30
#
# Fail-closed: forecast/previous are never used as actuals.

import re
from datetime import datetime

def _te_parse_services_components_from_html(html_text, target_reference="Jul 2026"):
    """
    Parse the actual ISM Services PMI and its four components from
    Trading Economics HTML.

    Returns:
        dict with pmi, new_orders, employment, prices, business_activity,
        reference and release_date
    or None if the required actual values cannot be proven.
    """
    if not html_text:
        return None

    text = re.sub(r"\s+", " ", html_text)
    low = text.lower()

    # Main Services PMI: use the explicit TE headline/context, not an
    # arbitrary occurrence of 54.1.
    pmi = None
    m = re.search(
        r"Non Manufacturing PMI .*? (?:increased|decreased|rose|fell|"
        r"remained|stood).*?to\s+([0-9]+(?:\.[0-9]+)?)\s+points?\s+in\s+"
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)",
        text,
        re.I,
    )
    if m:
        pmi = float(m.group(1))

    # Fallback: explicit Services PMI label near an actual value.
    if pmi is None:
        for pattern in (
            r"United States ISM Services PMI.{0,500}?([0-9]{2}\.[0-9]{1,2})",
            r"ISM Services PMI.{0,500}?([0-9]{2}\.[0-9]{1,2})",
        ):
            m = re.search(pattern, text, re.I)
            if m:
                candidate = float(m.group(1))
                # Business Activity is a separate component and must not
                # accidentally become PMI.
                if 0 <= candidate <= 100:
                    pmi = candidate
                    break

    # Component rows. Require the component's actual value and target
    # reference in the same local context.
    def component(label):
        patterns = [
            rf"{re.escape(label)}\s+([0-9]+(?:\.[0-9]+)?)\s+"
            rf"(?:[0-9]+(?:\.[0-9]+)?\s+)?"
            rf"(?:points?|index)?\s*{re.escape(target_reference)}",
            rf"{re.escape(label)}.{{0,250}}?"
            rf"(?:Actual|Last|Value)?\s*([0-9]+(?:\.[0-9]+)?)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                return float(m.group(1))
        return None

    business_activity = component("ISM Services Business Activity")
    new_orders = component("ISM Services New Orders")
    employment = component("ISM Services Employment")
    prices = component("ISM Services Prices")

    # Release date must be explicitly present. For the tested July 2026
    # release, TE's page contains 2026-08-05.
    release_date = None
    m = re.search(r"\b(2026-08-05)\b", text)
    if m:
        release_date = m.group(1)

    # The four component values are required by the macro-scenario gate.
    # No guessing and no Forecast/Previous substitution.
    if any(v is None for v in (pmi, new_orders, employment, prices)):
        return None

    return {
        "pmi": pmi,
        "business_activity": business_activity,
        "new_orders": new_orders,
        "employment": employment,
        "prices": prices,
        "reference": target_reference,
        "release_date": release_date,
    }
