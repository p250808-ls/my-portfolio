# Power BI 建置指引

三個頁面對應三種讀者。每個視覺直接綁定資料表欄位；DAX 只做顯示用途（格式、SUM/AVERAGE/COUNT、取最新一筆）。EDF、ESV、碳預算全部由 Python 算好。

## 快速開始

直接開啟已建好的報表 `powerbi/KMV-ESG.pbip`，步驟見 [README](README.md#我只想打開報表看3-步驟)。

本文件說明**手動建置或修改**報表：自動產生的報表未包含條件式色彩、常數線與相對時間篩選，需要時依下列各節補上。要整體改版面，建議改 `powerbi/generate_pbip.py` 後重新產生（會覆蓋 Desktop 裡的手動修改）。

## 0. 連線

1. Power BI Desktop →「取得資料」→「MySQL 資料庫」。第一次需安裝 MySQL Connector/NET（安裝檔在 `tools/`，裝完重開 Desktop）。展開「進階選項」勾選「**使用完整階層架構瀏覽**」，表名才不會帶 `kmv_esg ` 前綴。
2. 伺服器：`tokaido.proxy.rlwy.net:18473`（Railway MySQL 的 TCP Proxy）；資料庫：`kmv_esg`。
3. 連線模式：**匯入**。驗證選「資料庫」，帳號 `kmv`、密碼為 Railway kmv-worker 的 `KMV_DB_PASSWORD`，**勾選加密連線**。
4. 勾選六張表：`power_reading`、`interval_settlement`、`decision_log`、`decision_candidates`、`carbon_budget_daily`、`esg_waterfall_daily`。
5. 模型檢視建立關聯（都是一對多、單向篩選）：
   - `interval_settlement[interval_id]` 1 → * `power_reading[interval_id]`
   - `interval_settlement[interval_id]` 1 → * `decision_log[interval_id]`
   - `decision_log[decision_id]` 1 → * `decision_candidates[decision_id]`
   - `carbon_budget_daily[day]` 1 → * `esg_waterfall_daily[day]`
6. 自動頁面重新整理：頁面 1 的「格式」→「頁面重新整理」開啟，間隔 1 分鐘（Desktop 可到 1 秒；Pro 服務最短 30 分鐘，課堂以 Desktop 展示）。
7. 若 DirectQuery 卡頓：改 Import，「排程重新整理」5 分鐘；資料量 7 天 ≈ power_reading 10,080 列，Import 毫無壓力。
8. 時間軸篩選器：以 `power_reading[ts]` 做相對日期篩選「最近 24 小時」，設定為同步到所有頁面。

欄位格式：`edf`、`edf_at_5`、`edf_at_10`、`carbon_edf`、`edf_after` 設為百分比 1 位小數；所有 `*_ntd` 設千分位、0 位小數；`*_kw` 千分位、0 位小數。

## 1. 允許的 DAX（僅顯示用途）

取「最新一筆」在 DirectQuery 下需要量值；以下全部是彙總或格式，沒有模型邏輯。放在一個「量值」資料表即可。

```dax
最新EDF = CALCULATE ( MAX ( power_reading[edf] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )
最新剩餘分鐘 = CALCULATE ( MAX ( power_reading[remaining_minutes] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )
最新Buffer_kW = CALCULATE ( MAX ( power_reading[buffer_kw] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )
最新區間ID = CALCULATE ( MAX ( power_reading[interval_id] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )
最新鎖定狀態 = CALCULATE ( MAX ( power_reading[locked_status_text] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )
最新WBGT = CALCULATE ( MAX ( power_reading[wbgt] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )
最新製程溫度 = CALCULATE ( MAX ( power_reading[process_temp] ), TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ) )

避免罰款累計 = SUM ( interval_settlement[avoided_penalty_ntd] )
實際罰款累計 = SUM ( interval_settlement[penalty_ntd] )
預警次數 = CALCULATE ( COUNTROWS ( interval_settlement ), interval_settlement[edf_at_10] >= 0.5 )
預警命中數 = CALCULATE ( COUNTROWS ( interval_settlement ), interval_settlement[edf_at_10] >= 0.5, interval_settlement[exceeded] = TRUE () )
預警命中率 = DIVIDE ( [預警命中數], [預警次數] )

最新碳EDF = CALCULATE ( MAX ( carbon_budget_daily[carbon_edf] ), TOPN ( 1, ALL ( carbon_budget_daily ), carbon_budget_daily[day], DESC ) )
最新碳預算剩餘_kg = CALCULATE ( MAX ( carbon_budget_daily[budget_remaining_kg] ), TOPN ( 1, ALL ( carbon_budget_daily ), carbon_budget_daily[day], DESC ) )
累計Scope2_kg = SUM ( carbon_budget_daily[scope2_kg] )
累計避免碳排_kg = SUM ( carbon_budget_daily[avoided_carbon_kg] )
鎖定觸發次數 = CALCULATE ( COUNTROWS ( decision_log ), NOT ISEMPTY ( FILTER ( decision_log, LEN ( decision_log[locked_loads] ) > 2 ) ) )
```

說明：`locked_loads` 在 MySQL 是 JSON 文字 `[...]`，空清單為 `[]`（長度 2）。「鎖定違規 0 次」是結構保證（鎖定負載從候選清單過濾），卡片直接放文字 0 即可，不必算。

碳強度（頁面 3 雙軸圖右軸）唯一例外：`scope2_kg / kwh_total` 是兩個 Python 欄位的比值，屬於顯示用簡單除法：`碳強度 = DIVIDE ( SUM ( carbon_budget_daily[scope2_kg] ), SUM ( carbon_budget_daily[kwh_total] ) )`。

## 2. 頁面 1：即時超約戰情（值班人員）

| 視覺 | 類型 | 欄位 / 量值 | 設定 |
| --- | --- | --- | --- |
| 剩餘分鐘數 | 卡片 | `[最新剩餘分鐘]` | 標題「距結算」 |
| 當下 EDF | 量測計 | 值 `[最新EDF]`，最小 0、最大 1 | 格式 → 量測計軸 → 條件式色彩：<0.2 綠、0.2～0.5 黃、0.5～0.8 橘、≥0.8 紅；或用 3 個目標線 0.2／0.5／0.8 |
| 剩餘配額 | 卡片 | `[最新Buffer_kW]` | 負值代表預估已超約 |
| 區間內需量累積 | 折線圖 | X `power_reading[minute_in_interval]`，Y `avg_kw_so_far`（平均） | 視覺篩選器：`interval_id` = `[最新區間ID]`（用「前 N 個」依 ts 篩最新 15 列亦可）；分析窗格加常數線 10000 標「契約容量」 |
| 近 2 小時 EDF 歷史 | 折線圖 | X `power_reading[ts]`，Y `edf` | 視覺篩選器相對時間：最近 2 小時 |
| 安全鎖定狀態 | 多列卡片 或 表格 | `[最新鎖定狀態]`、`[最新WBGT]`、`[最新製程溫度]` | 條件式格式：WBGT ≥ 28 紅底；製程溫度 < 82 紅底；鎖定文字空字串顯示「無鎖定」 |

X 軸用 `minute_in_interval`（0～14）時請設定為「類別」軸，才會固定 15 格。

## 3. 頁面 2：決策品質（廠務主管）

| 視覺 | 類型 | 欄位 | 設定 |
| --- | --- | --- | --- |
| 方案象限圖 | 散佈圖 | X `decision_candidates[cost_ntd]`、Y `decision_candidates[delta_carbon_kg]`、大小 `shed_kw`、詳細資料 `candidate_id`、圖例 `is_chosen` | 彙總都設「不摘要」；`is_locked` = TRUE 的點用條件式色彩灰階；Y 軸 0 加常數線（上方增排、下方減碳）；X 軸常數線於成本中位數 → 四象限；加交叉分析篩選器 `decision_log[decision_id]` 看單次決策的 7 個點 |
| 決策日誌 | 表格 | `decision_log`：`ts`、`interval_id`、`edf_trigger`、`tier`、`locked_loads`、`chosen_plan`、`esv_ntd`、`shed_kw`、`delta_carbon_kg`、`approval_mode`、`approval_delay_s`、`outcome_exceeded`、`penalty_avoided_ntd`、`oc_production_ntd`、`carbon_cost_ntd`、`r_safety_ntd` | 交叉分析篩選器：`tier`、`approval_mode`；`outcome_exceeded` = TRUE 列紅字（降載後仍超約） |
| 預警命中率 | 卡片 | `[預警命中率]` | 副標註明「引擎介入後，實際校準見 backtest」 |
| 避免罰款累計 | 卡片 + 群組直條圖 | 卡片 `[避免罰款累計]`；直條 X `interval_settlement[interval_start]`（日層級）、Y `avoided_penalty_ntd` | 直條圖可加第二序列 `penalty_ntd`（實際罰款）對照 |
| 人工確認延遲分布 | 直方圖 | `decision_log[approval_delay_s]` | 內建無直方圖：用群組直條圖，X 軸 `approval_delay_s` 分組（資料 → 新增群組 → 間隔 10 秒），Y 計數 `decision_id`；篩選 `approval_mode` = manual |

## 4. 頁面 3：ESG 累計成效（管理層）

必做兩項：

| 視覺 | 類型 | 欄位 | 設定 |
| --- | --- | --- | --- |
| 月碳預算消耗 | 100% 堆疊橫條 或 KPI | 進度條：X `[累計Scope2_kg]` 與 `[最新碳預算剩餘_kg]` 兩個量值堆疊；旁邊卡片 `[最新碳EDF]` | 標題「7 天迷你月」；碳 EDF 卡片條件式色彩同頁面 1 |
| ESG 貢獻瀑布圖 | 瀑布圖 | 類別 `esg_waterfall_daily[component]`、Y `ntd`（SUM）、細目可選 `day` | 類別排序依 `sort_order`（資料行工具 → 依資料行排序）；最後總計即淨 ESV，與 `decision_log[esv_ntd]` 合計相等 |
| 安全鎖定 KPI | 兩張卡片 | `[鎖定觸發次數]`；「鎖定違規」固定文字 0 | 單位是次數，不放進瀑布圖 |

加分三項：

| 視覺 | 類型 | 欄位 | 設定 |
| --- | --- | --- | --- |
| 用電 vs 排碳係數熱圖 | 矩陣 | 列 `power_reading[ts]` 日層級、欄 `ts` 小時層級、值 `load_kw`（平均；÷60 即 kWh，顯示用）與 `grid_ef`（平均） | 值 → 條件式格式 → 背景色階：load 用藍、grid_ef 用灰到紅；兩個矩陣並排比對「高負載是否落在高係數時段」。日期階層：在 Power Query 以 `ts` 新增 `date` 與 `hour` 欄（純欄位拆解，非計算） |
| EDF vs 碳強度 雙軸 | 折線與群組直條圖 | 直條 Y `interval_settlement[edf_at_10]`（平均，按日）、線 Y `[碳強度]`，共用 X `carbon_budget_daily[day]` | 透過關聯 `interval_settlement[interval_start]` 日層級與 `day` 對齊；若關聯不方便，兩張圖上下對齊 X 軸即可 |
| 綠電匹配率 | 卡片 | 量值 `綠電匹配率 = CALCULATE ( AVERAGE ( power_reading[renewable_pct] ), power_reading[edf] >= 0.5 )` | 篩選 + 平均屬於彙總；副標「高 EDF 時段的綠電占比」 |

## 5. 驗證查詢（在 MySQL Workbench／DBeaver 跑，與卡片對數）

```sql
-- 頁面 1：最新一筆
SELECT ts, remaining_minutes, edf, buffer_kw, locked_status_text, wbgt, process_temp
FROM power_reading ORDER BY ts DESC LIMIT 1;

-- 頁面 2：命中率、避免罰款
SELECT SUM(edf_at_10 >= 0.5) AS alerts,
       SUM(edf_at_10 >= 0.5 AND exceeded) AS hits,
       sum(avoided_penalty_ntd) AS avoided, sum(penalty_ntd) AS paid
FROM interval_settlement;

-- 頁面 2：單次決策的 7 個候選點
SELECT plan_name, cost_ntd, delta_carbon_kg, shed_kw, esv_ntd, is_locked, is_chosen
FROM decision_candidates WHERE decision_id = 1 ORDER BY candidate_id;

-- 頁面 3：瀑布圖合計應等於 decision_log 的 ESV 合計
SELECT (SELECT sum(ntd) FROM esg_waterfall_daily) AS waterfall_total,
       (SELECT sum(esv_ntd) FROM decision_log) AS esv_total;

-- 頁面 3：碳預算
SELECT day, scope2_kg, budget_remaining_kg, carbon_edf, avoided_carbon_kg FROM carbon_budget_daily ORDER BY day;
```

## 6. 常見問題

- **連不上 MySQL**：確認用的是 `tokaido.proxy.rlwy.net:18473`，不是 `mysql.railway.internal`；資料庫填 `kmv_esg`。
- **DirectQuery 每個視覺都慢**：共用的 MySQL 走公開 Proxy，Power BI 每個視覺一條查詢。改 Import + 5 分鐘排程。
- **`ts` 時間**：MySQL 存的已是台北當地時間（DATETIME），直接顯示，不需時區轉換。
- **象限圖只剩一個點**：散佈圖預設會彙總，把 X／Y 的「摘要」改成「不摘要」，並在「詳細資料」放 `candidate_id`。
- **瀑布圖順序亂**：對 `component` 欄位設「依資料行排序 = sort_order」。
