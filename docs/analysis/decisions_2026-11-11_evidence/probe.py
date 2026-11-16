"""Read-only probe: which recent bars does Yahoo have closes for, right now? (sample of tickers)"""
import os, sqlite3, time, datetime, pandas as pd
from quant.config import load as load_config
from quant.types import SystemClock
from quant.data.yahoo import YahooClient
cfg = load_config(); assert "/nov11/" in str(cfg.paths.db)
c = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
tick = [r[0] for r in c.execute("select yahoo_ticker from symbol_history where valid_to is null and yahoo_ticker like '%.NS' and nse_symbol not like 'DUMMY%' order by security_id")]
sample = tick[::4][:125]
if "MRF.NS" not in sample: sample.append("MRF.NS")
if "RELIANCE.NS" not in sample: sample.append("RELIANCE.NS")
client = YahooClient(cfg=cfg, clock=SystemClock(), sleep=time.sleep)
df = client.download_batch(sample, "2026-11-03", "2026-11-13")
close = df["Close"]; vol = df["Volume"]
print("probe time (IST):", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "| tickers:", close.shape[1])
for d, row in close.iterrows():
    n = int(row.notna().sum()); v = int((vol.loc[d].fillna(0) > 0).sum())
    print(str(d)[:10], pd.Timestamp(d).day_name()[:3], f"closes {n}/{close.shape[1]} ({n/close.shape[1]:.0%})", f"| bars with volume {v}")
for t in ("MRF.NS", "RELIANCE.NS"):
    print(t, {str(k)[:10]: (None if pd.isna(x) else round(float(x), 2)) for k, x in close[t].tail(4).items()})
