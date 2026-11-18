"""
集中所有模擬與模型參數。課堂展示時只需改這裡。
部分參數可由 .env 覆寫（見 _env()）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default, cast=float):
    v = os.getenv(name)
    return cast(v) if v not in (None, "") else default


TZ = ZoneInfo("Asia/Taipei")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---------------------------------------------------------------- 時間與重置
TICK_SECONDS = 60            # 每 60 秒一筆
INTERVAL_MINUTES = 15        # 台電結算週期
RESET_DAYS = 7               # 每 7 天清空五張表並重建
EVAL_MINUTES = (5, 10)       # 第 5、第 10 分鐘評估 EDF（t = minute_in_interval + 1）
RANDOM_SEED = 20261014

# ---------------------------------------------------------------- 契約與費率
CONTRACT_KW = _env("CONTRACT_KW", 10_000.0)
CONTRACT_RATE_NTD_PER_KW = 236.0   # 契約容量費率（簡化固定值）
PENALTY_MULTIPLIER = 2.0           # 超約附加費 = 超約 kW × 費率 × 2

# ---------------------------------------------------------------- 負載產生（6.1）
BASE_LOAD_FRAC = 0.78              # 基礎負載 = 契約容量 78%
SINE_PERIOD_HOURS = 3.0            # 週期性製程負載週期 2～4 小時
SINE_AMP_FRAC = 0.10               # 振幅 10%
NOISE_SIGMA_FRAC = 0.04            # 隨機跳動 σ：契約容量 3%～5%，取 4%
SPIKE_RATE_PER_HOUR = 2.0          # 偶發尖峰每小時 1～3 次
SPIKE_DURATION_MIN = (2, 6)        # 持續 2～6 分鐘
# 尖峰幅度：校準規則只動這兩個數（每次 ±5%），基礎負載與 σ 不動
SPIKE_AMP_FRAC = (_env("SPIKE_AMP_LOW", 0.45), _env("SPIKE_AMP_HIGH", 0.55))  # 校準後值；規格初值 0.20～0.30 只得 0.3% 超約
NIGHT_FACTOR = 0.85                # 夜間 22:00～06:00 係數（1 小時平滑過渡）
DAY_START_HOUR, DAY_END_HOUR = 6, 22

# ---------------------------------------------------------------- 環境變數（6.2）
# grid_ef 24 小時曲線（kgCO2e/kWh）：深夜 0.55、正午 0.40、傍晚 0.52；示意值
GRID_EF_POINTS = {0: 0.55, 4: 0.55, 8: 0.48, 12: 0.40, 15: 0.44, 18: 0.52, 21: 0.54, 24: 0.55}
GRID_EF_NOISE = 0.01
RENEWABLE_PEAK_PCT = 35.0          # 12:00 達 35%，06:00 起升、18:00 歸零
AMBIENT_TEMP_BASE_C = 26.0         # 夏季示意：日均 26°C
AMBIENT_TEMP_AMP_C = 3.5           # 日振幅 ±3.5°C，15:00 最高
AMBIENT_RH_PCT = 60.0
WBGT_LOCK_C = 28.0                 # 職安署高氣溫作業指引分級；≥ 28 鎖定冰水主機
PROCESS_TEMP_BASE_C = 86.0
PROCESS_TEMP_DRIFT_AMP_C = 4.0     # 6 小時慢速漂移
PROCESS_TEMP_NOISE_C = 0.3
PROCESS_TEMP_LOWER_LIMIT_C = 82.0  # 低於下限鎖定次級製程馬達
PROCESS_TEMP_SHED_DROP_PER_MIN = 0.4
PROCESS_TEMP_RECOVER_PER_MIN = 0.2

# ---------------------------------------------------------------- KMV（6.3）
SIGMA_WINDOW_MIN = 30              # 近 30 分鐘單分鐘標準差
SIGMA_FLOOR_KW = 50.0              # 防止視窗內負載過平導致除零

# ---------------------------------------------------------------- 決策引擎（第 4 節）
TIER_THRESHOLDS = (0.20, 0.50, 0.80)   # tier1 <20%, tier2 20～50, tier3 50～80, tier4 ≥80
MANUAL_DELAY_RANGE_S = (0, 60)         # tier3 值班主管確認延遲
CARBON_PRICE_NTD_PER_TON = 300.0       # 內部碳價起算值
CARBON_ALPHA = 2.0                     # P_eff = P × (1 + α × carbon_edf)
SHED_UNTIL_INTERVAL_END = True         # 卸載持續到結算（第 10 分鐘觸發 → 卸 5 分鐘）


@dataclass(frozen=True)
class Load:
    key: str
    name: str
    shed_kw: float
    recovery_min: int
    oc_production_ntd: float     # 產能機會成本（每次事件）
    e_reheat_kg: float           # 恢復成本碳排（kgCO2e，每次事件）
    scrap_prob: float            # 不良品機率
    e_scrap_kg: float            # 不良品報廢碳排（kgCO2e）
    r_safety_ntd: float          # 安全風險懲罰（非鎖定狀態下的軟成本）
    lock_rule: str               # none / wbgt / process_temp
    allowed_in_tier2: bool = True


LOADS: list[Load] = [
    Load("pump", "輔助抽水機", 400, 5, oc_production_ntd=1_500, e_reheat_kg=0.0,
         scrap_prob=0.0, e_scrap_kg=0.0, r_safety_ntd=0.0, lock_rule="none"),
    Load("chiller", "空調冰水主機", 600, 15, oc_production_ntd=0.0, e_reheat_kg=8.0,
         scrap_prob=0.0, e_scrap_kg=0.0, r_safety_ntd=300.0, lock_rule="wbgt"),
    Load("motor", "次級製程馬達", 800, 30, oc_production_ntd=8_000, e_reheat_kg=45.0,
         scrap_prob=0.10, e_scrap_kg=60.0, r_safety_ntd=1_000.0, lock_rule="process_temp",
         allowed_in_tier2=False),
]

# ---------------------------------------------------------------- 碳預算（6.5、第 9 節設計決定 2）
CARBON_TARGET_RATIO = 0.95         # 預算 = 基準 × 0.95；基準 = 引擎不介入時 7 天預期排放
CARBON_SIGMA_DAILY_FRAC = 0.03     # 天數不足 3 天時，σ_daily 以日基準 3% 代替
CARBON_FEE_NTD_PER_TON = CARBON_PRICE_NTD_PER_TON  # avoided_carbon_fee_ntd 用的費率
