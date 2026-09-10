import os
from pathlib import Path


def test_hebeltrader_upload_requires_explicit_opt_in():
    source = Path(__file__).parents[1] / "upload_to_drive.py"
    text = source.read_text(encoding="utf-8")
    assert "UPLOAD_HEBELTRADER" in text
    assert "if upload_hebeltrader:" in text
    assert "HEBELTRADER-State wird nur vom dedizierten HEBELTRADER-Workflow hochgeladen" in text


def test_hebeltrader_workflow_explicitly_enables_upload():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "hebeltrader_einzel_check.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "UPLOAD_HEBELTRADER: '1'" in text
