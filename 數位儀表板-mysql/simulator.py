"""
負載與環境模擬（6.1、6.2）。
總負載 = 基礎負載 × 日係數 + 週期性製程負載 + 隨機跳動 + 偶發尖峰 − 卸載中負載。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

import config as C


def _hour_float(ts: datetime) -> float:
    t = ts.astimezone(C.TZ)
    return t.hour + t.minute / 60 + t.second / 3600


def day_factor(ts: datetime) -> float:
    """白天 1.0、夜間 0.85，前後各 1 小時線性過渡，避免階躍把 σ 撐大。"""
    h = _hour_float(ts)
    lo, hi = C.NIGHT_FACTOR, 1.0
    if C.DAY_START_HOUR <= h < C.DAY_START_HOUR + 1:
        return lo + (hi - lo) * (h - C.DAY_START_HOUR)
    if C.DAY_END_HOUR - 1 <= h < C.DAY_END_HOUR:
        return hi - (hi - lo) * (h - (C.DAY_END_HOUR - 1))
    return hi if C.DAY_START_HOUR + 1 <= h < C.DAY_END_HOUR - 1 else lo


def grid_ef(ts: datetime, rng: np.random.Generator | None = None) -> float:
    """固定 24 小時曲線線性插值 + 小幅雜訊。"""
    h = _hour_float(ts)
    xs = sorted(C.GRID_EF_POINTS)
    ys = [C.GRID_EF_POINTS[x] for x in xs]
    v = float(np.interp(h, xs, ys))
    if rng is not None:
        v += float(rng.normal(0, C.GRID_EF_NOISE))
    return round(max(v, 0.30), 4)


def renewable_pct(ts: datetime) -> float:
    """光電鐘形曲線：06:00 起升、12:00 達峰、18:00 歸零。"""
    h = _hour_float(ts)
    if h <= 6 or h >= 18:
        return 0.0
    return round(C.RENEWABLE_PEAK_PCT * math.sin(math.pi * (h - 6) / 12) ** 2, 3)


def ambient_temp(ts: datetime) -> float:
    h = _hour_float(ts)
    return C.AMBIENT_TEMP_BASE_C + C.AMBIENT_TEMP_AMP_C * math.sin(2 * math.pi * (h - 9) / 24)


def wbgt(ts: datetime, rng: np.random.Generator | None = None) -> float:
    """簡化 WBGT（澳洲氣象局近似式）：0.567T + 0.393e + 3.94，e 為水汽壓 hPa。"""
    T = ambient_temp(ts)
    if rng is not None:
        T += float(rng.normal(0, 0.3))
    es = 6.105 * math.exp(17.27 * T / (237.7 + T))
    e = C.AMBIENT_RH_PCT / 100 * es
    return round(0.567 * T + 0.393 * e + 3.94, 2)


@dataclass
class Spike:
    end: datetime
    amp_kw: float


@dataclass
class Shed:
    load_key: str
    shed_kw: float
    end: datetime           # 卸載結束（恢復開始）
    recovery_end: datetime  # 恢復完成


@dataclass
class Simulator:
    start: datetime
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(C.RANDOM_SEED))
    spikes: list[Spike] = field(default_factory=list)
    sheds: list[Shed] = field(default_factory=list)
    process_temp: float = C.PROCESS_TEMP_BASE_C
    sine_phase: float = 0.0

    def __post_init__(self):
        self.sine_phase = float(self.rng.uniform(0, 2 * math.pi))

    # ------------------------------------------------------------ 負載成分
    def _expected_spike_add_kw(self) -> float:
        rate = C.SPIKE_RATE_PER_HOUR
        dur = sum(C.SPIKE_DURATION_MIN) / 2 / 60
        amp = sum(C.SPIKE_AMP_FRAC) / 2 * C.CONTRACT_KW
        return rate * dur * amp

    def expected_load_kw(self, ts: datetime) -> float:
        """引擎不介入時的期望負載（正弦與雜訊期望為 0）；用於碳預算基準。"""
        return C.BASE_LOAD_FRAC * C.CONTRACT_KW * day_factor(ts) + self._expected_spike_add_kw()

    def _spawn_spikes(self, ts: datetime):
        # 每分鐘以 Poisson 判斷是否產生新尖峰
        n = self.rng.poisson(C.SPIKE_RATE_PER_HOUR / 60)
        for _ in range(n):
            dur = int(self.rng.integers(C.SPIKE_DURATION_MIN[0], C.SPIKE_DURATION_MIN[1] + 1))
            amp = float(self.rng.uniform(*C.SPIKE_AMP_FRAC)) * C.CONTRACT_KW
            self.spikes.append(Spike(end=ts + timedelta(minutes=dur), amp_kw=amp))
        self.spikes = [s for s in self.spikes if s.end > ts]

    def active_shed_kw(self, ts: datetime) -> float:
        return sum(s.shed_kw for s in self.sheds if s.end > ts)

    def is_motor_shedding_or_recovering(self, ts: datetime) -> tuple[bool, bool]:
        shedding = any(s.load_key == "motor" and s.end > ts for s in self.sheds)
        recovering = any(s.load_key == "motor" and s.end <= ts < s.recovery_end for s in self.sheds)
        return shedding, recovering

    def step(self, ts: datetime) -> dict:
        """產生一分鐘的負載與環境讀數。"""
        self._spawn_spikes(ts)
        self.sheds = [s for s in self.sheds if s.recovery_end > ts]

        minutes = (ts - self.start).total_seconds() / 60
        base = C.BASE_LOAD_FRAC * C.CONTRACT_KW * day_factor(ts)
        sine = C.SINE_AMP_FRAC * C.CONTRACT_KW * math.sin(
            2 * math.pi * minutes / (C.SINE_PERIOD_HOURS * 60) + self.sine_phase)
        noise = float(self.rng.normal(0, C.NOISE_SIGMA_FRAC * C.CONTRACT_KW))
        spike = sum(s.amp_kw for s in self.spikes)
        shed = self.active_shed_kw(ts)
        load = max(base + sine + noise + spike - shed, 0.0)

        # 製程溫度：慢速漂移 + 雜訊；馬達卸載中下降、恢復中回升
        drift = C.PROCESS_TEMP_DRIFT_AMP_C * math.sin(2 * math.pi * minutes / (6 * 60) + 1.0)
        target = C.PROCESS_TEMP_BASE_C + drift
        shedding, recovering = self.is_motor_shedding_or_recovering(ts)
        if shedding:
            self.process_temp -= C.PROCESS_TEMP_SHED_DROP_PER_MIN
        elif recovering:
            self.process_temp = min(target, self.process_temp + C.PROCESS_TEMP_RECOVER_PER_MIN)
        else:
            # 向目標靠近，模擬正常控溫
            self.process_temp += 0.3 * (target - self.process_temp)
        pt = self.process_temp + float(self.rng.normal(0, C.PROCESS_TEMP_NOISE_C))

        return {
            "load_kw": round(load, 1),
            "grid_ef": grid_ef(ts, self.rng),
            "renewable_pct": renewable_pct(ts),
            "wbgt": wbgt(ts, self.rng),
            "process_temp": round(pt, 2),
            "spike_kw": round(spike, 1),
            "shed_kw": round(shed, 1),
        }

    def apply_shed(self, ts: datetime, loads: list[C.Load], end: datetime):
        """決策引擎選定方案後呼叫：後續分鐘扣除 shed_kw，恢復時間後加回。"""
        for ld in loads:
            self.sheds.append(Shed(ld.key, ld.shed_kw, end, end + timedelta(minutes=ld.recovery_min)))


def locked_loads(wbgt_c: float, process_temp_c: float) -> tuple[list[C.Load], str]:
    """安全鎖定（硬約束）：回傳被鎖定的負載與摘要文字。"""
    locked, parts = [], []
    for ld in C.LOADS:
        if ld.lock_rule == "wbgt" and wbgt_c >= C.WBGT_LOCK_C:
            locked.append(ld)
            parts.append(f"{ld.name}鎖定（WBGT {wbgt_c:.1f}°C）")
        elif ld.lock_rule == "process_temp" and process_temp_c < C.PROCESS_TEMP_LOWER_LIMIT_C:
            locked.append(ld)
            parts.append(f"{ld.name}鎖定（製程溫度 {process_temp_c:.1f}°C）")
    return locked, "；".join(parts)
