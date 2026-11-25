"""Idempotent OHLCV release cleanup tests with a fake GitHub CLI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "scripts" / "cleanup_ohlcv_release_assets.sh"


class OhlcvReleaseCleanupTests(unittest.TestCase):
    def _run_cleanup(self, scenario, assets=None, max_attempts="3"):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            state_path = temp_path / "state.json"
            state = {
                "assets": assets if assets is not None else [
                    {"name": "historical_ohlcv_bundle_current.zip", "size": 10, "state": "uploaded"},
                    {"name": "historical_ohlcv_bundle_rollback.zip", "size": 10, "state": "uploaded"},
                    {"name": "historical_ohlcv_bundle_old.zip", "size": 10, "state": "uploaded"},
                ],
                "scenario": scenario,
                "delete_calls": 0,
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            gh = fake_bin / "gh"
            gh.write_text("""#!/usr/bin/env bash
set -euo pipefail
args="$*"
if [ "${1:-}" = "api" ]; then
  if [[ "$args" == *"releases/tags/historical-ohlcv"* ]]; then
    printf '{"id":123}\\n'
    exit 0
  fi
  if [[ "$args" == *"/releases/123/assets?per_page=100"* ]]; then
    jq -c '[.assets]' "$FAKE_GH_STATE"
    exit 0
  fi
fi
if [ "${1:-}" = "release" ] && [ "${2:-}" = "delete-asset" ]; then
  name="${4:?asset name required}"
  scenario="$(jq -r '.scenario' "$FAKE_GH_STATE")"
  calls="$(jq -r '.delete_calls + 1' "$FAKE_GH_STATE")"
  if [ "$scenario" = "delete_500_after_delete" ]; then
    jq --arg name "$name" --argjson calls "$calls" '.delete_calls=$calls | .assets=[.assets[] | select(.name != $name)]' "$FAKE_GH_STATE" > "$FAKE_GH_STATE.tmp"
    mv "$FAKE_GH_STATE.tmp" "$FAKE_GH_STATE"
    exit 1
  fi
  if [ "$scenario" = "delete_500_then_success" ] && [ "$calls" -eq 1 ]; then
    jq --argjson calls "$calls" '.delete_calls=$calls' "$FAKE_GH_STATE" > "$FAKE_GH_STATE.tmp"
    mv "$FAKE_GH_STATE.tmp" "$FAKE_GH_STATE"
    exit 1
  fi
  if [ "$scenario" = "terminal_failure" ]; then
    jq --argjson calls "$calls" '.delete_calls=$calls' "$FAKE_GH_STATE" > "$FAKE_GH_STATE.tmp"
    mv "$FAKE_GH_STATE.tmp" "$FAKE_GH_STATE"
    exit 1
  fi
  jq --arg name "$name" --argjson calls "$calls" '.delete_calls=$calls | .assets=[.assets[] | select(.name != $name)]' "$FAKE_GH_STATE" > "$FAKE_GH_STATE.tmp"
  mv "$FAKE_GH_STATE.tmp" "$FAKE_GH_STATE"
  exit 0
