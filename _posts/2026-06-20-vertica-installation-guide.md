---
layout: post
title: "Vertica 完整安裝手冊 — 從系統需求到 VMart 範例資料庫"
date: 2026-06-20 18:00:00 +0800
categories: vertica installation deployment
tags: [vertica, installation, deployment, VMart, admin, setup]
description: "從零開始安裝 Vertica 25.x 的完整指南 — 涵蓋系統需求、前置準備、安裝檢查、單節點安裝與資料庫建立、安裝後驗證，以及匯入 VMart 範例資料庫的逐步操作。"
---

本手涵蓋 Vertica 25.x 在 Linux 上的完整安裝流程，從系統需求到 VMart 範例資料庫匯入，以 **Ubuntu 26.04** 與 **Vertica 25.4.0-0** 為基礎實測撰寫。

<!--more-->

---

## 一、系統需求

### 支援的作業系統

Vertica 25.x 支援以下 Linux 發行版（以 Vertica 26.1 文件為準）：

| 發行版 | 版本 | 架構 |
|--------|------|------|
| **RHEL / Rocky / AlmaLinux** | 8.x, 9.x | x86_64 |
| **Ubuntu** | 22.04 LTS, 24.04 LTS, 26.04 LTS | x86_64 |
| **Debian** | 11, 12 | x86_64 |
| **SLES** | 15 | x86_64 |

> **注意**：Vertica 官方對 Ubuntu 26.04 的支援始於 24.4.0 版本。若使用較舊的 Vertica 版本，請確認 OS 相容性。

### 硬體需求

| 資源 | 最低要求 | 建議 |
|------|---------|------|
| **CPU** | 1 core (測試用) | 16+ cores (生產環境) |
| **RAM** | 4 GB (測試用) | 64 GB+ (依資料量) |
| **磁碟空間** | 2 GB (軟體) + 20 GB (資料) | SSD/NVMe，資料目錄至少預留 40% 可用空間 |
| **網路** | 1 Gbps | 10 Gbps+ (跨節點) |

### 本教學的實際環境

```
OS:      Ubuntu 26.04 LTS
CPU:     6 cores
RAM:     30 GB
Disk:    880 GB (SSD)
Vertica: 25.4.0-0 (Community Edition)
Mode:    Enterprise Mode (單節點)
```

---

## 二、前置準備

### 2.1 建立 dbadmin 使用者

Vertica 需要一個專用的管理帳號（通常為 `dbadmin`）來執行安裝與管理：

```bash
# 建立 dbadmin 使用者
$ sudo useradd -m -d /home/dbadmin -s /bin/bash dbadmin

# 設定密碼
$ sudo passwd dbadmin

# 授予 sudo 權限 (安裝時需要)
$ sudo usermod -aG sudo dbadmin
```

### 2.2 系統核心參數調校

編輯 `/etc/sysctl.conf` 或 `/etc/sysctl.d/99-vertica.conf`：

```conf
# Vertica 建議的核心參數
vm.swappiness = 0
vm.overcommit_memory = 0
vm.max_map_count = 65536
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.core.netdev_max_backlog = 10000
```

立即生效：

```bash
$ sudo sysctl -p /etc/sysctl.d/99-vertica.conf
```

### 2.3 檔案描述符限制

編輯 `/etc/security/limits.d/99-vertica.conf`：

```conf
# Vertica 建議的檔案描述符限制
dbadmin           soft    nofile          65536
dbadmin           hard    nofile          65536
dbadmin           soft    nproc           65536
dbadmin           hard    nproc           65536
dbadmin           soft    memlock         unlimited
dbadmin           hard    memlock         unlimited
```

### 2.4 防火牆設定

確認 Vertica 使用的連接埠（預設 5433）已開啟：

```bash
# 若使用 ufw
$ sudo ufw allow 5433/tcp

# 若使用 firewalld
$ sudo firewall-cmd --permanent --add-port=5433/tcp
$ sudo firewall-cmd --reload
```

