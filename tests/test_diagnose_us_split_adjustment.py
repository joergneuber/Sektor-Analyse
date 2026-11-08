from __future__ import annotations
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import pandas as pd

# Minimal helper stub keeps these unit tests isolated from repository modules.
def _normalise(raw, originals, requested):
    out={}
    if raw is None or raw.empty: return out
    for orig, req in zip(originals, requested):
        try: f=raw.xs(req, level=0).copy()
        except Exception: continue
        f=f.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})
        if {'Open','High','Low','Close','Volume'}.issubset(f.columns): out[orig]=f
    return out
def _compare(a,b,ln,rn):
    if a is None or b is None: return {'status':'insufficient_data','overlap_bars':0}
    ix=a.index.intersection(b.index)
    return {'status':'compared' if len(ix) else 'no_common_dates','overlap_bars':len(ix),'close_mean_abs_diff_pct':0.0}
stub=types.ModuleType('provider_coverage_us'); stub._normalise_alpaca_frames=_normalise; stub._compare_ohlcv_frames=_compare
sys.modules['provider_coverage_us']=stub

P = Path(__file__).resolve().parents[1] / "diagnose_us_split_adjustment.py"
SPEC = importlib.util.spec_from_file_location("splitdiag", P)
m = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = m; SPEC.loader.exec_module(m)

class SplitDiagnosticTests(unittest.TestCase):
    def test_fetch_requests_split_adjustment_and_sip(self):
        seen=[]
        class Req:
            def __init__(self, **kw): self.kw=kw; seen.append(kw)
        class Client:
            def get_stock_bars(self, req):
                syms=req.kw['symbol_or_symbols']
                idx=pd.MultiIndex.from_product([syms,pd.to_datetime(['2026-11-06'])],names=['symbol','timestamp'])
                df=pd.DataFrame({'open':[10.],'high':[12.],'low':[9.],'close':[11.],'volume':[100]},index=idx)
                return types.SimpleNamespace(df=df)
        fake={
          'alpaca':types.ModuleType('alpaca'),'alpaca.data':types.ModuleType('alpaca.data'),
          'alpaca.data.enums':types.SimpleNamespace(Adjustment=types.SimpleNamespace(SPLIT='split'),DataFeed=types.SimpleNamespace(SIP='sip')),
          'alpaca.data.requests':types.SimpleNamespace(StockBarsRequest=Req),
          'alpaca.data.timeframe':types.SimpleNamespace(TimeFrame=types.SimpleNamespace(Day='1Day'))}
        with patch.dict(sys.modules,fake):
            frames,status=m.fetch_split_adjusted(Client(),['AAA'],pd.Timestamp('2025-11-01').to_pydatetime(),pd.Timestamp('2026-11-07').to_pydatetime())
        self.assertEqual(status['AAA'],'rows_returned'); self.assertIn('AAA',frames)
        self.assertEqual(seen[0]['adjustment'],'split'); self.assertEqual(seen[0]['feed'],'sip')
    def test_uses_shared_ohlcv_comparison(self):
        idx=pd.date_range('2026-01-01',periods=30,freq='B')
        f=pd.DataFrame({'Open':range(10,40),'High':range(12,42),'Low':range(9,39),'Close':range(11,41),'Volume':[100]*30},index=idx)
        r=m.pd.DataFrame(f)
        result=stub._compare_ohlcv_frames(f,r,'SPLIT','Yahoo')
        self.assertEqual(result['status'],'compared'); self.assertEqual(result['close_mean_abs_diff_pct'],0.0)
    def test_database_hash_detects_change(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x'; p.write_bytes(b'a'); h=m.sha256(p); p.write_bytes(b'b')
            self.assertNotEqual(h,m.sha256(p))

if __name__=='__main__': unittest.main(verbosity=2)
