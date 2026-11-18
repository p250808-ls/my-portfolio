-- KMV 尖峰電力超約預警 × ESG 決策系統 — MySQL 8 schema（Railway）
-- 與 schema.sql（PostgreSQL）同一組六張表、同欄位名；全部為 Python 算完的結果；無 view、無 trigger、無任何計算。
-- 時間欄位為 DATETIME，存 Asia/Taipei 當地時間（MySQL DATETIME 不帶時區）。
-- locked_loads / candidate_json 為 JSON（空清單存 []）。
-- 由 python main.py --init-db 執行；7 天重置時同樣重跑此檔。

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS decision_candidates;
DROP TABLE IF EXISTS decision_log;
DROP TABLE IF EXISTS interval_settlement;
DROP TABLE IF EXISTS power_reading;
DROP TABLE IF EXISTS carbon_budget_daily;
DROP TABLE IF EXISTS esg_waterfall_daily;
SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE power_reading (
    ts                  DATETIME      NOT NULL PRIMARY KEY,
    interval_id         INT           NOT NULL,
    minute_in_interval  INT           NOT NULL,
    remaining_minutes   INT           NOT NULL,
    load_kw             DECIMAL(10,1) NOT NULL,
    avg_kw_so_far       DECIMAL(10,1) NOT NULL,
    contract_kw         DECIMAL(10,1) NOT NULL,
    buffer_kw           DECIMAL(10,1) NOT NULL,
    sigma_kw            DECIMAL(10,2) NOT NULL,
    sigma_avg_kw        DECIMAL(10,2) NOT NULL,
    edf                 DECIMAL(8,6)  NOT NULL,
    grid_ef             DECIMAL(6,4)  NOT NULL,
    renewable_pct       DECIMAL(6,3)  NOT NULL,
    wbgt                DECIMAL(5,2)  NOT NULL,
    process_temp        DECIMAL(6,2)  NOT NULL,
    locked_status_text  VARCHAR(255)  NOT NULL DEFAULT '',
    INDEX ix_power_reading_interval (interval_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE interval_settlement (
    interval_id          INT           NOT NULL PRIMARY KEY,
    interval_start       DATETIME      NOT NULL,
    avg_kw               DECIMAL(10,1) NOT NULL,
    contract_kw          DECIMAL(10,1) NOT NULL,
    exceeded             BOOLEAN       NOT NULL,
    penalty_ntd          DECIMAL(12,0) NOT NULL,
    edf_at_5             DECIMAL(8,6),
    edf_at_10            DECIMAL(8,6),
    action_taken         VARCHAR(255),
    avoided_penalty_ntd  DECIMAL(12,0) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE decision_log (
    decision_id         INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts                  DATETIME      NOT NULL,
    interval_id         INT           NOT NULL,
    edf_trigger         DECIMAL(8,6)  NOT NULL,
    tier                VARCHAR(16)   NOT NULL,
    candidate_json      JSON,
    locked_loads        JSON          NOT NULL,
    chosen_plan         VARCHAR(255)  NOT NULL,
    esv_ntd             DECIMAL(12,0) NOT NULL,
    shed_kw             DECIMAL(10,1) NOT NULL,
    delta_carbon_kg     DECIMAL(10,2) NOT NULL,
    approval_mode       VARCHAR(64)   NOT NULL,
    approval_delay_s    INT           NOT NULL DEFAULT 0,
    outcome_exceeded    BOOLEAN,
    penalty_avoided_ntd DECIMAL(12,0) NOT NULL DEFAULT 0,
    oc_production_ntd   DECIMAL(12,0) NOT NULL DEFAULT 0,
    carbon_cost_ntd     DECIMAL(12,1) NOT NULL DEFAULT 0,
    r_safety_ntd        DECIMAL(12,0) NOT NULL DEFAULT 0,
    INDEX ix_decision_log_interval (interval_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE decision_candidates (
    candidate_id          INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
    decision_id           INT           NOT NULL,
    plan_name             VARCHAR(255)  NOT NULL,
    shed_kw               DECIMAL(10,1) NOT NULL,
    edf_after             DECIMAL(8,6)  NOT NULL,
    penalty_avoided_ntd   DECIMAL(12,0) NOT NULL,
    cost_ntd              DECIMAL(12,0) NOT NULL,
    delta_carbon_kg       DECIMAL(10,2) NOT NULL,
    esv_ntd               DECIMAL(12,0) NOT NULL,
    is_locked             BOOLEAN       NOT NULL,
    is_chosen             BOOLEAN       NOT NULL,
    INDEX ix_decision_candidates_decision (decision_id),
    CONSTRAINT fk_candidates_decision FOREIGN KEY (decision_id)
        REFERENCES decision_log (decision_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE esg_waterfall_daily (
    day         DATE          NOT NULL,
    sort_order  INT           NOT NULL,
    component   VARCHAR(64)   NOT NULL,
    ntd         DECIMAL(14,0) NOT NULL,
    PRIMARY KEY (day, component)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE carbon_budget_daily (
    day                     DATE          NOT NULL PRIMARY KEY,
    kwh_total               DECIMAL(14,1) NOT NULL,
    scope2_kg               DECIMAL(14,1) NOT NULL,
    budget_remaining_kg     DECIMAL(14,1) NOT NULL,
    sigma_daily_kg          DECIMAL(14,1) NOT NULL,
    days_to_month_end       INT           NOT NULL,
    carbon_edf              DECIMAL(8,6)  NOT NULL,
    avoided_carbon_kg       DECIMAL(12,2) NOT NULL,
    avoided_carbon_fee_ntd  DECIMAL(12,0) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
