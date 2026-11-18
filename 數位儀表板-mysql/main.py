"""
排程主程式。

  python main.py --init-db                # 建表（DROP + CREATE 五張表）
  python main.py                          # 真實時間：每 60 秒一筆，寫入 DATABASE_URL
  python main.py --speed 0 --days 7       # 加速：不等待，一次跑完 7 天（展示 / 填資料）
  python main.py --speed 10 --days 1      # 加速 10 倍（每 6 秒一個模擬分鐘）
  python main.py --dry-run --days 1       # 不接資料庫，只印摘要
  python main.py --engine off --hours 2   # 校準用：關閉決策引擎

重啟時若 power_reading 已有資料，沿用其第一筆 ts 作為 7 天週期起點，interval_id 不會跳號。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

import config as C
from engine import Engine, floor_minute
from writer import MemoryWriter, make_writer


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="KMV 尖峰電力超約預警 × ESG 決策系統 模擬排程")
    p.add_argument("--init-db", action="store_true", help="重建五張表後結束")
    p.add_argument("--speed", type=float, default=None,
                   help="時間倍速；預設真實時間（每 60 秒一筆）；0 = 不等待；--dry-run / --start 時預設 0")
    p.add_argument("--days", type=float, default=None, help="跑滿 N 個模擬天後結束（預設不停）")
    p.add_argument("--hours", type=float, default=None, help="跑滿 N 個模擬小時後結束")
    p.add_argument("--start", type=str, default=None,
                   help="加速模式的模擬起點，如 2026-10-14T00:00（Asia/Taipei）；預設現在")
    p.add_argument("--engine", choices=["on", "off"], default="on", help="決策引擎開關")
    p.add_argument("--dry-run", action="store_true", help="不連資料庫（MemoryWriter）")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def summarize(mw: MemoryWriter):
    import pandas as pd
    iv = pd.DataFrame(mw.interval_settlement)
    dl = pd.DataFrame(mw.decision_log)
    print(f"\n讀數 {len(mw.power_reading):,} 筆 / 結算區間 {len(iv):,} / 決策 {len(dl):,}")
    if len(iv):
        print(f"超約區間比例 {iv.exceeded.mean():.1%}，罰款合計 NTD {iv.penalty_ntd.sum():,.0f}，"
              f"避免罰款 NTD {iv.avoided_penalty_ntd.sum():,.0f}")
    if len(dl):
        print(dl.groupby("tier").size().to_string())
        print(dl.chosen_plan.value_counts().to_string())
    for d, row in mw.carbon_budget_daily.items():
        print(f"{d} scope2={row['scope2_kg']:,.0f}kg remaining={row['budget_remaining_kg']:,.0f}kg "
              f"carbon_edf={row['carbon_edf']:.3f} avoided={row['avoided_carbon_kg']:.1f}kg")


def main(argv=None):
    a = parse_args(argv)
    log = (lambda *x, **k: None) if a.quiet else print

    writer = MemoryWriter() if a.dry_run else make_writer()
    if a.init_db:
        writer.init_schema()
        print("已重建資料表：power_reading, interval_settlement, decision_log, decision_candidates, carbon_budget_daily, esg_waterfall_daily")
        writer.close()
        return

    if not a.dry_run and writer.ensure_schema():
        log("資料表不存在，已自動建表")

    realtime = a.speed is None and not a.dry_run and a.start is None
    if a.speed is None:
        a.speed = 1.0 if realtime else 0.0
    if a.start:
        start = floor_minute(datetime.fromisoformat(a.start).replace(tzinfo=C.TZ))
    else:
        start = floor_minute(datetime.now(C.TZ))

    first = writer.first_reading_ts()
    if first is not None and realtime:
        epoch = first.astimezone(C.TZ)
        log(f"沿用既有週期起點 {epoch:%Y-%m-%d %H:%M}")
    else:
        if first is not None:
            log("既有資料與加速模式不相容，重建五張表")
            writer.init_schema()
        epoch = start

    eng = Engine(writer=writer, epoch=epoch, engine_on=(a.engine == "on"), log=log)
    log(f"契約容量 {C.CONTRACT_KW:,.0f} kW，7 天碳基準 {eng.baseline_week_kg/1000:,.1f} t，"
        f"預算 {eng.budget_kg/1000:,.1f} t（×{C.CARBON_TARGET_RATIO}）；引擎 {a.engine}")

    total_minutes = None
    if a.days is not None:
        total_minutes = int(a.days * 1440)
    elif a.hours is not None:
        total_minutes = int(a.hours * 60)

    ts = start
    n = 0
    try:
        while total_minutes is None or n < total_minutes:
            if realtime:
                ts = floor_minute(datetime.now(C.TZ))
            eng.tick(ts)
            n += 1
            if realtime:
                nxt = ts + timedelta(seconds=C.TICK_SECONDS)
                time.sleep(max((nxt - datetime.now(C.TZ)).total_seconds(), 0.5))
            else:
                ts += timedelta(minutes=1)
                if a.speed > 0:
                    time.sleep(C.TICK_SECONDS / a.speed)
    except KeyboardInterrupt:
        log("\n停止")
    finally:
        if a.dry_run and not a.quiet:
            summarize(writer)
        writer.close()
    return eng, writer


if __name__ == "__main__":
    main()
