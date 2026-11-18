"""
ESG 決策引擎（第 4 節、6.4）。
ESV_i = (EDF_before − EDF_after,i) × C_penalty − OC_production,i − ΔE_carbon,i × P_eff − R_safety,i
符號慣例：所有成本為正值扣除；ΔE_carbon 為有號數（減碳為負）。
安全鎖定是過濾不是權重：被鎖定的負載組合仍寫入候選表（is_locked = true），但不參與選擇。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

import config as C
import kmv
from simulator import locked_loads

ACCEPT_PENALTY = "接受罰款（不動作）"


@dataclass
class Candidate:
    plan_name: str
    loads: list[C.Load]
    shed_kw: float
    edf_after: float
    penalty_avoided_ntd: float
    oc_production_ntd: float
    delta_carbon_kg: float
    carbon_cost_ntd: float
    r_safety_ntd: float
    cost_ntd: float
    esv_ntd: float
    is_locked: bool
    is_chosen: bool = False
    allowed_by_tier: bool = True


@dataclass
class Decision:
    edf_trigger: float
    tier: str
    c_penalty_ntd: float
    p_eff_ntd_per_kg: float
    locked: list[C.Load]
    locked_text: str
    candidates: list[Candidate]
    chosen: Candidate | None            # None = 接受罰款
    approval_mode: str
    approval_delay_s: int

    @property
    def chosen_plan(self) -> str:
        return self.chosen.plan_name if self.chosen else ACCEPT_PENALTY

    @property
    def shed_kw(self) -> float:
        return self.chosen.shed_kw if self.chosen else 0.0

    @property
    def esv_ntd(self) -> float:
        return self.chosen.esv_ntd if self.chosen else 0.0

    @property
    def delta_carbon_kg(self) -> float:
        return self.chosen.delta_carbon_kg if self.chosen else 0.0

    @property
    def chosen_loads(self) -> list[C.Load]:
        return self.chosen.loads if self.chosen else []


def tier_of(edf: float) -> str:
    t1, t2, t3 = C.TIER_THRESHOLDS
    if edf < t1:
        return "tier1"
    if edf < t2:
        return "tier2"
    if edf < t3:
        return "tier3"
    return "tier4"


def delta_carbon_kg(loads: list[C.Load], shed_minutes: float, grid_ef: float) -> tuple[float, float, float]:
    """ΔE = −E_avoided + E_reheat + E_scrap（kgCO2e）。回傳 (ΔE, E_avoided, E_reheat+E_scrap)。"""
    e_avoided = sum(ld.shed_kw for ld in loads) * shed_minutes / 60 * grid_ef
    e_recovery = sum(ld.e_reheat_kg + ld.scrap_prob * ld.e_scrap_kg for ld in loads)
    return -e_avoided + e_recovery, e_avoided, e_recovery


def r_safety_ntd(loads: list[C.Load], wbgt_c: float, process_temp_c: float) -> float:
    """軟性安全成本：越接近鎖定門檻越高（鎖定本身已在過濾階段處理）。"""
    total = 0.0
    for ld in loads:
        base = ld.r_safety_ntd
        if ld.lock_rule == "wbgt":
            closeness = max(0.0, 1 - (C.WBGT_LOCK_C - wbgt_c) / 3)       # 25°C 以上開始上升
            total += base * (1 + 2 * closeness)
        elif ld.lock_rule == "process_temp":
            closeness = max(0.0, 1 - (process_temp_c - C.PROCESS_TEMP_LOWER_LIMIT_C) / 3)
            total += base * (1 + 2 * closeness)
        else:
            total += base
    return total


def decide(*, edf_before: float, sum_so_far: float, t: int, load_now: float, sig_kw: float,
           buffer_kw: float, s_avg: float, grid_ef: float, wbgt_c: float, process_temp_c: float,
           carbon_edf_value: float, rng: np.random.Generator) -> Decision:
    """第 10 分鐘 EDF ≥ 20% 時呼叫。"""
    tier = tier_of(edf_before)
    locked, locked_text = locked_loads(wbgt_c, process_temp_c)
    locked_keys = {ld.key for ld in locked}
    c_penalty = kmv.expected_penalty_ntd(buffer_kw, s_avg)
    p_eff = kmv.effective_carbon_price_ntd_per_kg(carbon_edf_value)
    shed_minutes = C.INTERVAL_MINUTES - t
    pen_before = kmv.expected_penalty_unconditional_ntd(buffer_kw, s_avg)

    candidates: list[Candidate] = []
    for r in range(1, len(C.LOADS) + 1):
        for combo in combinations(C.LOADS, r):
            loads = list(combo)
            shed = sum(ld.shed_kw for ld in loads)
            edf_after = kmv.edf_after_shed(sum_so_far, t, load_now, sig_kw, shed)
            buffer_after = buffer_kw + shed * shed_minutes / C.INTERVAL_MINUTES
            avoided = pen_before - kmv.expected_penalty_unconditional_ntd(buffer_after, s_avg)
            oc = sum(ld.oc_production_ntd for ld in loads)
            d_carbon, _, _ = delta_carbon_kg(loads, shed_minutes, grid_ef)
            carbon_cost = d_carbon * p_eff
            rs = r_safety_ntd(loads, wbgt_c, process_temp_c)
            cost = oc + carbon_cost + rs
            is_locked = any(ld.key in locked_keys for ld in loads)
            allowed = True
            if tier == "tier2" and any(not ld.allowed_in_tier2 for ld in loads):
                allowed = False
            candidates.append(Candidate(
                plan_name=" + ".join(ld.name for ld in loads), loads=loads, shed_kw=shed,
                edf_after=edf_after, penalty_avoided_ntd=avoided, oc_production_ntd=oc,
                delta_carbon_kg=d_carbon, carbon_cost_ntd=carbon_cost, r_safety_ntd=rs,
                cost_ntd=cost, esv_ntd=avoided - cost, is_locked=is_locked, allowed_by_tier=allowed))

    eligible = [c for c in candidates if not c.is_locked and c.allowed_by_tier]
    chosen: Candidate | None = None
    approval_mode, delay = "auto", 0

    if tier == "tier1":
        chosen = None
    elif tier == "tier4":
        # 執行所有未鎖定負載卸載（安全優先於 ESV）
        unlocked = [ld for ld in C.LOADS if ld.key not in locked_keys]
        for c in candidates:
            if [ld.key for ld in c.loads] == [ld.key for ld in unlocked]:
                chosen = c
    else:
        if eligible:
            best = max(eligible, key=lambda c: c.esv_ntd)
            chosen = best if best.esv_ntd > 0 else None   # ESV ≤ 0 → 接受罰款更划算
        if tier == "tier3":
            approval_mode = "manual"
            delay = int(rng.integers(C.MANUAL_DELAY_RANGE_S[0], C.MANUAL_DELAY_RANGE_S[1] + 1))

    if chosen is not None:
        chosen.is_chosen = True

    return Decision(edf_before, tier, c_penalty, p_eff, locked, locked_text, candidates,
                    chosen, approval_mode, delay)
