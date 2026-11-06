# Power BI 建置指引（修正版 v2，2026-11-01 實測可用）

三個頁面對應三種讀者。每個視覺直接綁定資料表欄位；DAX 只做顯示用途（格式、SUM/AVERAGE/COUNT、取最新一筆、條件式色彩）。EDF、ESV、碳預算全部由 Python 算好。時區轉換、日期／小時拆欄、延遲分組在 PostgreSQL 原生查詢中完成（純欄位拆解，非模型計算）。

成品為 Power BI 專案（PBIP）：`PowerOverload_ESG.pbip` + `PowerOverload_ESG.SemanticModel`（TMDL）+ `PowerOverload_ESG.Report`（PBIR）。

## 0. 連線

1. 連線參數做成 Power Query 參數（`expressions.tmdl`），不寫死在查詢裡：
   - `PG_Server` = `43.133.30.68:32740`（Zeabur 對外 IP:埠號；**不可**用環境變數中的 `POSTGRES_HOST`，那是容器內部名稱）
   - `PG_Database` = `zeabur`
   - 更改方式：常用 → 轉換資料 → 編輯參數。
2. 帳號 `root`；密碼只在 Desktop 認證視窗輸入，**不寫入任何檔案或文件**。加密選「無」。首次出現「原生資料庫查詢需核准」按「執行」。
3. 首次連線若要求 Npgsql，依提示安裝後重開 Desktop。
4. 連線模式：**DirectQuery**（6 張表）。卡頓時改 Import：模型檢視 → 選取 6 張表 → 屬性 → 儲存模式 → 匯入（或把 `tables/*.tmdl` 的 `mode: directQuery` 改成 `mode: import`）。
5. 每張表的分割區來源一律為：

```m
let
    Source = PostgreSQL.Database(PG_Server, PG_Database),
    Result = Value.NativeQuery(Source, "<下方 SQL>", null, [EnableFolding = true])
in
    Result
```

6. 各表原生查詢（只列 Power BI 用得到的欄位；numeric 一律轉 float8，時間一律轉台北時間）：

```sql
-- power_reading
SELECT (ts AT TIME ZONE 'Asia/Taipei') AS ts,
       (ts AT TIME ZONE 'Asia/Taipei')::date AS date_local,
       EXTRACT(HOUR FROM (ts AT TIME ZONE 'Asia/Taipei'))::int AS hour_local,
       interval_id, minute_in_interval, remaining_minutes,
       load_kw::float8 AS load_kw, avg_kw_so_far::float8 AS avg_kw_so_far, buffer_kw::float8 AS buffer_kw,
       edf::float8 AS edf, grid_ef::float8 AS grid_ef, renewable_pct::float8 AS renewable_pct,
       wbgt::float8 AS wbgt, process_temp::float8 AS process_temp,
       COALESCE(locked_status_text, '') AS locked_status_text
FROM public.power_reading

-- interval_settlement
SELECT interval_id,
       (interval_start AT TIME ZONE 'Asia/Taipei') AS interval_start,
       (interval_start AT TIME ZONE 'Asia/Taipei')::date AS settle_day,
       exceeded, penalty_ntd::float8 AS penalty_ntd,
       edf_at_5::float8 AS edf_at_5, edf_at_10::float8 AS edf_at_10,
       avoided_penalty_ntd::float8 AS avoided_penalty_ntd
FROM public.interval_settlement

-- decision_log
SELECT decision_id, (ts AT TIME ZONE 'Asia/Taipei') AS ts, interval_id,
       edf_trigger::float8 AS edf_trigger, tier,
       COALESCE(locked_loads::text, '{}') AS locked_loads,
       chosen_plan, esv_ntd::float8 AS esv_ntd, shed_kw::float8 AS shed_kw,
       delta_carbon_kg::float8 AS delta_carbon_kg, approval_mode, approval_delay_s::int AS approval_delay_s,
       ((approval_delay_s::int / 10) * 10)::int AS approval_delay_bin,
       outcome_exceeded,
       penalty_avoided_ntd::float8 AS penalty_avoided_ntd, oc_production_ntd::float8 AS oc_production_ntd,
       carbon_cost_ntd::float8 AS carbon_cost_ntd, r_safety_ntd::float8 AS r_safety_ntd
FROM public.decision_log

-- decision_candidates
SELECT candidate_id, decision_id, plan_name,
       shed_kw::float8 AS shed_kw, edf_after::float8 AS edf_after, cost_ntd::float8 AS cost_ntd,
       delta_carbon_kg::float8 AS delta_carbon_kg, esv_ntd::float8 AS esv_ntd, is_locked, is_chosen
FROM public.decision_candidates

-- carbon_budget_daily
SELECT day, kwh_total::float8 AS kwh_total, scope2_kg::float8 AS scope2_kg,
       budget_remaining_kg::float8 AS budget_remaining_kg, carbon_edf::float8 AS carbon_edf,
       avoided_carbon_kg::float8 AS avoided_carbon_kg
FROM public.carbon_budget_daily

-- esg_waterfall_daily
SELECT day, component, ntd::float8 AS ntd, sort_order
FROM public.esg_waterfall_daily
```

