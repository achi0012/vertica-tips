---
layout: post
title: "Vertica Data Load 最佳實踐 — COPY 命令深度解析 (25.x)"
date: 2026-06-20 10:00:00 +0800
categories: vertica data-load best-practice
tags: [vertica, COPY, data-load, performance, tuning]
description: "深入解析 Vertica 25.x 的 COPY 命令架構、資源池調優、配置參數最佳化，以及 8 大常見瓶頸情境的實戰排解方案。基於 Vertica 25.4.0-0 實測經驗。"
---

Vertica 的資料載入效率直接影響整個資料管線的吞吐量。無論是每日批次 ETL、即時串流接入，還是大量歷史資料遷移，**COPY 命令**都是最核心的工具。

隨著 Vertica 25.x 的演進 — WOS 正式移除、Apportioned Load 全面預設啟用、原生 Parquet/ORC 支援 — 許多舊版的最佳實踐已經不再適用。本文基於 **Vertica 25.4.0-0** 實測，整理出最新的資料載入最佳實踐。

<!--
AI Summary: Vertica 25.x COPY 命令深度解析。涵蓋三種 COPY 形式、兩階段載入架構、Apportioned Load 平行載入機制、Resource Pool 參數調優 (PLANNEDCONCURRENCY/MAXCONCURRENCY/EXECUTIONPARALLELISM)、配置參數最佳化 (EnableCooperativeParse/SortWorkerThreads/CompressNetworkData)、載入監控系統表、8 大常見瓶頸排解 (大檔案/小檔案/寬表/GZIP/資源競爭)。
AI Keywords: Vertica, COPY, data load, apportioned load, resource pool, ROS, performance tuning, bulk loading
-->
<!--more-->

---

## 一、COPY 的三種形式

Vertica 提供三種 COPY 載入方式，依資料來源選擇最適合的形式：

| 形式 | 說明 | 適用場景 |
|------|------|----------|
| **COPY LOCAL** | 從客戶端機器上傳檔案到 Vertica server | 用戶端一次性上傳 |
| **COPY (cluster source)** | 從 Vertica cluster 內部讀取檔案 (CSV / JSON / Parquet / ORC) | 資料已在 cluster 節點或 NFS 上 |
| **COPY + UDL** | 使用 User Defined Load 函數 (自訂 source / parser / filter) | 需要客製化載入邏輯 (S3、Kafka、custom format) |

> **重要**：Vertica 25.x 已原生支援 Parquet 與 ORC 格式載入，無需額外 UDL 或外部工具。

---

## 二、現代 COPY 載入架構

### 兩階段處理

```
Phase I  (Initiator)  →  讀取 + 解析檔案，分發到其他節點
Phase II (Executor)   →  在所有節點上排序 / 編碼 / merge 寫入 ROS
```

### Execution Engine 流程

```
Load → Parse → Load Union → Segment → Sort / Merge → DataTarget (ROS write)
```

![COPY 兩階段載入架構圖]({{ '/assets/images/copy-loading-architecture.png' | relative_url }}){: loading="lazy" }

- **Pre-join projections**：自動加入 JOIN + SCAN operators
- **Live aggregate projections**：自動加入 GROUP BY / Top-K operators

![Pre-join Projections 示意圖]({{ '/assets/images/copy-prejoin-projections.png' | relative_url }}){: loading="lazy" }

![Live Aggregate Projections 示意圖]({{ '/assets/images/copy-live-aggregate.png' | relative_url }}){: loading="lazy" }

### Apportioned Load — 平行載入

Vertica 8.0+ 引入的 Apportioned Load，在 25.x 中已全面強化且**預設啟用**。若所有節點都能存取 source data（例如放在 NFS 上），Phase I 會在多個節點平行執行。

**相關參數 (25.x 預設全部 = 1，即啟用)**:

