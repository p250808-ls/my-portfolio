# 部署到 Railway（共用 MySQL）

`kmv-worker` 放在 Railway 專案 **victorious-amazement**，與 breakfast-app 共用同一個 MySQL 服務，但使用**獨立資料庫 `kmv_esg` 與專用帳號 `kmv`**（只對 `kmv_esg.*` 有權限）。worker 每 7 天會 DROP + CREATE 自己的六張表，不會碰到其他專案的資料庫。

```
GitHub my-portfolio/數位儀表板-mysql ──► Railway kmv-worker（Dockerfile，python main.py）
                      │ DATABASE_URL = mysql://kmv:${{KMV_DB_PASSWORD}}@${{MySQL.RAILWAY_PRIVATE_DOMAIN}}:3306/kmv_esg（內網）
                      ▼
               Railway MySQL（共用）── 資料庫：railway / breakfast / kmv_esg / …
                      │
                      └─► TCP Proxy tokaido.proxy.rlwy.net:18473 ──► Power BI Desktop
```

## 1. 目前設定

| 項目 | 值 |
| --- | --- |
| 專案 | `victorious-amazement`（production） |
| MySQL | 既有的 `MySQL` 服務（mysql:8），對外 TCP Proxy `tokaido.proxy.rlwy.net:18473` |
| 資料庫／帳號 | `kmv_esg`／`kmv`（密碼存在 kmv-worker 的 `KMV_DB_PASSWORD` 變數） |
| kmv-worker 來源 | GitHub `p250808-ls/my-portfolio`，`main` 分支，Root Directory `/數位儀表板-mysql` |
| 自動部署範圍 | Watch Paths `/數位儀表板-mysql/**`：只有這個資料夾有變動才重新部署，作品集其他資料夾不影響 |
| kmv-worker 變數 | `KMV_DB_PASSWORD`、`DATABASE_URL=mysql://kmv:${{KMV_DB_PASSWORD}}@${{MySQL.RAILWAY_PRIVATE_DOMAIN}}:3306/kmv_esg`、`TZ=Asia/Taipei` |
| 建置／重啟 | 直接設在 Railway 服務 Settings：Dockerfile Path `Dockerfile`、Restart Policy `Always`（不使用 railway.json，Railway 已淘汰該設定方式） |

程式依 `DATABASE_URL` 的開頭自動選擇：`mysql://` → `MySQLWriter`（PyMySQL、`sql/schema_mysql.sql`）；`postgresql://` → 原本的 `PostgresWriter`。

worker 不開 HTTP 埠，不需要 Domain；**不要開 App Sleeping**。首次啟動自動建表，Deploy Logs：

```
資料表不存在，已自動建表
契約容量 10,000 kW，7 天碳基準 667.9 t，預算 634.5 t（×0.95）；引擎 on
```

重啟後會看到 `沿用既有週期起點 YYYY-MM-DD HH:MM`。

## 2. MySQL 與 PostgreSQL 版本的差異

| 欄位 | PostgreSQL | MySQL |
| --- | --- | --- |
| `ts`、`interval_start` | timestamptz | DATETIME，存**台北當地時間** |
| `locked_loads` | text[]（`{...}`） | JSON（`[...]`，空清單 `[]`，長度一樣是 2） |
| `candidate_json` | jsonb | JSON |
| 布林欄位 | boolean | BOOLEAN（= TINYINT(1)，0/1） |
| 自動編號 | serial | AUTO_INCREMENT |

## 3. 新專案共用這個 MySQL

每個專案各開一個資料庫 + 專用帳號（用 root 連上公開 Proxy 後執行）：

```sql
CREATE DATABASE myapp CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myapp'@'%' IDENTIFIED BY '<強密碼>';
GRANT ALL PRIVILEGES ON myapp.* TO 'myapp'@'%';
```

同專案內的服務用 `${{MySQL.RAILWAY_PRIVATE_DOMAIN}}:3306`（內網，不算流量）；其他 Railway 專案或外部只能走 `tokaido.proxy.rlwy.net:18473`。

## 4. 本機灌 7 天資料

```bash
export DATABASE_URL="mysql://kmv:<密碼>@tokaido.proxy.rlwy.net:18473/kmv_esg"
python main.py --speed 0 --days 7 --start 2026-10-07T00:00
```

跑完到 Railway 對 kmv-worker 按 **Redeploy**。

## 5. 改參數

kmv-worker → Variables 可覆寫 `CONTRACT_KW`、`SPIKE_AMP_LOW`、`SPIKE_AMP_HIGH`。其他參數改 `config.py` 後 push 即自動部署。

## 6. 常見狀況

| 狀況 | 處理 |
| --- | --- |
| `DATABASE_URL 未設定` | kmv-worker Variables 缺 `DATABASE_URL` |
| `invalid dsn: missing "="` | 部署到不支援 MySQL 的舊版程式；確認服務分支有 `sql/schema_mysql.sql` |
| `Access denied for user 'kmv'` | 密碼不符，或連到 `railway` 資料庫而不是 `kmv_esg` |
| 換密碼 | 先在 MySQL `ALTER USER 'kmv'@'%' IDENTIFIED BY '<新密碼>'`，再改 `KMV_DB_PASSWORD` 並重新部署 |
| 本機 `Name or service not known ... railway.internal` | 本機要用公開的 `tokaido.proxy.rlwy.net:18473` |
| Power BI 時間差 8 小時 | MySQL 存的已是台北時間，不要再做時區轉換 |