7. 關聯（全部多對一、單向篩選，「多」在左）：
   - `power_reading[interval_id]` * → 1 `interval_settlement[interval_id]`
   - `decision_log[interval_id]` * → 1 `interval_settlement[interval_id]`
   - `decision_candidates[decision_id]` * → 1 `decision_log[decision_id]`
   - `esg_waterfall_daily[day]` * → 1 `carbon_budget_daily[day]`
   - `interval_settlement[settle_day]` * → 1 `carbon_budget_daily[day]`（**新增**，供頁面 3 雙軸圖共用日期軸）
8. 模型設定：關閉自動日期／時間（`__PBI_TimeIntelligenceEnabled = 0`）；`esg_waterfall_daily[component]` 依資料行排序 = `sort_order`。

欄位格式：`edf`、`edf_at_5`、`edf_at_10`、`edf_trigger`、`edf_after`、`carbon_edf` 百分比 1 位小數；所有 `*_ntd` 千分位 0 位小數；`*_kw` 千分位 0 位小數；`wbgt`、`process_temp` 1 位小數；`ts` 格式 `yyyy-mm-dd hh:nn`，日期欄 `yyyy-mm-dd`。ID 類欄位與 `minute_in_interval`、`hour_local` 摘要設為「不摘要」。

## 1. 量值（僅顯示用途）

量值放在各自的資料表，以顯示資料夾分組（頁面1／頁面2／頁面3／格式色彩），**不另建「量值」計算資料表**（避免純 DirectQuery 模型混入計算資料表）。

DAX 撰寫規則（實測踩過的坑）：
- **變數不可命名為 `id`**，會讓量值出錯並被視覺自動移除。變數一律用 `_` 開頭的描述性名稱（如 `_latestInterval`），或單一字母 `t`、`e`、`s`。
- CALCULATE 的篩選條件不可用 `NOT ISEMPTY ( FILTER ( 整張表, ... ) )`，計數改用 `COUNTROWS ( FILTER ( ... ) )`。
- 取「最新一筆」統一用 `MAXX ( TOPN ( 1, ALL ( 表 ), 表[時間欄], DESC ), 表[欄位] )`，文字欄也適用。

### power_reading

| 量值 | DAX | 格式 |
| --- | --- | --- |
| 最新EDF | `MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[edf] )` | 0.0% |
| 最新剩餘分鐘 | `MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[remaining_minutes] )` | 0 |
| 最新Buffer_kW | `MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[buffer_kw] )` | #,0 |
| 最新區間ID | `MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[interval_id] )` | 0 |
| 最新鎖定狀態 | `VAR s = MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[locked_status_text] ) RETURN IF ( s = "", "無鎖定", s )` | — |
| 最新WBGT | `MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[wbgt] )` | #,0.0 |
| 最新製程溫度 | `MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[process_temp] )` | #,0.0 |
| 當前區間累積需量 | `VAR _latestInterval = MAXX ( TOPN ( 1, ALL ( power_reading ), power_reading[ts], DESC ), power_reading[interval_id] ) RETURN CALCULATE ( AVERAGE ( power_reading[avg_kw_so_far] ), KEEPFILTERS ( power_reading[interval_id] = _latestInterval ) )` | #,0 |
| 近2小時EDF | `VAR t = MAXX ( ALL ( power_reading ), power_reading[ts] ) RETURN CALCULATE ( AVERAGE ( power_reading[edf] ), KEEPFILTERS ( power_reading[ts] > t - 2 / 24 ) )` | 0.0% |
| 綠電匹配率 | `CALCULATE ( AVERAGE ( power_reading[renewable_pct] ), power_reading[edf] >= 0.5 )` | 0.00（renewable_pct 若為 0～1 小數改 0.0%） |
| 色_EDF燈號 | `VAR e = [最新EDF] RETURN SWITCH ( TRUE (), e >= 0.8, "#D13438", e >= 0.5, "#F7630C", e >= 0.2, "#FFB900", "#107C10" )` | — |
| 色_Buffer字色 | `IF ( [最新Buffer_kW] < 0, "#D13438", "#252423" )` | — |
| 色_WBGT底色 | `IF ( [最新WBGT] >= 28, "#F4B6B6", "#FFFFFF" )` | — |
| 色_製程溫度底色 | `IF ( [最新製程溫度] < 82, "#F4B6B6", "#FFFFFF" )` | — |

