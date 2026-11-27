from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GROUP = "offene-positionen-drive-${{ github.repository }}"
EXPECTED_WRITERS = {
    ".github/workflows/main.yml",
    ".github/workflows/stop_check.yml",
    ".github/workflows/benchmarks_check.yml",
    ".github/workflows/positions-recovery.yml",
    ".github/workflows/positions-performance-normalization.yml",
    ".github/workflows/positions-drive-backup-cleanup.yml",
}

# Entry points that are known to write or synchronize the shared positions source.
_WRITER_SCRIPT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:positionen_tracker|offene_positionen_check|cleanup_positions_drive_backups)\.py(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_NORMALIZATION_ENTRYPOINT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:scripts\s*\.\s*)?normalize_history_performance(?:\.py)?(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_RECOVERY_ENTRYPOINT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:scripts\s*\.\s*)?restore_positions(?:\.py)?(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_SHARED_SOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])Offene_Positionen\.csv(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)


def _strip_yaml_comment(value: str) -> str:
    """Remove a trailing YAML comment without truncating quoted scalars."""
    stripped = value.lstrip()
    if stripped.startswith("'"):
        index = 1
        while index < len(stripped):
            if stripped[index] == "'":
                if index + 1 < len(stripped) and stripped[index + 1] == "'":
                    index += 2
                    continue
                tail = stripped[index + 1:]
                return stripped[:index + 1] if tail.lstrip().startswith("#") else value
            index += 1
        return value
    if stripped.startswith('"'):
        escaped = False
        for index in range(1, len(stripped)):
            char = stripped[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                tail = stripped[index + 1:]
                return stripped[:index + 1] if tail.lstrip().startswith("#") else value
        return value

    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _extract_run_commands(text: str) -> list[str]:
    """Extract inline and YAML block-scalar run bodies without extra deps."""
    lines = text.splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        match = re.match(
            r"^(?P<indent>[ \t]*)(?P<list>-\s+)?run:\s*(?P<value>.*)$",
            lines[index],
        )
        if not match:
            index += 1
            continue

        key_indent = len(match.group("indent").expandtabs(8)) + len(match.group("list") or "")
        value = match.group("value").strip()
        block_header = re.fullmatch(
            r"(?P<style>[|>])"
            r"(?:(?P<indent>[1-9])(?P<chomp>[+-])?"
            r"|(?P<chomp_first>[+-])(?P<indent_after>[1-9])?)?"
            r"(?:[ \t]+#.*)?",
            value,
        )
        if value == "" or block_header:
            explicit_indent = 0
            if block_header:
                explicit = block_header.group("indent") or block_header.group("indent_after")
                explicit_indent = int(explicit) if explicit else 0

            index += 1
            block: list[str] = []
            content_indent = key_indent + explicit_indent if explicit_indent else None
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip():
                    candidate_indent = len(candidate) - len(candidate.lstrip(" \t"))
                    if content_indent is None:
                        if candidate_indent <= key_indent:
                            break
                        content_indent = candidate_indent
                    elif candidate_indent < content_indent:
                        break
                block.append(candidate)
                index += 1
            commands.append("\n".join(block))
            continue

        commands.append(_strip_yaml_comment(value))
        index += 1

    return commands


def _workflow_touches_open_positions(path: Path) -> bool:
    commands = _extract_run_commands(path.read_text(encoding="utf-8"))
    executable_text = "\n".join(
        line
        for command in commands
        for line in command.splitlines()
        if not line.lstrip().startswith("#")
    )
    # Shell continuations and line wrapping must not hide a known entry point.
    normalized = re.sub(r"\\\s*\n", " ", executable_text)
    normalized = re.sub(r"\s+", " ", normalized)
    return bool(
        _WRITER_SCRIPT_RE.search(normalized)
        or _RECOVERY_ENTRYPOINT_RE.search(normalized)
        or _NORMALIZATION_ENTRYPOINT_RE.search(normalized)
        or _SHARED_SOURCE_RE.search(normalized)
    )


def _top_level_block_body(text: str, key: str) -> str | None:
    """Return only the direct indented body of a top-level YAML mapping key."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line != f"{key}:":
            continue
        body: list[str] = []
        for candidate in lines[index + 1:]:
            if candidate.strip() and not candidate[0].isspace():
                break
            if candidate.strip():
                body.append(candidate)
        return "\n".join(body)
    return None


def _assert_shared_queue(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    body = _top_level_block_body(text, "concurrency")
    assert body is not None, f"{workflow.name}: workflow-level concurrency fehlt"
    assert f"group: {EXPECTED_GROUP}" in body, (
        f"{workflow.name}: nicht dieselbe repositoryweite Drive-Sperrgruppe"
    )
    assert re.search(r"(?m)^  cancel-in-progress:\s*false\s*$", body), (
        f"{workflow.name}: laufender Writer darf nicht abgebrochen werden"
    )
    assert re.search(r"(?m)^  queue:\s*max\s*$", body), (
        f"{workflow.name}: wartende Läufe müssen gepuffert werden"
    )


def _without_workflow_concurrency(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n") == "concurrency:"),
        None,
    )
    assert start is not None, "Test-Fixture muss einen workflow-level concurrency-Block enthalten"

    end = start + 1
    while end < len(lines):
        candidate = lines[end]
        if candidate.strip() and not candidate[0].isspace():
            break
        end += 1

    return "".join(lines[:start] + lines[end:])

def test_every_open_positions_workflow_uses_the_same_non_cancelling_queue():
    workflow_dir = ROOT / ".github" / "workflows"
    workflows = (*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml"))
    writers = {
        path.relative_to(ROOT).as_posix()
        for path in workflows
        if _workflow_touches_open_positions(path)
    }
    assert writers == EXPECTED_WRITERS, (
        "Das Inventar der Workflows mit Zugriff auf Offene_Positionen hat sich "
        f"geändert. Neue/entfernte Zugriffe prüfen und Sperre ergänzen: {sorted(writers)}"
    )
    for relative in sorted(writers):
        _assert_shared_queue(ROOT / relative)


def test_main_keeps_repository_wide_drive_lock_not_old_main_only_group():
    main = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
    assert "btc-50w-main-${{ github.ref }}" not in main
    assert main.count(EXPECTED_GROUP) == 1


def test_writer_detection_covers_inline_multiline_and_wrapped_commands(tmp_path):
    variants = [
        "steps:\n  - run: python -u positionen_tracker.py\n",
        "steps:\n  - run: |\n      uv run python offene_positionen_check.py\n",
        "steps:\n  - run: >-\n      bash -c 'python positionen_tracker.py'\n",
        "steps:\n  - run: |\n      python3 -u offene_positionen_check.py --dry-run\n",
        "steps:\n  - run: |\n      python -m scripts.restore_positions\n",
        "steps:\n  - run: >-\n      python \\\n        -m \\\n        scripts.restore_positions\n",
        "steps:\n  - run: bash -c 'python -m scripts.restore_positions'\n",
        "steps:\n  - run: python -m scripts.normalize_history_performance --migrate\n",
    ]
    for index, workflow_text in enumerate(variants):
        workflow = tmp_path / f"writer-{index}.yml"
        workflow.write_text(workflow_text, encoding="utf-8")
        assert _workflow_touches_open_positions(workflow), workflow_text




@pytest.mark.parametrize(
    "header,command",
    [
        ("| # writer comment", "python positionen_tracker.py"),
        (">- # folded writer command", "python offene_positionen_check.py"),
        ("|2-", "python -m scripts.restore_positions"),
        ("|-2 # explicit indentation after chomping", "python positionen_tracker.py"),
        (">+2 # folded explicit indentation", "python offene_positionen_check.py"),
    ],
)
def test_writer_detection_supports_yaml_block_header_comments_and_indicators(
    header, command, tmp_path
):
    workflow = tmp_path / "block-header.yml"
    workflow.write_text(
        "steps:\n"
        "  - name: protected writer\n"
        f"    run: {header}\n"
        f"      {command}\n"
        "    env:\n"
        "      SAFE: 'not part of run'\n",
        encoding="utf-8",
    )
    assert _workflow_touches_open_positions(workflow), workflow.read_text(encoding="utf-8")


def test_writer_detection_does_not_consume_sibling_yaml_keys_after_explicit_indent(
    tmp_path,
):
    workflow = tmp_path / "explicit-indent-boundary.yml"
    workflow.write_text(
        "steps:\n"
        "  - name: harmless\n"
        "    run: |2-\n"
        "      echo 'safe command'\n"
        "    env:\n"
        "      NOTE: python positionen_tracker.py\n",
        encoding="utf-8",
    )
    assert not _workflow_touches_open_positions(workflow)


def test_inline_scalar_comment_is_not_misread_as_a_block_header(tmp_path):
    workflow = tmp_path / "inline-comment.yml"
    workflow.write_text(
        "steps:\n"
        "  - run: echo 'safe' # python positionen_tracker.py\n",
        encoding="utf-8",
    )
    assert not _workflow_touches_open_positions(workflow)


def test_quoted_run_scalar_keeps_hash_characters_inside_the_yaml_string(tmp_path):
    workflow = tmp_path / "quoted-hash.yml"
    workflow.write_text(
        "steps:\n"
        '  - run: "echo \'# harmless text\' && python positionen_tracker.py"\n',
        encoding="utf-8",
    )
    assert _workflow_touches_open_positions(workflow)


def test_quoted_run_scalar_discards_only_a_comment_after_the_closing_quote(tmp_path):
    workflow = tmp_path / "quoted-trailing-comment.yml"
    workflow.write_text(
        "steps:\n"
        '  - run: "echo safe" # python positionen_tracker.py\n',
        encoding="utf-8",
    )
    assert not _workflow_touches_open_positions(workflow)


def test_comments_do_not_create_false_writer_entries(tmp_path):
    workflow = tmp_path / "comment-only.yml"
    workflow.write_text(
        "steps:\n  - run: |\n      echo 'ordinary check'\n"
        "      # python -m scripts.restore_positions\n"
        "      # python positionen_tracker.py\n",
        encoding="utf-8",
    )
    assert not _workflow_touches_open_positions(workflow)


def test_shared_source_reference_in_multiline_run_is_inventoried(tmp_path):
    workflow = tmp_path / "explicit-source.yml"
    workflow.write_text(
        "steps:\n  - run: |\n      ./scripts/update_positions.sh Offene_Positionen.csv\n",
        encoding="utf-8",
    )
    assert _workflow_touches_open_positions(workflow)



def test_values_outside_the_concurrency_block_cannot_fake_a_valid_lock(tmp_path):
    workflow = tmp_path / "decoy-lock.yml"
    workflow.write_text(
        "name: decoy\n"
        "concurrency:\n"
        "  group: wrong-group\n"
        "  cancel-in-progress: true\n"
        "  queue: one\n"
        "jobs:\n"
        "  fake:\n"
        "    env:\n"
        f"      GROUP: {EXPECTED_GROUP}\n"
        "      CANCEL: false\n"
        "      QUEUE: max\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="nicht dieselbe repositoryweite"):
        _assert_shared_queue(workflow)

@pytest.mark.parametrize("relative", sorted(EXPECTED_WRITERS))
def test_each_writer_fails_when_its_shared_concurrency_lock_is_removed(relative, tmp_path):
    original = (ROOT / relative).read_text(encoding="utf-8")
    assert _workflow_touches_open_positions(ROOT / relative), relative

    mutated = _without_workflow_concurrency(original)
    candidate = tmp_path / Path(relative).name
    candidate.write_text(mutated, encoding="utf-8")

    with pytest.raises(AssertionError, match="workflow-level concurrency fehlt"):
        _assert_shared_queue(candidate)


def test_recovery_writer_is_explicitly_part_of_the_protected_inventory():
    recovery = ROOT / ".github" / "workflows" / "positions-recovery.yml"
    assert _workflow_touches_open_positions(recovery)
    _assert_shared_queue(recovery)


def test_drive_backup_cleanup_workflow_is_hard_limited_to_main():
    workflow = (ROOT / ".github" / "workflows" / "positions-drive-backup-cleanup.yml").read_text(encoding="utf-8")
    assert "  cleanup-expired-drive-backup" in workflow
    assert "    if: github.ref == 'refs/heads/main'" in workflow


def test_main_never_uploads_local_position_snapshots_as_artifacts():
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/main.yml"
    ).read_text(encoding="utf-8")
    # The repository is public. Rollback snapshots can contain the complete
    # positions master and must not be exported as downloadable workflow artifacts.
    assert "google_rollback" not in workflow
    assert ".Offene_Positionen.csv.google_rollback_*.bak" not in workflow
    assert "Positions-Rollback-Snapshots bleiben ausschließlich auf dem Runner" in workflow
