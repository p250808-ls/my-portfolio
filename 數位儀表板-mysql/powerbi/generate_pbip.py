"""
產生 Power BI 專案（PBIP）：KMV-ESG.pbip + 語意模型（TMDL）+ 三頁報表（report.json）。

  python powerbi/generate_pbip.py

用 Power BI Desktop 開啟 powerbi/KMV-ESG.pbip → 常用 → 重新整理（第一次會要求輸入資料庫帳密）。
連線、表、關聯、量值、視覺效果全部定義在此檔；要改報表就改這裡再重新產生。
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

SERVER = "tokaido.proxy.rlwy.net:18473"
DATABASE = "kmv_esg"
NAME = "KMV-ESG"
OUT = Path(__file__).parent
NS = uuid.UUID("6b1f3c1e-6f0e-4d8a-9a43-2b8f0d5e7c11")   # 固定 namespace → 每次產生的 lineageTag 相同


def tag(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


# ------------------------------------------------------------------ 語意模型
# 型別：int / num / dt / date / text / bool；fmt 為顯示格式
I, N, DT, D, T, B = "int", "num", "dt", "date", "text", "bool"
TABLES: dict[str, list[tuple]] = {
    "power_reading": [
        ("ts", DT), ("interval_id", I), ("minute_in_interval", I), ("remaining_minutes", I),
        ("load_kw", N), ("avg_kw_so_far", N), ("contract_kw", N), ("buffer_kw", N), ("sigma_kw", N),
        ("sigma_avg_kw", N), ("edf", N), ("grid_ef", N), ("renewable_pct", N), ("wbgt", N),
        ("process_temp", N), ("locked_status_text", T),
    ],
    "interval_settlement": [
        ("interval_id", I), ("interval_start", DT), ("avg_kw", N), ("contract_kw", N), ("exceeded", B),
        ("penalty_ntd", N), ("edf_at_5", N), ("edf_at_10", N), ("action_taken", T), ("avoided_penalty_ntd", N),
    ],
    "decision_log": [
        ("decision_id", I), ("ts", DT), ("interval_id", I), ("edf_trigger", N), ("tier", T),
        ("locked_loads", T), ("chosen_plan", T), ("esv_ntd", N), ("shed_kw", N), ("delta_carbon_kg", N),
        ("approval_mode", T), ("approval_delay_s", I), ("outcome_exceeded", B), ("penalty_avoided_ntd", N),
        ("oc_production_ntd", N), ("carbon_cost_ntd", N), ("r_safety_ntd", N),
    ],
    "decision_candidates": [
        ("candidate_id", I), ("decision_id", I), ("plan_name", T), ("shed_kw", N), ("edf_after", N),
        ("penalty_avoided_ntd", N), ("cost_ntd", N), ("delta_carbon_kg", N), ("esv_ntd", N),
        ("is_locked", B), ("is_chosen", B),
    ],
    "carbon_budget_daily": [
        ("day", D), ("kwh_total", N), ("scope2_kg", N), ("budget_remaining_kg", N), ("sigma_daily_kg", N),
        ("days_to_month_end", I), ("carbon_edf", N), ("avoided_carbon_kg", N), ("avoided_carbon_fee_ntd", N),
    ],
    "esg_waterfall_daily": [("day", D), ("sort_order", I), ("component", T), ("ntd", N)],
}
# Power Query 拆出的顯示用欄位（純欄位拆解，非模型計算）
DERIVED = {
    "power_reading": [("date", D, "DateTime.Date([ts])", "type date"), ("hour", I, "Time.Hour([ts])", "Int64.Type")],
    "interval_settlement": [("day", D, "DateTime.Date([interval_start])", "type date")],
    # 篩選器用的可讀標籤，例如「11/16 20:09｜tier4｜EDF 100%」
    "decision_log": [("decision_label", T,
                      'DateTime.ToText([ts], "MM/dd HH:mm") & "｜" & [tier] & "｜EDF " & Number.ToText([edf_trigger], "P0")',
                      "type text")],
    # 象限圖圖例：選定／安全鎖定／未選
    "decision_candidates": [("plan_status", T,
                             'if [is_chosen] then "選定方案" else if [is_locked] then "安全鎖定（不可選）" else "未選方案"',
                             "type text")],
}
NO_SUM = {"interval_id", "minute_in_interval", "remaining_minutes", "decision_id", "candidate_id", "sort_order",
          "days_to_month_end", "hour", "approval_delay_s"}
PCT = {"edf", "edf_at_5", "edf_at_10", "edf_trigger", "edf_after", "carbon_edf"}
SORT_BY = {("esg_waterfall_daily", "component"): "sort_order"}

M_TYPE = {I: "Int64.Type", N: "type number", DT: "type datetime", D: "type date", T: "type text", B: "type logical"}
TMDL_TYPE = {I: "int64", N: "double", DT: "dateTime", D: "dateTime", T: "string", B: "boolean"}

RELATIONSHIPS = [  # (多端, 一端)
    ("power_reading.interval_id", "interval_settlement.interval_id"),
    ("decision_log.interval_id", "interval_settlement.interval_id"),
    ("decision_candidates.decision_id", "decision_log.decision_id"),
    ("esg_waterfall_daily.day", "carbon_budget_daily.day"),
    ("interval_settlement.day", "carbon_budget_daily.day"),
]

LATEST = "VAR t = CALCULATE ( MAX ( power_reading[ts] ), ALL ( power_reading ) )"
MEASURES: dict[str, list[tuple[str, str, str]]] = {   # 表 → [(名稱, DAX, 格式)]
    "power_reading": [
        ("最新EDF", f"{LATEST}\nRETURN CALCULATE ( MAX ( power_reading[edf] ), ALL ( power_reading ), power_reading[ts] = t )", "0.0%"),
        ("最新剩餘分鐘", f"{LATEST}\nRETURN CALCULATE ( MAX ( power_reading[remaining_minutes] ), ALL ( power_reading ), power_reading[ts] = t )", "0"),
        ("最新Buffer_kW", f"{LATEST}\nRETURN CALCULATE ( MAX ( power_reading[buffer_kw] ), ALL ( power_reading ), power_reading[ts] = t )", "#,0"),
        ("最新區間ID", f"{LATEST}\nRETURN CALCULATE ( MAX ( power_reading[interval_id] ), ALL ( power_reading ), power_reading[ts] = t )", "0"),
        ("最新WBGT", f"{LATEST}\nRETURN CALCULATE ( MAX ( power_reading[wbgt] ), ALL ( power_reading ), power_reading[ts] = t )", "0.0"),
        ("最新製程溫度", f"{LATEST}\nRETURN CALCULATE ( MAX ( power_reading[process_temp] ), ALL ( power_reading ), power_reading[ts] = t )", "0.0"),
        ("最新鎖定狀態", f"{LATEST}\nVAR s = LOOKUPVALUE ( power_reading[locked_status_text], power_reading[ts], t )\nRETURN IF ( s = \"\", \"無鎖定\", s )", ""),
        ("最新區間平均需量", "VAR latestInterval = [最新區間ID]\nRETURN CALCULATE ( AVERAGE ( power_reading[avg_kw_so_far] ), power_reading[interval_id] = latestInterval )", "#,0"),
        ("契約容量", "MAX ( power_reading[contract_kw] )", "#,0"),
        ("近2小時EDF", f"{LATEST}\nRETURN IF ( MAX ( power_reading[ts] ) >= t - 2 / 24, AVERAGE ( power_reading[edf] ) )", "0.0%"),
        ("綠電匹配率", "CALCULATE ( AVERAGE ( power_reading[renewable_pct] ), power_reading[edf] >= 0.5 ) / 100", "0.0%"),
    ],
    "interval_settlement": [
        ("避免罰款累計", "SUM ( interval_settlement[avoided_penalty_ntd] )", "#,0"),
        ("實際罰款累計", "SUM ( interval_settlement[penalty_ntd] )", "#,0"),
        ("預警次數", "CALCULATE ( COUNTROWS ( interval_settlement ), interval_settlement[edf_at_10] >= 0.5 )", "0"),
        ("預警命中數", "CALCULATE ( COUNTROWS ( interval_settlement ), interval_settlement[edf_at_10] >= 0.5, interval_settlement[exceeded] = TRUE () )", "0"),
        ("預警命中率", "DIVIDE ( [預警命中數], [預警次數] )", "0.0%"),
        ("日平均EDF10", "AVERAGE ( interval_settlement[edf_at_10] )", "0.0%"),
    ],
    "decision_log": [
        ("決策次數", "COUNTROWS ( decision_log )", "0"),
        ("人工決策次數", "CALCULATE ( COUNTROWS ( decision_log ), decision_log[approval_mode] = \"manual\" )", "0"),
        ("鎖定觸發次數", "CALCULATE ( COUNTROWS ( decision_log ), LEN ( decision_log[locked_loads] ) > 2 )", "0"),
    ],
    "carbon_budget_daily": [
        ("最新碳EDF", "VAR d = CALCULATE ( MAX ( carbon_budget_daily[day] ), ALL ( carbon_budget_daily ) )\nRETURN CALCULATE ( MAX ( carbon_budget_daily[carbon_edf] ), ALL ( carbon_budget_daily ), carbon_budget_daily[day] = d )", "0.0%"),
        ("最新碳預算剩餘_kg", "VAR d = CALCULATE ( MAX ( carbon_budget_daily[day] ), ALL ( carbon_budget_daily ) )\nRETURN CALCULATE ( MAX ( carbon_budget_daily[budget_remaining_kg] ), ALL ( carbon_budget_daily ), carbon_budget_daily[day] = d )", "#,0"),
        ("累計Scope2_kg", "SUM ( carbon_budget_daily[scope2_kg] )", "#,0"),
        ("累計避免碳排_kg", "SUM ( carbon_budget_daily[avoided_carbon_kg] )", "#,0.0"),
        ("碳強度", "DIVIDE ( SUM ( carbon_budget_daily[scope2_kg] ), SUM ( carbon_budget_daily[kwh_total] ) )", "0.000"),
    ],
}


def m_source(table: str) -> str:
    cols = TABLES[table]
    types = ", ".join(f'{{"{c}", {M_TYPE[t]}}}' for c, t in cols if c != "locked_loads")
    lines = [
        "let",
        f'    Source = MySQL.Database("{SERVER}", "{DATABASE}", [ReturnSingleDatabase = true]),',
        f'    Data = Source{{[Schema = "{DATABASE}", Item = "{table}"]}}[Data],',
        f"    Kept = Table.SelectColumns(Data, {{{', '.join(chr(34) + c + chr(34) for c, _ in cols)}}}),",
    ]
    step = "Kept"
    if any(c == "locked_loads" for c, _ in cols):   # MySQL JSON 欄位可能回傳 binary
        lines.append(f'    Json = Table.TransformColumns({step}, {{{{"locked_loads", each if _ is binary then Text.FromBinary(_) else _, type text}}}}),')
        step = "Json"
    lines.append(f"    Typed = Table.TransformColumnTypes({step}, {{{types}}}),")
    step = "Typed"
    for i, (c, _, expr, mt) in enumerate(DERIVED.get(table, [])):
        lines.append(f'    Add{i} = Table.AddColumn({step}, "{c}", each {expr}, {mt}),')
        step = f"Add{i}"
    lines[-1] = lines[-1].rstrip(",")
    lines += ["in", f"    {step}"]
    return "\n".join(lines)


def q(name: str) -> str:
    return f"'{name}'"


def table_tmdl(table: str) -> str:
    out = [f"table {table}", f"\tlineageTag: {tag(table)}", ""]
    for name, dax, fmt in MEASURES.get(table, []):
        body = dax.split("\n")
        if len(body) == 1:
            out.append(f"\tmeasure {q(name)} = {body[0]}")
        else:
            out.append(f"\tmeasure {q(name)} =")
            out += [f"\t\t\t{x}" for x in body]
        if fmt:
            out.append(f"\t\tformatString: {fmt}")
        out += [f"\t\tlineageTag: {tag(table, 'm', name)}", ""]
    cols = [(c, t) for c, t in TABLES[table]] + [(c, t) for c, t, _, _ in DERIVED.get(table, [])]
    for c, t in cols:
        out += [f"\tcolumn {c}", f"\t\tdataType: {TMDL_TYPE[t]}"]
        if t == DT:
            out.append("\t\tformatString: yyyy-mm-dd hh:nn")
        elif t == D:
            out.append("\t\tformatString: yyyy-mm-dd")
        elif c in PCT:
            out.append("\t\tformatString: 0.0%")
        elif t == N:
            out.append("\t\tformatString: #,0.##")
        out.append(f"\t\tlineageTag: {tag(table, c)}")
        out.append(f"\t\tsummarizeBy: {'sum' if t == N and c not in NO_SUM else 'none'}")
        out.append(f"\t\tsourceColumn: {c}")
        if (table, c) in SORT_BY:
            out.append(f"\t\tsortByColumn: {SORT_BY[(table, c)]}")
        out.append("")
    out += [f"\tpartition {table} = m", "\t\tmode: import", "\t\tsource ="]
    out += [f"\t\t\t\t{x}" for x in m_source(table).split("\n")]
    out.append("")
    return "\n".join(out)


def write_model(root: Path):
    d = root / "definition"
    (d / "tables").mkdir(parents=True)
    (root / "definition.pbism").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "settings": {}}, indent=2), encoding="utf-8")
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1567\n", encoding="utf-8")
    order = json.dumps(list(TABLES), ensure_ascii=False)
    model = ["model Model", "\tculture: zh-TW", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
             "\tsourceQueryCulture: zh-TW", "\tdataAccessOptions", "\t\tlegacyRedirects", "\t\treturnErrorValuesAsNull", "",
             f"\tannotation PBI_QueryOrder = {order}", "", "\tannotation __PBI_TimeIntelligenceEnabled = 0", ""]
    model += [f"ref table {t}" for t in TABLES]
    (d / "model.tmdl").write_text("\n".join(model) + "\n", encoding="utf-8")
    rel = []
    for f, t in RELATIONSHIPS:
        rel += [f"relationship {tag('rel', f, t)}", f"\tfromColumn: {f}", f"\ttoColumn: {t}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel), encoding="utf-8")
    for t in TABLES:
        (d / "tables" / f"{t}.tmdl").write_text(table_tmdl(t), encoding="utf-8")


# ------------------------------------------------------------------ 報表
AGG = {"sum": (0, "Sum"), "avg": (1, "Avg"), "min": (3, "Min"), "max": (4, "Max")}


def lit(v) -> dict:
    if isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, (int, float)):
        s = f"{v}D"
    else:
        s = "'" + str(v).replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": s}}}


class Visual:
    """fields：role → ['表.欄位'（欄）、'表.[量值]'、'avg:表.欄位'（彙總）]"""

    def __init__(self, vtype, x, y, w, h, fields, title=None, objects=None, order_desc=None):
        self.vtype, self.pos, self.fields, self.title = vtype, (x, y, w, h), fields, title
        self.objects, self.order_desc = objects or {}, order_desc

    def config(self, idx: int, page: int) -> dict:
        aliases, select, proj = {}, [], {}

        def src(entity):
            if entity not in aliases:
                aliases[entity] = f"t{len(aliases)}"
            return {"SourceRef": {"Source": aliases[entity]}}

        def item(spec):
            agg = None
            if ":" in spec:
                agg, spec = spec.split(":", 1)
            entity, prop = spec.split(".", 1)
            if prop.startswith("["):
                prop = prop[1:-1]
                return {"Measure": {"Expression": src(entity), "Property": prop}, "Name": f"{entity}.{prop}"}
            colref = {"Column": {"Expression": src(entity), "Property": prop}}
            if agg:
                fn, fname = AGG[agg]
                return {"Aggregation": {"Expression": colref, "Function": fn}, "Name": f"{fname}({entity}.{prop})"}
            return {**colref, "Name": f"{entity}.{prop}"}

        for role, specs in self.fields.items():
            proj[role] = []
            for s in specs:
                it = item(s)
                if it["Name"] not in {x["Name"] for x in select}:
                    select.append(it)
                proj[role].append({"queryRef": it["Name"]})
        query = {"Version": 2, "From": [{"Name": a, "Entity": e, "Type": 0} for e, a in aliases.items()],
                 "Select": select}
        if self.order_desc:
            e, p = self.order_desc.split(".", 1)
            query["OrderBy"] = [{"Direction": 2, "Expression": {"Column": {"Expression": src(e), "Property": p}}}]
            query["From"] = [{"Name": a, "Entity": e2, "Type": 0} for e2, a in aliases.items()]
        x, y, w, h = self.pos
        sv = {"visualType": self.vtype, "projections": proj, "prototypeQuery": query,
              "drillFilterOtherVisuals": True}
        if self.objects:
            sv["objects"] = {k: [{"properties": {pk: lit(pv) for pk, pv in props.items()}}]
                             for k, props in self.objects.items()}
        if self.title:
            sv["vcObjects"] = {"title": [{"properties": {"show": lit(True), "text": lit(self.title)}}]}
        return {"name": f"p{page}v{idx:02d}{tag('v', str(page), str(idx))[:8]}",
                "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": idx, "width": w, "height": h}}],
                "singleVisual": sv}


class TextBox(Visual):
    def __init__(self, x, y, w, h, text):
        super().__init__("textbox", x, y, w, h, {})
        self.text = text

    def config(self, idx: int, page: int) -> dict:
        x, y, w, h = self.pos
        runs = [{"textRuns": [{"value": line or " ", "textStyle": {"fontSize": "10pt", "color": "#52514e"}}]}
                for line in self.text.split("\n")]
        return {"name": f"p{page}v{idx:02d}{tag('v', str(page), str(idx))[:8]}",
                "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": idx, "width": w, "height": h}}],
                "singleVisual": {"visualType": "textbox", "drillFilterOtherVisuals": True,
                                 "objects": {"general": [{"properties": {"paragraphs": runs}}]}}}


PR, IS, DL, DC, CB, EW = ("power_reading", "interval_settlement", "decision_log", "decision_candidates",
                          "carbon_budget_daily", "esg_waterfall_daily")
LIST_SLICER = {"data": {"mode": "Basic"}, "header": {"show": False}}   # 標題已說明，不再顯示英文欄位名
DROPDOWN = {"data": {"mode": "Dropdown"}, "header": {"show": False}}
CARD = {"labels": {"labelDisplayUnits": 1}}            # 顯示完整數字，不縮成「百萬」
CAT_AXIS = {"categoryAxis": {"axisType": "Categorical"}}  # 日期／秒數當類別，一格一個

PAGES = [
    ("即時超約戰情", [
        Visual("card", 20, 20, 200, 130, {"Values": [f"{PR}.[最新剩餘分鐘]"]}, "距結算（分鐘）", CARD),
        Visual("gauge", 240, 20, 320, 200, {"Y": [f"{PR}.[最新EDF]"]}, "當下 EDF（超約機率）",
               {"axis": {"min": 0, "max": 1}}),
        Visual("card", 20, 160, 200, 130, {"Values": [f"{PR}.[最新Buffer_kW]"]}, "剩餘配額 kW（負值＝預估超約）", CARD),
        Visual("multiRowCard", 580, 20, 680, 200,
               {"Values": [f"{PR}.[最新鎖定狀態]", f"{PR}.[最新WBGT]", f"{PR}.[最新製程溫度]"]},
               "安全鎖定狀態（WBGT ≥ 28 鎖冰水主機；製程溫度 < 82 鎖馬達）"),
        Visual("lineChart", 240, 240, 500, 460,
               {"Category": [f"{PR}.minute_in_interval"], "Y": [f"{PR}.[最新區間平均需量]", f"{PR}.[契約容量]"]},
               "本結算區間需量累積 vs 契約容量"),
        Visual("lineChart", 760, 240, 500, 460, {"Category": [f"{PR}.ts"], "Y": [f"{PR}.[近2小時EDF]"]},
               "近 2 小時 EDF"),
    ]),
    ("決策品質", [
        Visual("slicer", 20, 20, 200, 110, {"Values": [f"{DL}.decision_label"]},
               "挑一次決策（時間｜分級｜EDF）", DROPDOWN, order_desc=f"{DL}.decision_label"),
        TextBox(20, 135, 200, 110, "選一次決策 → 象限圖只顯示\n該次的候選方案（最多 7 個）。\n\n不選＝顯示所有決策。"),
        Visual("slicer", 20, 255, 200, 190, {"Values": [f"{DL}.tier"]}, "風險分級（tier2 低 → tier4 高）", LIST_SLICER),
        Visual("slicer", 20, 455, 200, 130, {"Values": [f"{DL}.approval_mode"]}, "核准方式（auto 自動／manual 人工）", LIST_SLICER),
        Visual("scatterChart", 240, 20, 520, 340,
               {"Category": [f"{DC}.candidate_id"], "Series": [f"{DC}.plan_status"],
                "X": [f"sum:{DC}.cost_ntd"], "Y": [f"sum:{DC}.delta_carbon_kg"]},
               "方案象限圖（X 成本 NTD；Y 碳排變化 kg，負值＝減碳）"),
        Visual("card", 780, 20, 230, 110, {"Values": [f"{IS}.[預警命中率]"]}, "預警命中率（引擎介入後）"),
        Visual("card", 1030, 20, 230, 110, {"Values": [f"{IS}.[避免罰款累計]"]}, "避免罰款累計 NTD", CARD),
        Visual("clusteredColumnChart", 780, 140, 480, 220,
               {"Category": [f"{IS}.day"], "Y": [f"{IS}.[避免罰款累計]", f"{IS}.[實際罰款累計]"]},
               "每日避免罰款 vs 實際罰款（NTD）", CAT_AXIS),
        Visual("tableEx", 240, 380, 640, 320,
               {"Values": [f"{DL}.ts", f"{DL}.tier", f"{DL}.chosen_plan", f"sum:{DL}.edf_trigger",
                           f"sum:{DL}.esv_ntd", f"sum:{DL}.shed_kw", f"sum:{DL}.delta_carbon_kg",
                           f"{DL}.approval_mode", f"{DL}.outcome_exceeded", f"{DL}.locked_loads"]},
               "決策日誌", {"total": {"totals": False}}, order_desc=f"{DL}.ts"),
        Visual("clusteredColumnChart", 900, 380, 360, 320,
               {"Category": [f"{DL}.approval_delay_s"], "Y": [f"{DL}.[人工決策次數]"]},
               "人工確認延遲分布（tier3，秒）", CAT_AXIS),
    ]),
    ("ESG 累計成效", [
        Visual("hundredPercentStackedBarChart", 20, 20, 600, 150,
               {"Y": [f"{CB}.[累計Scope2_kg]", f"{CB}.[最新碳預算剩餘_kg]"]}, "碳預算消耗（7 天迷你月）"),
        Visual("card", 640, 20, 200, 150, {"Values": [f"{CB}.[最新碳EDF]"]}, "碳 EDF（超預算機率）"),
        Visual("card", 860, 20, 190, 150, {"Values": [f"{DL}.[鎖定觸發次數]"]}, "安全鎖定觸發次數", CARD),
        Visual("card", 1070, 20, 190, 150, {"Values": [f"{PR}.[綠電匹配率]"]}, "綠電匹配率（高 EDF 時段）"),
        Visual("waterfallChart", 20, 190, 600, 260,
               {"Category": [f"{EW}.component"], "Y": [f"sum:{EW}.ntd"]}, "ESG 貢獻瀑布圖（NTD，總計＝淨 ESV）"),
        Visual("clusteredColumnChart", 640, 190, 300, 260,
               {"Category": [f"{CB}.day"], "Y": [f"{IS}.[日平均EDF10]"]}, "每日平均 EDF（第 10 分鐘）", CAT_AXIS),
        Visual("lineChart", 960, 190, 300, 260,
               {"Category": [f"{CB}.day"], "Y": [f"{CB}.[碳強度]"]}, "每日碳強度 kgCO2e/kWh", CAT_AXIS),
        Visual("pivotTable", 20, 470, 620, 230,
               {"Rows": [f"{PR}.date"], "Columns": [f"{PR}.hour"], "Values": [f"avg:{PR}.load_kw"]},
               "平均負載 kW（日 × 小時）"),
        Visual("pivotTable", 660, 470, 600, 230,
               {"Rows": [f"{PR}.date"], "Columns": [f"{PR}.hour"], "Values": [f"avg:{PR}.grid_ef"]},
               "平均排碳係數 kgCO2e/kWh（日 × 小時）"),
    ]),
]


THEME_FILE = "KMV_ESG_theme.json"
INK, INK2, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"
THEME = {
    "name": "KMV ESG",
    # 已驗證的類別色順序（藍、橘、青、黃…）：固定順序，不循環
    "dataColors": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "background": SURFACE, "foreground": INK, "tableAccent": "#2a78d6",
    "good": "#0ca30c", "neutral": "#fab219", "bad": "#d03b3b",
    "minimum": "#cde2fb", "center": "#6da7ec", "maximum": "#184f95",   # 單一藍色階（矩陣色階用）
    "textClasses": {
        "title": {"color": INK, "fontSize": 12},
        "header": {"color": INK, "fontSize": 12},
        "label": {"color": INK2, "fontSize": 10},
        "callout": {"color": INK, "fontSize": 28},
    },
    "visualStyles": {
        "*": {"*": {
            "title": [{"show": True, "fontColor": {"solid": {"color": INK}}, "fontSize": 12, "bold": True}],
            "background": [{"show": True, "color": {"solid": {"color": "#ffffff"}}}],
            "border": [{"show": True, "color": {"solid": {"color": "#e4e3df"}}, "radius": 6}],
            "valueAxis": [{"gridlineColor": {"solid": {"color": "#e4e3df"}}, "labelColor": {"solid": {"color": INK2}}}],
            "categoryAxis": [{"labelColor": {"solid": {"color": INK2}}}],
            "legend": [{"labelColor": {"solid": {"color": INK2}}}],
        }},
        "page": {"*": {"background": [{"color": {"solid": {"color": "#f4f3f0"}}, "transparency": 0}]}},
        "waterfallChart": {"*": {"sentimentColors": [{
            "increaseFill": {"solid": {"color": "#2a78d6"}},
            "decreaseFill": {"solid": {"color": "#e34948"}},
            "totalFill": {"solid": {"color": "#52514e"}}}]}},
        "card": {"*": {"labels": [{"color": {"solid": {"color": INK}}}],
                       "categoryLabels": [{"color": {"solid": {"color": INK2}}}]}},
    },
}


def write_report(root: Path):
    root.mkdir(parents=True)
    res = root / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    (res / THEME_FILE).write_text(json.dumps(THEME, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / THEME_FILE).write_text(json.dumps(THEME, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "definition.pbir").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}}, indent=2),
        encoding="utf-8")
    sections = []
    for p, (title, visuals) in enumerate(PAGES):
        containers = []
        for i, v in enumerate(visuals):
            x, y, w, h = v.pos
            containers.append({"config": json.dumps(v.config(i, p), ensure_ascii=False), "filters": "[]",
                               "height": h, "width": w, "x": x, "y": y, "z": i})
        sections.append({"config": "{}", "displayName": title, "displayOption": 1, "filters": "[]",
                         "height": 720, "width": 1280, "name": f"ReportSection{p}", "ordinal": p,
                         "visualContainers": containers})
    report = {"config": json.dumps({"version": "5.43", "activeSectionIndex": 0, "themeCollection": {
                  "customTheme": {"name": THEME_FILE, "version": "5.43", "type": 1}}}),
              "layoutOptimization": 0,
              "resourcePackages": [{"resourcePackage": {"disabled": False, "name": "RegisteredResources", "type": 1,
                                    "items": [{"name": THEME_FILE, "path": THEME_FILE, "type": 201}]}}],
              "sections": sections}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    for sub in (f"{NAME}.SemanticModel", f"{NAME}.Report"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    write_model(OUT / f"{NAME}.SemanticModel")
    write_report(OUT / f"{NAME}.Report")
    (OUT / f"{NAME}.pbip").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True}}, indent=2), encoding="utf-8")
    print(f"已產生 {OUT / (NAME + '.pbip')}")


if __name__ == "__main__":
    main()