「當前區間累積需量」「近 2 小時 EDF」以量值取代視覺篩選器：前者只回傳最新區間的值，後者以資料最新時間往回 2 小時（不依賴系統時間，模擬資料也正確）。

### interval_settlement

| 量值 | DAX | 格式 |
| --- | --- | --- |
| 避免罰款累計 | `SUM ( interval_settlement[avoided_penalty_ntd] )` | #,0 |
| 實際罰款累計 | `SUM ( interval_settlement[penalty_ntd] )` | #,0 |
| 預警次數 | `CALCULATE ( COUNTROWS ( interval_settlement ), interval_settlement[edf_at_10] >= 0.5 )` | 0 |
| 預警命中數 | `CALCULATE ( COUNTROWS ( interval_settlement ), interval_settlement[edf_at_10] >= 0.5, interval_settlement[exceeded] = TRUE () )` | 0 |
| 預警命中率 | `DIVIDE ( [預警命中數], [預警次數] )` | 0.0% |

### decision_log

| 量值 | DAX | 格式 |
| --- | --- | --- |
| 鎖定觸發次數 | `COUNTROWS ( FILTER ( decision_log, LEN ( decision_log[locked_loads] ) > 2 ) ) + 0` | 0 |
| 鎖定違規次數 | `0`（結構保證：鎖定負載已從候選清單過濾） | 0 |
| 人工確認筆數 | `CALCULATE ( COUNTROWS ( decision_log ), decision_log[approval_mode] = "manual" )` | 0 |
| 色_決策列字色 | `IF ( SELECTEDVALUE ( decision_log[outcome_exceeded] ) = TRUE (), "#D13438", "#252423" )` | — |

`locked_loads` 在 SQL 中轉為文字 `{...}`，空陣列與 NULL 都是 `{}`（長度 2）。

### decision_candidates

| 量值 | DAX |
| --- | --- |
| 方案名稱 | `SELECTEDVALUE ( decision_candidates[plan_name] )` |
| 色_候選點 | `SWITCH ( TRUE (), SELECTEDVALUE ( decision_candidates[is_locked] ) = TRUE (), "#B3B3B3", SELECTEDVALUE ( decision_candidates[is_chosen] ) = TRUE (), "#E8710A", "#2B7BB9" )` |

### carbon_budget_daily

| 量值 | DAX | 格式 |
| --- | --- | --- |
| 最新碳EDF | `MAXX ( TOPN ( 1, ALL ( carbon_budget_daily ), carbon_budget_daily[day], DESC ), carbon_budget_daily[carbon_edf] )` | 0.0% |
| 最新碳預算剩餘_kg | `MAXX ( TOPN ( 1, ALL ( carbon_budget_daily ), carbon_budget_daily[day], DESC ), carbon_budget_daily[budget_remaining_kg] )` | #,0 |
| 累計Scope2_kg | `SUM ( carbon_budget_daily[scope2_kg] )` | #,0 |
| 累計避免碳排_kg | `SUM ( carbon_budget_daily[avoided_carbon_kg] )` | #,0.0 |
| 碳強度 | `DIVIDE ( SUM ( carbon_budget_daily[scope2_kg] ), SUM ( carbon_budget_daily[kwh_total] ) )` | 0.000 |
| 色_碳EDF燈號 | `VAR e = [最新碳EDF] RETURN SWITCH ( TRUE (), e >= 0.8, "#D13438", e >= 0.5, "#F7630C", e >= 0.2, "#FFB900", "#107C10" )` | — |

## 2. 頁面 1：即時超約戰情（值班人員）

畫布 1280 × 720。

