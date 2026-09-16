from __future__ import annotations
import tempfile
from pathlib import Path
import pandas as pd
import pi_cycle_bottom


def main():
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1), periods=600, freq="D")
    close = [100.0] * 573 + [1.0] * 27
    hist = pd.DataFrame({"Close": close}, index=idx)
    with tempfile.TemporaryDirectory() as td:
        pi_cycle_bottom.STATE_FILE = Path(td) / "pi_state.json"
        result = pi_cycle_bottom.calculate_pi_cycle_bottom(hist)
        assert result["signal"] is True, result
        assert result["signal_type"] == "BOTTOM_LONG", result
        assert result["state"] == "ACCUMULATION", result
    # Static guard: the documented mapping must never regress to UP=BUY/DOWN=SELL.
    source = Path(pi_cycle_bottom.__file__).read_text(encoding="utf-8")
    assert 'signal = "BOTTOM_LONG" if bool(event["cross_down"]) else "ACCUMULATION_END"' in source
    assert "von oben nach unten" in source
    print("PI_CYCLE_BOTTOM_TESTS: 1 PASS")


if __name__ == "__main__":
    main()