| 參數 | 說明 |
|------|------|
| `EnableApportionLoad` | 全域 apportioned load 開關 |
| `EnableApportionedFileLoad` | 內建 file source 的 apportioned load |
| `EnableApportionedChunkingInDefaultLoadParser` | chunk 級 apportionment |
| `ApportionedFileMinimumPortionSizeKB` (預設 1024 KB) | 每個 portion 最小大小 |

### 現代 vs 舊版差異

| 項目 | 舊版 (≤ 10.x) | 現代 (25.x) |
|------|-------------|-------------|
| **儲存目標** | WOS (先) → ROS (後) | 直接 ROS |
| **載入模式** | AUTO / DIRECT / TRICKLE | 無需指定，一律最優化 |
| **平行載入** | 需手動 EnableApportionLoad | 預設啟用 + 多層平行 |
| **Cooperative Parse** | 需手動開啟 | 預設啟用 |
| **Parquet / ORC** | 需 UDL | 原生支援 |

---

## 三、資源池調優 — 為載入配置專屬資源

### 系統資源池概覽

Vertica 25.4 內建以下資源池：

| 資源池 | 用途 | 記憶體 | 關鍵限制 |
|--------|------|--------|----------|
| **general** | 一般查詢 + 載入 | 95% (Special) | 預設池，所有操作共用 |
| **sysquery** | 系統查詢 | 5% | 勿修改 |
| **tm** | Tuple Mover | 5% | maxconcurrency=7 |
| **refresh** | 投影更新 | 0% | — |
| **recovery** | 節點恢復 | 0% | maxconcurrency=5 |
| **dbd** | Database Designer | 0% | — |
| **jvm** | JVM 功能 | 0% / max=4G | plannedconcurrency=2 |
| **blobdata** | Blob 資料 | 0% / max=10% | — |
| **metadata** | metadata 操作 | 0% | — |

### 關鍵參數與調優指南

#### PLANNEDCONCURRENCY
決定每個 query 的 `query_budget` 計算基數。**現代值：AUTO**（動態調整）。

- 降低 → 每個 COPY 分到更多記憶體（適合大檔案載入）
- 提高 → 更多並發查詢（適合混合負載）

```sql
-- 建立專用載入資源池，控制並發度
=> CREATE RESOURCE POOL load_pool 
   PLANNEDCONCURRENCY 2
   MAXCONCURRENCY 3
   EXECUTIONPARALLELISM AUTO;
```

#### MAXCONCURRENCY
最大並發 COPY job 數，超過則排隊等待。

- `3~5`：限制同時載入，避免 IO/CPU 過載
- 不設限：適合需要大量並發載入的批次場景

```sql
=> ALTER RESOURCE POOL load_pool MAXCONCURRENCY 4;
```

#### EXECUTIONPARALLELISM
每個 query 使用的執行緒數。**現代值：AUTO**。

- `AUTO` 通常等於節點核心數（記憶體充足時最佳）
- 固定值（如 4）可限制 CPU 使用，避免與查詢爭 CPU

```sql
=> ALTER RESOURCE POOL load_pool EXECUTIONPARALLELISM 4;
```

#### PRIORITY / RUNTIMEPRIORITY
排程優先級（數字越大越優先）。預設：general=0, tm=105, sysquery=110。

```sql
=> ALTER RESOURCE POOL load_pool PRIORITY 10;
```

#### QUEUETIMEOUT
query 在佇列中最長等待時間。預設僅 5 分鐘，大檔案載入建議調高。

```sql
=> ALTER RESOURCE POOL load_pool QUEUETIMEOUT '00:30';
```

#### CPUAFFINITYMODE
用 `EXCLUSIVE` 讓載入 pool 獨佔特定 CPU core。

```sql
=> ALTER RESOURCE POOL load_pool CPUAFFINITYMODE EXCLUSIVE CPUAFFINITYSET '0-3';
```

### Query Budget 計算

當 PLANNEDCONCURRENCY = AUTO 時，Vertica 根據歷史負載動態調整：