| 視覺 | 類型 | 欄位 / 量值 | 設定 |
| --- | --- | --- | --- |
| 標題 | 文字方塊 | — | 高度至少 78，否則副標會被切掉 |
| 時間範圍 | 交叉分析篩選器 | `power_reading[ts]` | 不另設視覺標題（避免與篩選器標頭重複）；手動改「相對時間：最近 24 小時」 |
| 距結算（分鐘） | 卡片 | `[最新剩餘分鐘]` | 顯示單位「無」 |
| 剩餘配額 kW | 卡片 | `[最新Buffer_kW]` | 顯示單位「無」；字色 fx = `[色_Buffer字色]`；副標「負值代表預估已超約」 |
| 當下 EDF | 量測計 | 值 `[最新EDF]` | 軸最小 0、最大 1；填滿色彩 fx = `[色_EDF燈號]` |
| 安全鎖定狀態 | 資料表 | `[最新鎖定狀態]`、`[最新WBGT]`、`[最新製程溫度]` | WBGT 欄背景 fx = `[色_WBGT底色]`；製程溫度欄背景 fx = `[色_製程溫度底色]` |
| 目前區間需量累積 | 折線圖 | X `power_reading[minute_in_interval]`、Y `[當前區間累積需量]` | X 軸類型「類別」（固定 0～14）；**不加排序定義**；常數線 10000「契約容量」 |
| 近 2 小時 EDF 歷史 | 折線圖 | X `power_reading[ts]`、Y `[近2小時EDF]` | 常數線 0.5「預警線 50%」 |

手動：頁面格式 → 頁面重新整理 → 開啟，1 分鐘（僅 DirectQuery 有效；Desktop 可到 1 秒，Pro 服務最短 30 分鐘）。

## 3. 頁面 2：決策品質（廠務主管）

| 視覺 | 類型 | 欄位 | 設定 |
| --- | --- | --- | --- |
| 決策編號／決策分級／確認模式 | 交叉分析篩選器 ×3 | `decision_log[decision_id]`、`[tier]`、`[approval_mode]` | 下拉式；不另設視覺標題 |
| 方案象限圖 | 散佈圖 | 值 `decision_candidates[candidate_id]`；X `cost_ntd`、Y `delta_carbon_kg`、大小 `shed_kw` **一律設「加總」**；工具提示 `[方案名稱]`、`esv_ntd`（加總） | **「值」有欄位時 X／Y 不可設「不摘要」，否則報錯**（每個 candidate_id 只有一列，加總即原值）；資料點色彩 fx = `[色_候選點]`（橘＝選定、藍＝其他、灰＝鎖定）；Y 軸常數線 0；手動：分析窗格加 X 軸中位數線 → 四象限 |
| 預警命中率 | 卡片 | `[預警命中率]` | 副標「引擎介入後，實際校準見 backtest」 |
| 避免罰款累計 NTD | 卡片 | `[避免罰款累計]` | **顯示單位「無」**（預設會縮成「9 百萬」） |
| 每日避免罰款 vs 實際罰款 | 群組直條圖 | X `interval_settlement[settle_day]`；Y `avoided_penalty_ntd`、`penalty_ntd`（加總） | X 軸類型「類別」，依日期遞增 |
| 決策日誌 | 資料表 | `decision_log`：`decision_id`、`ts`、`interval_id`、`edf_trigger`、`tier`、`locked_loads`、`chosen_plan`、`esv_ntd`、`shed_kw`、`delta_carbon_kg`、`approval_mode`、`approval_delay_s`、`outcome_exceeded`、`penalty_avoided_ntd`、`oc_production_ntd`、`carbon_cost_ntd`、`r_safety_ntd` | 依 `ts` 遞減；每欄字色 fx = `[色_決策列字色]`（降載後仍超約整列紅字） |
| 人工確認延遲分布 | 群組直條圖 | X `decision_log[approval_delay_bin]`、Y `[人工確認筆數]` | 分組在 SQL 完成（每 10 秒），不用 Power BI 群組；X 軸「類別」遞增 |

## 4. 頁面 3：ESG 累計成效（管理層）

畫布 1280 × 1000。

必做：

| 視覺 | 類型 | 欄位 | 設定 |
| --- | --- | --- | --- |
| 碳 EDF | 卡片 | `[最新碳EDF]` | 字色 fx = `[色_碳EDF燈號]` |
| 月碳預算消耗（7 天迷你月） | 100% 堆疊橫條圖 | 值 `[累計Scope2_kg]`、`[最新碳預算剩餘_kg]`（無類別） | 顯示資料標籤、圖例置頂 |
| ESG 貢獻瀑布圖 | 瀑布圖 | 類別 `esg_waterfall_daily[component]`、Y `ntd`（加總） | 依 component 遞增（模型已設依 sort_order 排序）；總計＝淨 ESV |
| 鎖定觸發次數／鎖定違規 | 卡片 ×2 | `[鎖定觸發次數]`、`[鎖定違規次數]` | 單位是次數，不放進瀑布圖 |

