# tools：安裝檔

## MySQL Connector/NET（開啟 Power BI 報表必裝）

把安裝檔放在這個資料夾，檔名維持官方原名，例如：

```
tools/mysql-connector-net-9.1.0.msi
```

使用者雙擊安裝（選 **Typical**），裝完重開 Power BI Desktop。

- 官方下載：<https://dev.mysql.com/downloads/connector/net/> → Windows (x86, 64-bit), MSI Installer
- 若新版安裝後連線出現「找不到要求的值 'None'」，是因為沒勾「使用加密連線」；勾選即可。