fi
echo "unexpected fake gh invocation: $args" >&2
exit 2
""", encoding="utf-8")
            gh.chmod(0o755)
            sleep = fake_bin / "sleep"
            sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sleep.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PATH": str(fake_bin) + os.pathsep + env["PATH"],
                "FAKE_GH_STATE": str(state_path),
                "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
                "OHLCV_RELEASE_DELETE_MAX_ATTEMPTS": max_attempts,
            })
            cleanup_candidates = [
                asset["name"] for asset in state["assets"]
                if asset["name"].startswith("historical_ohlcv_bundle_")
                and asset["name"] not in (
                    "historical_ohlcv_bundle_current.zip",
                    "historical_ohlcv_bundle_rollback.zip",
                )
            ]
            result = subprocess.run(
                ["bash", str(CLEANUP),
                 "historical_ohlcv_bundle_current.zip",
                 "historical_ohlcv_bundle_rollback.zip",
                 *cleanup_candidates],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
            )
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            return result, final_state

    def test_http_500_after_server_side_delete_is_confirmed_as_success(self):
        result, state = self._run_cleanup("delete_500_after_delete")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("historical_ohlcv_bundle_old.zip", [a["name"] for a in state["assets"]])
        self.assertEqual(state["delete_calls"], 1)
        self.assertIn("DELETE_CONFIRMED", result.stdout)

    def test_http_500_with_asset_still_present_retries_after_refresh(self):
        result, state = self._run_cleanup("delete_500_then_success")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("historical_ohlcv_bundle_old.zip", [a["name"] for a in state["assets"]])
        self.assertEqual(state["delete_calls"], 2)

    def test_terminal_delete_failure_keeps_gate_failed(self):
        result, state = self._run_cleanup("terminal_failure", max_attempts="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remains present", result.stdout)
        self.assertIn("historical_ohlcv_bundle_old.zip", [a["name"] for a in state["assets"]])
        self.assertEqual(state["delete_calls"], 2)

    def test_retry_limit_is_hard_bounded(self):
        result, state = self._run_cleanup("success", max_attempts="6")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(state["delete_calls"], 0)
        self.assertIn("between 1 and 5", result.stdout)

    def test_already_absent_asset_is_idempotent_success(self):
        assets = [
            {"name": "historical_ohlcv_bundle_current.zip", "size": 10, "state": "uploaded"},
            {"name": "historical_ohlcv_bundle_rollback.zip", "size": 10, "state": "uploaded"},
        ]
        result, state = self._run_cleanup("success", assets=assets)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(state["delete_calls"], 0)
        self.assertIn("Bereinigung PASS", result.stdout)

    def test_missing_protected_bundle_blocks_all_deletions(self):
        assets = [
            {"name": "historical_ohlcv_bundle_current.zip", "size": 10, "state": "uploaded"},
            {"name": "historical_ohlcv_bundle_old.zip", "size": 10, "state": "uploaded"},
        ]
        result, state = self._run_cleanup("success", assets=assets)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["delete_calls"], 0)
        self.assertIn("rollback asset disappeared", result.stdout)

    def test_main_workflow_routes_cleanup_through_idempotent_script_and_guards_upload(self):
        workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/cleanup_ohlcv_release_assets.sh", workflow)
        self.assertNotIn("gh release delete-asset historical-ohlcv", workflow)
        self.assertIn("verify_existing_ohlcv_asset", workflow)
        self.assertIn("OHLCV_RELEASE_UPLOAD=ALREADY_PRESENT_AND_VERIFIED", workflow)
        self.assertIn("Remote-Status lesen, nie blind", workflow)
        self.assertIn("manifest(sys.argv[1]) != manifest(sys.argv[2])", workflow)
        self.assertIn("HTTP[[:space:]]*500", workflow)
        self.assertIn('upload_attempted=0', workflow)
        self.assertIn('if [ "$upload_attempted" -ne 0 ]; then', workflow)
        self.assertIn('ausschließlich der Remote-Zustand weiter geprüft', workflow)

    def test_legacy_assets_are_removed_only_after_two_verified_bundles_are_protected(self):
        assets = [
            {"name": "historical_ohlcv_bundle_current.zip", "size": 10, "state": "uploaded"},
            {"name": "historical_ohlcv_bundle_rollback.zip", "size": 10, "state": "uploaded"},
            {"name": "historical_ohlcv_bundle_old.zip", "size": 10, "state": "uploaded"},
            {"name": "historical_ohlcv.sqlite", "size": 10, "state": "uploaded"},
            {"name": "historical_ohlcv_universe.json", "size": 10, "state": "uploaded"},
        ]
        result, state = self._run_cleanup("delete_500_after_delete", assets=assets)
        names = [asset["name"] for asset in state["assets"]]
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("historical_ohlcv_bundle_current.zip", names)
        self.assertIn("historical_ohlcv_bundle_rollback.zip", names)
        self.assertNotIn("historical_ohlcv_bundle_old.zip", names)
        self.assertNotIn("historical_ohlcv.sqlite", names)
        self.assertNotIn("historical_ohlcv_universe.json", names)
        self.assertEqual(state["delete_calls"], 3)

    def test_invalid_protected_asset_name_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            gh = fake_bin / "gh"
            gh.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
            gh.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PATH": str(fake_bin) + os.pathsep + env["PATH"],
                "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
            })
            result = subprocess.run(
                ["bash", str(CLEANUP), "not-a-bundle.zip", "historical_ohlcv_bundle_rollback.zip"],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Invalid protected OHLCV bundle name", result.stdout)

    def test_workflow_upload_rechecks_remote_state_and_guards_transient_download_failures(self):
        workflow = (ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")
        self.assertIn('upload_output="$(gh release upload historical-ohlcv "$bundle_name" 2>&1)"', workflow)
        self.assertIn('release_assets="$(bash scripts/get_gh_release_assets.sh "$GITHUB_REPOSITORY" historical-ohlcv)"', workflow)
        self.assertIn('HTTP[[:space:]]*500|status[[:space:]]*500|500[[:space:]]+Internal[[:space:]]+Server[[:space:]]+Error|timed[[:space:]]*out|timeout|connection[[:space:]]+reset', workflow)
        self.assertGreaterEqual(workflow.count('if verify_existing_ohlcv_asset "$existing_digest"; then'), 3)
        self.assertIn('return 1', workflow)
        self.assertIn('return 2', workflow)
        self.assertIn('OHLCV_RELEASE_UPLOAD=VERIFIED_AFTER_ERROR', workflow)
        self.assertIn('if [ "$upload_verified" -ne 1 ]; then', workflow)
        self.assertIn('bash scripts/cleanup_ohlcv_release_assets.sh', workflow)


if __name__ == "__main__":
    unittest.main()