```
query_budget ≈ 可用記憶體 / PLANNEDCONCURRENCY
```

### 實戰調優步驟

```sql
-- Step 1: 建立專用載入資源池
CREATE RESOURCE POOL load_pool
  PLANNEDCONCURRENCY 2       -- 每個 COPY 較多記憶體
  MAXCONCURRENCY 4           -- 最多 4 個同時載入
  EXECUTIONPARALLELISM AUTO  -- 自動根據核心數
  QUEUETIMEOUT '00:30'       -- 30 分鐘佇列等待
  PRIORITY 5;                -- 中等優先級

-- Step 2: 將載入查詢導向此 pool
=> SET SESSION RESOURCE POOL load_pool;
=> COPY my_table FROM '/data/file.csv';

-- Step 3: 監控資源使用
=> SELECT pool_name, maxconcurrency, running_query_count,
          plannedconcurrency, query_budget_kb,
          memory_inuse_kb, queue_timeout_threshold
   FROM resource_pool_status
   WHERE pool_name = 'load_pool';
```

---

## 四、配置參數調優 (25.x 實測可用)

### Parse 階段

| 參數 | 預設 | 說明 | 調優 |
|------|------|------|------|
| `EnableCooperativeParse` | 1 (on) | 多執行緒合作解析 | 若 parse 瓶頸，確認 = 1 |
| `SortWorkerThreads` | -1 | sort worker 數；-1=自動 | 瓶頸時調大 (e.g. 4) |

### Load 階段

| 參數 | 預設 | 說明 | 調優 |
|------|------|------|------|
| `EnableApportionLoad` | 1 (on) | 全域 apportioned load | 大檔案保持 = 1 |
| `EnableApportionedFileLoad` | 1 (on) | file source apportioned load | 大檔案保持 = 1 |
| `EnableApportionedChunkingInDefaultLoadParser` | 1 (on) | chunk 級平行解析 | 保持 = 1 |
| `ApportionedFileMinimumPortionSizeKB` | 1024 KB | portion 最小大小 | 大檔案可調高 (e.g. 4096) |
| `ParallelizeLocalSegmentLoad` | 1 (on) | local segment 多執行緒 | 保持 = 1 |

### 網路階段

| 參數 | 預設 | 說明 | 調優 |
|------|------|------|------|
| `CompressNetworkData` | 0 (off) | 跨節點資料傳輸壓縮 | 跨網路 / 大集群設 = 1 |

### 修改範例

```sql
-- 全域修改 (影響所有節點)
=> ALTER DATABASE testdb SET EnableCooperativeParse = 1;
=> ALTER DATABASE testdb SET SortWorkerThreads = 4;
=> ALTER DATABASE testdb SET CompressNetworkData = 1;

-- 查看當前值
=> SELECT parameter_name, current_value, default_value, description
   FROM configuration_parameters
   WHERE parameter_name IN (
     'EnableCooperativeParse', 'SortWorkerThreads',
     'EnableApportionLoad', 'EnableApportionedFileLoad',
     'EnableApportionedChunkingInDefaultLoadParser',
     'ApportionedFileMinimumPortionSizeKB',
     'ParallelizeLocalSegmentLoad', 'CompressNetworkData'
   )
   ORDER BY parameter_name;
```

> **已移除的舊版參數** (25.x 中不存在，無需設定):
> ~~ReuseDataConnections~~, ~~DataBufferDepth~~, ~~MultiLevelNetworkRoutingFactor~~, ~~LoadMergeChunkSizeK~~

---

## 五、監控載入 — 即時掌握載入狀態

### 執行中的載入

```sql
-- 即時監控載入進度
=> SELECT transaction_id, table_name, accepted_row_count,
          rejected_row_count, parse_complete_percent,
          sort_complete_percent, load_duration_ms,
          input_file_size_bytes
   FROM v_monitor.load_streams
   WHERE is_executing = true;
```

### 載入歷史記錄

