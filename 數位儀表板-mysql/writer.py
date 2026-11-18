"""
資料庫寫入。所有欄位都是算完的結果；資料庫端不做任何運算。
依 DATABASE_URL 的 scheme 選 writer：mysql:// → MySQLWriter（PyMySQL，Railway），
postgres(ql):// → PostgresWriter（psycopg2）。
MemoryWriter 供校準、回測與 --dry-run 使用，介面相同。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import config as C

SCHEMA_SQL = Path(__file__).parent / "sql" / "schema.sql"
SCHEMA_MYSQL = Path(__file__).parent / "sql" / "schema_mysql.sql"


def is_mysql_url(url: str) -> bool:
    return url.split("://", 1)[0].lower().startswith("mysql")


def mysql_connect(url: str):
    """mysql://user:pwd@host:port/db → PyMySQL 連線（utf8mb4）。"""
    import pymysql
    u = urlparse(url)
    return pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                           password=unquote(u.password or ""), database=u.path.lstrip("/"),
                           charset="utf8mb4", autocommit=False)


def connect(url: str = C.DATABASE_URL):
    """analyze.py 等讀取用：依 scheme 回傳 DB-API 連線。"""
    if not url:
        raise SystemExit("DATABASE_URL 未設定：請複製 .env.example 為 .env 並填入連線字串")
    if is_mysql_url(url):
        return mysql_connect(url)
    import psycopg2
    return psycopg2.connect(url)


def make_writer(url: str = C.DATABASE_URL):
    if not url:
        raise SystemExit("DATABASE_URL 未設定：請複製 .env.example 為 .env 並填入連線字串")
    return MySQLWriter(url) if is_mysql_url(url) else PostgresWriter(url)


