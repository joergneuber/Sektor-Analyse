"""Strict structural validation for the production Trading-Radar report."""
from __future__ import annotations

import re
import sys
import json
import hashlib
import math
import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


SECTION_NAMES = (
    "Trading-Radar",
    "Thesen-Update",
    "Ereignisradar",
    "Assetklassenvergleich",
    "Technische Kandidaten",
    "Portfolio & Risiko",
    "MSCI-Vergleich",
    "Daten-/Prüfanhang",
)

# Accept the legacy report's numbered dot and the specification's middle dot,
# but only as a numbered heading line (not an incidental body-text mention).
HEADER_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?P<number>[1-8])\s*[.·]\s*(?P<title>[^\r\n]+?)\s*$"
)



def _section_body(text: str, number: int, title: str) -> str:
    """Return the body of one canonical numbered section, bounded by the next heading."""
    expected = {index: name.casefold() for index, name in enumerate(SECTION_NAMES, start=1)}
    headings = [
        match for match in HEADER_RE.finditer(text)
        if expected.get(int(match.group("number"))) == match.group("title").strip().casefold()
    ]
    start = next((m for m in headings if int(m.group("number")) == number
                  and m.group("title").strip().casefold() == title.casefold()), None)
    if start is None:
        return ""
    end = next((m.start() for m in headings if m.start() > start.start()
                and int(m.group("number")) > number), len(text))
    return text[start.end():end]

def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns)

def _normalize_tab2_entry_date(value: str) -> str | None:
    """Return an ISO date only for dates accepted by the Tab-2 data contract."""
    raw = str(value or "").strip()
    if not raw or _MISSING_VALUE_RE.fullmatch(raw):
        return None
    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(raw, date_format).date().isoformat()
        except ValueError:
            continue
    return None


_MISSING_VALUE_RE = re.compile(
    r"^(?:|[-–—]|n/?a|na|tbd|todo|xxx|placeholder|platzhalter|unbekannt|"
    r"nicht\s+(?:vorhanden|verfügbar|verifizierbar|bekannt)|fehlt|unklar)$",
    re.IGNORECASE,
)


def _label_value(text: str, labels: tuple[str, ...]) -> str | None:
    """Extract a labeled value without accepting an empty/placeholder value.

    Values may be inline (separated by | or ;) or on the following line.
    The extraction is intentionally conservative: a label without a value is
    not evidence that the field is present.
    """
    label = r"(?:" + "|".join(labels) + r")"
    match = re.search(
        rf"(?im)(?:^|[|;]\s*|\n)\s*(?:[-*•]\s+|\d+[.)]\s+)?"
        rf"(?:\*\*)?{label}(?:\*\*)?\s*[:=](?:\*\*)?\s*"
        rf"(?:([^|;\r\n]+)|(?:\r?\n\s*([^|;\r\n]+)))",
        text,
    )
    if not match:
        return None
    value = (match.group(1) or match.group(2) or "").strip().strip(". ")
    if _MISSING_VALUE_RE.fullmatch(value):
        return None
    return value


def _is_explicit_unavailable(text: str, labels: tuple[str, ...]) -> bool:
    """Allow an explicit unavailable state, but not a blank or vague mention."""
    label = r"(?:" + "|".join(labels) + r")"
    return bool(re.search(
        rf"(?i)(?:\*\*)?(?:{label})(?:\*\*)?\s*(?:[:=](?:\*\*)?\s*)?"
        rf"(?:nicht\s+(?:verfügbar|verifizierbar|vorhanden)|fehlt|unklar)\b",
        text,
    ))


def _valid_numeric_value(value: str | None) -> bool:
    """Accept a complete numeric field, optionally with a known unit/currency.

    Do not search for a number inside arbitrary prose (e.g. ``bald 100``).
    Decimal comma and common thousands separators are supported.
    """
    if value is None:
        return False
    cleaned = value.strip()
    if not cleaned or _MISSING_VALUE_RE.fullmatch(cleaned):
        return False

    # Explicitly supported wrappers; the numeric core must occupy the entire
    # remaining field. Units are deliberately limited to common price/ratio
    # labels rather than accepting arbitrary trailing prose.
    cleaned = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*(?:EUR|USD|GBP|€|\$|£|%|x|\b(?:Punkte|pts)\b)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    if not re.fullmatch(r"[-+]?(?:\d{1,3}(?:[ .’']\d{3})+|\d+)(?:[,.]\d+)?", cleaned):
        return False

    normalized = cleaned.replace(" ", "").replace("’", "").replace("'", "")
    # A dot/comma followed by exactly three digits is treated as a grouping
    # separator when another grouping separator exists; otherwise it is the
    # decimal separator. This handles common German and English report values.
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        number = float(normalized)
        return number == number and abs(number) != float("inf")
    except (TypeError, ValueError):
        return False



# Canonical recognizer shared with the production provenance enricher.
# Explicit US labels are accepted everywhere. The historical entity-resolver
# form "(10Y) Treasury" is accepted only inside the executive Zinsen/Renditen
# curve row when that same row contains the US-specific TIPS anchor.
_US10Y_EXPLICIT_LABEL_RE = re.compile(
    r"(?i)(?:"
    r"US\s*[- ]?\s*10Y(?:\s*[- ]?Treasury)?"
    r"|US\s*[- ]?\s*10J(?:\s*[- ]?Rendite)?"
    r"|10Y\s*[- ]?\s*US\s*[- ]?Treasury"
    r"|10\s*[- ]?year\s+US\s+Treasury"
    r"|10\s*[- ]?year\s+US\s+yield"
    r"|10\s*[- ]?jährig(?:er|e|en)?\s+US\s*[- ]?\s*(?:Treasury(?:\s*[- ]?\s*Zins)?|Staatsanleihen?zins|Rendite)"
    r")"
)
_US10Y_RESOLVER_LABEL_RE = re.compile(
    r"(?i)(?:\(\s*10Y\s*\)\s*[- ]?\s*Treasury|10Y\s*\)\s*[- ]?\s*Rendite)"
)
_US10Y_VALUE_AFTER_LABEL_RE = re.compile(
    r"(?i)^\s*(?:\([^)]*\)\s*)?(?:(?::|=|bei\b|liegt\s+bei\b|beträgt\b|betrug\b|auf\b|von\b|at\b|is\b)\s*)?"
    r"(?P<value>[+-]?\d+(?:[,.]\d+)?)\s*(?:%|Prozent)(?!\w)"
)
_FOREIGN_10Y_LABEL_RE = re.compile(
    r"(?i)\b(?:UK|U\.K\.|United\s+Kingdom|Britain|British|Japan(?:ese)?|"
    r"Germany|German|Bund(?:es)?|France|French|Italy|Italian|Canada|Canadian|"
    r"Australia|Australian|China|Chinese|India|Indian|Spain|Spanish|"
    r"Switzerland|Swiss)\b[^\r\n]{0,35}\b(?:10Y|10J|10\s*[- ]?year|10\s*[- ]?jährig)"
)


def _is_run1270_us_curve_context(line: str) -> bool:
    """Recognize only the known normalized US curve row from Run #1270."""
    return bool(
        isinstance(line, str)
        and re.search(r"(?i)^\s*Zinsen/Renditen\s*:", line)
        and re.search(r"(?i)\bTIPS\b", line)
        and not _FOREIGN_10Y_LABEL_RE.search(line)
    )



def _is_us10y_conflict_line(line: str) -> bool:
    """Recognize paired US-10Y observations, including the temporal form."""
    return bool(
        isinstance(line, str)
        and re.search(
            r"(?i)(?:QUELLENKONFLIKT\s*:|ZEITLICH UNTERSCHIEDLICHE BEOBACHTUNGEN|"
            r"GETRENNT DOKUMENTIERTE US-10Y-BEOBACHTUNGEN)",
            line,
        )
        and re.search(r"(?i)Instrument\s*:\s*US\s*[- ]?10Y\s+Treasury", line)
    )