加分：

| 視覺 | 類型 | 欄位 | 設定 |
| --- | --- | --- | --- |
| 用電熱圖 | 矩陣 | 列 `power_reading[date_local]`、欄 `hour_local`、值 `load_kw`（平均） | 值背景色階 白 → #1F6FB2 |
| 排碳係數熱圖 | 矩陣 | 同上，值 `grid_ef`（平均） | 值背景色階 #E6E6E6 → #D13438；與用電熱圖並排比對 |
| EDF vs 碳強度 | 折線與群組直條圖 | X `carbon_budget_daily[day]`；直條 `interval_settlement[edf_at_10]`（平均）；線 `[碳強度]` | 依新增的 settle_day → day 關聯對齊；X 軸「類別」 |
| 綠電匹配率 | 卡片 | `[綠電匹配率]` | 副標「高 EDF 時段（EDF ≥ 0.5）的綠電占比」 |
| 累計避免碳排 kg | 卡片 | `[累計避免碳排_kg]` | — |

所有卡片：類別標籤關閉、字級 32、**顯示單位「無」**。

## 5. 驗證查詢（在 psql／DBeaver 跑，與卡片對數）

```sql
-- 頁面 1：最新一筆
SELECT ts, remaining_minutes, edf, buffer_kw, locked_status_text, wbgt, process_temp
FROM power_reading ORDER BY ts DESC LIMIT 1;

-- 頁面 2：命中率、避免罰款
SELECT count(*) FILTER (WHERE edf_at_10 >= 0.5) AS alerts,
       count(*) FILTER (WHERE edf_at_10 >= 0.5 AND exceeded) AS hits,
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

-- 鎖定觸發次數
SELECT count(*) FROM decision_log WHERE cardinality(locked_loads) > 0;
```

## 6. 常見問題

- **Npgsql 連不上 Zeabur**：確認用的是 Zeabur 網路頁的「對外」IP:埠號，不是 `*.zeabur.internal` 或 `POSTGRES_HOST`；加密先選「無」。
- **DirectQuery 每個視覺都慢**：Zeabur 免費方案連線數有限，Power BI 每個視覺一條查詢。改 Import + 5 分鐘排程（7 天 ≈ power_reading 10,080 列，Import 毫無壓力）。
- **時間顯示錯時區**：已在 SQL 以 `AT TIME ZONE 'Asia/Taipei'` 處理，不需在 Power Query 另做轉換。
- **散佈圖報錯「移除 [值] 以顯示 X 軸與 Y 軸」**：X／Y／大小改成「加總」。
- **象限圖只剩一個點**：「值」（詳細資料）沒放 `candidate_id`。
- **量值旁出現紅色警告三角形、視覺欄位被自動移除**：量值公式有錯，滑鼠移到三角形看訊息；先檢查變數名稱是否為 `id`。
- **卡片顯示「9 百萬」**：格式 → 圖說文字值 → 顯示單位改「無」。
- **篩選器標題出現兩次**：關閉視覺標題，只保留篩選器標頭。
- **瀑布圖順序亂**：對 `component` 設「依資料行排序 = sort_order」。
- **新版檔案開啟後殘留舊視覺**：先刪除舊的專案資料夾再解壓縮，不要直接覆蓋。

## 7. 以 PBIP 產生檔案時的技術規格

- 報表格式 PBIR：`report.json` schema 2.1.0、`page.json` 2.0.0、`visual.json` 2.4.0、`version.json` 為 `2.0.0`；基礎佈景主題 `CY24SU10`，檔案須放在 `StaticResources/SharedResources/BaseThemes/`。
- 頁面與視覺的 `name` 只能用英數字、底線、連字號，且與資料夾名稱相同。
- 語意模型 TMDL：`compatibilityLevel: 1567`；名稱含中文的量值須以單引號包住；M 運算式比 `source =` 多縮排兩層。
- 產生後先以官方 JSON schema 驗證所有 JSON，再比對每個視覺引用的資料表／欄位／量值都存在於模型。