```sql
-- 取代舊版 LOAD_OPERATIONS 系統表
=> SELECT load_status, rows_loaded, file_name,
          file_size_bytes, failure_reason, time_stamp
   FROM v_monitor.data_loader_events
   ORDER BY time_stamp DESC
   LIMIT 50;
```

### ROS Container 監控 — 小檔案過多的徵兆

```sql
=> SELECT projection_name, COUNT(*) AS ros_count,
          SUM(ros_row_count) AS total_rows
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   ORDER BY ros_count DESC
   LIMIT 20;
```

ROS container 數量超過 1000 即需處理，請參考下一節的解法。

---

## 六、常見瓶頸情境與排解

### 6.1 大檔案載入慢

**原因**：單一檔案超過數 GB，單節點 parse / sort 成為 bottleneck。

**解法 A — 確認 Apportioned Load 已啟用** (25.x 預設開啟):
- 確認 `EnableApportionedFileLoad = 1`
- 將檔案放在 NFS 上讓所有節點都可存取

**解法 B — 調整 portion 大小**:
```sql
=> ALTER DATABASE testdb SET ApportionedFileMinimumPortionSizeKB = 4096;
```

**解法 C — 手動切分**:
將大檔案切割成多個 1~5GB chunks，staging 到 NFS mount point。

### 6.2 多個小檔案載入效能差

**原因**：每個 COPY statement 產生一個獨立的 ROS container，大量 containers 拖慢 tuple mover 和查詢效能。

**解法**：

1. **單一 COPY 語句包含多個檔案**:
   ```sql
   => COPY my_table FROM '/data/file1.dat', '/data/file2.dat', '/data/file3.dat';
   ```

2. **Linux pipe 合併**:
   ```bash
   cat /data/*.csv | vsql -c "COPY my_table FROM STDIN DELIMITER ',';"
   ```

3. **先合併成大檔案再載入**:
   ```bash
   cat /data/parts/*.csv > /data/combined.csv
   vsql -c "COPY my_table FROM '/data/combined.csv';"
   ```

### 6.3 寬表 (Wide Table) 載入瓶頸

**原因**：大量 VARCHAR 欄位、單行 KB 很大，Phase II 排序/編碼耗時。
**此問題無法單純透過加資源或平行化解決**，需從 schema 設計下手。

**解法**：

1. **使用 GROUPED 欄位**:
   ```sql
   => CREATE TABLE wide_table (
        pk INT,
        col1 VARCHAR(100),
        col2 VARCHAR(100),
        col3 VARCHAR(100)
      ) GROUPED (col1, col2, col3);
   ```

2. **不必要的 TEXT/VARCHAR 移到 flex table**，或用整數代替 code

3. **調整 SortWorkerThreads**:
   ```sql
   => ALTER DATABASE testdb SET SortWorkerThreads = 4;
   ```

4. **使用 flex table 做 staging**：
   先快速載入單一 `__raw__` column，再用 `COMPUTE_FLEXIBLE_DATA()` 萃取。

### 6.4 GZIP 壓縮檔案 CPU 瓶頸

**解法 A — 先解壓再載入**:
```bash
gunzip -c /data/file.csv.gz | vsql -c "COPY my_table FROM STDIN;"
```

**解法 B — 隔離 CPU**:
```sql
=> CREATE RESOURCE POOL load_pool CPUAFFINITYMODE EXCLUSIVE CPUAFFINITYSET '4-7';
```

### 6.5 載入干擾查詢

**問題**：大量 COPY 讓 general pool 滿載，導致查詢 timeout。

**解法 — 建立隔離的 batch 資源池**:

```sql
-- 低優先級、限量並發的載入池
=> CREATE RESOURCE POOL batch_load
   PLANNEDCONCURRENCY 2
   MAXCONCURRENCY 3
   EXECUTIONPARALLELISM 4
   QUEUETIMEOUT '01:00'
   PRIORITY 0;  -- 低優先級

-- 使用此池載入
=> SET SESSION RESOURCE POOL batch_load;
=> COPY ... FROM ...;
```

