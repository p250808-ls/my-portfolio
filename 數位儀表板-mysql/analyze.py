"""
從資料庫讀出結果做書面說明用的分析（也可用來驗證 Power BI 卡片的數字）。

  python analyze.py                 # 摘要 + 決策範例 + 「降載反而增排」案例
  python analyze.py --decision 11   # 印出指定 decision_id 的完整候選表
"""
from __future__ import annotations

import argparse
import json
import warnings

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

import config as C
from writer import connect

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)
pd.set_option("display.float_format", lambda x: f"{x:,.2f}")


def _json(v):
    """MySQL JSON 欄位回傳字串；PostgreSQL jsonb / text[] 已是 Python 物件。"""
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def q(conn, sql, params=None) -> pd.DataFrame:
    df = pd.read_sql_query(sql, conn, params=params)
    for col in ("candidate_json", "locked_loads"):
        if col in df:
            df[col] = df[col].map(_json)
    for col in ("exceeded", "is_locked", "is_chosen", "outcome_exceeded"):   # MySQL BOOLEAN = TINYINT 0/1
        if col in df:
            df[col] = df[col].map(lambda v: v if v is None or pd.isna(v) else bool(v))
    return df


def _local(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize(C.TZ) if t.tzinfo is None else t.tz_convert(C.TZ)


def show_decision(conn, decision_id: int):
    d = q(conn, "SELECT * FROM decision_log WHERE decision_id = %s", (decision_id,)).iloc[0]
    c = q(conn, """SELECT plan_name, shed_kw, edf_after, penalty_avoided_ntd, cost_ntd, delta_carbon_kg,
                          esv_ntd, is_locked, is_chosen
                   FROM decision_candidates WHERE decision_id = %s ORDER BY candidate_id""", (decision_id,))
    j = d.candidate_json
    d["ts"] = _local(d.ts)
    print(f"\n決策 #{decision_id}  {d.ts:%Y-%m-%d %H:%M}  區間 {d.interval_id}  {d.tier}  EDF_10 = {float(d.edf_trigger):.3f}")
    print(f"  預估結算平均 {j['projected_avg_kw']:,.0f} kW，buffer {j['buffer_kw']:,.0f} kW，σ_avg {j['sigma_avg_kw']:,.0f} kW")
    print(f"  C_penalty = NTD {j['c_penalty_ntd']:,}；carbon_edf = {j['carbon_edf']:.3f} → P_eff = {j['p_eff_ntd_per_kg']*1000:,.0f} NTD/t")
    print(f"  WBGT {j['wbgt']}°C，製程溫度 {j['process_temp']}°C，鎖定：{list(d.locked_loads) or '無'}")
    print(f"  選定：{d.chosen_plan}（{d.approval_mode}，延遲 {d.approval_delay_s}s）ESV = NTD {float(d.esv_ntd):,.0f}，"
          f"ΔC = {float(d.delta_carbon_kg):+.1f} kg，結算後{'仍超約' if d.outcome_exceeded else '未超約'}")
    print(c.to_string(index=False))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--decision", type=int)
    a = p.parse_args()
    conn = connect()

    if a.decision:
        show_decision(conn, a.decision)
        return

    iv = q(conn, "SELECT * FROM interval_settlement ORDER BY interval_id")
    dl = q(conn, "SELECT * FROM decision_log ORDER BY decision_id")
    cb = q(conn, "SELECT * FROM carbon_budget_daily ORDER BY day")

    print("== 結算摘要 ==")
    print(f"區間 {len(iv)}，超約 {int(iv.exceeded.sum())}（{iv.exceeded.mean():.1%}），罰款 NTD {iv.penalty_ntd.sum():,.0f}，"
          f"避免罰款 NTD {iv.avoided_penalty_ntd.sum():,.0f}")
    hit = iv[iv.edf_at_10 >= 0.5]
    if len(hit):
        print(f"預警命中率（edf_at_10 ≥ 50% 且 exceeded）：{hit.exceeded.mean():.1%}（{len(hit)} 次預警；引擎介入後的數字，"
              f"真實校準請看 backtest.py）")

    if dl.empty:
        print("\n尚無決策（EDF_10 ≥ 20% 的區間還沒出現）；跑久一點再執行。")
        conn.close()
        return

    print("\n== 決策分級 ==")
    print(dl.groupby(["tier", "approval_mode"]).agg(次數=("decision_id", "size"),
                                                    結算仍超約=("outcome_exceeded", "sum"),
                                                    平均ESV=("esv_ntd", "mean")).to_string())
    print("\n== 選定方案分布 ==")
    print(dl.chosen_plan.value_counts().to_string())

    print("\n== 碳預算 ==")
    print(cb[["day", "kwh_total", "scope2_kg", "budget_remaining_kg", "carbon_edf", "avoided_carbon_kg",
              "avoided_carbon_fee_ntd"]].to_string(index=False))

    # 加分項：降載反而增排
    up = dl[dl.delta_carbon_kg > 0]
    print(f"\n== 「降載反而增排」：選定方案 ΔC > 0 的決策 {len(up)} / {len(dl)} ==")
    if len(up):
        print(up.groupby(["tier", "chosen_plan"]).size().to_string())
        ex = up.iloc[0]
        print("\n範例（第一筆）：")
        show_decision(conn, int(ex.decision_id))
        # 碳價敏感度：碳價要多高，ESV 才會改選最低碳方案
        c = q(conn, "SELECT * FROM decision_candidates WHERE decision_id = %s AND is_locked = FALSE", (int(ex.decision_id),))
        chosen = c[c.is_chosen].iloc[0]
        green = c.loc[c.delta_carbon_kg.idxmin()]
        p_eff = ex.candidate_json["p_eff_ntd_per_kg"]
        gap_ntd = float(chosen.esv_ntd - green.esv_ntd)
        dC = float(chosen.delta_carbon_kg - green.delta_carbon_kg)
        if dC > 0:
            need = (gap_ntd / dC + p_eff) * 1000
            print(f"\n敏感度：選定方案比最低碳方案「{green.plan_name}」多排 {dC:.1f} kg、ESV 高 NTD {gap_ntd:,.0f}；"
                  f"有效碳價需 ≥ NTD {need:,.0f}/t（目前 {p_eff*1000:,.0f}/t）才會改選低碳方案。")

    # 完整決策範例：挑一個 tier2 無鎖定的案例
    t2 = dl[(dl.tier == "tier2") & (dl.locked_loads.apply(len) == 0)]
    if len(t2):
        print("\n== 書面說明用完整決策範例（tier2、無鎖定）==")
        show_decision(conn, int(t2.iloc[0].decision_id))
    conn.close()


if __name__ == "__main__":
    main()
