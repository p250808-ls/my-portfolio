"""
每 60 秒一輪：產生負載 → 更新 KMV → 必要時執行決策引擎 → 寫入資料庫。
Engine 不管時間怎麼流（真實 / 加速），只處理「給我一個 ts，我做這一分鐘的事」。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

import config as C
import kmv
from decision import Decision, decide
from simulator import Simulator, grid_ef, locked_loads


def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


@dataclass
class IntervalState:
    interval_id: int
    start: datetime
    sum_kw: float = 0.0
    count: int = 0
    edf_at_5: float | None = None
    edf_at_10: float | None = None
    decision: Decision | None = None
    decision_id: int | None = None


@dataclass
class Engine:
    writer: object
    epoch: datetime                       # 7 天週期起點（整分）
    engine_on: bool = True
    log: object = print
    sim: Simulator = field(init=False)
    rng: np.random.Generator = field(init=False)
    recent_loads: deque = field(init=False)
    cur: IntervalState | None = None
    # 碳預算
    baseline_week_kg: float = 0.0
    budget_kg: float = 0.0
    week_scope2_kg: float = 0.0
    day_kwh: float = 0.0
    day_scope2_kg: float = 0.0
    day_avoided_kg: float = 0.0
    day_esv: dict = field(default_factory=lambda: {"penalty": 0.0, "carbon": 0.0, "oc": 0.0, "safety": 0.0})
    cur_day: date | None = None
    daily_history_kg: list[float] = field(default_factory=list)
    latest_carbon_edf: float = 0.0
    # 統計
    n_ticks: int = 0

    def __post_init__(self):
        self.rng = np.random.default_rng(C.RANDOM_SEED + 7)
        self.sim = Simulator(start=self.epoch, rng=np.random.default_rng(C.RANDOM_SEED))
        self.recent_loads = deque(maxlen=C.SIGMA_WINDOW_MIN)
        self.baseline_week_kg = self.compute_baseline_week_kg()
        self.budget_kg = self.baseline_week_kg * C.CARBON_TARGET_RATIO

    # ------------------------------------------------------------ 碳預算基準（開機時解析計算一次）
    def compute_baseline_week_kg(self) -> float:
        total = 0.0
        ts = self.epoch
        end = self.epoch + timedelta(days=C.RESET_DAYS)
        while ts < end:
            kw = self.sim.expected_load_kw(ts)
            total += kw / 60 * grid_ef(ts)
            ts += timedelta(minutes=1)
        return total

    # ------------------------------------------------------------ 7 天重置
    def reset(self, ts: datetime):
        self.log(f"[reset] {ts:%Y-%m-%d %H:%M} 已滿 {C.RESET_DAYS} 天，清空五張表並重建")
        self.writer.init_schema()
        self.epoch = ts
        self.__post_init__()
        self.cur = None
        self.week_scope2_kg = self.day_kwh = self.day_scope2_kg = self.day_avoided_kg = 0.0
        self.day_esv = {k: 0.0 for k in self.day_esv}
        self.cur_day = None
        self.daily_history_kg = []
        self.latest_carbon_edf = 0.0

    # ------------------------------------------------------------ 主流程
    def tick(self, ts: datetime):
        ts = floor_minute(ts)
        minutes = int((ts - self.epoch).total_seconds() // 60)
        if minutes >= C.RESET_DAYS * 1440:
            self.reset(ts)
            minutes = 0
        interval_id = minutes // C.INTERVAL_MINUTES + 1
        mii = minutes % C.INTERVAL_MINUTES
        t = mii + 1

        local_day = ts.astimezone(C.TZ).date()
        if self.cur_day is None:
            self.cur_day = local_day
        elif local_day != self.cur_day:
            self._close_day(ts)
            self.cur_day = local_day

        if self.cur is None or self.cur.interval_id != interval_id:
            self.cur = IntervalState(interval_id, self.epoch + timedelta(minutes=(interval_id - 1) * C.INTERVAL_MINUTES))

        # 1. 模擬
        r = self.sim.step(ts)
        load = r["load_kw"]
        self.recent_loads.append(load)
        sig = kmv.sigma_kw(list(self.recent_loads))

        # 2. KMV
        st = self.cur
        if st.count == 0 and mii > 0:
            # 重啟時落在區間中段：缺少的前幾分鐘以當前負載補齊，避免平均需量失真
            st.sum_kw, st.count = load * mii, mii
        st.sum_kw += load
        st.count += 1
        res = kmv.short_term_edf(st.sum_kw, t, load, sig)
        locked, locked_text = locked_loads(r["wbgt"], r["process_temp"])

        self.writer.write_reading({
            "ts": ts, "interval_id": interval_id, "minute_in_interval": mii,
            "remaining_minutes": C.INTERVAL_MINUTES - mii,
            "load_kw": load, "avg_kw_so_far": round(res.avg_kw_so_far, 1),
            "contract_kw": C.CONTRACT_KW, "buffer_kw": round(res.buffer_kw, 1),
            "sigma_kw": round(sig, 2), "sigma_avg_kw": round(res.sigma_avg_kw, 2),
            "edf": round(res.edf, 6), "grid_ef": r["grid_ef"], "renewable_pct": r["renewable_pct"],
            "wbgt": r["wbgt"], "process_temp": r["process_temp"], "locked_status_text": locked_text,
        })
        self.n_ticks += 1

        # 3. 碳累計
        self.day_kwh += load / 60
        kg = load / 60 * r["grid_ef"]
        self.day_scope2_kg += kg
        self.week_scope2_kg += kg

        # 4. 評估時點
        if t == C.EVAL_MINUTES[0]:
            st.edf_at_5 = res.edf
        elif t == C.EVAL_MINUTES[1]:
            st.edf_at_10 = res.edf
            if self.engine_on and res.edf >= C.TIER_THRESHOLDS[0]:
                self._run_decision(ts, st, res, load, sig, r)

        # 5. 結算
        if res.settled:
            self._settle(ts, st, res)

    # ------------------------------------------------------------ 決策引擎
    def _run_decision(self, ts, st: IntervalState, res, load, sig, r):
        d = decide(edf_before=res.edf, sum_so_far=st.sum_kw, t=res.t, load_now=load, sig_kw=sig,
                   buffer_kw=res.buffer_kw, s_avg=res.sigma_avg_kw, grid_ef=r["grid_ef"],
                   wbgt_c=r["wbgt"], process_temp_c=r["process_temp"],
                   carbon_edf_value=self.latest_carbon_edf, rng=self.rng)
        st.decision = d
        cand_rows = [{
            "plan_name": c.plan_name, "shed_kw": c.shed_kw, "edf_after": round(c.edf_after, 6),
            "penalty_avoided_ntd": round(c.penalty_avoided_ntd), "cost_ntd": round(c.cost_ntd),
            "delta_carbon_kg": round(c.delta_carbon_kg, 2), "esv_ntd": round(c.esv_ntd),
            "is_locked": c.is_locked, "is_chosen": c.is_chosen,
        } for c in d.candidates]
        log_row = {
            "ts": ts, "interval_id": st.interval_id, "edf_trigger": round(d.edf_trigger, 6), "tier": d.tier,
            "candidate_json": {
                "c_penalty_ntd": round(d.c_penalty_ntd), "p_eff_ntd_per_kg": round(d.p_eff_ntd_per_kg, 4),
                "carbon_edf": round(self.latest_carbon_edf, 4), "projected_avg_kw": round(res.projected_avg_kw, 1),
                "buffer_kw": round(res.buffer_kw, 1), "sigma_avg_kw": round(res.sigma_avg_kw, 2),
                "wbgt": r["wbgt"], "process_temp": r["process_temp"],
                "candidates": [{**c, "oc_production_ntd": round(x.oc_production_ntd),
                                "carbon_cost_ntd": round(x.carbon_cost_ntd, 1),
                                "r_safety_ntd": round(x.r_safety_ntd), "allowed_by_tier": x.allowed_by_tier}
                               for c, x in zip(cand_rows, d.candidates)],
            },
            "locked_loads": [ld.name for ld in d.locked], "chosen_plan": d.chosen_plan,
            "esv_ntd": round(d.esv_ntd), "shed_kw": d.shed_kw, "delta_carbon_kg": round(d.delta_carbon_kg, 2),
            "approval_mode": d.approval_mode, "approval_delay_s": d.approval_delay_s, "outcome_exceeded": None,
            "penalty_avoided_ntd": round(d.chosen.penalty_avoided_ntd) if d.chosen else 0,
            "oc_production_ntd": round(d.chosen.oc_production_ntd) if d.chosen else 0,
            "carbon_cost_ntd": round(d.chosen.carbon_cost_ntd, 1) if d.chosen else 0.0,
            "r_safety_ntd": round(d.chosen.r_safety_ntd) if d.chosen else 0,
        }
        st.decision_id = self.writer.write_decision(log_row, cand_rows)
        if d.chosen is not None:
            end = st.start + timedelta(minutes=C.INTERVAL_MINUTES)
            self.sim.apply_shed(ts, d.chosen_loads, end)
            if d.delta_carbon_kg < 0:
                self.day_avoided_kg += -d.delta_carbon_kg
            self.day_esv["penalty"] += d.chosen.penalty_avoided_ntd
            self.day_esv["carbon"] += -d.chosen.carbon_cost_ntd      # 減碳為正、增排為負
            self.day_esv["oc"] -= d.chosen.oc_production_ntd
            self.day_esv["safety"] -= d.chosen.r_safety_ntd
        self.log(f"[decision] {ts:%m-%d %H:%M} iv={st.interval_id} EDF10={d.edf_trigger:.2f} {d.tier} "
                 f"→ {d.chosen_plan} ESV={d.esv_ntd:,.0f} ΔC={d.delta_carbon_kg:+.1f}kg"
                 + (f" 鎖定:{d.locked_text}" if d.locked else ""))

    # ------------------------------------------------------------ 結算
    def _settle(self, ts, st: IntervalState, res):
        avg = res.avg_kw_so_far
        exceeded = avg > C.CONTRACT_KW
        pen = kmv.penalty_ntd(avg)
        action, avoided = None, 0.0
        if st.decision and st.decision.chosen:
            action = st.decision.chosen_plan
            shed_min = C.INTERVAL_MINUTES - C.EVAL_MINUTES[1]
            avg_without = avg + st.decision.shed_kw * shed_min / C.INTERVAL_MINUTES
            avoided = kmv.penalty_ntd(avg_without) - pen
        elif st.decision:
            action = st.decision.chosen_plan   # 接受罰款（不動作）
        self.writer.write_settlement({
            "interval_id": st.interval_id, "interval_start": st.start, "avg_kw": round(avg, 1),
            "contract_kw": C.CONTRACT_KW, "exceeded": exceeded, "penalty_ntd": round(pen),
            "edf_at_5": None if st.edf_at_5 is None else round(st.edf_at_5, 6),
            "edf_at_10": None if st.edf_at_10 is None else round(st.edf_at_10, 6),
            "action_taken": action, "avoided_penalty_ntd": round(avoided),
        })
        if st.decision_id is not None:
            self.writer.backfill_outcome(st.interval_id, exceeded)
        self._upsert_carbon_day(ts)

    # ------------------------------------------------------------ 碳預算
    def _days_remaining(self, ts) -> float:
        return max((self.epoch + timedelta(days=C.RESET_DAYS) - ts).total_seconds() / 86400, 0.0)

    def _carbon_stats(self, ts):
        days_rem = self._days_remaining(ts)
        elapsed = C.RESET_DAYS - days_rem
        daily_base = self.baseline_week_kg / C.RESET_DAYS
        sigma = float(np.std(self.daily_history_kg, ddof=1)) if len(self.daily_history_kg) >= 3 \
            else C.CARBON_SIGMA_DAILY_FRAC * daily_base
        expected_daily = self.week_scope2_kg / elapsed if elapsed >= 0.25 else daily_base
        remaining = self.budget_kg - self.week_scope2_kg
        dd, edf = kmv.carbon_edf(remaining, sigma, days_rem, expected_daily)
        return remaining, sigma, days_rem, edf

    def _upsert_carbon_day(self, ts):
        remaining, sigma, days_rem, edf = self._carbon_stats(ts)
        self.latest_carbon_edf = edf
        self.writer.upsert_carbon_day({
            "day": self.cur_day, "kwh_total": round(self.day_kwh, 1), "scope2_kg": round(self.day_scope2_kg, 1),
            "budget_remaining_kg": round(remaining, 1), "sigma_daily_kg": round(sigma, 1),
            "days_to_month_end": math.ceil(days_rem), "carbon_edf": round(edf, 6),
            "avoided_carbon_kg": round(self.day_avoided_kg, 2),
            "avoided_carbon_fee_ntd": round(self.day_avoided_kg / 1000 * C.CARBON_FEE_NTD_PER_TON),
        })
        self.writer.upsert_waterfall([
            {"day": self.cur_day, "sort_order": 1, "component": "規避罰款（期望值）", "ntd": round(self.day_esv["penalty"])},
            {"day": self.cur_day, "sort_order": 2, "component": "碳費（減碳為正／增排為負）", "ntd": round(self.day_esv["carbon"])},
            {"day": self.cur_day, "sort_order": 3, "component": "產能機會成本", "ntd": round(self.day_esv["oc"])},
            {"day": self.cur_day, "sort_order": 4, "component": "安全風險成本", "ntd": round(self.day_esv["safety"])},
        ])

    def _close_day(self, ts):
        """每日 00:00：定稿前一日、推入歷史、歸零日累計。"""
        self._upsert_carbon_day(ts - timedelta(minutes=1))
        self.daily_history_kg.append(self.day_scope2_kg)
        self.log(f"[day] {self.cur_day} kWh={self.day_kwh:,.0f} scope2={self.day_scope2_kg:,.0f}kg "
                 f"avoided={self.day_avoided_kg:.1f}kg carbon_edf={self.latest_carbon_edf:.3f}")
        self.day_kwh = self.day_scope2_kg = self.day_avoided_kg = 0.0
        self.day_esv = {k: 0.0 for k in self.day_esv}