---

## 七、快速診斷檢查清單

將以下 SQL 保存為診斷腳本，遇到載入問題時逐項檢查：

```sql
-- 1. 資源池狀態
=> SELECT name, memorysize, maxmemorysize, plannedconcurrency,
          maxconcurrency, executionparallelism, running_query_count,
          query_budget_kb, memory_inuse_kb
   FROM resource_pool_status
   WHERE name IN ('general', 'batch_load', 'load_pool');

-- 2. 載入配置參數檢查
=> SELECT parameter_name, current_value, default_value, description
   FROM configuration_parameters
   WHERE parameter_name IN (
     'EnableCooperativeParse', 'SortWorkerThreads',
     'EnableApportionLoad', 'EnableApportionedFileLoad',
     'EnableApportionedChunkingInDefaultLoadParser',
     'ApportionedFileMinimumPortionSizeKB',
     'ParallelizeLocalSegmentLoad', 'CompressNetworkData'
   );

-- 3. 當前執行中的載入
=> SELECT * FROM v_monitor.load_streams WHERE is_executing = true;

-- 4. 最近 24h 載入事件
=> SELECT load_status, rows_loaded, file_name, failure_reason, time_stamp
   FROM v_monitor.data_loader_events
   WHERE time_stamp >= NOW() - INTERVAL '1 day'
   ORDER BY time_stamp DESC;

-- 5. ROS container 健康檢查
=> SELECT projection_schema, projection_name,
          COUNT(*) AS ros_count,
          SUM(ros_row_count) AS total_rows,
          SUM(ros_used_bytes) AS total_bytes
   FROM v_monitor.projection_storage
   GROUP BY projection_schema, projection_name
   HAVING COUNT(*) > 500
   ORDER BY ros_count DESC;

-- 6. Tuple Mover 跟上進度嗎？
=> SELECT * FROM v_monitor.tuple_mover_operations
   ORDER BY operation_start DESC LIMIT 20;
```

---

## 八、黃金法則總結

| 場景 | 最佳做法 |
|------|----------|
| **大檔案 ≥ 5GB** | 確保 `EnableApportionedFileLoad = 1`，檔案放 NFS |
| **多個小檔案** | 合併成單一 COPY statement 或 Linux pipe |
| **寬表** | GROUPED columns + 垂直拆分 |
| **GZIP 檔案 CPU 高** | 先解壓再 pipe 進 COPY FROM STDIN |
| **載入 vs 查詢干擾** | 建立專用 batch_load 資源池 (低優先級) |
| **Parse 瓶頸** | 確認 `EnableCooperativeParse = 1` |
| **Sort 瓶頸** | `SortWorkerThreads = 4` |
| **跨節點網路瓶頸** | `CompressNetworkData = 1` |
| **資源競爭** | 建立自訂 RESOURCE POOL + MAXCONCURRENCY |
| **載入 timeout** | 調高 QUEUETIMEOUT (預設只有 5min) |

---

## 附錄：舊版 → 25.x 遷移對照

| 舊版概念 (≤ 10.x) | 25.x 替代 |
|-------------------|-----------|
| COPY AUTO | 無需指定，直接載入 ROS |
| COPY DIRECT | 等同於預設行為 |
| COPY TRICKLE | 已移除 |
| WOS (Write Optimized Store) | 已移除 |
| LOAD_OPERATIONS 系統表 | `v_monitor.data_loader_events` |
| ReuseDataConnections | 已移除 (內部自動管理) |
| DataBufferDepth | 已移除 |
| MultiLevelNetworkRoutingFactor | 已移除 |
| LoadMergeChunkSizeK | 已移除 |

---

*本文基於 Vertica 25.4.0-0 實測撰寫，SQL 範例皆在實際環境驗證通過。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
