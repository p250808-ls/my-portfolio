#zeabur
#2026/12/1 起共享叢集服務將暫停、專用伺服器停止續費，12/31 全面停止。
#請儘早備份與遷移。遷移指南等資訊將陸續更新於公告。

#此課堂作業將會連不上資料庫
#PowerBI有版本問題，家中版本版本: 2.159.883.0 64-bit (2026年10月)可以運行，學校沒辦法



# KMV 尖峰電力超約預警 × ESG 決策系統

Python 模擬連續型製程負載 → 每 60 秒計算 KMV 違約機率（EDF）與 ESG 決策（ESV）→ 寫入 Zeabur PostgreSQL（TT1）→ Power BI 只讀取與視覺化。
所有模型邏輯在 Python；資料庫無 view／trigger；Power BI 無模型 DAX。

## 快速開始

```bash
pip install -r requirements.txt
cp .env.example .env            # 填入 DATABASE_URL=postgresql://user:pwd@host:port/db
python main.py --init-db        # 建五張表（DROP + CREATE）
python main.py                  # 真實時間：每 60 秒一筆
```

課堂展示想立刻有 7 天資料可看：

```bash
python main.py --init-db
python main.py --speed 0 --days 7 --start 2026-10-14T00:00   # 本機 PostgreSQL 約 20 秒；遠端依網路延遲約 10～30 分鐘
python analyze.py                                            # 摘要、決策範例、「降載反而增排」案例
```

然後再切回 `python main.py` 讓即時資料繼續累積（會沿用資料表中第一筆 ts 作為 7 天週期起點）。

## 檔案

| 檔案 | 內容 |
| --- | --- |
| `config.py` | 全部參數：契約容量、負載成分、三個負載清單、門檻、碳價、重置週期 |
| `simulator.py` | 負載四成分、grid_ef／renewable_pct／WBGT／製程溫度、安全鎖定判斷 |
| `kmv.py` | 短週期 EDF（σ_avg = σ_kw·√(15−t)/15）、罰款期望值、長週期 carbon_edf、P_eff |
| `decision.py` | 7 種組合的 ESV、安全鎖定過濾、四級分級、人工確認延遲 |
| `engine.py` | 每分鐘流程：模擬 → KMV → 決策 → 結算 → 碳預算 → 7 天重置 |
| `writer.py` | `PostgresWriter`（psycopg2）與 `MemoryWriter`（校準／回測用） |
| `main.py` | 排程 CLI（真實時間／加速／dry-run／引擎開關） |
| `calibrate.py` | 6.1 節校準：引擎關閉，只調尖峰幅度到超約比例 8%～12% |
| `backtest.py` | 加分項：edf_at_10 vs exceeded 命中率、Brier、σ 視窗 15/30/60 比較 |
| `analyze.py` | 從資料庫產出書面說明用的數字 |
| `sql/schema.sql` | 五張表 DDL |
| `POWERBI_GUIDE.md` | Power BI 三頁面建置指引（連線、欄位綁定、顯示用 DAX、驗證 SQL） |
| `DEPLOY_ZEABUR.md` | Zeabur 部署步驟（PostgreSQL 服務 + Python worker、Dockerfile） |
| `Dockerfile` / `zbpack.json` | Zeabur 建置設定；首次啟動自動建表 |

## 常用指令

```bash
python main.py --dry-run --days 1            # 不接資料庫，印摘要
python main.py --engine off --hours 2        # 引擎關閉（校準用）
python calibrate.py --hours 48 --auto        # 自動搜尋尖峰幅度
python backtest.py --days 7 --windows 15 30 60
python analyze.py --decision 1               # 印出某次決策的完整候選表
```

## 已校準的參數與重要發現

- 規格初值尖峰 20%～30% 只得 0.3% 超約；依校準規則（只動尖峰幅度，每次 ±5%）調到 **45%～55%** 得 8.3%（72 小時、引擎關閉）。
- 碳預算 = 基準 × 0.95，但降載一週只省約 0.1% 排放，`carbon_edf` 從第一天就是 1.0。要在頁面 3 看到 EDF 變化，把 `CARBON_TARGET_RATIO` 改為 0.99～1.0；0.95 保留為對外承諾口徑。
- 長週期 DD 加入了漂移項：`DD = (budget_remaining − μ_daily × 剩餘天數) / (σ_daily × √剩餘天數)`，否則 EDF 在最後幾小時前永遠是 0。
- 「避免罰款」改用罰款期望值差 `E[penalty_before] − E[penalty_after]`；C_penalty 視為常數時即為規格的 `(EDF_before − EDF_after) × C_penalty`，但在 EDF ≈ 1 的 tier4 仍能反映少超 kW 的實際效益。
- 回測：σ 視窗 15/30/60 的 Brier 幾乎相同（0.059～0.060）；主要誤差來自「剩餘分鐘維持當前負載」的外推假設 —— 尖峰常在結算前結束，EDF 95%～100% 的區間實際只有 70% 超約。

## 重啟與重置

- 真實時間模式重啟時，以 `power_reading` 第一筆 ts 為週期起點，`interval_id` 連續；重啟落在區間中段時，缺少的分鐘以當前負載補齊。
- 滿 7 天：Python 重跑 `schema.sql` 清空重建，`interval_id`／`decision_id` 自 1 起算。
- 加速模式（`--speed 0` 或 `--start`）總是先重建五張表。