### 2.5 確認安裝套件

Vertica 提供 `.rpm`（RHEL 系）和 `.deb`（Debian/Ubuntu 系）兩種安裝包。

本教學使用 Vertica 25.4.0-0 Community Edition：

```bash
$ ls -lh /home/achi/vertica_25.4.0-0_amd64.deb
-rwx------ 1 achi achi 660M Jun 14 17:04 vertica_25.4.0-0_amd64.deb
```

---

## 三、安裝檢查

在執行安裝前，先進行以下檢查：

### 3.1 確認 OS 版本

```bash
$ cat /etc/os-release
PRETTY_NAME="Ubuntu 26.04 LTS"
VERSION_ID="26.04"
```

### 3.2 確認資源充足

```bash
# CPU
$ nproc
6

# 記憶體
$ free -h
Mem: 30Gi

# 磁碟
$ df -h /
Filesystem  Size  Used Avail Use%
/dev/sda2   880G  79G  756G  10%
```

### 3.3 確認 dbadmin 使用者

```bash
$ getent passwd dbadmin
dbadmin:x:1001:1001:,,,:/home/dbadmin:/bin/bash
```

### 3.4 確認安裝套件完整性

```bash
$ dpkg-deb --info vertica_25.4.0-0_amd64.deb | grep -E 'Package|Version|Architecture'
 Package: vertica
 Version: 25.4.0-0
 Architecture: amd64
```

---

## 四、安裝與建立資料庫

### 4.1 安裝 Vertica 軟體

使用 `dbadmin` 使用者執行：

```bash
# 切換到 dbadmin
$ sudo -i -u dbadmin
$ cd ~

# 安裝 Vertica .deb 套件
$ sudo dpkg -i /path/to/vertica_25.4.0-0_amd64.deb
```

安裝過程會：
1. 將 Vertica 部署到 `/opt/vertica/`
2. 安裝 `admintools` 命令列工具
3. 安裝 `vsql` 資料庫客戶端
4. 安裝 Vertica SDK 與 libraries

> **Community Edition 限制**：免費版最多 3 個節點、1TB 資料。

### 4.2 驗證安裝

```bash
# 確認版本
$ /opt/vertica/bin/vsql --version
vsql (Vertica) 25.4.0-0

# 確認 admintools
$ /opt/vertica/bin/admintools --version
```

### 4.3 安裝 Vertica 到節點

Vertica 需要執行 `install_vertica` 腳本來初始化節點：

```bash
# 單節點安裝
$ sudo /opt/vertica/sbin/install_vertica \
    --license /opt/vertica/config/license/ce/license.dat \
    --hosts 127.0.0.1 \
    --dbadmin-user dbadmin \
    --dba-user-password <密碼> \
    --data-dir /home/dbadmin \
    --failure-threshold HALT \
    --timeout 600
```

關鍵參數說明：

| 參數 | 說明 |
|------|------|
| `--license` | 授權檔案路徑（CE 版在 `config/license/ce/`） |
| `--hosts` | 節點 IP 或主機名清單（逗號分隔） |
| `--dbadmin-user` | Vertica 管理帳號 |
| `--data-dir` | 資料存放目錄 |
| `--failure-threshold` | 失敗處理方式（HALT / FAIL） |

### 4.4 建立資料庫

使用 `admintools` 建立資料庫：

```bash
# 建立資料庫
$ /opt/vertica/bin/admintools -t create_db \
    --database testdb \
    --hosts 127.0.0.1 \
    --password 'dbadmin密碼' \
    --communal-storage default
```

參數說明：

| 參數 | 說明 |
|------|------|
| `-t create_db` | 建立資料庫 |
| `--database` | 資料庫名稱 |
| `--hosts` | 節點清單 |
| `--password` | dbadmin 密碼 |
| `--communal-storage` | `default` (Enterprise Mode) 或 `s3://...` (Eon Mode) |

