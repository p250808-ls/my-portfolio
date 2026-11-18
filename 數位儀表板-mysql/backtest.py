"""
加分項：預警命中率回測。
決策引擎關閉（避免干預改變結果），比較 edf_at_10 與實際 exceeded：
  - 命中率（precision）：edf_at_10 ≥ 50% 的區間中，實際超約的比例
  - 召回率（recall）：實際超約的區間中，edf_at_10 ≥ 50% 的比例
  - Brier score：mean((edf_at_10 − exceeded)²)，越小越好
  - 校準表：EDF 分桶 vs 實際超約率（理想情況兩者相等）
並比較 σ 估計視窗 15／30／60 分鐘。

  python backtest.py --days 7
  python backtest.py --days 7 --windows 15 30 60 90
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config as C
from engine import Engine
from writer import MemoryWriter


def run(days: float, window: int, start: datetime) -> pd.DataFrame:
    C.SIGMA_WINDOW_MIN = window
    mw = MemoryWriter()
    eng = Engine(writer=mw, epoch=start, engine_on=False, log=lambda *a, **k: None)
    ts = start
    for _ in range(int(days * 1440)):
        eng.tick(ts)
        ts += timedelta(minutes=1)
    return pd.DataFrame(mw.interval_settlement)


def metrics(iv: pd.DataFrame, threshold: float = 0.5) -> dict:
    iv = iv.dropna(subset=["edf_at_10"])
    pred = iv.edf_at_10 >= threshold
    act = iv.exceeded.astype(bool)
    precision = (pred & act).sum() / pred.sum() if pred.sum() else float("nan")
    recall = (pred & act).sum() / act.sum() if act.sum() else float("nan")
    brier = float(((iv.edf_at_10 - act.astype(float)) ** 2).mean())
    return {"區間數": len(iv), "實際超約率": act.mean(), "預警數(EDF10≥50%)": int(pred.sum()),
            "命中率": precision, "召回率": recall, "Brier": brier}


def calibration_table(iv: pd.DataFrame) -> pd.DataFrame:
    iv = iv.dropna(subset=["edf_at_10"]).copy()
    bins = [0, 0.05, 0.2, 0.5, 0.8, 0.95, 1.0001]
    labels = ["0–5%", "5–20%", "20–50%", "50–80%", "80–95%", "95–100%"]
    iv["bucket"] = pd.cut(iv.edf_at_10, bins=bins, labels=labels, include_lowest=True)
    g = iv.groupby("bucket", observed=False).agg(區間數=("exceeded", "size"),
                                                平均EDF=("edf_at_10", "mean"),
                                                實際超約率=("exceeded", "mean"))
    return g


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--windows", type=int, nargs="+", default=[15, 30, 60])
    p.add_argument("--start", default="2026-10-14T00:00")
    a = p.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=C.TZ)

    rows = []
    tables = {}
    for w in a.windows:
        iv = run(a.days, w, start)
        m = metrics(iv)
        rows.append({"σ視窗(分)": w, **m})
        tables[w] = calibration_table(iv)
    df = pd.DataFrame(rows).set_index("σ視窗(分)")
    pd.set_option("display.float_format", lambda x: f"{x:.3f}")
    print(f"\n== 命中率回測（{a.days:g} 天，引擎關閉）==")
    print(df.to_string())
    for w, t in tables.items():
        print(f"\n-- 校準表：σ 視窗 {w} 分鐘 --")
        print(t.to_string())
    best = df["Brier"].idxmin()
    print(f"\nBrier 最低的視窗：{best} 分鐘 → 建議 config.SIGMA_WINDOW_MIN = {best}")


if __name__ == "__main__":
    main()