def _balanced_parenthetical(text: str, start: int) -> str:
    """Return balanced parenthetical content starting at ``start``; nested pairs are supported."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "(":
        return ""
    stack = []
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            stack.append(index)
        elif char == ")":
            if not stack:
                return ""
            stack.pop()
            if not stack:
                return text[start + 1:index]
    return ""


def _us10y_conflict_observation_records(line: str) -> list[tuple[str, Decimal, str]]:
    """Return independently attributable Makro/Live records, retaining their role."""
    if not _is_us10y_conflict_line(line):
        return []

    records = []
    field_patterns = {
        "Quelle": r"(?:Quelle|Source)",
        "Abrufzeit": r"(?:Abruf|Abrufzeit|Abrufzeitpunkt|Retrieved(?:_At)?)",
        "Datenstichtag": r"(?:Datenstand|Datenstichtag|Beobachtungsdatum|Observation_Date)",
        "Einheit": r"(?:Einheit|Unit)",
        "Verzögerung": r"(?:Verzögerung|Verzoegerung|Delay(?:_Days)?)",
    }
    role_labels = {
        "Makro": r"(?:Makro-Wert|FRED-Beobachtung)",
        "Live": r"(?:Live-Wert|Yahoo-Beobachtung)",
    }
    for role in ("Makro", "Live"):
        value_match = re.search(
            rf"(?i)\b{role_labels[role]}\s*:\s*([+-]?\d+(?:[,.]\d+)?)\s*%",
            line,
        )
        if not value_match:
            continue
        try:
            value = Decimal(value_match.group(1).replace(",", "."))
        except InvalidOperation:
            continue
        if not value.is_finite():
            continue

        provenance_text = ""
        after_value = line[value_match.end():]
        if role == "Makro":
            explicit = re.search(r"(?i)(?:Makro-Provenienz|Provenienz)\s*:\s*", after_value)
            if explicit:
                # The marker itself is inside the Makro annotation. Extract
                # the complete outer balanced group first, then remove only
                # the marker; otherwise its closing ')' is misread as a
                # malformed field value (and URL parentheses can confuse a
                # naive first-close split).
                outer = _balanced_parenthetical(after_value, 0)
                if outer:
                    provenance_text = re.sub(
                        r"(?i)^\s*(?:Makro-Provenienz|Provenienz)\s*:\s*", "", outer, count=1
                    )
                else:
                    candidate = after_value[explicit.end():]
                    candidate = re.split(
                        r"(?i)\|\s*Live-Wert\s*:", candidate, maxsplit=1
                    )[0]
                    provenance_text = candidate.strip().rstrip(")").strip()
            else:
                # Some producers place the macro annotation directly after
                # Makro-Wert. Parse it, but do not infer metadata from the Live side.
                provenance_text = _balanced_parenthetical(after_value, 0)
        else:
            provenance_text = _balanced_parenthetical(after_value, 0)

        parsed_fields, field_conflicts = _top_level_provenance_values(
            provenance_text, allow_first=True
        )
        field_names = {
            "source": "Quelle",
            "retrieved": "Abrufzeit",
            "observed": "Datenstichtag",
            "unit": "Einheit",
            "delay": "Verzögerung",
        }
        fields = [
            f"{field_names[key]}: {parsed_fields[key]}"
            for key in field_names if key in parsed_fields
        ]
        if field_conflicts:
            fields.append("Quelle: MEHRFACH WIDERSPRÜCHLICHE ANGABEN")
        synthetic = f"US 10Y Treasury {value}%"
        if fields:
            synthetic += " | " + " | ".join(fields)

        records.append((role, value.normalize(), synthetic))
    return records

def extract_us10y_observations(
    line: str, *, allow_run1270_context: bool = False
) -> list[Decimal]:
    """Extract nominal US-10Y observations with an explicit geographic basis.

    Normal US labels must say US. The one historical entity-resolver form
    "(10Y) Treasury 4,98%" is allowed only when the caller has established the
    Run #1270 US yield-curve row, whose same line contains the US-specific TIPS
    observation. Foreign-country 10Y labels are never treated as US yields.
    """
    if not isinstance(line, str) or not line.strip():
        return []
    if _is_us10y_conflict_line(line):
        # Paired observation rows contain two independent observations.
        # Extract each role explicitly; never let Yahoo metadata stand in for
        # FRED provenance, even when the dates differ.
        return [value for _, value, _ in _us10y_conflict_observation_records(line)]
    label_matches = list(_US10Y_EXPLICIT_LABEL_RE.finditer(line))
    if allow_run1270_context and _is_run1270_us_curve_context(line):
        label_matches.extend(_US10Y_RESOLVER_LABEL_RE.finditer(line))
    values: list[Decimal] = []
    for label_match in label_matches:
        tail = line[label_match.end():label_match.end() + 100]
        value_match = _US10Y_VALUE_AFTER_LABEL_RE.match(tail)
        if not value_match:
            continue
        try:
            value = Decimal(value_match.group("value").replace(",", "."))
        except InvalidOperation:
            continue
        if value.is_finite():
            values.append(value.normalize())
    return values


def _us10y_observation_records(
    line: str, *, allow_run1270_context: bool = False
) -> list[tuple[Decimal, str]]:
    """Return value-scoped records when one line contains multiple US-10Y values.

    A single repeated value retains the established whole-line behavior for
    narrative duplicates. Distinct values are scoped independently up to the
    next US-10Y label, so each observation must carry its own local provenance.
    """
    if not isinstance(line, str) or not line.strip():
        return []
    label_matches = list(_US10Y_EXPLICIT_LABEL_RE.finditer(line))
    if allow_run1270_context and _is_run1270_us_curve_context(line):
        label_matches.extend(_US10Y_RESOLVER_LABEL_RE.finditer(line))
    label_matches.sort(key=lambda match: match.start())

    parsed = []
    for match in label_matches:
        value_match = _US10Y_VALUE_AFTER_LABEL_RE.match(line[match.end():match.end() + 100])
        if not value_match:
            continue
        try:
            value = Decimal(value_match.group("value").replace(",", "."))
        except InvalidOperation:
            continue
        if value.is_finite():
            parsed.append((match, value.normalize()))

    if not parsed:
        return []
    distinct_values = {value for _, value in parsed}
    if len(distinct_values) == 1:
        # Preserve the existing rule: narrative repetitions of one already
        # sourced value are not treated as independent observations.
        value = next(iter(distinct_values))
        return [(value, line)]

    # For each distinct value, use its final occurrence. This preserves the
    # established support for repeated narrative mentions before the actual
    # source-stamped occurrence, while preventing metadata from a later,
    # different Treasury value from being borrowed.
    last_by_value = {}
    for match, value in parsed:
        last_by_value[value] = match
    records = []
    for value, match in last_by_value.items():
        next_labels = [candidate.start() for candidate in label_matches if candidate.start() > match.start()]
        end = min(next_labels) if next_labels else len(line)
        # Keep the original row prefix for the Run #1270 resolver context and
        # let _us10y_provenance_fields select this final label in the slice.
        records.append((value, line[:end]))
    return records



_US10Y_PROVENANCE_FIELD_RE = re.compile(
    r"(?i)^\s*(Quelle|Source|Abruf|Abrufzeit|Abrufzeitpunkt|Retrieved(?:_At)?|"
    r"Datenstand|Datenstichtag|Beobachtungsdatum|Observation_Date|DATA_DATE|"
    r"Einheit|Unit|Verzögerung|Verzoegerung|Delay(?:_Days)?)\s*[:=]\s*(.*?)\s*$"
)


def _balanced_parenthetical_span(text: str, start: int):
    """Return (open, close, content) for a genuinely balanced nested group."""
    if start >= len(text) or text[start] != "(":
        return None
    stack = []
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            stack.append(index)
        elif char == ")":
            if not stack:
                return None
            stack.pop()
            if not stack:
                return start, index, text[start + 1:index]
    return None


def _is_opaque_parenthetical_segment(value: str) -> bool:
    stripped = (value or "").strip()
    span = _balanced_parenthetical_span(stripped, 0) if stripped.startswith("(") else None
    return bool(span and span[1] == len(stripped) - 1)


def _top_level_provenance_values(fragment: str, *, allow_first: bool):
    """Parse only complete fields at top-level segment starts; retain duplicates."""
    values = {}
    conflicts = set()
    stack = []
    start = 0
    delimiter = ""
    parts = []
    malformed = False
    for index, char in enumerate(fragment):
        if char == "(":
            stack.append(index)
        elif char == ")":
            if not stack:
                malformed = True
                break
            stack.pop()
        elif char in "|;" and not stack:
            parts.append((delimiter, fragment[start:index]))
            delimiter = char
            start = index + 1
    if stack:
        malformed = True
    if malformed:
        return {}, {"MALFORMED"}
    parts.append((delimiter, fragment[start:]))
    started = False
    first_segment = True
    for delim, part in parts:
        if first_segment and not allow_first:
            first_segment = False
            if part.strip().strip(".!?"):
                # Only an opaque parenthetical or malformed group is a hard
                # foreign-context boundary. Ordinary trailing prose stops
                # row-tail parsing but does not invalidate an adjacent local
                # provenance annotation.
                if _is_opaque_parenthetical_segment(part):
                    return {}, {"FOREIGN_CONTEXT"}
                stack_check = []
                malformed = False
                for char in part:
                    if char == "(":
                        stack_check.append(char)
                    elif char == ")":
                        if not stack_check:
                            malformed = True
                            break
                        stack_check.pop()
                if malformed or stack_check:
                    return {}, {"MALFORMED"}
                return {}, {"UNSCOPED_CONTEXT"}
            continue
        first_segment = False
        match = _US10Y_PROVENANCE_FIELD_RE.fullmatch(part)
        if not match:
            # Once an unrelated top-level segment appears, later fields may
            # belong to another indicator. Never resume provenance parsing.
            if _is_opaque_parenthetical_segment(part):
                # Opaque notes may describe another indicator. Invalidate
                # fields already seen as well as every field after the note.
                values.clear()
                conflicts.add("FOREIGN_CONTEXT")
                break
            if part.strip():
                break
            continue
        started = True
        raw_key = match.group(1).casefold()
        if raw_key in {"quelle", "source"}:
            key = "source"
        elif raw_key in {"abruf", "abrufzeit", "abrufzeitpunkt", "retrieved_at"}:
            key = "retrieved"
        elif raw_key in {"datenstand", "datenstichtag", "beobachtungsdatum", "observation_date", "data_date"}:
            key = "observed"
        elif raw_key in {"einheit", "unit"}:
            key = "unit"
        else:
            key = "delay"
        value = match.group(2).strip().rstrip(".")
        normalized = re.sub(r"\s+", " ", value).casefold()
        if key in values and values[key][1] != normalized:
            conflicts.add(key)
        elif key not in values:
            values[key] = (value, normalized)
    return {key: item[0] for key, item in values.items()}, conflicts


def _us10y_provenance_fields(line: str):
    """Read local US10Y annotation and top-level row-tail fields only."""
    if not isinstance(line, str):
        return {}, {"MALFORMED"}
    label_matches = list(_US10Y_EXPLICIT_LABEL_RE.finditer(line))
    if not label_matches and _is_run1270_us_curve_context(line):
        label_matches = list(_US10Y_RESOLVER_LABEL_RE.finditer(line))
    if not label_matches:
        return {}, set()
    # When the same observation is repeated narratively before one shared
    # row-end provenance block, the metadata follows the final occurrence.
    # Parsing from the first label would treat the intervening prose as an
    # unscoped foreign context and incorrectly discard that shared block.
    for label in reversed(label_matches):
        value_match = _US10Y_VALUE_AFTER_LABEL_RE.match(line[label.end():label.end() + 100])
        if not value_match:
            continue
        value_end = label.end() + value_match.end()
        cursor = value_end
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
        combined = {}
        conflicts = set()
        annotation = _balanced_parenthetical_span(line, cursor) if cursor < len(line) and line[cursor] == "(" else None
        if annotation:
            local, local_conflicts = _top_level_provenance_values(annotation[2], allow_first=True)
            # An adjacent parenthetical comment with no US10Y provenance is
            # an attribution boundary, not a harmless wrapper around later
            # row-tail fields.
            if annotation[2].strip() and not local:
                return {}, {"FOREIGN_CONTEXT"}
            combined.update(local)
            conflicts.update(local_conflicts)
            suffix_start = annotation[1] + 1
        else:
            suffix_start = value_end
        suffix, suffix_conflicts = _top_level_provenance_values(line[suffix_start:], allow_first=False)
        # Ordinary prose after a complete local annotation terminates row-tail
        # parsing, but is not itself a provenance conflict. The parser returns
        # no suffix fields at that boundary, so later metadata cannot be
        # borrowed. Keep hard foreign-context and malformed conflicts fatal.
        if annotation and local and suffix_conflicts == {"UNSCOPED_CONTEXT"}:
            suffix_conflicts = set()
        for key, value in suffix.items():
            if key in combined and re.sub(r"\s+", " ", combined[key]).casefold() != re.sub(r"\s+", " ", value).casefold():
                conflicts.add(key)
            elif key not in combined:
                combined[key] = value
        conflicts.update(suffix_conflicts)
        return combined, conflicts
    return {}, set()


def _treasury_observation_has_complete_provenance(line: str) -> bool:
    """Require concrete, complete provenance values on the same observation line."""
    if "MEHRERE_WERTE_NICHT_EINDEUTIG_ZUORDENBAR" in line:
        return False
    extracted, conflicting_fields = _us10y_provenance_fields(line)
    if conflicting_fields:
        return False


    placeholder_re = re.compile(
        r"(?i)\b(?:NICHT\s+VERF(?:Ü|UE)GBAR|NICHT\s+VERFUEGBAR|"
        r"NICHT\s+BEKANNT|UNBEKANNT|FEHLT|OFFEN|"
        r"SIEHE\s+OBEN|WIE\s+OBEN|UNKNOWN|NOT\s+AVAILABLE|"
        r"N/?A|TBD|NONE|NULL|KEINE\s+ANGABE|WIRD\s+NACHGEREICHT|"
        r"NOCH\s+ZU\s+PRÜFEN|NOCH\s+ZU\s+PRUEFEN|"
        r"QUELLE\s+VORHANDEN|DATENQUELLE\s+INTERN|"
        r"MEHRFACH\s+WIDERSPRÜCHLICHE(?:\s+ANGABEN)?)\b"
    )
    required_fields = {"source", "retrieved", "observed", "unit", "delay"}
    if not required_fields.issubset(extracted) or any(
        not extracted[key] or placeholder_re.search(extracted[key])
        for key in required_fields
    ):
        return False

    # A source must identify both a recognizable provider and a concrete
    # series/ticker/instrument. A bare URL, provider name, or generic label is
    # not source provenance. Provider + identifier may include a URL as a
    # supporting locator, but the URL alone is insufficient.
    source = extracted["source"]
    # DFII10 is the 10-year TIPS real yield, never nominal US10Y.
    # Reject it independently of provider naming, including mixed identifiers.
    if re.search(r"(?i)(?<![A-Z0-9])DFII10(?![A-Z0-9])", source):
        return False
    series_ids = re.findall(
        r"(?i)(?<![A-Z0-9])[A-Z]{1,8}\d+[A-Z0-9._-]*(?![A-Z0-9])", source
    )
    normalized_series_ids = {series_id.upper() for series_id in series_ids}
    if "DGS10" in normalized_series_ids and normalized_series_ids != {"DGS10"}:
        return False
    # A FRED-sourced nominal US10Y observation must identify DGS10 exactly.
    if re.search(r"(?i)\b(?:FRED|Federal\s+Reserve\s+Economic\s+Data)\b", source):
        if normalized_series_ids != {"DGS10"}:
            return False
    provider_re = re.compile(
        r"(?i)\b(?:FRED|Federal\s+Reserve\s+Economic\s+Data|"
        r"Yahoo\s+Finance|Bloomberg|Refinitiv|LSEG|Federal\s+Reserve(?:\s+Board)?(?:\s+H\.?15)?|U\.?S\.?\s+Treasury|"
        r"US\s+Treasury|Treasury\.gov|Investing\.com|Trading\s+Economics|"
        r"MarketWatch|Stooq)\b"
    )
    provider_match = provider_re.search(source)
    if not provider_match:
        return False
    source_without_url = re.sub(r"(?i)https?://\S+", " ", source)
    source_without_provider = provider_re.sub(" ", source_without_url)
    # A concrete series/ticker normally contains a digit or a ticker marker
    # (e.g. DGS10, USGG10YR, ^TNX). Provider + URL does not meet this test.
    if not re.search(r"(?i)(?:\b[A-Z]{0,8}\d+[A-Z0-9._-]*\b|\^[A-Z0-9.^_-]{2,})", source_without_provider):
        return False
    if extracted["unit"].casefold() not in {"%", "prozent", "percent"}:
        return False

    try:
        retrieved = datetime.datetime.fromisoformat(
            extracted["retrieved"].replace("Z", "+00:00")
        )
        if retrieved.tzinfo is None or retrieved.utcoffset() is None:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    try:
        observed_raw = extracted["observed"]
        if "T" in observed_raw:
            observed = datetime.datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None:
                return False
        else:
            datetime.date.fromisoformat(observed_raw)
    except (TypeError, ValueError, OverflowError):
        return False

    # Delay must be a concrete numeric duration or one of the explicitly
    # supported market-data cadences. Free-form explanations/statuses are not
    # accepted as a substitute for a measurable lag.
    delay = extracted["delay"].strip()
    delay_match = re.search(
        r"(?i)(?:^|[|;(])\s*(?:Verzögerung|Verzoegerung|Delay(?:_Days)?)\s*[:=]\s*([^|;\r\n]+)(?P<tail>[^|\r\n]*)",
        line,
    )
    if delay_match and placeholder_re.search(delay_match.group("tail")):
        return False
    numeric_delay = re.fullmatch(
        r"(?i)\d+(?:[.,]\d+)?\s*(?:kalendertage?n?|tage?n?|"
        r"börsentage?n?|handelstage?n?|minuten?|stunden?|sekunden?|"
        r"days?|business\s+days?|minutes?|hours?)",
        delay,
    )
    cadence_delay = re.fullmatch(
        r"(?i)(?:Tages[- ]Schlusskurs|Tagesabschluss|End[- ]of[- ]Day|EOD|"
        r"Realtime|Echtzeit|Intraday)",
        delay,
    )
    if not (numeric_delay or cadence_delay):
        return False
    return True


def _validate_m05_m10(text: str, errors: list[str]) -> None:
    """Check concrete M-05..M-10 report evidence; reject blank placeholders."""
    status_markers = re.findall(
        r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text
    )
    report_claims_final = len(status_markers) == 1 and status_markers[0] == "FINAL"
    # M-05: the deterministic Tab-2 source block and row/count comparison are
    # checked in validate_report. Quantity and fees are not contract fields.

    # M-06: each field is evaluated independently. A placeholder cannot be
    # rescued by an unrelated unavailable-status mention elsewhere in the section.
    benchmark = _section_body(text, 7, SECTION_NAMES[6])
    portfolio_labels = (r"Portfolio(?:-Gesamt)?performance", r"Gesamtperformance\s+Portfolio")
    average_labels = (r"(?:Ø|durchschnitt(?:liche|s)?|mittelwert)\s*Positionsperformance",
                      r"Positionsperformance(?:\s*\(?(?:Ø|Durchschnitt|Mittelwert)\)?)?")

    def performance_field_ok(labels):
        label = r"(?:" + "|".join(labels) + r")"
        matches = list(re.finditer(
            rf"(?im)(?:^|[|;]\s*|\n)\s*(?:[-*•]\s*)?{label}\s*[:=]\s*([^|;\r\n]+)",
            benchmark,
        ))
        if matches:
            normalized_values = []
            for match in matches:
                value = match.group(1).strip().strip(". ")
                if _valid_numeric_value(value):
                    numeric = value.strip().replace("%", "").replace(" ", "").replace("’", "").replace("'", "")
                    if "," in numeric and "." in numeric:
                        numeric = numeric.replace(".", "").replace(",", ".") if numeric.rfind(",") > numeric.rfind(".") else numeric.replace(",", "")
                    elif "," in numeric:
                        numeric = numeric.replace(",", ".")
                    try:
                        canonical = f"{float(numeric):.10f}".rstrip("0").rstrip(".")
                    except ValueError:
                        return False
                    normalized_values.append(("numeric", canonical))
                    continue
                if re.fullmatch(
                    r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?",
                    value,
                ):
                    normalized_values.append(("unavailable", "NICHT VERFÜGBAR"))
                    continue
                return False
            return len(set(normalized_values)) == 1
        # Legacy prose form remains accepted only if it is an explicit status.
        return _is_explicit_unavailable(benchmark, labels) or _has_any(
            benchmark,
            (r"Portfolio-Gesamtperformance\s+(?:nicht verfügbar|nicht verifizierbar)",
             r"Positionsperformance\s+(?:nicht verfügbar|nicht verifizierbar)"),
        )

    portfolio_ok = performance_field_ok(portfolio_labels)
    average_ok = performance_field_ok(average_labels)
    if not (portfolio_ok and average_ok):
        errors.append("M-01: Portfolio-Gesamtperformance und durchschnittliche Positionsperformance müssen jeweils einen eindeutigen Zahlenwert oder einen ausdrücklichen Nichtverfügbarkeitsstatus haben.")
    portfolio_numeric = bool(re.search(
        r"(?im)Portfolio(?:-Gesamt)?performance\s*[:=]\s*[-+]?\d",
        benchmark,
    ))
    # The user-defined portfolio metric is the equal-weighted arithmetic mean
    # of each unique trade's percentage return (one unit per trade).
    # Capital weighting and summing percentage points are explicitly forbidden.
    if portfolio_numeric:
        method_ok = bool(
            re.search(
                r"(?is)Berechnungsmethode:.*arithmetischen Mittelwert.*"
                r"individuellen prozentualen Renditen.*jeder Trade zählt genau einmal.*"
                r"genau 1 Stück je Trade.*abgeschlossene Trades zum Ausstiegskurs.*"
                r"offene Trades zum aktuellen Kurs.*Kosten=0.*keine Kapitalgewichtung.*keine Währungsumrechnung",
                benchmark,
            )
            and re.search(
                r"(?im)^Performance-Datenbasis:\s*(\d+) eindeutige Trades; abgeschlossen=(\d+); offen=(\d+)",
                benchmark,
            )
            and re.search(
                r"(?im)^Formel je Trade:.*Long=.*Short=.*Portfolio-Gesamtperformance=.*Anzahl eindeutiger Trades",
                benchmark,
            )
            and re.search(
                r"(?im)^Benchmarkpositionen ausgeschlossen:\s*(?:EUNL\.DE|KEINE)(?:[^\r\n]*)$",
                benchmark,
            )
        )
        count_match = re.search(
            r"(?im)^Performance-Datenbasis:\s*(\d+) eindeutige Trades; abgeschlossen=(\d+); offen=(\d+)",
            benchmark,
        )
        counts_ok = bool(count_match and int(count_match.group(1)) == int(count_match.group(2)) + int(count_match.group(3)))
        if not method_ok or not counts_ok:
            errors.append("M-01: Numerische Gesamtperformance benötigt den dokumentierten gleichgewichteten Mittelwert der Einzelrenditen, genau 1 Stück je Trade, vollständige offene/geschlossene Datenbasis, Benchmark-Abgrenzung und Kosten=0.")
        # Recompute the reported portfolio metric from the per-trade audit rows.
        detail_match = re.search(r"(?im)^Performance-Einzelrenditen:\s*(.+)$", benchmark)
        digest_match = re.search(r"(?im)^Performance-Einzelrenditen-SHA256:\s*([0-9a-f]{64})\s*$", benchmark)
        if not detail_match or detail_match.group(1).strip().upper() == "NICHT VERFÜGBAR":
            errors.append("M-01: Einzelrenditen-Nachweis für die reproduzierbare Portfolioaggregation fehlt.")
        elif not digest_match:
            errors.append("M-01: SHA256-Fingerabdruck der Einzelrenditen fehlt.")
        else:
            detail_payload = detail_match.group(1).strip()
            actual_detail_hash = hashlib.sha256(detail_payload.encode("utf-8")).hexdigest()
            if actual_detail_hash != digest_match.group(1).lower():
                errors.append("M-01: SHA256 der Einzelrenditen stimmt nicht mit dem Detailblock überein.")
            detail_returns = []
            open_returns = []
            detail_keys = set()
            try:
                if count_match is None:
                    raise ValueError("Performance-Datenbasis mit Trade-/Statusanzahl fehlt")
                for item in detail_payload.split(";"):
                    fields = item.split("|")
                    if len(fields) != 8:
                        raise ValueError("falsche Feldanzahl")
                    ticker, entry_date, identity_hash, trade_status, direction, entry_raw, mark_raw, return_raw = fields
                    if ticker.upper() == "EUNL.DE":
                        raise ValueError("externe MSCI-World-Benchmarkposition EUNL.DE darf nicht als System-Trade einfließen")
                    if (not ticker or not _normalize_tab2_entry_date(entry_date)
                            or not re.fullmatch(r"[0-9a-f]{16}", identity_hash)
                            or trade_status not in {"OPEN", "CLOSED"}
                            or direction not in {"LONG", "SHORT"}):
                        raise ValueError("ungültige Trade-Identität/Status/Richtung")
                    identity = (ticker.upper(), entry_date, identity_hash)
                    if identity in detail_keys:
                        raise ValueError("Trade-Identität doppelt")
                    detail_keys.add(identity)
                    entry_value = float(entry_raw)
                    mark_value = float(mark_raw)
                    stated_return = float(return_raw)
                    if not math.isfinite(entry_value) or not math.isfinite(mark_value) or not math.isfinite(stated_return) or entry_value <= 0 or mark_value <= 0:
                        raise ValueError("ungültiger Einstieg/Kurs/Rendite")
                    calculated = ((mark_value - entry_value) if direction == "LONG" else (entry_value - mark_value)) / entry_value * 100.0
                    if abs(calculated - stated_return) > 0.0001:
                        raise ValueError("Einzelrendite stimmt nicht mit Einstieg/Kurs/Richtung überein")
                    detail_returns.append(stated_return)
                    if trade_status == "OPEN":
                        open_returns.append(stated_return)
                if len(detail_returns) != int(count_match.group(1)):
                    raise ValueError("Einzelrenditenanzahl stimmt nicht mit Datenbasis überein")
                if len(detail_returns) != int(count_match.group(2)) + int(count_match.group(3)):
                    raise ValueError("offene/geschlossene Einzelrenditenanzahl widerspricht Datenbasis")
                reproduced = sum(detail_returns) / len(detail_returns)
                portfolio_value_match = re.search(
                    r"(?im)^Portfolio-Gesamtperformance:\s*([+-]?\d+(?:[,.]\d+)?)\s*%",
                    benchmark,
                )
                if not portfolio_value_match:
                    raise ValueError("numerische Portfolio-Gesamtperformance fehlt")
                reported = float(portfolio_value_match.group(1).replace(",", "."))
                if abs(reproduced - reported) > 0.01:
                    raise ValueError("gleichgewichteter Mittelwert stimmt nicht mit Portfolio-Gesamtperformance überein")
                open_value_match = re.search(
                    r"(?im)^Ø Positionsperformance:\s*([+-]?\d+(?:[,.]\d+)?)\s*%",
                    benchmark,
                )
                if open_returns:
                    if not open_value_match:
                        raise ValueError("offener Positionsdurchschnitt fehlt trotz offener Trades")
                    if abs(sum(open_returns) / len(open_returns) - float(open_value_match.group(1).replace(",", "."))) > 0.01:
                        raise ValueError("offener Positionsdurchschnitt stimmt nicht mit offenen Einzelrenditen überein")
                elif open_value_match:
                    raise ValueError("offener Positionsdurchschnitt numerisch trotz null offener Trades")
            except (ValueError, TypeError, OverflowError) as exc:
                errors.append(f"M-01: Einzelrenditen-/Portfolio-Rekonstruktion fehlgeschlagen: {exc}")
        benchmark_match = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?Benchmark-Performance[^\r\n]*?([+-]?\d+(?:[,.]\d+)?)\s*%",
            benchmark,
        )
        outperformance_match = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?Out-/Underperformance:\s*([+-]?\d+(?:[,.]\d+)?)\s*Prozentpunkte",
            benchmark,
        )
        if benchmark_match or re.search(r"(?im)^\s*(?:[-*•]\s*)?Out-/Underperformance:", benchmark):
            live_basis = bool(re.search(
                r"(?im)^Benchmark-Vergleichsbasis:\s*LIVE-SYSTEM\b", benchmark
            ))
            if live_basis:
                system_live_match = re.search(
                    r"(?im)^System-Performance-Live:\s*([+-]?\d+(?:[,.]\d+)?)\s*%",
                    benchmark,
                )
                if benchmark_match and system_live_match:
                    expected_delta = float(system_live_match.group(1).replace(",", ".")) - float(benchmark_match.group(1).replace(",", "."))
                    if not outperformance_match or abs(
                        expected_delta - float(outperformance_match.group(1).replace(",", "."))
                    ) > 0.02:
                        errors.append("M-01: Live-System-Out-/Underperformance stimmt nicht mit System-Ø minus MSCI-Ø im gleichen Trade-Zeitraum überein.")
                elif outperformance_match:
                    errors.append("M-01: Live-System-Out-/Underperformance benötigt belegte System- und MSCI-Werte.")
                elif not re.search(r"(?im)^\s*(?:[-*•]\s*)?Out-/Underperformance:\s*NICHT VERFÜGBAR", benchmark):
                    errors.append("M-01: Fehlende Live-System-/Benchmarkgrundlage muss Out-/Underperformance als NICHT VERFÜGBAR ausweisen.")
            else:
                portfolio_match = re.search(
                    r"(?im)^Portfolio-Gesamtperformance:\s*([+-]?\d+(?:[,.]\d+)?)\s*%",
                    benchmark,
                )
                if benchmark_match and portfolio_match:
                    expected_delta = float(portfolio_match.group(1).replace(",", ".")) - float(benchmark_match.group(1).replace(",", "."))
                    if not outperformance_match or abs(
                        expected_delta - float(outperformance_match.group(1).replace(",", "."))
                    ) > 0.02:
                        errors.append("M-01: Out-/Underperformance stimmt nicht mit Portfolio-Gesamtperformance minus Benchmark überein.")
                elif outperformance_match:
                    errors.append("M-01: Out-/Underperformance darf ohne Portfolio- und Benchmarkwert nicht numerisch ausgewiesen werden.")
                elif not re.search(r"(?im)^\s*(?:[-*•]\s*)?Out-/Underperformance:\s*NICHT VERFÜGBAR", benchmark):
                    errors.append("M-01: Fehlende Portfolio-/Benchmarkgrundlage muss Out-/Underperformance als NICHT VERFÜGBAR ausweisen.")

    # M-01: numeric live-system/benchmark comparisons are independent of the
    # portfolio-wide aggregate. Validate their identity and arithmetic even
    # when Portfolio-Gesamtperformance is explicitly unavailable.
    benchmark_section = _section_body(text, 7, SECTION_NAMES[6])
    if not portfolio_numeric:
        numeric_benchmark = re.search(
            r"(?im)^Benchmark-Performance[^\r\n]*?([+-]?\d+(?:[,.]\d+)?)\s*%",
            benchmark_section,
        )
        numeric_delta = re.search(
            r"(?im)^Out-/Underperformance:\s*([+-]?\d+(?:[,.]\d+)?)\s*Prozentpunkte",
            benchmark_section,
        )
        system_live = re.search(
            r"(?im)^System-Performance-Live:\s*([+-]?\d+(?:[,.]\d+)?)\s*%",
            benchmark_section,
        )
        if numeric_benchmark and (system_live or numeric_delta):
            live_basis = bool(re.search(
                r"(?im)^Benchmark-Vergleichsbasis:\s*LIVE-SYSTEM\b", benchmark_section
            ))
            basis_line = re.search(r"(?im)^Benchmark-Vergleichsbasis:\s*([^\r\n]+)$", benchmark_section)
            if not live_basis or not basis_line or "Benchmark=EUNL.DE" not in basis_line.group(1):
                errors.append("M-01: Numerischer Benchmarkvergleich benötigt die eindeutige Basis LIVE-SYSTEM gegen MSCI World (EUNL.DE).")
            if not system_live:
                errors.append("M-01: Numerischer Live-Systemvergleich benötigt System-Performance-Live.")
            if not numeric_delta:
                errors.append("M-01: Numerischer Live-System-/Benchmarkvergleich benötigt die berechnete Out-/Underperformance in Prozentpunkten.")
            if system_live and numeric_delta:
                expected_delta = float(system_live.group(1).replace(",", ".")) - float(numeric_benchmark.group(1).replace(",", "."))
                stated_delta = float(numeric_delta.group(1).replace(",", "."))
                if abs(expected_delta - stated_delta) > 0.02:
                    errors.append("M-01: Live-System-Out-/Underperformance stimmt nicht mit System-Ø minus MSCI-Ø im gleichen Trade-Zeitraum überein.")

    # M-01: the LIVE-SYSTEM aggregate must reconcile with the independently
    # counted better/worse/tied trade population, not only with its delta.
    live_basis_marker = re.search(
        r"(?im)^Benchmark-Vergleichsbasis:\s*LIVE-SYSTEM\b", benchmark_section
    )
    if live_basis_marker:
        count_control = re.search(
            r"(?im)^Benchmark-Zählkontrolle:\s*besser=(\d+);\s*schlechter=(\d+);"
            r"\s*gleichauf=(\d+);\s*gesamt=(\d+)\s*\.?$",
            benchmark_section,
        )
        if not count_control:
            errors.append("M-01: Live-Benchmark benötigt eine vollständige Zählkontrolle besser/schlechter/gleichauf/gesamt.")
        else:
            better, worse, tied, total = map(int, count_control.groups())
            if total <= 0 or better + worse + tied != total:
                errors.append("M-01: Live-Benchmark-Zählkontrolle ist rechnerisch inkonsistent.")
            source_counts = []
            for label in ("besser", "schlechter", "gleichauf"):
                match = re.search(rf"(?im)^Positionen {label} als MSCI World:\s*(\d+)/(\d+)\s*$", benchmark_section)
                if match:
                    source_counts.append((int(match.group(1)), int(match.group(2))))
            if len(source_counts) == 3 and (
                source_counts != [(better, total), (worse, total), (tied, total)]
            ):
                errors.append("M-01: Live-Benchmark-Zählkontrolle widerspricht den Quellzählungen.")

    # M-01: aggregate system-performance claims outside the canonical
    # section are forbidden when the canonical portfolio metric is unavailable.
    canonical_portfolio = re.search(
        r"(?im)^Portfolio-Gesamtperformance:\s*(NICHT VERFÜGBAR|[+-]?\d+(?:[,.]\d+)?)",
        benchmark,
    )
    if canonical_portfolio and canonical_portfolio.group(1).upper() == "NICHT VERFÜGBAR":
        forbidden_aggregate_claims = (
            r"Live-System-Performance\s*[:=]?\s*[+-]?\d",
            r"System-Offenpositionen[^\r\n]*[+-]?\d+(?:[,.]\d+)?\s*%",
            r"Underperformance gegenüber dem MSCI[^\r\n]*[+-]?\d",
            r"Underperformance\s+(?:des\s+)?Portfolios?[^\r\n]*[+-]?\d+(?:[,.]\d+)?\s*%",
            r"Portfolio[^\r\n]*(?:vs\.?|gegenüber|im\s+Vergleich\s+zu)[^\r\n]*[+-]?\d+(?:[,.]\d+)?\s*%",
            r"Systemperformance\s*[+-]\d+(?:[,.]\d+)?\s*%",
            r"Out-/Underperformance\s*[:=]\s*[+-]?\d",
        )
        section7_heading = re.search(
            r"(?im)^[ \t]*(?:#{1,6}\s*)?7\s*[.·]\s*MSCI-Vergleich[^\r\n]*$",
            text,
        )
        section8_heading = (
            re.search(
                r"(?im)^[ \t]*(?:#{1,6}\s*)?8\s*[.·]\s*Daten-/Prüfanhang[^\r\n]*$",
                text[section7_heading.end():],
            ) if section7_heading else None
        )
        if section7_heading and section8_heading:
            section7_end = section7_heading.end() + section8_heading.start()
            text_outside_benchmark = text[:section7_heading.start()] + text[section7_end:]
        else:
            text_outside_benchmark = text
        for pattern in forbidden_aggregate_claims:
            if re.search(pattern, text_outside_benchmark, re.I):
                errors.append("M-01: Numerische System-/Benchmark-Performance außerhalb der verfügbaren kanonischen Berechnungsgrundlage.")
                break

    # M-01: the same metric may not be numeric in one place and unavailable
    # elsewhere. Detect this report-wide, not only inside the MSCI subsection.
    for metric_label, pattern in (
        ("Portfolio-Gesamtperformance", r"Portfolio(?:-Gesamt)?performance"),
        ("Ø Positionsperformance", r"(?:Ø|durchschnitt(?:liche|s)?)\s*Positionsperformance"),
    ):
        observations = []
        for line in text.splitlines():
            if re.search(pattern, line, re.I) and re.search(r"(?i)(?:NICHT VERFÜGBAR|FEHLT|UNKLAR)", line):
                observations.append(("unavailable", line.strip()))
            elif re.search(pattern, line, re.I):
                numeric = re.search(r"[-+]?\d+(?:[,.]\d+)?\s*%", line)
                if numeric:
                    raw_number = numeric.group(0).replace("%", "").replace(",", ".").strip()
                    try:
                        canonical = f"{float(raw_number):.10f}".rstrip("0").rstrip(".")
                    except ValueError:
                        canonical = raw_number
                    observations.append(("numeric", canonical))
        kinds = {kind for kind, _ in observations}
        if len(kinds) > 1:
            errors.append(f"M-01: {metric_label} wird im Bericht zugleich numerisch und als NICHT VERFÜGBAR ausgewiesen.")
        numeric_values = []
        for kind, value in observations:
            if kind == "numeric":
                numeric_values.append(value)
        if len(set(numeric_values)) > 1:
            errors.append(f"M-01: {metric_label} hat widersprüchliche numerische Mehrfachangaben.")

    # M-07: identity/version/status values must be populated, not merely labels.
    thesis = _section_body(text, 2, SECTION_NAMES[1])
    thesis_id = _label_value(thesis, (r"Thesen-ID", r"thesis_id"))
    version = _label_value(thesis, (r"Version", r"version"))
    status = _label_value(thesis, (r"Status", r"status"))
    # The validated machine-readable register is also a valid source for these
    # identity fields. JSON keys are quoted, so the prose label parser above
    # intentionally cannot see them; parse only the exact marked block.
    structured_identity_valid = False
    if not (thesis_id and version and status):
        # Readable report variant: one compact line per thesis, e.g.
        # "- THESIS_ID | Version 3 | Status BESTÄTIGT | Konfidenz mittel".
        compact_rows = re.findall(
            r"(?im)^\s*-\s*([A-Za-z0-9_.:-]+)\s*\|\s*Version\s+(\d+)\s*"
            r"\|\s*Status\s+([A-ZÄÖÜ]+)\b",
            thesis,
        )
        allowed_statuses = {"NEU", "BESTÄTIGT", "WIDERLEGT", "ABGESCHWÄCHT", "ARCHIVIERT", "UNVERÄNDERT"}
        if compact_rows and all(
            int(row_version) >= 1 and row_status.upper() in allowed_statuses
            for _row_id, row_version, row_status in compact_rows
        ):
            thesis_id = thesis_id or ", ".join(row_id for row_id, _version, _status in compact_rows)
            version = version or ", ".join(row_version for _row_id, row_version, _status in compact_rows)
            status = status or ", ".join(row_status for _row_id, _version, row_status in compact_rows)
            structured_identity_valid = True
        blocks = re.findall(
            r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", thesis,
            flags=re.DOTALL,
        )
        if len(blocks) == 1:
            try:
                payload = json.loads(blocks[0])
                records = payload.get("theses", []) if isinstance(payload, dict) else []
                if isinstance(records, list) and records and all(isinstance(item, dict) for item in records):
                    allowed_statuses = {"NEU", "BESTÄTIGT", "WIDERLEGT", "ABGESCHWÄCHT", "ARCHIVIERT", "UNVERÄNDERT"}
                    valid_records = all(
                        str(item.get("thesis_id", "")).strip()
                        and isinstance(item.get("version"), int) and item.get("version", 0) >= 1
                        and str(item.get("status", "")).strip().upper() in allowed_statuses
                        for item in records
                    )
                    if valid_records:
                        thesis_id = thesis_id or ", ".join(str(item["thesis_id"]).strip() for item in records)
                        version = version or ", ".join(str(item["version"]) for item in records)
                        status = status or ", ".join(str(item["status"]).strip() for item in records)
                        structured_identity_valid = True
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    version_match = re.search(r"\d+", str(version or ""))
    status_ok = bool(status and re.search(
        r"(?i)\b(?:NEU|AKTUALISIERT|BESTÄTIGT|ABGESCHWÄCHT|WARTET AUF EREIGNIS|INVALIDIERT|ERSETZT|FEHLT|UNKLAR|NICHT VERFÜGBAR)\b",
        status,
    ))
    no_thesis_update = _has_any(thesis, (
        r"keine\s+neue\s+oder\s+aktualisierte\s+these\s*[;,.]?\s*"
        r"thesenregister\s+geprüft,?\s*keine\s+änderung",
        r"keine\s+neue\s+oder\s+aktualisierte\s+these\s*[;,.]?\s*"
        r"thesenregister\s+geprueft,?\s*keine\s+aenderung",
        r"(?:keine\s+neuen?\s+thesen|keine\s+thesenaktualisierung|"
        r"keine\s+aktualisierung\s+der\s+thesen).{0,120}"
        r"(?:thesenregister.{0,60}(?:geprüft|geprueft|unverändert|unveraendert|keine\s+änderung|keine\s+aenderung)|"
        r"keine\s+änderung(?:en)?\s+am\s+thesenregister)",
        r"thesenregister.{0,80}(?:unverändert|unveraendert|keine\s+änderung|keine\s+aenderung)",
    ))
    def _one_register_value(label: str) -> str | None:
        matches = re.findall(rf"(?im)^{re.escape(label)}:\s*([^\r\n]+)\s*$", thesis)
        return matches[0].strip() if len(matches) == 1 else None

    before_hash = _one_register_value("VORHER-SHA256")
    after_hash = _one_register_value("NACHHER-SHA256")
    comparison_id = _one_register_value("THESENREGISTER-VERGLEICH-ID")

    def _register_rows(id_label: str, version_label: str, active_label: str):
        ids = re.findall(rf"(?im)^{re.escape(id_label)}:\s*([A-Za-z0-9_.:-]+)\s*$", thesis)
        versions = re.findall(rf"(?im)^{re.escape(version_label)}:\s*(\d+)\s*$", thesis)
        active = re.findall(rf"(?im)^{re.escape(active_label)}:\s*(YES|NO)\s*$", thesis)
        return ids, versions, active

    previous_ids, previous_versions, previous_active = _register_rows(
        "VORHER-THESIS-ID", "VORHER-REGISTER-VERSION", "VORHER-THESIS-ACTIVE"
    )
    current_ids, current_versions, current_active = _register_rows(
        "THESIS_ID", "REGISTER-VERSION", "THESIS_ACTIVE"
    )

    def _count(label: str) -> int | None:
        raw = _one_register_value(label)
        return int(raw) if raw is not None and re.fullmatch(r"\d+", raw) else None

    before_count = _count("VORHER_THESEN_COUNT")
    before_active_count = _count("VORHER_AKTIVE_THESEN")
    after_count = _count("NACHHER_THESEN_COUNT")
    after_active_count = _count("NACHHER_AKTIVE_THESEN")

    comparison_hash_valid = False
    if before_hash and after_hash and comparison_id:
        comparison_payload = {
            "previous_sha256": before_hash,
            "current_sha256": after_hash,
            "schema": "trading-radar-thesis-register-comparison-v1",
        }
        expected_comparison_id = hashlib.sha256(
            json.dumps(comparison_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        comparison_hash_valid = comparison_id == expected_comparison_id

    previous_rows_valid = bool(
        before_count is not None and before_active_count is not None
        and before_count > 0 and 0 <= before_active_count <= before_count
        and len(previous_ids) == len(previous_versions) == len(previous_active) == before_count
        and len(set(previous_ids)) == len(previous_ids)
        and all(int(version) >= 1 for version in previous_versions)
        and sum(value == "YES" for value in previous_active) == before_active_count
    )
    current_rows_valid = bool(
        after_count is not None and after_active_count is not None
        and after_count > 0 and 0 <= after_active_count <= after_count
        and len(current_ids) == len(current_versions) == len(current_active) == after_count
        and len(set(current_ids)) == len(current_ids)
        and all(int(version) >= 1 for version in current_versions)
        and sum(value == "YES" for value in current_active) == after_active_count
    )

    register_evidence_ok = bool(
        re.search(r"(?im)^THESENREGISTER-VERGLEICH:\s*PASS\s*$", thesis)
        and before_hash and re.fullmatch(r"[0-9a-f]{64}", before_hash)
        and after_hash and re.fullmatch(r"[0-9a-f]{64}", after_hash)
        and comparison_hash_valid
        and previous_rows_valid
        and current_rows_valid
    )
    if not register_evidence_ok:
        errors.append("M-05: Deterministischer Thesenregistervergleich mit PASS sowie Vorher-/Nachher-SHA256 fehlt.")
    change_labels = ("THESEN_NEU", "THESEN_AKTUALISIERT", "THESEN_UNVERAENDERT", "THESEN_ARCHIVIERT")
    change_values = {}
    for label in change_labels:
        matches = re.findall(rf"(?im)^{label}:\s*([^\r\n]+)\s*$", thesis)
        if len(matches) != 1 or not matches[0].strip():
            errors.append(f"M-05: Deterministischer Änderungsnachweis {label} fehlt oder ist nicht eindeutig.")
        else:
            change_values[label] = matches[0].strip()
    if change_values and previous_rows_valid and current_rows_valid:
        def _change_ids(label: str) -> set[str] | None:
            raw = change_values.get(label, "").strip()
            if raw.upper() == "KEINE":
                return set()
            values = [item.strip() for item in raw.split(",")]
            if not values or any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", item) for item in values):
                return None
            if len(values) != len(set(values)):
                return None
            return set(values)

        reported_changes = {
            label: _change_ids(label)
            for label in change_labels
        }
        previous_by_id = {
            thesis_id: (int(version), active == "YES")
            for thesis_id, version, active in zip(
                previous_ids, previous_versions, previous_active
            )
        }
        current_by_id = {
            thesis_id: (int(version), active == "YES")
            for thesis_id, version, active in zip(
                current_ids, current_versions, current_active
            )
        }
        expected_changes = {
            "THESEN_NEU": set(),
            "THESEN_AKTUALISIERT": set(),
            "THESEN_UNVERAENDERT": set(),
            "THESEN_ARCHIVIERT": set(),
        }
        semantic_errors = []
        dropped_ids = set(previous_by_id) - set(current_by_id)
        if dropped_ids:
            semantic_errors.append(
                "Vorherige Thesen wurden ohne Archivierung aus dem Nachregister entfernt: "
                               + ", ".join(sorted(dropped_ids))
            )
        for thesis_id, (current_version, current_active) in current_by_id.items():
            previous_record = previous_by_id.get(thesis_id)
            if previous_record is None:
                if current_version != 1 or not current_active:
                    semantic_errors.append(
                        f"Neue These {thesis_id} muss aktiv mit Version 1 beginnen"
                    )
                expected_changes["THESEN_NEU"].add(thesis_id)
                continue
            previous_version, previous_active = previous_record
            if current_version == previous_version + 1:
                if not current_active:
                    semantic_errors.append(
                        f"Archivierte These {thesis_id} darf beim Archivieren keine neue Version erhalten"
                    )
                else:
                    expected_changes["THESEN_AKTUALISIERT"].add(thesis_id)
            elif current_version == previous_version:
                if previous_active and not current_active:
                    expected_changes["THESEN_ARCHIVIERT"].add(thesis_id)
                elif current_active:
                    expected_changes["THESEN_UNVERAENDERT"].add(thesis_id)
            else:
                semantic_errors.append(
                    f"These {thesis_id}: unplausibler Versionsübergang "
                    f"{previous_version} -> {current_version}"
                )
        if any(value is None for value in reported_changes.values()):
            semantic_errors.append("Änderungslisten enthalten ungültige oder doppelte IDs")
        else:
            for label in change_labels:
                if reported_changes[label] != expected_changes[label]:
                    semantic_errors.append(
                        f"{label} widerspricht dem Vorher-/Nachher-Register "
                        f"(gemeldet={sorted(reported_changes[label])}, "
                        f"ermittelt={sorted(expected_changes[label])})"
                    )
        if semantic_errors:
            errors.append(
                "M-05: Semantischer Thesenregistervergleich widersprüchlich: "
                + " | ".join(semantic_errors)
            )
    if no_thesis_update and change_values:
        if any(change_values.get(label, "").upper() != "KEINE"
               for label in ("THESEN_NEU", "THESEN_AKTUALISIERT", "THESEN_ARCHIVIERT")):
            errors.append("M-05: Bericht behauptet 'keine Änderung', Registervergleich weist aber neue/aktualisierte/archivierte Thesen aus.")
    if not (
        thesis_id and version_match and int(version_match.group(0)) >= 1 and status_ok
    ) and not (no_thesis_update and register_evidence_ok):
        errors.append("M-05: Thesen-Update benötigt Thesen-ID, positive Version und zulässigen Status oder einen technisch belegten unveränderten Registervergleich.")
        if not thesis_id or not version_match or not status_ok:
            errors.append("M-07: Thesen-Identität, positive Version und zulässiger Status sind nicht vollständig belegt.")

    # M-08: validate each event record independently. A complete event must
    # not mask a second event that lacks its own time/zone/metric/period/value.
    events = _section_body(text, 3, SECTION_NAMES[2])
    event_labels = (
        (r"Uhrzeit", r"Zeit"),
        (r"Zeitzone",),
        (r"Kennzahl", r"Indikator"),
        (r"Berichtszeitraum", r"Zeitraum"),
    )
    observation_labels = (r"letzter Wert", r"Vorwert", r"Konsens", r"Istwert")
    no_relevant_events = bool(re.fullmatch(
        r"(?is)\s*(?:[-*•]\s*)?(?:Ereignisradar\s*:\s*)?"
        r"keine\s+(?:(?:relevanten|anstehenden)\s+)?ereignisse"
        r"(?:\s+im\s+[^.!?\r\n]+)?[.!]?\s*",
        events,
    ))

    def event_records(section_body):
        """Split bullet/numbered event entries; field-only bullets continue a record."""
        records = []
        current = []
        field_only = re.compile(
            r"(?i)^\s*(?:[-*•]\s+|\d+[.)]\s+)?(?:\*\*)?"
            r"(?:Datum|Date|Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|Berichtszeitraum|Zeitraum|"
            r"letzter Wert|Vorwert|Konsens|Istwert|Quelle|Source|"
            r"Szenarien(?:\s+(?:unter/entsprechend/über|unter|entsprechend|über|ueber)\s+Konsens)?|"
            r"Reaktionskanäle|Wirkung|Bestätigung/Widerlegung|Bestaetigung/Widerlegung|"
            r"Was die These bestätigen/widerlegen würde|Quellenprüfung|Quellenpruefung|"
            r"Betroffene These/Assetklasse/konkrete offene Positionen|"
            r"Szenarien(?:\s+(?:unter/entsprechend/über|unter|entsprechend|über|ueber)\s+Konsens)?)(?:\*\*)?\s*[:=](?:\*\*)?"
        )
        bullet = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.*)$")
        named_event = re.compile(r"(?i)^\s*(?:Ereignis|Termin|Event)\s*[:=]")

        def fomc_event_kind(value):
            """Return a conservative FOMC event identity; unknown kinds never merge."""
            if not re.search(r"(?i)\bFOMC\b", value):
                return None
            if re.search(
                r"(?i)\b(?:minutes|meeting\s+minutes|protokoll(?:s)?|sitzungsprotokoll)\b",
                value,
            ):
                return "minutes"
            # A decision record may also mention its associated press conference.
            if re.search(
                r"(?i)\b(?:zinsentscheid(?:ung)?|rate\s+decision|target\s+range|"
                r"fed\s+funds\s+rate|statement|geldpolitische\s+entscheidung)\b",
                value,
            ):
                return "decision"
            if re.search(r"(?i)\b(?:press\s+conference|pressekonferenz)\b", value):
                return "press_conference"
            return None

        for line in section_body.splitlines():
            match = bullet.match(line)
            same_fomc_event = False
            if match and current:
                current_text = "\n".join(current)
                current_date = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", current_text)
                summary_date = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", match.group(1))
                current_kind = fomc_event_kind(current_text)
                summary_kind = fomc_event_kind(match.group(1))
                same_fomc_event = bool(
                    current_date and summary_date
                    and current_date.group(0) == summary_date.group(0)
                    and current_kind is not None
                    and current_kind == summary_kind
                )
            starts_record = (
                ((bool(match) and not field_only.match(line)) and not same_fomc_event)
                or (bool(named_event.match(line)) and not field_only.match(line))
            )
            if starts_record and current:
                records.append("\n".join(current).strip())
                current = []
            current.append(line)
        if current:
            records.append("\n".join(current).strip())
        records = [record for record in records if record]
        return records or ([section_body.strip()] if section_body.strip() else [])

    def field_ok(record, labels, timezone=False):
        label = r"(?:" + "|".join(labels) + r")"
        matches = list(re.finditer(
            rf"(?im)(?:^|[|;]\s*|\n)\s*(?:[-•]\s+|\d+[.)]\s+)?"
            rf"(?:\*\*)?{label}(?:\*\*)?\s*[:=](?:\*\*)?\s*([^|;\r\n]+)",
            record,
        ))
        if not matches:
            return _is_explicit_unavailable(record, labels)
        for match in matches:
            value = match.group(1).strip().strip(". ")
            if re.fullmatch(
                r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?",
                value,
            ):
                continue
            if _MISSING_VALUE_RE.fullmatch(value):
                return False
            if timezone and not re.search(
                r"(?i)(?:\b(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|UTC|CET|CEST|GMT)\b|"
                r"\b(?:UTC|GMT)?[+-]\d{1,2}(?::?\d{2})?\b|"
                r"\b[A-Za-z_]+/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?\b)", value
            ):
                return False
        return True

    if not no_relevant_events:
        records = event_records(events)
        for event_index, record in enumerate(records, start=1):
            values_ok = all(
                field_ok(record, event_labels[i], timezone=(i == 1))
                for i in range(4)
            )
            observation_ok = any(
                _label_value(record, (label,)) is not None
                or _is_explicit_unavailable(record, (label,))
                for label in observation_labels
            )
            if not values_ok or not observation_ok:
                errors.append(
                    f"M-08: Ereignisdatensatz {event_index} benötigt eigene Uhrzeit, "
                    "Zeitzone, Kennzahl, Zeitraum und einen ausgewiesenen Wert/Konsens "
                    "oder expliziten Fehlstatus."
                )
    # M-09: isolate candidate records. A complete candidate must never supply
    # fields for another candidate via section-wide/global label searches.
    candidates = _section_body(text, 5, SECTION_NAMES[4])

    def split_candidate_records(section_body: str) -> list[str]:
        records: list[str] = []
        current: list[str] = []
        # Candidate boundaries are identity-based. Nested metadata bullets
        # (Sektor, Richtung, Datenstichtag, Quelle, ...) belong to the current
        # candidate and must never split it into synthetic records.
        identity_start = re.compile(
            r"(?i)^\s*(?:[-*•]\s+|\d+[.)]\s+)?(?:\*\*)?"
            r"(?:Asset/Ticker|Ticker|Asset)(?:\*\*)?\s*[:=]"
        )
        def flush_candidate():
            nonlocal current
            candidate_text = "\n".join(current).strip()
            # Source inventories, section preambles and M-06 summaries are not
            # candidate records. A record starts only with its own identity key.
            if re.search(
                r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?(?:Asset/Ticker|Ticker|Asset)(?:\*\*)?\s*[:=]",
                candidate_text,
            ):
                records.append(candidate_text)
            current = []

        for line in section_body.splitlines():
            if re.match(
                r"(?i)^\s*(?:JETZT TECHNISCH PRÜFEN|AUF TRIGGER WARTEN|EREIGNIS ABWARTEN|"
                r"NICHT HANDELBAR|VERWERFEN\s*/\s*INVALIDIERT)\s*[:：]",
                line,
            ):
                if current:
                    flush_candidate()
                continue
            if re.fullmatch(r"(?i)\s*(?:keine kandidaten(?: in dieser gruppe)?|0 kandidaten|explizit leer|leer)[.!—-]*\s*", line):
                continue
            starts = bool(identity_start.match(line))
            if starts and current:
                flush_candidate()
            # Preserve original indentation and bullet markers: _label_value()
            # accepts them and they are useful forensic evidence in diagnostics.
            current.append(line)
        if current:
            flush_candidate()
        # The report's established one-candidate inline format is a single
        # record. Empty records are ignored, but an explicit no-candidate
        # declaration is handled separately below.
        return [record for record in records if record.strip()]

    def parse_number(value: str | None) -> float | None:
        if value is None or not _valid_numeric_value(value):
            return None
        cleaned = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", value.strip(), flags=re.I)
        cleaned = re.sub(r"\s*(?:EUR|USD|GBP|€|\$|£|%|x|\b(?:Punkte|pts)\b)$", "", cleaned, flags=re.I).strip()
        cleaned = cleaned.replace(" ", "").replace("’", "").replace("'", "")
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".") if cleaned.rfind(",") > cleaned.rfind(".") else cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            number = float(cleaned)
            return number if math.isfinite(number) else None
        except (ValueError, NameError):
            return None

    no_actionable_candidates = bool(re.search(
        r"(?im)^\s*(?:Keine belastbaren technischen Kandidaten|"
        r"Keine Kandidaten mit (?:belastbarem|konkretem) Setup|"
        r"Kein belastbarer technischer Kandidat)\b",
        candidates,
    ))
    allowed_candidate_status = re.compile(
        r"JETZT TECHNISCH PRÜFEN|AUF TRIGGER WARTEN|EREIGNIS ABWARTEN|"
        r"NICHT HANDELBAR|VERWERFEN\s*/\s*INVALIDIERT",
        re.IGNORECASE,
    )
    if not no_actionable_candidates:
        records = split_candidate_records(candidates)
        if not records:
            errors.append("M-09: Keine separat prüfbaren Kandidatendatensätze gefunden.")
        for candidate_index, record in enumerate(records, start=1):
            def field(labels: tuple[str, ...]) -> str | None:
                return _label_value(record, labels)

            asset = field((r"Asset/Ticker", r"Ticker", r"Asset"))
            trigger = field((r"Trigger", r"Bestätigung", r"Bestaetigung"))
            entry_text = field((r"Einstieg", r"Entry"))
            stop_text = field((r"Stop", r"Stop-Loss"))
            tp1_text = field((r"TP1", r"Ziel\s*1"))
            tp2_text = field((r"TP2", r"Ziel\s*2"))
            crv_text = field((r"CRV", r"Chance-Risiko"))
            counter = field((r"Gegenargument", r"Risiko"))
            rule_data = field((r"Regel-/Datenstatus", r"Regelstatus", r"Datenstatus"))
            status_ok = bool(allowed_candidate_status.search(record))
            nontradable = bool(re.search(r"NICHT HANDELBAR|VERWERFEN\s*/\s*INVALIDIERT", record, re.IGNORECASE))
            level_labels = (
                (r"Einstieg", r"Entry"),
                (r"Stop", r"Stop-Loss"),
                (r"TP1", r"Ziel\s*1"),
                (r"TP2", r"Ziel\s*2"),
                (r"CRV", r"Chance-Risiko"),
            )
            explicit_level_states = all(
                parse_number(field(labels)) is not None
                or _is_explicit_unavailable(record, labels)
                for labels in level_labels
            )
            required = (asset, trigger, counter, rule_data)
            # TP2 is optional at entry; CRV1 is measured against TP1.
            numeric_values = (entry_text, stop_text, tp1_text, crv_text)
            numeric_ok = all(parse_number(value) is not None for value in numeric_values)
            entry_num, stop_num = parse_number(entry_text), parse_number(stop_text)
            tp1_num, crv_num = parse_number(tp1_text), parse_number(crv_text)
            if nontradable:
                # A non-tradable/status-only record may correctly lack levels.
                # It must still name its asset, evidence/trigger, counterargument,
                # explicit non-tradable status, and explicitly account for every
                # unavailable numeric level. No trading math is run for it.
                if not all(required) or not status_ok or not explicit_level_states:
                    errors.append(
                        f"M-09: Nicht handelbarer Kandidat {candidate_index} benötigt Asset, "
                        "Trigger, Gegenargument, Regel-/Datenstatus und explizite numerische Werte "
                        "oder NICHT VERFÜGBAR für Einstieg/Stop/TP1/TP2/CRV."
                    )
                continue
            if not all((asset, trigger, entry_text, stop_text, tp1_text, crv_text, counter, rule_data)) or not numeric_ok or not status_ok:
                errors.append(
                    f"M-09: Kandidat {candidate_index} benötigt eigene Asset-/Trigger-/Einstiegs-/Stop-/TP-/CRV-/"
                    "Gegenargument-/Regelstatusfelder und einen zulässigen Status; Zahlen müssen numerisch sein."
                )
                continue

            direction_text = field((r"Richtung", r"Side", r"Positionstyp")) or ""
            direction = direction_text.strip().casefold()
            is_short = direction in {"short", "verkauf", "sell"}
            is_long = direction in {"long", "kauf", "buy"}
            # Explicit direction enables deterministic stop-side validation.
            # If omitted, infer only from the common directional relationship;
            # equal entry/stop is always invalid.
            invalid_stop = stop_num == entry_num
            if is_long and stop_num >= entry_num:
                invalid_stop = True
            elif is_short and stop_num <= entry_num:
                invalid_stop = True
            elif not is_long and not is_short:
                if stop_num > entry_num and tp1_num > entry_num:
                    invalid_stop = True
                elif stop_num < entry_num and tp1_num < entry_num:
                    invalid_stop = True
            if invalid_stop:
                errors.append(
                    f"M-09: Kandidat {candidate_index} ({asset}) hat eine unplausible Stop-Lage "
                    f"(Einstieg={entry_num:g}, Stop={stop_num:g}, Richtung={direction_text or 'nicht angegeben'})."
                )
                continue

            if is_short:
                risk = stop_num - entry_num
                reward = entry_num - tp1_num
            else:
                risk = entry_num - stop_num
                reward = tp1_num - entry_num
            if risk <= 0:
                errors.append(f"M-09: Kandidat {candidate_index} ({asset}) hat kein positives Risiko zwischen Einstieg und Stop.")
                continue
            computed_crv = reward / risk
            if abs(computed_crv - crv_num) > max(0.35, abs(computed_crv) * 0.20):
                errors.append(
                    f"M-09: Kandidat {candidate_index} ({asset}) weist CRV={crv_num:g} aus, "
                    f"aus Einstieg/Stop/TP1 ergibt sich jedoch {computed_crv:.2f}."
                )
            if crv_num < 1 and re.search(r"JETZT TECHNISCH PRÜFEN", record, re.IGNORECASE):
                errors.append(f"M-09: Kandidat {candidate_index} ({asset}) hat CRV<1 und darf nicht als JETZT TECHNISCH PRÜFEN gelten.")

    # M-10: a concrete OHLCV issue and its candidate impact must both be stated.
    appendix = _section_body(text, 8, SECTION_NAMES[7])
    warning = _has_any(appendix, (
        r"OHLCV.*(?:fehlt|fehlend|missing|luecke|lücke|nicht verfügbar|veraltet)",
        r"(?:fehlt|fehlend|missing|luecke|lücke|nicht verfügbar|veraltet).*OHLCV",
    ))
    impact = _has_any(appendix, (
        r"Kandidat(?:en)?(?:[^\r\n]{0,100})(?:eingeschränkt|nicht prüfbar|nicht valide|gesperrt)",
        r"(?:eingeschränkt|nicht prüfbar|nicht valide|gesperrt)(?:[^\r\n]{0,100})Kandidat(?:en)?",
    ))
    clean_data = _has_any(appendix, (
        r"OHLCV[^\r\n]{0,100}(?:vollständig|vollstaendig|keine\s+(?:Daten)?lücken|keine\s+fehlenden\s+Daten)",
        r"(?:vollständig|vollstaendig|keine\s+(?:Daten)?lücken|keine\s+fehlenden\s+Daten)[^\r\n]{0,100}OHLCV",
    )) and _has_any(appendix, (
        r"Kandidat(?:en)?[^\r\n]{0,100}(?:nicht eingeschränkt|keine Einschränkung|unverändert valide)",
        r"(?:nicht eingeschränkt|keine Einschränkung|unverändert valide)[^\r\n]{0,100}Kandidat(?:en)?",
        # The report's established wording states that no technical OHLCV gaps
        # restrict the sector analysis. When paired with explicit complete
        # OHLCV coverage above, this is equivalent evidence of no candidate
        # restriction and must not be rejected as a false M-10 failure.
        r"keine\s+technischen\s+Datenlücken[^\r\n]{0,100}(?:Sektor-Analyse|Sektoranalyse)[^\r\n]{0,60}einschränken",
        r"keine\s+technischen\s+Datenluecken[^\r\n]{0,100}(?:Sektor-Analyse|Sektoranalyse)[^\r\n]{0,60}einschraenken",
    ))
    if not ((warning and impact) or clean_data):
        errors.append("M-10: OHLCV-Datenlücke/-veraltung und konkrete Auswirkung auf Kandidaten müssen ausgewiesen sein; bei lückenfreien Daten sind vollständige OHLCV-Abdeckung und fehlende Kandidateneinschränkung ausdrücklich zu nennen.")

    # M-03: four mutually exclusive state lists must be present and complete.
    state_names = ("VALID", "MISSING", "STALE", "INVALID")
    state_rows = {}
    for state_name in state_names:
        match = re.search(
            rf"(?im)^OHLCV-Symbole-{state_name}:\s*(.+?)\s*$",
            appendix,
        )
        if match:
            raw_values = match.group(1).strip()
            state_rows[state_name] = [] if raw_values.upper() == "KEINE" else [
                item.strip().upper() for item in raw_values.split(",") if item.strip()
            ]
    if len(state_rows) != 4:
        errors.append("M-03: vollständige OHLCV-Symbolzuordnung für VALID/MISSING/STALE/INVALID fehlt.")
    else:
        flattened = [ticker for values in state_rows.values() for ticker in values]
        if len(flattened) != len(set(flattened)):
            errors.append("M-03: mindestens ein OHLCV-Symbol ist mehreren Statusklassen zugeordnet.")
        counts_match = re.search(
            r"(?i)OHLCV-Qualitätsstatus:.*VALID=(\d+).*MISSING=(\d+).*STALE=(\d+).*INVALID=(\d+).*Summe=(\d+)/(\d+)",
            appendix,
        )
        if not counts_match:
            errors.append("M-03: OHLCV-Zustandszahlen mit Gesamtuniversum fehlen.")
        else:
            expected = [int(counts_match.group(i)) for i in range(1, 5)]
            actual = [len(state_rows[name]) for name in state_names]
            if actual != expected or sum(actual) != int(counts_match.group(6)):
                errors.append("M-03: OHLCV-Symbolzuordnung stimmt nicht mit den vier Zustandszahlen überein.")


    # M-04: comparable 1M/3M windows must be present for the market-rotation
    # comparison. Do not infer a ranking from qualitative or mismatched periods.
    executive = _section_body(text, 1, SECTION_NAMES[0])
    live_lines = [line for line in executive.splitlines()
                  if re.search(r"(?i)\bIndex\b", line)]
    comparable_index_rows = [
        line for line in live_lines
        if re.search(r"(?i)\b1M\b[^;\r\n]*[-+]?\d", line)
        and re.search(r"(?i)\b3M\b[^;\r\n]*[-+]?\d", line)
    ]
    if len(comparable_index_rows) < 2:
        errors.append("M-04: Relative-Stärke-Vergleich benötigt mindestens zwei Indexreihen mit vergleichbaren 1M- und 3M-Werten oder explizit begründeten Datenlücken.")
    rotation = _section_body(text, 4, SECTION_NAMES[3])
    if not re.search(r"(?i)Relative[- ]Stärke[- ]Matrix", rotation):
        errors.append("M-04: Relative-Stärke-Matrix für Indizes, Regionen, Sektoren und Anlageklassen fehlt.")
    # M-05: a generic 'no changes' sentence is not evidence of a deterministic
    # register comparison. Require stable IDs/versions and before/after hashes.
    thesis_evidence = re.search(
        r"(?is)THESENREGISTER-VERGLEICH\s*:\s*PASS.*?"
        r"(?:VORHER|PREVIOUS)[-_ ]SHA256\s*[:=]\s*[0-9a-f]{64}.*?"
        r"(?:NACHHER|CURRENT)[-_ ]SHA256\s*[:=]\s*[0-9a-f]{64}.*?"
        r"(?:(?:THESIS_ID|THESEN-ID)\s*[:=]\s*[A-Za-z0-9_.:-]+.*?"
        r"(?:VERSION|REGISTER-VERSION)\s*[:=]\s*\d+|"
        r"AKTIVE_THESEN\s*[:=]\s*0\b)",
        text,
    )
    if not thesis_evidence:
        errors.append("M-05: Deterministischer Thesenregistervergleich mit Vorher-/Nachher-SHA256, thesis_id und Version ist nicht belegt.")

    # M-07: every distinct nominal US-10Y Treasury observation must have
    # complete provenance on at least one line carrying that exact value.
    # Narrative repetitions of an already sourced value are not new observations.
    observation_records = []
    conflict_observation_records = []
    conflict_observation_groups = []
    for line in text.splitlines():
        if _is_us10y_conflict_line(line):
            # Keep the role as part of the identity. Equal numeric values do
            # not make Makro and Live interchangeable provenance records.
            role_records = _us10y_conflict_observation_records(line)
            conflict_observation_records.extend(role_records)
            conflict_observation_groups.append(role_records)
            continue
        contextual = _is_run1270_us_curve_context(line)
        observation_records.extend(
            _us10y_observation_records(line, allow_run1270_context=contextual)
        )

    distinct_treasury_values = sorted({value for value, _ in observation_records})
    if observation_records or conflict_observation_records:
        # Every distinct value must have a complete source record, even when
        # the report contains only one US-10Y observation.
        provenance_complete = all(
            any(
                observed_value == expected_value
                and _treasury_observation_has_complete_provenance(line)
                for observed_value, line in observation_records
            )
            for expected_value in distinct_treasury_values
        )
        # Paired rows require exactly one complete record for each role.
        # Do not collapse by Decimal value: equal values still have two sources.
        def conflict_group_complete(role_records):
            roles = [role for role, _, _ in role_records]
            return (
                len(role_records) == 2
                and sorted(roles) == ["Live", "Makro"]
                and all(
                    _treasury_observation_has_complete_provenance(record)
                    for _, _, record in role_records
                )
            )

        conflict_roles_complete = all(
            conflict_group_complete(group)
            for group in conflict_observation_groups
        )
        if conflict_observation_groups:
            provenance_complete = provenance_complete and conflict_roles_complete
        provenance_labels = re.compile(
            r"(?i)(?:^|[|;(])\s*(?:Quelle|Source|Abrufzeit|Abrufzeitpunkt|"
            r"Retrieved(?:_At)?|Datenstand|Datenstichtag|Beobachtungsdatum|"
            r"Observation_Date|Einheit|Unit|Verzögerung|Verzoegerung|Delay(?:_Days)?)\s*[:=]"
        )
        # A source-like record with partial metadata must not be hidden by a
        # second complete line carrying the same numeric value. Plain narrative
        # repetitions without provenance fields remain harmless.
        partial_source_records = [
            line for _, line in observation_records
            if provenance_labels.search(line)
            and not _treasury_observation_has_complete_provenance(line)
        ]
        provenance_complete = provenance_complete and not partial_source_records
        explicit_open = any(
            re.search(r"(?i)QUELLENKONFLIKT\s*:\s*OFFEN", line)
            and "US-10Y" in line.upper()
            for line in text.splitlines()
        )
        if not provenance_complete:
            suffix = " Der Konflikt ist ausdrücklich offen ausgewiesen." if explicit_open else ""
            errors.append(
                "M-07: Jede abweichende 10Y-Treasury-Beobachtung benötigt separat "
                "Quelle, Abrufzeit, Datenstichtag, Einheit und Verzögerung; "
                "eine offene Konfliktmarkierung ersetzt diese Provenienz nicht."
                + suffix
            )


    # M-04: Relative strength must use comparable 1M/3M windows and a common cutoff.
    rotation = _section_body(text, 4, SECTION_NAMES[3])
    if rotation:
        if not re.search(r"(?i)(?:Relative[- ]Stärke|Kapitalrotation).{0,100}(?:Matrix|Vergleich|Ranking)", rotation):
            errors.append("M-04: Relative-Stärke-/Kapitalrotationsvergleich fehlt.")
        for window in ("1M", "3M"):
            if not re.search(rf"(?i)\b{window}\b", rotation):
                errors.append(f"M-04: Vergleichsfenster {window} fehlt im Assetklassenvergleich.")
        if not re.search(r"(?i)(?:gemeinsamer\s+)?Datenstichtag\s*[:=]", rotation):
            errors.append("M-04: gemeinsamer Datenstichtag des Relative-Stärke-Vergleichs fehlt.")
        for concept in ("Gewinner", "Verlierer", "Divergenz"):
            if not re.search(rf"(?i){concept}", rotation):
                errors.append(f"M-04: {concept} im vergleichbaren Rotationsbild nicht ausgewiesen.")
        group_aliases = {
            "Regionen": {"regionen", "region"},
            "Indizes": {"indizes", "index"},
            "Sektoren": {"sektoren", "sektor"},
            "Anlageklassen": {"anlageklassen", "assetklassen"},
        }
        category_heading = re.compile(
            r"(?i)^\s*(?:[-*•]\s*)?(?:\*\*)?"
            r"(Regionen|Region|Indizes|Index|Sektoren|Sektor|Anlageklassen|Assetklassen)"
            r"(?:\*\*)?\s*[:：]\s*(.*)$"
        )
        headings = []
        for line_no, line in enumerate(rotation.splitlines()):
            match = category_heading.match(line)
            if match:
                label = match.group(1).casefold()
                canonical = next(
                    name for name, aliases in group_aliases.items() if label in aliases
                )
                headings.append((line_no, canonical, match.group(2)))
        rotation_lines = rotation.splitlines()
        for group in group_aliases:
            matching = [item for item in headings if item[1] == group]
            if not matching:
                errors.append(f"M-04: {group} fehlen als eigener Vergleichsbereich.")
                continue
            # Each category is a bounded block: heading text plus all following
            # rows until the next category heading. This supports both legacy
            # inline summaries and current multi-row Markdown matrices.
            blocks = []
            for line_no, _canonical, inline_value in matching:
                later = [h[0] for h in headings if h[0] > line_no]
                block_end = min(later) if later else len(rotation_lines)
                block_lines = ([inline_value] if inline_value.strip() else [])
                block_lines.extend(rotation_lines[line_no + 1:block_end])
                blocks.append(block_lines)
            comparable = any(
                re.search(r"(?i)\b1M\b[^\r\n]*?[-+]?\d", line)
                and re.search(r"(?i)\b3M\b[^\r\n]*?[-+]?\d", line)
                for block in blocks for line in block
            )
            unavailable = any(
                re.search(
                    r"(?i)NICHT VERFÜGBAR[^\r\n]*(?:Datenlücke|keine vergleichbaren Reihen|Daten fehlen|keine Vergleichsdaten)",
                    line,
                )
                for block in blocks for line in block
            )
            if not (comparable or unavailable):
                errors.append(
                    f"M-04: {group} benötigen vergleichbare 1M-/3M-Werte "
                    "oder eine konkrete Datenlücke innerhalb ihres Matrixblocks."
                )


    # M-06: candidate sources, confirmed setups, watchlist and rejected candidates
    # must be distinguishable. A source count is not a candidate count.
    candidate_section = _section_body(text, 5, SECTION_NAMES[4])
    if candidate_section:
        if not re.search(r"(?i)Kandidatenquellen\s*:", candidate_section):
            errors.append("M-06: Verfügbare Kandidatenquellen müssen mit Quelle/Bestand nachvollziehbar ausgewiesen werden.")
        group_patterns = (
            ("bestätigte Setups", r"(?i)(?:BESTÄTIGTE\s+SETUPS|FREIGEGEBENE\s+SETUPS)"),
            ("Watchlist", r"(?i)WATCHLIST"),
            ("verworfene Kandidaten", r"(?i)(?:VERWORFENE\s+KANDIDATEN|ABGELEHNTE\s+KANDIDATEN)"),
        )
        group_matches = []
        for label, pattern in group_patterns:
            match = re.search(pattern, candidate_section)
            if not match:
                errors.append(f"M-06: Abschnitt 5 muss den Bereich '{label}' separat ausweisen (auch explizit leer).")
            else:
                group_matches.append((label, match))
        # A ticker may appear once per status group. Duplicate source rows are
        # consolidated upstream and retained as multiple Quellzeile references.
        for label, match in group_matches:
            later_starts = [other.start() for _, other in group_matches if other.start() > match.start()]
            group_end = min(later_starts) if later_starts else len(candidate_section)
            group_body = candidate_section[match.end():group_end]
            candidate_lines = re.findall(
                r"(?im)^\s*[-•]?\s*(?:Asset/Ticker|Name|Ticker|Symbol):\s*[^|\r\n]*?\(([A-Z0-9.^=_/-]{2,30})\)([^\r\n]*)",
                group_body,
            )
            # Ticker alone is not a candidate identity: one instrument may have
            # several independent setups. The producer emits a stable Setup-ID.
            # Legacy reports without Setup-ID retain the conservative ticker key.
            identities = []
            for ticker, tail in candidate_lines:
                setup_match = re.search(r"(?i)Setup-ID\s*:\s*([^|;]+)", tail)
                setup_id = re.sub(r"\s+", " ", setup_match.group(1)).strip().casefold() if setup_match else ""
                identities.append((ticker.upper(), setup_id or "<legacy-ticker-only>"))
            if len(identities) != len(set(identities)):
                errors.append(f"M-06: Doppelte Kandidatenidentität im Bereich '{label}' ist nicht konsolidiert.")
        for label, match in group_matches:
            later = [other.start() for _, other in group_matches if other.start() > match.start()]
            end = min(later) if later else min(len(candidate_section), match.end() + 1600)
            content = candidate_section[match.end():end].strip(" :|;.-\n\r")
            explicit_empty = bool(re.search(
                r"(?i)(?:keine|0)\s+(?:bestätigten?\s+)?(?:setups?|kandidaten?|watchlist-einträge|verworfenen kandidaten)|leer|nicht vorhanden",
                content[:400],
            ))
            if len(re.sub(r"\W", "", content)) < 1:
                errors.append(f"M-06: Bereich '{label}' ist leer.")
            elif explicit_empty:
                if not re.search(r"(?i)(?:Begründung|Grund|weil|Datenlücke|keine belastbaren Trigger|nicht ableitbar)\s*[:=]?", content):
                    errors.append(f"M-06: Nullmeldung für '{label}' benötigt eine konkrete Begründung.")
            else:
                has_identity = bool(re.search(r"(?i)(?:Ticker|Symbol|Name)\s*[:=]", content))
                has_source = bool(re.search(r"(?i)(?:Quelle|Source)\s*[:=]", content))
                has_reason = bool(re.search(r"(?i)(?:Begründung|Grund|Setup|Status)\s*[:=]", content))
                trigger_match = re.search(
                    r"(?i)(?:Trigger|Kurslevel|Strukturlevel|Auslöser)\s*[:=]\s*([^|;\r\n]+)",
                    content,
                )
                has_trigger = bool(
                    trigger_match
                    and not re.fullmatch(
                        r"(?i)(?:NICHT VERFÜGBAR|FEHLT|UNBEKANNT|UNKLAR|"
                        r"überprüfbarer Kurs-/Strukturtrigger fehlt|nicht ableitbar)[.! ]*",
                        trigger_match.group(1).strip(),
                    )
                )
                if not (has_identity and has_source and has_reason and (has_trigger or label.casefold() == "verworfene kandidaten")):
                    errors.append(f"M-06: Einträge in '{label}' benötigen Identität, Quelle, Begründung/Status und bei Watchlist/Setups einen überprüfbaren Trigger.")

        # A source-level status such as "valide" is not a technical confirmation.
        # The compact confirmed-setups summary must carry the evidence required
        # by Zielarchitektur v2.1; otherwise it may only remain a scanner candidate.
        confirmed_heading = re.search(
            r"(?im)^\s*(?:BESTÄTIGTE\s+SETUPS|FREIGEGEBENE\s+SETUPS)\s*:",
            candidate_section,
        )
        if confirmed_heading:
            next_heading = re.search(
                r"(?im)^\s*(?:WATCHLIST|VERWORFENE\s+KANDIDATEN|ABGELEHNTE\s+KANDIDATEN)\s*:",
                candidate_section[confirmed_heading.end():],
            )
            confirmed_end = (confirmed_heading.end() + next_heading.start()) if next_heading else len(candidate_section)
            confirmed_body = candidate_section[confirmed_heading.end():confirmed_end]
            explicit_no_confirmed_setups = bool(re.search(
                r"(?i)(?:keine\s+bestätigten\s+setups|0\s+bestätigte\s+setups)\b",
                confirmed_body,
            ))
            summary_starts = [
                match.start() for match in re.finditer(
                    r"(?im)^\s*(?:[-*•]\s*)?(?:Name|Asset/Ticker|Ticker)\s*:",
                    confirmed_body,
                )
            ]
            if explicit_no_confirmed_setups and summary_starts:
                errors.append(
                    "M-06: BESTÄTIGTE SETUPS meldet null Setups, enthält aber zugleich Setup-Datensätze."
                )
            if not explicit_no_confirmed_setups:
                summary_records = [
                    confirmed_body[start:(summary_starts[index + 1] if index + 1 < len(summary_starts) else len(confirmed_body))].strip()
                    for index, start in enumerate(summary_starts)
                ]
                if not summary_records:
                    errors.append("M-06: BESTÄTIGTE SETUPS enthält keine einzeln prüfbaren Datensätze.")
                required_confirmation_fields = {
                    "Setup-ID": r"Setup-ID\s*:",
                    "Quelle": r"Quelle\s*:",
                    "Richtung": r"Richtung\s*:",
                    "These": r"(?:Verknüpfte\s+These|Thesen-ID)\s*:",
                    "technische Bestätigung": r"Technische\s+Bestätigung\s*:",
                    "Daten-Cut-off": r"(?:Datenstichtag|Daten-Cut-?off)\s*:",
                    "Einstieg": r"Einstieg\s*:",
                    "Stop": r"Stop\s*:",
                    "CRV": r"CRV\s*:",
                    "Gegenargument": r"Gegenargument\s*:",
                    "Datenlücken": r"Datenlücken?\s*:",
                    "Regelstatus": r"Regel-/Datenstatus\s*:",
                }
                for index, record in enumerate(summary_records, 1):
                    missing_fields = [
                        field for field, pattern in required_confirmation_fields.items()
                        if not re.search(pattern, record, re.I)
                    ]
                    if missing_fields:
                        errors.append(
                            f"M-06: Bestätigtes Setup {index} ist nicht technisch nachgewiesen; "
                            "fehlende Felder: " + ", ".join(missing_fields)
                        )
                        continue
                    technical = _label_value(record, (r"Technische\s+Bestätigung",))
                    if not technical or not re.search(r"(?i)^(?:BESTÄTIGT|CONFIRMED)\b", technical.strip()):
                        errors.append(f"M-06: Bestätigtes Setup {index} hat keine explizite technische Bestätigung.")
                    direction = _label_value(record, (r"Richtung",))
                    if not direction or direction.strip().upper() not in {"LONG", "SHORT"}:
                        errors.append(f"M-06: Bestätigtes Setup {index} hat keine gültige Handelsrichtung LONG/SHORT.")
                    thesis_value = _label_value(record, (r"Verknüpfte\s+These", r"Thesen-ID"))
                    if not thesis_value or _MISSING_VALUE_RE.fullmatch(thesis_value.strip()):
                        errors.append(f"M-06: Bestätigtes Setup {index} ist keiner belastbaren These zugeordnet.")
                    cutoff = _label_value(record, (r"Datenstichtag", r"Daten-Cut-?off"))
                    if not cutoff or not re.search(r"\d{4}-\d{2}-\d{2}", cutoff):
                        errors.append(f"M-06: Bestätigtes Setup {index} hat keinen ISO-Datenstichtag.")
                    for price_label in ("Einstieg", "Stop"):
                        price = _label_value(record, (price_label,))
                        if not price or not _valid_numeric_value(price):
                            errors.append(f"M-06: Bestätigtes Setup {index} hat keinen numerischen {price_label}.")
                        else:
                            try:
                                numeric_price = price.strip().replace(",", ".").replace("€", "").replace("USD", "").strip()
                                if float(numeric_price) <= 0:
                                    errors.append(f"M-06: Bestätigtes Setup {index} hat einen nicht-positiven {price_label}.")
                            except ValueError:
                                errors.append(f"M-06: Bestätigtes Setup {index} hat einen ungültigen {price_label}.")
                    crv_value = _label_value(record, (r"CRV",))
                    if not crv_value or not _valid_numeric_value(crv_value):
                        errors.append(f"M-06: Bestätigtes Setup {index} hat kein numerisches CRV.")
                    else:
                        numeric_crv = crv_value.strip().replace(",", ".").lower().removesuffix("x")
                        try:
                            if float(numeric_crv) < 1.0:
                                errors.append(f"M-06: Bestätigtes Setup {index} unterschreitet CRV1 >= 1.")
                        except ValueError:
                            errors.append(f"M-06: Bestätigtes Setup {index} hat ein ungültiges CRV.")


    # M-09: scenario alternatives are an explicit content contract. An
    # unavailable scenario is acceptable only with a concrete data-gap reason.
    scenario_section = executive
    scenario_patterns = (
        ("Basisszenario", r"(?i)Basisszenario\s*:"),
        ("positives Alternativszenario", r"(?i)(?:positives|positiv(?:es)?s?)\s+Alternativszenario\s*:"),
        ("negatives Alternativszenario", r"(?i)(?:negatives|negativ(?:es)?s?)\s+Alternativszenario\s*:"),
    )
    scenario_matches = []
    for label, pattern in scenario_patterns:
        match = re.search(pattern, scenario_section)
        if not match:
            errors.append(f"M-09-SZENARIO: {label} fehlt.")
        else:
            scenario_matches.append((label, match))
    for label, match in scenario_matches:
        later_starts = [other.start() for _, other in scenario_matches if other.start() > match.start()]
        block_end = min(later_starts) if later_starts else min(len(scenario_section), match.end() + 1400)
        tail = scenario_section[match.end():block_end]
        trigger_patterns = (
            r"(?i)(?:Eintrittsbedingung|Eintritt)\s*:\s*([^\r\n]+)",
            r"(?i)Bestätigungstrigger\s*:\s*([^\r\n]+)",
            r"(?i)Invalidierungstrigger\s*:\s*([^\r\n]+)",
        )
        trigger_values = [re.search(pattern, tail) for pattern in trigger_patterns]
        has_triggers = all(trigger_values) and all(
            not re.search(
                r"(?i)^(?:NICHT VERFÜGBAR|NICHT ABLEITBAR|FEHLT|UNBEKANNT|UNKLAR)",
                match.group(1).strip(),
            )
            for match in trigger_values if match
        )
        has_context = (
            re.search(r"(?i)Zeithorizont\s*:", tail)
            and re.search(r"(?i)(?:Indikatoren|relevante\s+Indikatoren)\s*:", tail)
            and re.search(r"(?i)(?:Konsequenz|Auswirkung)\s*:", tail)
        )
        explicit_gap = bool(re.search(
            r"(?i)(?:NICHT ABLEITBAR|NICHT VERFÜGBAR)[^\r\n]{0,180}(?:Datenlücke|Daten fehlen|keine belastbare Datenbasis|Begründung)",
            tail,
        ))
        if not ((has_triggers and has_context) or explicit_gap):
            errors.append(f"M-09-SZENARIO: {label} benötigt Trigger, Zeithorizont, Indikatoren und Konsequenz oder eine begründete Datenlücke.")
        scenario_declares_unavailable = bool(re.match(
            r"(?i)\s*(?:NICHT ABLEITBAR|NICHT VERFÜGBAR|FEHLT|UNBEKANNT|UNKLAR)\b",
            tail,
        ))
        if report_claims_final and (scenario_declares_unavailable or (explicit_gap and not (has_triggers and has_context))):
            errors.append(
                f"M-09-SZENARIO: {label} ist wegen einer ausgewiesenen Datenlücke nicht ableitbar; "
                "ein solcher Bericht darf nicht FINAL sein."
            )

def validate_report(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["Radar-Bericht ist leer"]

    matches = list(HEADER_RE.finditer(text))
    expected = list(SECTION_NAMES)
    found = [(int(m.group("number")), m.group("title").strip(), m.start(), m.end()) for m in matches]

    for number, title in enumerate(expected, start=1):
        matching = [item for item in found if item[0] == number and item[1].casefold() == title.casefold()]
        if len(matching) != 1:
            errors.append(f"Pflichtabschnitt {number} ({title}): erwartet genau einmal, gefunden {len(matching)}")

    valid = [item for item in found if item[1].casefold() in {x.casefold() for x in expected}]
    if len(valid) == 8 and [item[0] for item in valid] != list(range(1, 9)):
        errors.append("Trading-Radar-Abschnitte stehen nicht in Soll-Reihenfolge")

    # Check content only for correctly identified section headings. The body
    # ends at the next numbered expected heading; empty/placeholder bodies fail.
    for number, title, start, end in found:
        if number < 1 or number > 8 or title.casefold() != expected[number - 1].casefold():
            continue
        later = [
            item[2] for item in found
            if item[2] > start and item[0] in range(number + 1, 9)
            and item[1].casefold() == expected[item[0] - 1].casefold()
        ]
        body_end = min(later) if later else len(text)
        body = text[end:body_end].strip()
        body = re.sub(r"(?im)^TRADING_RADAR_FACHLICH_FINAL=.*$", "", body).strip()
        meaningful = re.sub(r"[\W_]", "", body, flags=re.UNICODE)
        if len(meaningful) < 20:
            errors.append(f"Pflichtabschnitt {number} ({title}): Inhalt fehlt/zu kurz")


    # Fachlich unvollständige Berichte dürfen den regulären Publikations-Gate
    # nicht passieren. NICHT_FINAL bleibt ein Fehler-/Teilbericht.
    status = re.findall(r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text)
    if len(status) != 1:
        errors.append("Fachstatusmarker fehlt oder ist nicht eindeutig")
    elif status[0] != "FINAL":
        errors.append("Fachstatus NICHT_FINAL: reguläre Veröffentlichung gesperrt")

    # M-02: a market-regime label must be tied to the actual market snapshot.
    # A qualitative label alone is insufficient: breadth, rates and
    # dollar/liquidity/volatility observations must be independently visible.
    executive = _section_body(text, 1, SECTION_NAMES[0])
    regime_match = re.search(r"(?im)^\s*Marktregime\s*:\s*([^\r\n]+)", executive)
    if not regime_match or _MISSING_VALUE_RE.fullmatch(regime_match.group(1).strip()):
        errors.append("M-02: explizite, nichtleere Marktregime-Einstufung fehlt.")
    cutoff_match = re.search(
        r"(?im)^\s*Marktregime-Datenstichtag\s*:\s*(\d{4}-\d{2}-\d{2})\s*$",
        executive,
    )
    if not cutoff_match:
        errors.append("M-02: Datenstichtag der Marktregime-Einstufung fehlt oder ist nicht ISO-datiert.")
    breadth_line = next(
        (line for line in executive.splitlines() if re.search(r"(?i)Indizes/Marktbreite\s*:", line)),
        "",
    )
    if not re.search(r"\b\d+\s*/\s*\d+\b", breadth_line):
        errors.append("M-02: Marktbreite benötigt einen numerischen Zähler/Nenner-Nachweis.")
    rates_line = next(
        (line for line in executive.splitlines() if re.search(r"(?i)Zinsen/Renditen\s*:", line)),
        "",
    )
    if not extract_us10y_observations(
        rates_line, allow_run1270_context=_is_run1270_us_curve_context(rates_line)
    ):
        errors.append("M-02: US-10Y-Rendite fehlt als numerische Beobachtung.")
    risk_line = next(
        (line for line in executive.splitlines()
         if re.search(r"(?i)Dollar/Liquidität/Volatilität\s*:", line)),
        "",
    )
    if not re.search(r"(?i)\bVIX\b.{0,40}\d+(?:[,.]\d+)?", risk_line):
        errors.append("M-02: numerische VIX-Beobachtung fehlt.")
    if not re.search(r"(?i)\bDXY\b.{0,40}\d+(?:[,.]\d+)?", risk_line):
        errors.append("M-02: numerische DXY-/Dollarproxy-Beobachtung fehlt.")

    # Tab 2 muss im Bericht als deterministischer Quellenblock vorkommen und
    # die ausgewiesene Zeilenzahl muss mit den exportierten Datensätzen stimmen.
    # Bound the block at chapter 11, not only at the final status marker.
    # Otherwise method/data-appendix lines are accidentally included in the
    # export digest and a correct SHA-256 appears invalid.
    closed_pattern = re.compile(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?10\.5[.]?\s+Geschlossene Positionen[^\r\n]*\r?\n(.*?)"
        r"(?=^[ \t]*(?:#{1,6}\s*)?11(?:\s*[.·](?!\d)|\s+)[^\r\n]*$"
        r"|^[ \t]*TRADING_RADAR_FACHLICH_FINAL=|\Z)"
    )
    closed_sections = list(closed_pattern.finditer(text))
    if len(closed_sections) > 1:
        errors.append("Mehrere Abschnitte 10.5 Geschlossene Positionen gefunden")
    canonical_sections = [
        match for match in closed_sections
        if re.search(r"(?m)^TAB-2-ANZAHL=\d+\s*$", match.group(1))
        and re.search(r"(?m)^TAB-2-QUELLE=[^\r\n]+\s*$", match.group(1))
    ]
    if len(canonical_sections) == 1:
        closed = canonical_sections[0]
    elif not closed_sections:
        closed = None
        errors.append("Geschlossene-Positionen-Abschnitt 10.5 fehlt")
    elif not canonical_sections:
        closed = closed_sections[0]
    else:
        closed = None
        errors.append("Mehrere kanonische Tab-2-Exportblöcke gefunden")
    if closed is not None:
        body = closed.group(1)
        count_match = re.search(r"(?m)^TAB-2-ANZAHL=(\d+)\s*$", body)
        source_match = re.search(r"(?m)^TAB-2-QUELLE=([^\r\n]+)\s*$", body)
        if not source_match or not source_match.group(1).strip():
            errors.append("Tab-2-Quellenmarker fehlt oder ist leer")
        ticker_values = [
            match.group(1).strip()
            for line in body.splitlines()
            if (match := re.search(r"(?:^|\s)Ticker:\s*([^|\r\n]+)", line))
        ]
        rows = ticker_values
        if not count_match:
            errors.append("Tab-2-Quellen-/Exportzählung fehlt")
        elif int(count_match.group(1)) != len(rows):
            errors.append(f"Tab-2-Zählung widerspricht Positionszeilen: gemeldet={count_match.group(1)}, erkannt={len(rows)}")
        position_lines = [
            line for line in body.splitlines()
            if re.search(r"(?:^|\s)Ticker:\s*\S+", line)
        ]
        # Jede Position muss ueber den vereinbarten Schluessel Ticker +
        # Einstiegsdatum identifizierbar sein. Der komplette Zeilentext ist
        # kein geeigneter Schluessel, da Quellzeilennummern unterschiedlich
        # sein koennen und weitere Faktenfelder sich wiederholen duerfen.
        position_keys = []
        invalid_identity_rows = []
        for line in position_lines:
            ticker_match = re.search(r"(?:^|\s)Ticker:\s*([^|\r\n]+)", line)
            entry_match = re.search(r"(?:^|\s)Einstiegsdatum:\s*([^|\r\n]+)", line)
            source_row_match = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
            ticker = ticker_match.group(1).strip().casefold() if ticker_match else ""
            entry_date = entry_match.group(1).strip() if entry_match else ""
            source_row = source_row_match.group(1) if source_row_match else "?"
            normalized_date = _normalize_tab2_entry_date(entry_date)
            if not ticker or normalized_date is None:
                invalid_identity_rows.append(source_row)
            else:
                position_keys.append((ticker, normalized_date))
        if invalid_identity_rows:
            errors.append(
                "Tab-2-Positionsidentitaet fehlt: Ticker+Einstiegsdatum "
                "nicht verifizierbar bei Quellzeilen " + ", ".join(invalid_identity_rows)
            )
        duplicate_keys = sorted(
            f"{ticker}|{entry_date}"
            for ticker, entry_date in set(position_keys)
            if position_keys.count((ticker, entry_date)) > 1
        )
        if duplicate_keys:
            errors.append(
                "Tab-2-Positionsschluessel Ticker+Einstiegsdatum doppelt: "
                + ", ".join(duplicate_keys)
            )
        source_rows = []
        for line in position_lines:
            source_match_row = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
            if source_match_row:
                source_rows.append(int(source_match_row.group(1)))
        if position_lines and len(source_rows) != len(position_lines):
            errors.append("Tab-2-Quellzeilennummer fehlt bei mindestens einem Positionsdatensatz")
        if len(set(source_rows)) != len(source_rows):
            errors.append("Tab-2-Quellzeilennummer ist doppelt; Quellen-/Exportidentität nicht eindeutig")
        if "TAB-2-QUELLE NICHT VERIFIZIERBAR" in body:
            errors.append("Tab-2-Quelle im Bericht als nicht verifizierbar markiert")
        digest_match = re.search(r"(?m)^TAB-2-EXPORT-SHA256=([0-9a-f]{64})\s*$", body)
        if not digest_match:
            errors.append("Tab-2-Export-Fingerabdruck fehlt oder ist ungueltig")
        else:
            # Hash only the exact Tab-2 payload before its fingerprint line.
            # Report-level findings appended after the fingerprint are outside
            # the exported payload and must not invalidate its digest.
            payload = body[:digest_match.start()]
            export_lines = [line.rstrip() for line in payload.splitlines()]
            while export_lines and not export_lines[0].strip():
                export_lines.pop(0)
            while export_lines and not export_lines[-1].strip():
                export_lines.pop()
            export_lines = [line for line in export_lines
                            if not line.startswith("TAB-2-EXPORT-SHA256=")]
            actual_digest = hashlib.sha256("\n".join(export_lines).encode("utf-8")).hexdigest()
            if actual_digest != digest_match.group(1):
                errors.append("Tab-2-Export-Fingerabdruck stimmt nicht mit gespeicherten Zeilen ueberein")
        if count_match and int(count_match.group(1)) > 0 and not ticker_values:
            errors.append("Tab-2 meldet Positionen, aber keine konkreten Ticker-Datensätze")

    if r"\n" in text:
        errors.append("Bericht enthält literal \\n statt physischer Zeilenumbrüche")

    _validate_m05_m10(text, errors)
    appendix = _section_body(text, 8, SECTION_NAMES[7])
    quality_match = re.search(
        r"(?i)OHLCV-Qualitätsstatus\s*:\s*VALID\s*=\s*(\d+)\s*[,;]\s*"
        r"MISSING\s*=\s*(\d+)\s*[,;]\s*STALE\s*=\s*(\d+)\s*[,;]\s*"
        r"INVALID\s*=\s*(\d+).*?Summe\s*=\s*(\d+)\s*/\s*(\d+)",
        appendix,
    )
    if not quality_match:
        errors.append("M-03: OHLCV-Qualitätsstatus VALID/MISSING/STALE/INVALID mit vollständiger Summenkontrolle fehlt.")
    else:
        valid, missing, stale, invalid, total, universe = map(int, quality_match.groups())
        if valid + missing + stale + invalid != total or total != universe:
            errors.append("M-03: OHLCV-Zustandsklassen sind nicht disjunkt/vollständig oder ihre Summe entspricht nicht dem geprüften Universum.")
        state_lists = {}
        for state in ("VALID", "MISSING", "STALE", "INVALID"):
            match = re.search(rf"(?im)^OHLCV-Symbole-{state}:\s*(.+)$", appendix)
            if not match or match.group(1).strip().casefold() in {"nicht verifizierbar", "fehlt"}:
                errors.append(f"M-03: vollständige Symbolzuordnung für {state} fehlt.")
                continue
            values = [] if match.group(1).strip().upper() == "KEINE" else [
                item.strip().upper() for item in match.group(1).split(",") if item.strip()
            ]
            state_lists[state] = values
            ticker_pattern = re.compile(r"(?:\^[A-Z0-9][A-Z0-9._=/\-]{0,29}|[A-Z0-9][A-Z0-9._=/\-]{0,29})")
            malformed = [ticker for ticker in values if not ticker_pattern.fullmatch(ticker)]
            if malformed:
                errors.append(f"M-03: Symbolzuordnung {state} enthält keine tickerreinen Identitäten: " + ", ".join(malformed[:10]))
            expected_count = {"VALID": valid, "MISSING": missing, "STALE": stale, "INVALID": invalid}[state]
            if len(values) != expected_count:
                errors.append(f"M-03: Symbolanzahl der Klasse {state} stimmt nicht mit der Statuszählung überein.")
        flattened = [ticker for values in state_lists.values() for ticker in values]
        if len(flattened) != len(set(flattened)):
            errors.append("M-03: Ein Symbol ist mehreren OHLCV-Zustandsklassen zugeordnet.")
        if len(flattened) != universe:
            errors.append("M-03: Symbolstatusliste deckt das geprüfte Universum nicht exakt ab.")
        non_valid = missing + stale + invalid
        if non_valid > 0 and re.search(
            r"(?i)(?:OHLCV\s+)?(?:vollständig\s*,?\s*)?keine\s+(?:historischen\s+)?Datenlücken|"
            r"keine\s+fehlenden\s+(?:OHLCV[- ]?)?(?:Daten|Kerzen|Handelstage)",
            appendix,
        ):
            errors.append(
                "M-03: Freitext behauptet Datenlückenfreiheit, obwohl die OHLCV-Statuszählung "
                f"{non_valid} nicht-VALID Symbole ausweist."
            )
        mapping_unverifiable = bool(re.search(
            r"(?i)Kandidaten-Mapping\s*:\s*NICHT VERIFIZIERBAR", appendix
        ))
        if mapping_unverifiable and re.search(
            r"(?i)Kandidaten\s+nicht\s+eingeschränkt", appendix
        ):
            errors.append(
                "M-03: Kandidaten-Mapping ist NICHT VERIFIZIERBAR, zugleich werden Kandidaten "
                "als nicht eingeschränkt bezeichnet."
            )
    if not re.search(r"(?i)Kandidaten-Mapping\s*:\s*(?:vollständig|vollstaendig|unvollständig|unvollstaendig|NICHT VERIFIZIERBAR)", appendix):
        errors.append("M-03: Vollständigkeit des Kandidaten-Mappings muss ausdrücklich ausgewiesen werden.")
    return errors


def validate_run_evidence(path: Path, *, require_report_gate: bool = True) -> list[str]:
    errors = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"M-08: Run-Evidence fehlt oder ist unlesbar: {exc}"]
    identity = payload.get("run_identity") if isinstance(payload, dict) else None
    if not isinstance(identity, dict):
        return ["M-08: Run-Evidence enthält keine run_identity."]
    commit = str(identity.get("commit_sha") or "")
    run_id = str(identity.get("workflow_run_id") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit, re.I):
        errors.append("M-08: Commit-SHA fehlt oder ist kein vollständiger Git-SHA.")
    if not run_id.isdigit():
        errors.append("M-08: GitHub-Workflow-Run-ID fehlt.")
    if not str(identity.get("repository") or "").strip() or not str(identity.get("ref") or "").strip():
        errors.append("M-08: Repository und Git-Ref müssen im Run-Evidence-Nachweis vorhanden sein.")
    hashes = identity.get("source_file_hashes")
    required_source_hashes = {
        "Trading_Radar_Alleinauswertung.py",
        "trading_radar_neuauswertung.py",
        "trading_radar_market_first.py",
        "trading_radar_thesen.py",
        "trading_radar_report_gate.py",
        "trading_radar_run_evidence.py",
        "trading_radar_portfolio_performance.py",
        "makro_szenario.py",
        "offene_positionen_check.py",
        "historical_ohlcv.py",
        ".github/workflows/main.yml",
    }
    if not isinstance(hashes, dict) or not required_source_hashes.issubset(set(hashes or {})) or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(value or ""), re.I)
        for value in (hashes or {}).values()
    ):
        errors.append("M-08: SHA256 der vollständigen kritischen Quellcodedateien fehlen oder sind unvollständig.")
    gates = identity.get("gate_results")
    required_gates = (
        "TRADING_RADAR_CONTRACT_OUTCOME", "TRADING_RADAR_STEP_OUTCOME",
        "TRADING_RADAR_OHLCV_PERSISTENCE_GATE", "TRADING_RADAR_MARKET_FIRST_GATE",
        "TRADING_RADAR_FACHLICH_FINAL",
    ) + (("TRADING_RADAR_REPORT_GATE", "TRADING_RADAR_REPORT_GATE_OUTCOME")
         if require_report_gate else ())
    if not isinstance(gates, dict) or any(not gates.get(key) for key in required_gates):
        errors.append("M-08: Datenvertrags-, Lauf-, OHLCV-Persistenz- und Market-First-Gate-Ergebnisse sind nicht vollständig mit dem Laufnachweis verknüpft.")
    elif (
        gates.get("TRADING_RADAR_CONTRACT_OUTCOME") != "success"
        or gates.get("TRADING_RADAR_STEP_OUTCOME") != "success"
        or gates.get("TRADING_RADAR_OHLCV_PERSISTENCE_GATE") != "PASS"
        or gates.get("TRADING_RADAR_MARKET_FIRST_GATE") != "PASS"
        or (require_report_gate and gates.get("TRADING_RADAR_REPORT_GATE") not in {"PASS", "FAIL"})
        or (require_report_gate and gates.get("TRADING_RADAR_REPORT_GATE_OUTCOME") not in {"success", "failure"})
        or gates.get("TRADING_RADAR_FACHLICH_FINAL") not in {"FINAL", "NICHT_FINAL"}
    ):
        errors.append("M-08: Mindestens ein verpflichtendes technisches Gate ist nicht PASS/success.")
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        errors.append("M-08: Start- und Endnachweis sind beide verpflichtend.")
        start = start if isinstance(start, dict) else {}
        end = end if isinstance(end, dict) else {}
    source_entries = end.get("sources") if isinstance(end, dict) else None
    if not isinstance(source_entries, list) or not source_entries or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""), re.I)
        for item in source_entries if isinstance(item, dict)
    ) or any(not isinstance(item, dict) for item in (source_entries or [])):
        errors.append("M-08: Eingangsdaten-/Quellen-SHA256 fehlen im Endnachweis.")
    if start and end:
        start_identity = start.get("run_identity") or {}
        end_identity = end.get("run_identity") or {}
        if start_identity.get("commit_sha") != end_identity.get("commit_sha") or start_identity.get("workflow_run_id") != end_identity.get("workflow_run_id"):
            errors.append("M-08: Start- und Endnachweis stammen nicht vom selben Commit/Lauf.")
    return errors


def main() -> int:
    if len(sys.argv) not in (2, 4) or (len(sys.argv) == 4 and sys.argv[2] != "--evidence"):
        print("Usage: python trading_radar_report_gate.py REPORT.txt [--evidence RUN_EVIDENCE.json]", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"REPORT_CHECK=FAIL | Bericht nicht lesbar: {exc}")
        return 1
    errors = validate_report(text)
    if len(sys.argv) == 4:
        errors.extend(validate_run_evidence(Path(sys.argv[3])))
    if errors:
        for error in errors:
            print(f"REPORT_CHECK=FAIL | {error}")
        return 1
    print("REPORT_CHECK=PASS | acht Pflichtabschnitte eindeutig, geordnet und mit Inhalt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