class MemoryWriter:
    """不接資料庫；把每張表存成 list[dict]，供 pandas 分析。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.power_reading: list[dict] = []
        self.interval_settlement: list[dict] = []
        self.decision_log: list[dict] = []
        self.decision_candidates: list[dict] = []
        self.carbon_budget_daily: dict[str, dict] = {}
        self.esg_waterfall_daily: dict[tuple, dict] = {}
        self._next_decision_id = 1

    def init_schema(self):
        self.reset()

    def first_reading_ts(self) -> datetime | None:
        return self.power_reading[0]["ts"] if self.power_reading else None

    def write_reading(self, row: dict):
        self.power_reading.append(row)

    def write_settlement(self, row: dict):
        self.interval_settlement.append(row)

    def write_decision(self, log_row: dict, candidate_rows: list[dict]) -> int:
        did = self._next_decision_id
        self._next_decision_id += 1
        log_row = {**log_row, "decision_id": did}
        self.decision_log.append(log_row)
        for c in candidate_rows:
            self.decision_candidates.append({**c, "decision_id": did})
        return did

    def backfill_outcome(self, interval_id: int, exceeded: bool):
        for d in self.decision_log:
            if d["interval_id"] == interval_id:
                d["outcome_exceeded"] = exceeded

    def upsert_carbon_day(self, row: dict):
        self.carbon_budget_daily[str(row["day"])] = row

    def upsert_waterfall(self, rows: list[dict]):
        for r in rows:
            self.esg_waterfall_daily[(str(r["day"]), r["component"])] = r

    def close(self):
        pass


class PostgresWriter:
    def __init__(self, dsn: str = C.DATABASE_URL):
        import psycopg2
        if not dsn:
            raise SystemExit("DATABASE_URL 未設定：請複製 .env.example 為 .env 並填入連線字串")
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False

    # ------------------------------------------------------------ schema / reset
    def init_schema(self):
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        self.conn.commit()

    def ensure_schema(self) -> bool:
        """資料表不存在時自動建表（首次部署用）。回傳是否剛建立。"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.esg_waterfall_daily')")
            exists = cur.fetchone()[0] is not None
        if not exists:
            self.init_schema()
        return not exists

    def first_reading_ts(self) -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT min(ts) FROM power_reading")
            return cur.fetchone()[0]

    # ------------------------------------------------------------ writes
    def write_reading(self, r: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO power_reading (ts, interval_id, minute_in_interval, remaining_minutes,
                       load_kw, avg_kw_so_far, contract_kw, buffer_kw, sigma_kw, sigma_avg_kw, edf,
                       grid_ef, renewable_pct, wbgt, process_temp, locked_status_text)
                   VALUES (%(ts)s, %(interval_id)s, %(minute_in_interval)s, %(remaining_minutes)s,
                       %(load_kw)s, %(avg_kw_so_far)s, %(contract_kw)s, %(buffer_kw)s, %(sigma_kw)s,
                       %(sigma_avg_kw)s, %(edf)s, %(grid_ef)s, %(renewable_pct)s, %(wbgt)s,
                       %(process_temp)s, %(locked_status_text)s)
                   ON CONFLICT (ts) DO NOTHING""", r)
        self.conn.commit()

    def write_settlement(self, r: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO interval_settlement (interval_id, interval_start, avg_kw, contract_kw,
                       exceeded, penalty_ntd, edf_at_5, edf_at_10, action_taken, avoided_penalty_ntd)
                   VALUES (%(interval_id)s, %(interval_start)s, %(avg_kw)s, %(contract_kw)s, %(exceeded)s,
                       %(penalty_ntd)s, %(edf_at_5)s, %(edf_at_10)s, %(action_taken)s, %(avoided_penalty_ntd)s)
                   ON CONFLICT (interval_id) DO UPDATE SET
                       avg_kw = EXCLUDED.avg_kw, exceeded = EXCLUDED.exceeded,
                       penalty_ntd = EXCLUDED.penalty_ntd, edf_at_5 = EXCLUDED.edf_at_5,
                       edf_at_10 = EXCLUDED.edf_at_10, action_taken = EXCLUDED.action_taken,
                       avoided_penalty_ntd = EXCLUDED.avoided_penalty_ntd""", r)
        self.conn.commit()

    def write_decision(self, log_row: dict, candidate_rows: list[dict]) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO decision_log (ts, interval_id, edf_trigger, tier, candidate_json, locked_loads,
                       chosen_plan, esv_ntd, shed_kw, delta_carbon_kg, approval_mode, approval_delay_s,
                       penalty_avoided_ntd, oc_production_ntd, carbon_cost_ntd, r_safety_ntd)
                   VALUES (%(ts)s, %(interval_id)s, %(edf_trigger)s, %(tier)s, %(candidate_json)s,
                       %(locked_loads)s, %(chosen_plan)s, %(esv_ntd)s, %(shed_kw)s, %(delta_carbon_kg)s,
                       %(approval_mode)s, %(approval_delay_s)s,
                       %(penalty_avoided_ntd)s, %(oc_production_ntd)s, %(carbon_cost_ntd)s, %(r_safety_ntd)s)
                   RETURNING decision_id""",
                {**log_row, "candidate_json": json.dumps(log_row["candidate_json"], ensure_ascii=False)})
            did = cur.fetchone()[0]
            cur.executemany(
                """INSERT INTO decision_candidates (decision_id, plan_name, shed_kw, edf_after,
                       penalty_avoided_ntd, cost_ntd, delta_carbon_kg, esv_ntd, is_locked, is_chosen)
                   VALUES (%(decision_id)s, %(plan_name)s, %(shed_kw)s, %(edf_after)s, %(penalty_avoided_ntd)s,
                       %(cost_ntd)s, %(delta_carbon_kg)s, %(esv_ntd)s, %(is_locked)s, %(is_chosen)s)""",
                [{**c, "decision_id": did} for c in candidate_rows])
        self.conn.commit()
        return did

    def backfill_outcome(self, interval_id: int, exceeded: bool):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE decision_log SET outcome_exceeded = %s WHERE interval_id = %s",
                        (exceeded, interval_id))
        self.conn.commit()

    def upsert_carbon_day(self, r: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO carbon_budget_daily (day, kwh_total, scope2_kg, budget_remaining_kg,
                       sigma_daily_kg, days_to_month_end, carbon_edf, avoided_carbon_kg, avoided_carbon_fee_ntd)
                   VALUES (%(day)s, %(kwh_total)s, %(scope2_kg)s, %(budget_remaining_kg)s, %(sigma_daily_kg)s,
                       %(days_to_month_end)s, %(carbon_edf)s, %(avoided_carbon_kg)s, %(avoided_carbon_fee_ntd)s)
                   ON CONFLICT (day) DO UPDATE SET
                       kwh_total = EXCLUDED.kwh_total, scope2_kg = EXCLUDED.scope2_kg,
                       budget_remaining_kg = EXCLUDED.budget_remaining_kg, sigma_daily_kg = EXCLUDED.sigma_daily_kg,
                       days_to_month_end = EXCLUDED.days_to_month_end, carbon_edf = EXCLUDED.carbon_edf,
                       avoided_carbon_kg = EXCLUDED.avoided_carbon_kg,
                       avoided_carbon_fee_ntd = EXCLUDED.avoided_carbon_fee_ntd""", r)
        self.conn.commit()

    def upsert_waterfall(self, rows: list[dict]):
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO esg_waterfall_daily (day, sort_order, component, ntd)
                   VALUES (%(day)s, %(sort_order)s, %(component)s, %(ntd)s)
                   ON CONFLICT (day, component) DO UPDATE SET ntd = EXCLUDED.ntd, sort_order = EXCLUDED.sort_order""",
                rows)
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------- MySQL（Railway）
def _py(v):
    """numpy 純量 → Python；tz-aware datetime → 台北當地時間的 naive DATETIME。"""
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    if isinstance(v, datetime) and v.tzinfo is not None:
        v = v.astimezone(C.TZ).replace(tzinfo=None)
    return v


def _row(r: dict) -> dict:
    return {k: _py(v) for k, v in r.items()}


class MySQLWriter:
    def __init__(self, url: str = C.DATABASE_URL):
        if not url:
            raise SystemExit("DATABASE_URL 未設定：請複製 .env.example 為 .env 並填入連線字串")
        self.conn = mysql_connect(url)

    # ------------------------------------------------------------ schema / reset
    def init_schema(self):
        sql = re.sub(r"--[^\n]*", "", SCHEMA_MYSQL.read_text(encoding="utf-8"))
        with self.conn.cursor() as cur:
            for stmt in (x.strip() for x in sql.split(";")):
                if stmt:
                    cur.execute(stmt)
        self.conn.commit()

    def ensure_schema(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name = 'esg_waterfall_daily'")
            exists = cur.fetchone()[0] > 0
        if not exists:
            self.init_schema()
        return not exists

    def first_reading_ts(self) -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT MIN(ts) FROM power_reading")
            v = cur.fetchone()[0]
        self.conn.commit()
        return v.replace(tzinfo=C.TZ) if v is not None else None

    # ------------------------------------------------------------ writes
    def write_reading(self, r: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT IGNORE INTO power_reading (ts, interval_id, minute_in_interval, remaining_minutes,
                       load_kw, avg_kw_so_far, contract_kw, buffer_kw, sigma_kw, sigma_avg_kw, edf,
                       grid_ef, renewable_pct, wbgt, process_temp, locked_status_text)
                   VALUES (%(ts)s, %(interval_id)s, %(minute_in_interval)s, %(remaining_minutes)s,
                       %(load_kw)s, %(avg_kw_so_far)s, %(contract_kw)s, %(buffer_kw)s, %(sigma_kw)s,
                       %(sigma_avg_kw)s, %(edf)s, %(grid_ef)s, %(renewable_pct)s, %(wbgt)s,
                       %(process_temp)s, %(locked_status_text)s)""", _row(r))
        self.conn.commit()

    def write_settlement(self, r: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO interval_settlement (interval_id, interval_start, avg_kw, contract_kw,
                       exceeded, penalty_ntd, edf_at_5, edf_at_10, action_taken, avoided_penalty_ntd)
                   VALUES (%(interval_id)s, %(interval_start)s, %(avg_kw)s, %(contract_kw)s, %(exceeded)s,
                       %(penalty_ntd)s, %(edf_at_5)s, %(edf_at_10)s, %(action_taken)s, %(avoided_penalty_ntd)s)
                   AS new ON DUPLICATE KEY UPDATE
                       avg_kw = new.avg_kw, exceeded = new.exceeded,
                       penalty_ntd = new.penalty_ntd, edf_at_5 = new.edf_at_5,
                       edf_at_10 = new.edf_at_10, action_taken = new.action_taken,
                       avoided_penalty_ntd = new.avoided_penalty_ntd""", _row(r))
        self.conn.commit()

    def write_decision(self, log_row: dict, candidate_rows: list[dict]) -> int:
        row = _row(log_row)
        row["candidate_json"] = json.dumps(log_row["candidate_json"], ensure_ascii=False, default=_py)
        row["locked_loads"] = json.dumps(list(log_row["locked_loads"]), ensure_ascii=False)
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO decision_log (ts, interval_id, edf_trigger, tier, candidate_json, locked_loads,
                       chosen_plan, esv_ntd, shed_kw, delta_carbon_kg, approval_mode, approval_delay_s,
                       penalty_avoided_ntd, oc_production_ntd, carbon_cost_ntd, r_safety_ntd)
                   VALUES (%(ts)s, %(interval_id)s, %(edf_trigger)s, %(tier)s, %(candidate_json)s,
                       %(locked_loads)s, %(chosen_plan)s, %(esv_ntd)s, %(shed_kw)s, %(delta_carbon_kg)s,
                       %(approval_mode)s, %(approval_delay_s)s,
                       %(penalty_avoided_ntd)s, %(oc_production_ntd)s, %(carbon_cost_ntd)s, %(r_safety_ntd)s)""",
                row)
            did = cur.lastrowid
            cur.executemany(
                """INSERT INTO decision_candidates (decision_id, plan_name, shed_kw, edf_after,
                       penalty_avoided_ntd, cost_ntd, delta_carbon_kg, esv_ntd, is_locked, is_chosen)
                   VALUES (%(decision_id)s, %(plan_name)s, %(shed_kw)s, %(edf_after)s, %(penalty_avoided_ntd)s,
                       %(cost_ntd)s, %(delta_carbon_kg)s, %(esv_ntd)s, %(is_locked)s, %(is_chosen)s)""",
                [_row({**c, "decision_id": did}) for c in candidate_rows])
        self.conn.commit()
        return did

    def backfill_outcome(self, interval_id: int, exceeded: bool):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE decision_log SET outcome_exceeded = %s WHERE interval_id = %s",
                        (_py(exceeded), _py(interval_id)))
        self.conn.commit()

    def upsert_carbon_day(self, r: dict):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO carbon_budget_daily (day, kwh_total, scope2_kg, budget_remaining_kg,
                       sigma_daily_kg, days_to_month_end, carbon_edf, avoided_carbon_kg, avoided_carbon_fee_ntd)
                   VALUES (%(day)s, %(kwh_total)s, %(scope2_kg)s, %(budget_remaining_kg)s, %(sigma_daily_kg)s,
                       %(days_to_month_end)s, %(carbon_edf)s, %(avoided_carbon_kg)s, %(avoided_carbon_fee_ntd)s)
                   AS new ON DUPLICATE KEY UPDATE
                       kwh_total = new.kwh_total, scope2_kg = new.scope2_kg,
                       budget_remaining_kg = new.budget_remaining_kg, sigma_daily_kg = new.sigma_daily_kg,
                       days_to_month_end = new.days_to_month_end, carbon_edf = new.carbon_edf,
                       avoided_carbon_kg = new.avoided_carbon_kg,
                       avoided_carbon_fee_ntd = new.avoided_carbon_fee_ntd""", _row(r))
        self.conn.commit()

    def upsert_waterfall(self, rows: list[dict]):
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO esg_waterfall_daily (day, sort_order, component, ntd)
                   VALUES (%(day)s, %(sort_order)s, %(component)s, %(ntd)s)
                   AS new ON DUPLICATE KEY UPDATE ntd = new.ntd, sort_order = new.sort_order""",
                [_row(r) for r in rows])
        self.conn.commit()

    def close(self):
        self.conn.close()
