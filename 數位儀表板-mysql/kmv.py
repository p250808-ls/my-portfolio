"""
KMV 違約距離與違約機率（第 3 節、6.3、6.5）。
短週期：buffer_kw / σ_avg(t)，σ_avg = σ_kw × √(15 − t) / 15。
長週期：碳預算 DD（含預期消耗漂移，見 carbon_edf 說明）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

import config as C


def sigma_kw(recent_loads: list[float]) -> float:
    """近 N 分鐘單分鐘負載標準差（樣本數不足時以 config 的雜訊 σ 代替）。"""
    if len(recent_loads) < 5:
        return C.NOISE_SIGMA_FRAC * C.CONTRACT_KW
    return max(float(np.std(recent_loads, ddof=1)), C.SIGMA_FLOOR_KW)


def sigma_avg_kw(sig_kw: float, t: int, n: int = C.INTERVAL_MINUTES) -> float:
    """平均需量的標準差：剩餘 n − t 分鐘獨立波動，分攤回 n 分鐘平均。"""
    return sig_kw * math.sqrt(max(n - t, 0)) / n


def projected_avg_kw(sum_so_far: float, t: int, load_now: float, n: int = C.INTERVAL_MINUTES) -> float:
    """外推到結算：假設剩餘 n − t 分鐘維持當前負載。"""
    return (sum_so_far + load_now * (n - t)) / n


@dataclass
class ShortTermResult:
    t: int
    avg_kw_so_far: float
    projected_avg_kw: float
    buffer_kw: float
    sigma_kw: float
    sigma_avg_kw: float
    dd: float
    edf: float
    settled: bool


def short_term_edf(sum_so_far: float, t: int, load_now: float, sig_kw: float,
                   contract_kw: float = C.CONTRACT_KW) -> ShortTermResult:
    """
    t = minute_in_interval + 1（1～15）。t = 15 為結算時刻：不算 DD，
    avg_kw > contract_kw 則 edf = 1.0，否則 0.0，避免除零。
    """
    n = C.INTERVAL_MINUTES
    avg_so_far = sum_so_far / t
    if t >= n:
        exceeded = avg_so_far > contract_kw
        return ShortTermResult(t, avg_so_far, avg_so_far, contract_kw - avg_so_far, sig_kw, 0.0,
                               math.inf if not exceeded else -math.inf, 1.0 if exceeded else 0.0, True)
    proj = projected_avg_kw(sum_so_far, t, load_now)
    buffer = contract_kw - proj
    s_avg = sigma_avg_kw(sig_kw, t)
    dd = buffer / s_avg
    edf = float(norm.cdf(-dd))
    return ShortTermResult(t, avg_so_far, proj, buffer, sig_kw, s_avg, dd, edf, False)


def edf_after_shed(sum_so_far: float, t: int, load_now: float, sig_kw: float, shed_kw: float,
                   contract_kw: float = C.CONTRACT_KW) -> float:
    """方案 i 執行後重算的 EDF：剩餘分鐘的負載扣除 shed_kw。"""
    return short_term_edf(sum_so_far, t, max(load_now - shed_kw, 0.0), sig_kw, contract_kw).edf


def expected_excess_given_exceed(buffer_kw: float, s_avg: float) -> float:
    """
    超約條件下的期望超約量 E[X | X > 0]，X ~ N(−buffer, σ_avg)。
    用於估算 C_penalty：若真的超約，平均會超多少 kW。
    """
    if s_avg <= 0:
        return max(-buffer_kw, 0.0)
    mu = -buffer_kw
    z = mu / s_avg
    tail = norm.cdf(z)
    if tail < 1e-9:
        return max(mu, 0.0) + s_avg * 0.01
    return mu + s_avg * norm.pdf(z) / tail


def penalty_ntd(avg_kw: float, contract_kw: float = C.CONTRACT_KW) -> float:
    """超約附加費 = 超約部分 × 契約容量費率 × 倍數。"""
    return max(avg_kw - contract_kw, 0.0) * C.CONTRACT_RATE_NTD_PER_KW * C.PENALTY_MULTIPLIER


def expected_penalty_ntd(buffer_kw: float, s_avg: float) -> float:
    """C_penalty：若超約，預估罰款 = E[超約 kW | 超約] × 費率 × 倍數。"""
    return expected_excess_given_exceed(buffer_kw, s_avg) * C.CONTRACT_RATE_NTD_PER_KW * C.PENALTY_MULTIPLIER


def expected_penalty_unconditional_ntd(buffer_kw: float, s_avg: float) -> float:
    """
    罰款期望值 E[penalty] = EDF × C_penalty = 費率 × 倍數 × E[max(X, 0)]，X ~ N(−buffer, σ_avg)。
    E[max(X,0)] = μ Φ(μ/σ) + σ φ(μ/σ)。
    方案 i 的「避免罰款」= E[penalty_before] − E[penalty_after,i]；
    當 C_penalty 視為常數時即退化為規格的 (EDF_before − EDF_after,i) × C_penalty，
    但在 EDF 已接近 1 的 tier4 情境，仍能反映「少超 400 kW 就少罰 400 × 472」的真實效益。
    """
    rate = C.CONTRACT_RATE_NTD_PER_KW * C.PENALTY_MULTIPLIER
    mu = -buffer_kw
    if s_avg <= 0:
        return max(mu, 0.0) * rate
    z = mu / s_avg
    return (mu * norm.cdf(z) + s_avg * norm.pdf(z)) * rate


# ---------------------------------------------------------------- 長週期：碳預算
def carbon_edf(budget_remaining_kg: float, sigma_daily_kg: float, days_remaining: float,
               expected_daily_kg: float) -> tuple[float, float]:
    """
    月（7 天迷你月）碳預算違約機率。
    規格第 3 節：DD = budget_remaining ÷ (σ_daily × √剩餘天數)。
    實作加入 KMV 原式中的漂移項：剩餘天數內「照目前方式」還會排放 μ_daily × 剩餘天數，
    所以分子改為 budget_remaining − μ_daily × 剩餘天數。
    不加漂移時 DD 會在最後幾小時前都接近 +∞，EDF 恆為 0，失去預警功能。
    回傳 (DD, EDF)。
    """
    if days_remaining <= 1e-6:
        return (math.inf, 0.0) if budget_remaining_kg >= 0 else (-math.inf, 1.0)
    sigma = max(sigma_daily_kg, 1.0)
    dd = (budget_remaining_kg - expected_daily_kg * days_remaining) / (sigma * math.sqrt(days_remaining))
    return dd, float(norm.cdf(-dd))


def effective_carbon_price_ntd_per_kg(carbon_edf_value: float) -> float:
    """P_eff = P_carbon × (1 + α × carbon_edf)，單位 NTD/kg。"""
    return C.CARBON_PRICE_NTD_PER_TON / 1000 * (1 + C.CARBON_ALPHA * carbon_edf_value)