### 4.5 確認資料庫狀態

```bash
# 查看叢集狀態
$ /opt/vertica/bin/admintools -t view_cluster

# 預期輸出
DB     | Hosts   |  State
testdb | 192.168.100.9 | UP
```

### 4.6 使用 vsql 連接

```bash
# 以 dbadmin 連線
$ /opt/vertica/bin/vsql -d testdb -w '密碼'

# 或使用作業系統認證
$ sudo -u dbadmin /opt/vertica/bin/vsql -d testdb
```

建立後即可執行 SQL：

```sql
=> SELECT version();
          version
─────────────────────────────
 Vertica Analytic Database v25.4.0-0

=> SELECT node_name, node_state FROM v_catalog.nodes;
    node_name    | node_state
─────────────────┼────────────
 v_testdb_node0001 | UP
```

---

## 五、安裝後檢查

### 5.1 確認節點狀態

```sql
=> SELECT node_name, node_state, node_address
   FROM v_catalog.nodes;
```

### 5.2 確認授權狀態

```sql
=> SELECT license_name, license_type, end_date, node_count, size_limit
   FROM v_catalog.licenses;
```

### 5.3 確認系統資源池

```sql
=> SELECT name, memorysize, maxmemorysize, plannedconcurrency,
          maxconcurrency, executionparallelism, priority
   FROM resource_pools
   ORDER BY name;
```

### 5.4 確認資料庫版本與配置

```sql
=> SELECT version();
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name IN (
     'EnableApportionLoad',
     'EnableCooperativeParse',
     'CompressNetworkData'
   );
```

### 5.5 檢查 ROS 儲存狀態

```sql
=> SELECT SUM(used_bytes) / (1024^3) AS database_size_gb
   FROM v_monitor.projection_storage;
```

### 5.6 基本連線測試

```bash
# 從本機測試
$ /opt/vertica/bin/vsql -d testdb -w '密碼' -c "SELECT 1 AS test;"
 test
──────
    1

# 從遠端測試
$ /opt/vertica/bin/vsql -h 192.168.100.9 -p 5433 -d testdb -w '密碼' -c "SELECT 'OK' AS connection_test;"
```

---

## 六、匯入 VMart 範例資料庫

Vertica 提供了 **VMart** 範例資料庫，包含零售業的完整資料模型，非常適合用來測試和學習。

### 6.1 VMart 資料模型

VMart 包含三個 Schema：

| Schema | 說明 |
|--------|------|
| **public** | 一般維度表（Customer, Product, Date 等） |
| **store** | 實體店面銷售（Store_Sales_Fact, Store_Orders_Fact） |
| **online_sales** | 線上銷售（Online_Sales_Fact, Online_Page_Dimension） |

### 6.2 產生範例資料

VMart 的範例資料產生器位於 `/opt/vertica/examples/VMart_Schema/`：

```bash
$ cd /tmp
$ mkdir vmart_data && cd vmart_data

# 複製 schema 與產生器
$ cp /opt/vertica/examples/VMart_Schema/* .

# 執行資料產生器 (產生 500 萬筆銷售資料)
$ ./vmart_gen

# 預期輸出
Using default parameters
datadirectory = ./
numfiles = 1
seed = 20177
... (產生約 500MB 的 .tbl 檔案)
```

產生的檔案：

```bash
$ ls -lh *.tbl | head -10
-rw-rw-r-- 1 dbadmin dbadmin 1.2M Date_Dimension.tbl
-rw-rw-r-- 1 dbadmin dbadmin 2.1M Product_Dimension.tbl
-rw-rw-r-- 1 dbadmin dbadmin 204M Store_Sales_Fact.tbl
... (共約 500MB)
```

> **注意**：`vmart_gen` 使用預設種子（seed=20177），每次產生相同的資料以確保可重複性。可透過 `--seed` 參數變更。

### 6.3 建立 Schema 與資料表

