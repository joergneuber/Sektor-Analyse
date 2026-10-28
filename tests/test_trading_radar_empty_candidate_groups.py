from trading_radar_market_first import report_has_explicit_empty_candidate_groups


def report(groups):
    return "# 5. Technische Kandidaten\n\n" + groups + "\n\n# 6. Fazit\nWeiterer Inhalt.\n"


def test_all_four_explicitly_empty_groups_are_accepted():
    text = report("\n".join([
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten",
        "AUF TRIGGER WARTEN: Keine Kandidaten",
        "EREIGNIS ABWARTEN: Keine Kandidaten",
        "NICHT HANDELBAR: Keine Kandidaten",
    ]))
    assert report_has_explicit_empty_candidate_groups(text)


def test_missing_group_is_rejected():
    text = report("\n".join([
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten",
        "AUF TRIGGER WARTEN: Keine Kandidaten",
        "EREIGNIS ABWARTEN: Keine Kandidaten",
    ]))
    assert not report_has_explicit_empty_candidate_groups(text)


def test_candidate_record_in_zero_candidate_report_is_rejected():
    text = report("\n".join([
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten",
        "AUF TRIGGER WARTEN: Keine Kandidaten",
        "EREIGNIS ABWARTEN: Keine Kandidaten",
        "NICHT HANDELBAR: Keine Kandidaten",
        "Asset/Ticker: ABC",
    ]))
    assert not report_has_explicit_empty_candidate_groups(text)


def test_duplicate_group_is_rejected():
    text = report("\n".join([
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten",
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten",
        "AUF TRIGGER WARTEN: Keine Kandidaten",
        "EREIGNIS ABWARTEN: Keine Kandidaten",
        "NICHT HANDELBAR: Keine Kandidaten",
    ]))
    assert not report_has_explicit_empty_candidate_groups(text)


def test_plain_language_statement_without_four_groups_is_not_enough():
    assert not report_has_explicit_empty_candidate_groups(
        report("Keine belastbaren technischen Kandidaten vorhanden.")
    )
