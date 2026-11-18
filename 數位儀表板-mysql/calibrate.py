"""
校準規則（6.1）：決策引擎關閉下先跑一段時間，統計 interval_settlement 中 exceeded 的比例，
目標 8%～12%。偏離時只調尖峰幅度（每次 ±5%），基礎負載與 σ 不動。

  python calibrate.py                 # 規格預設：2 小時（8 個區間，統計雜訊大）
  python calibrate.py --hours 48      # 建議：至少 24～48 小時再定案
  python calibrate.py --hours 48 --auto   # 自動以 ±5% 步進搜尋，印出建議的 SPIKE_AMP_LOW/HIGH
"""
from __future__ import annotations

import argparse
from datetime import datetime

import numpy as np
import pandas as pd

import config as C
from engine import Engine
from writer import MemoryWriter


def run_once(hours: float, amp: tuple[float, float], start: datetime) -> float:
    C.SPIKE_AMP_FRAC = amp
    mw = MemoryWriter()
    eng = Engine(writer=mw, epoch=start, engine_on=False, log=lambda *a, **k: None)
    ts = start
    from datetime import timedelta
    for _ in range(int(hours * 60)):
        eng.tick(ts)
        ts += timedelta(minutes=1)
    iv = pd.DataFrame(mw.interval_settlement)
    return float(iv.exceeded.mean()) if len(iv) else 0.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=2.0)
    p.add_argument("--auto", action="store_true")
    p.add_argument("--start", default="2026-10-14T00:00")
    a = p.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=C.TZ)

    lo, hi = C.SPIKE_AMP_FRAC
    rate = run_once(a.hours, (lo, hi), start)
    print(f"尖峰幅度 {lo:.0%}～{hi:.0%}：{a.hours:g} 小時內超約區間比例 {rate:.1%}（目標 8%～12%）")
    if not a.auto:
        if rate < 0.08:
            print("→ 低於目標：將 config.SPIKE_AMP_FRAC 兩端各 +5%")
        elif rate > 0.12:
            print("→ 高於目標：將 config.SPIKE_AMP_FRAC 兩端各 −5%")
        else:
            print("→ 落在目標區間，無需調整")
        return

    for _ in range(12):
        if 0.08 <= rate <= 0.12:
            break
        step = 0.05 if rate < 0.08 else -0.05
        lo, hi = round(lo + step, 2), round(hi + step, 2)
        rate = run_once(a.hours, (lo, hi), start)
        print(f"  嘗試 {lo:.0%}～{hi:.0%} → {rate:.1%}")
    print(f"\n建議寫入 config.py：SPIKE_AMP_FRAC = ({lo:.2f}, {hi:.2f})   （超約比例 {rate:.1%}）")


if __name__ == "__main__":
    main()