```bash
# 使用 vsql 執行 schema 定義
$ /opt/vertica/bin/vsql -d testdb -w '密碼' -f vmart_define_schema.sql
```

此腳本會建立：
- `store` schema（Store_Sales_Fact, Store_Orders_Fact 等）
- `online_sales` schema（Online_Sales_Fact 等）
- `public` schema 下的維度表（Customer_Dimension, Product_Dimension, Date_Dimension 等）

### 6.4 載入資料

```bash
# 載入所有資料
$ /opt/vertica/bin/vsql -d testdb -w '密碼' -f vmart_load_data.sql
```

此腳本會將所有 `.tbl` 檔案透過 `COPY DIRECT` 載入對應的資料表。

### 6.5 驗證資料

```sql
=> SELECT COUNT(*) AS total_rows,
          COUNT(DISTINCT table_name) AS tables_loaded
   FROM (
     SELECT 'Date_Dimension' AS table_name FROM Date_Dimension
     UNION ALL
     SELECT 'Product_Dimension' FROM Product_Dimension
     UNION ALL
     SELECT 'Customer_Dimension' FROM Customer_Dimension
     UNION ALL
     SELECT 'Store_Sales_Fact' FROM store.Store_Sales_Fact
     UNION ALL
     SELECT 'Online_Sales_Fact' FROM online_sales.Online_Sales_Fact
   ) t;
```

各表預估資料量：

| 資料表 | 資料量 |
|--------|--------|
| Date_Dimension | ~2,190 行 |
| Product_Dimension | ~60,000 行 |
| Customer_Dimension | ~50,000 行 |
| Store_Dimension | ~250 行 |
| Store_Sales_Fact | ~5,000,000 行 |
| Online_Sales_Fact | ~5,000,000 行 |
| Inventory_Fact | ~300,000 行 |

### 6.6 執行範例查詢

VMart 附帶多個範例查詢：

```bash
# 執行所有範例查詢
$ /opt/vertica/bin/vsql -d testdb -w '密碼' -f vmart_queries.sql
```

也可以單獨測試：

```sql
-- Q1: 各區域的銷售總額
=> SELECT customer_region, ROUND(SUM(store_sales)) AS total_sales
   FROM store.Store_Sales_Fact f
   JOIN Customer_Dimension c ON f.customer_key = c.customer_key
   GROUP BY customer_region
   ORDER BY total_sales DESC;

-- Q2: 每月銷售趨勢
=> SELECT d.date_year, d.date_month,
          ROUND(SUM(store_sales)) AS monthly_sales
   FROM store.Store_Sales_Fact f
   JOIN Date_Dimension d ON f.date_key = d.date_key
   GROUP BY d.date_year, d.date_month
   ORDER BY d.date_year, d.date_month;
```

---

## 七、FAQ

### Q: 忘記 dbadmin 密碼怎麼辦？

```bash
$ sudo /opt/vertica/bin/admintools -t reset_password \
    --database testdb \
    --password '新密碼'
```

### Q: 如何啟動/停止資料庫？

```bash
# 啟動
$ /opt/vertica/bin/admintools -t start_db --database testdb

# 停止
$ /opt/vertica/bin/admintools -t stop_db --database testdb
```

### Q: 可以在單台機器上安裝多節點嗎？

可以，透過不同連接埠（如 5433, 5434）在同一台機器上執行多個 Vertica 程序，但僅建議用於測試。

### Q: Community Edition 與 Enterprise 版的差異？

| 項目 | Community Edition | Enterprise |
|------|------------------|------------|
| 節點數 | 最多 3 節點 | 無限制 |
| 資料量 | 最多 1TB | 無限制 |
| 功能 | 核心功能完整 | 完整功能 |
| 費用 | 免費 | 付費 |

---

*本文基於 Vertica 25.4.0-0 + Ubuntu 26.04 LTS 實際安裝經驗撰寫。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
