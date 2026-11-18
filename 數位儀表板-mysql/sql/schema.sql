-- KMV 尖峰電力超約預警 × ESG 決策系統 — PostgreSQL (TT1) schema
-- 規格的五張表 + 補充表 esg_waterfall_daily（瀑布圖資料），全部為 Python 算完的結果；無 view、無 trigger、無任何計算。
-- 由 python main.py --init-db 執行；7 天重置時同樣重跑此檔。

DROP TABLE IF EXISTS decision_candidates;
DROP TABLE IF EXISTS decision_log;
DROP TABLE IF EXISTS interval_settlement;
DROP TABLE IF EXISTS power_reading;
DROP TABLE IF EXISTS carbon_budget_daily;
DROP TABLE IF EXISTS esg_waterfall_daily;

-- 5.1 每 60 秒一筆
CREATE TABLE power_reading (
    ts                  timestamptz PRIMARY KEY,
    interval_id         int         NOT NULL,
    minute_in_interval  int         NOT NULL,
    remaining_minutes   int         NOT NULL,
    load_kw             numeric(10,1) NOT NULL,
    avg_kw_so_far       numeric(10,1) NOT NULL,
    contract_kw         numeric(10,1) NOT NULL,
    buffer_kw           numeric(10,1) NOT NULL,
    sigma_kw            numeric(10,2) NOT NULL,
    sigma_avg_kw        numeric(10,2) NOT NULL,
    edf                 numeric(8,6)  NOT NULL,
    grid_ef             numeric(6,4)  NOT NULL,
    renewable_pct       numeric(6,3)  NOT NULL,
    wbgt                numeric(5,2)  NOT NULL,
    process_temp        numeric(6,2)  NOT NULL,
    locked_status_text  text        NOT NULL DEFAULT ''
);
CREATE INDEX ix_power_reading_interval ON power_reading (interval_id);

-- 5.2 每 15 分鐘一筆
CREATE TABLE interval_settlement (
    interval_id          int PRIMARY KEY,
    interval_start       timestamptz NOT NULL,
    avg_kw               numeric(10,1) NOT NULL,
    contract_kw          numeric(10,1) NOT NULL,
    exceeded             boolean NOT NULL,
    penalty_ntd          numeric(12,0) NOT NULL,
    edf_at_5             numeric(8,6),
    edf_at_10            numeric(8,6),
    action_taken         text,
    avoided_penalty_ntd  numeric(12,0) NOT NULL DEFAULT 0
);

-- 5.3 每次觸發一筆（G 維度核心）
CREATE TABLE decision_log (
    decision_id       serial PRIMARY KEY,
    ts                timestamptz NOT NULL,
    interval_id       int NOT NULL,
    edf_trigger       numeric(8,6) NOT NULL,
    tier              text NOT NULL,
    candidate_json    jsonb,
    locked_loads      text[] NOT NULL DEFAULT '{}',
    chosen_plan       text NOT NULL,
    esv_ntd           numeric(12,0) NOT NULL,
    shed_kw           numeric(10,1) NOT NULL,
    delta_carbon_kg   numeric(10,2) NOT NULL,
    approval_mode     text NOT NULL,
    approval_delay_s  int NOT NULL DEFAULT 0,
    outcome_exceeded  boolean,
    -- 選定方案的 ESV 拆項（頁面 3 瀑布圖與稽核用；接受罰款時皆為 0）
    penalty_avoided_ntd numeric(12,0) NOT NULL DEFAULT 0,
    oc_production_ntd   numeric(12,0) NOT NULL DEFAULT 0,
    carbon_cost_ntd     numeric(12,1) NOT NULL DEFAULT 0,   -- ΔE_carbon × P_eff，減碳為負
    r_safety_ntd        numeric(12,0) NOT NULL DEFAULT 0
);
CREATE INDEX ix_decision_log_interval ON decision_log (interval_id);

-- 5.4 每次觸發寫入所有候選方案，每方案一列（Power BI 象限圖直接讀此表）
CREATE TABLE decision_candidates (
    candidate_id          serial PRIMARY KEY,
    decision_id           int NOT NULL REFERENCES decision_log(decision_id) ON DELETE CASCADE,
    plan_name             text NOT NULL,
    shed_kw               numeric(10,1) NOT NULL,
    edf_after             numeric(8,6) NOT NULL,
    penalty_avoided_ntd   numeric(12,0) NOT NULL,
    cost_ntd              numeric(12,0) NOT NULL,
    delta_carbon_kg       numeric(10,2) NOT NULL,
    esv_ntd               numeric(12,0) NOT NULL,
    is_locked             boolean NOT NULL,
    is_chosen             boolean NOT NULL
);
CREATE INDEX ix_decision_candidates_decision ON decision_candidates (decision_id);

-- 5.6（補充）ESG 貢獻瀑布圖資料：每日 × 4 個組成，全為 NTD，Σ ntd = 當日選定方案 ESV 合計
-- Power BI 瀑布圖直接綁 component（類別）與 ntd（值），不需任何 DAX
CREATE TABLE esg_waterfall_daily (
    day         date NOT NULL,
    sort_order  int  NOT NULL,
    component   text NOT NULL,
    ntd         numeric(14,0) NOT NULL,
    PRIMARY KEY (day, component)
);

-- 5.5 每日一筆（當日列每 15 分鐘 upsert，00:00 定稿）
CREATE TABLE carbon_budget_daily (
    day                     date PRIMARY KEY,
    kwh_total               numeric(14,1) NOT NULL,
    scope2_kg               numeric(14,1) NOT NULL,
    budget_remaining_kg     numeric(14,1) NOT NULL,
    sigma_daily_kg          numeric(14,1) NOT NULL,
    days_to_month_end       int NOT NULL,
    carbon_edf              numeric(8,6) NOT NULL,
    avoided_carbon_kg       numeric(12,2) NOT NULL,
    avoided_carbon_fee_ntd  numeric(12,0) NOT NULL
);
