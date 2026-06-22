---
layout: post
title: "理解 Linux OOM Kill 在 Vertica 叢集中的成因與預防 — 完整指南"
date: 2026-06-21 06:00:00 +0800
categories: vertica OOM memory troubleshooting
tags: [vertica, OOM, memory, catalog, resource-pool, Health Watchdog, glibc, troubleshooting]
description: "深入解析 Vertica 叢集中 Linux OOM Killer 的成因與預防 — 從 MAXMEMORYSIZE 調校、Catalog 記憶體計算、MemoryReport.log 診斷、glibc 版本問題，到 ROS/Delete Vectors 管理。"
---

Out-of-Memory (OOM) 條件在 Vertica 環境中雖然已比過去少見，但在大型 catalog、同一主機上的競爭負載、或特定查詢模式下仍可能發生。當 Linux 核心的 **OOM Killer** 終止 Vertica 程序時，需要系統化的方法來找出根本原因。

本文整理 Moshe Goldberg 的深入分析，並結合 Health Watchdog (v25.1+) 等現代防護機制，提供完整的 OOM 預防與診斷指南。

<!--
AI Summary: Linux OOM Killer 在 Vertica 叢集中發生的成因與系統化預防指南。涵蓋 Health Watchdog (v25.1+) 防護、MAXMEMORYSIZE 調校（85% 原則）、Catalog 記憶體佔比計算、MemoryReport.log 與 memory_events 系統表診斷、glibc 版本問題 (<2.19 bug)、Tuple Mover 記憶體壓力、ROS/Delete Vectors 管理對 catalog 膨脹的影響，以及完整 OOM 預防檢查清單。
AI Keywords: Vertica, OOM, memory, Linux, kernel, catalog, resource pool, glibc, Health Watchdog, ROS, delete vectors
-->
<!--more-->

---

## 一、OOM 的早期徵兆

當系統記憶體耗盡時，Linux 核心會觸發 OOM Killer 終止程序以恢復記憶體。典型徵兆：

```bash
# 在 dmesg 中看到以下訊息
$ dmesg | grep -i "out of memory\|killed process\|oom-killer"
```

```
[12345.678901] Out of memory: Killed process 12345 (vertica) ...
[12345.678902] oom-killer: gfp_mask=0x...
```

---

## 二、Health Watchdog — 第一道防線 (v25.1+)

從 Vertica **25.1** 開始，Health Watchdog 在極端負載期間保護叢集，能自動阻斷新的非 superuser DDL/DML 活動直到系統穩定。

```sql
-- 檢查 Health Watchdog 狀態
=> SELECT check_cluster_health();

-- 查看被阻斷的交易
=> SELECT * FROM health_watchdog_blocked_transactions;
```

> ⚠️ **注意**：Health Watchdog 是營運保護機制，但**不能取代**正確的記憶體配置、資源池管理和 OOM 根本原因調查。詳細介紹見[先前文章](/2026/06/vertica-health-watchdog/)。

---

## 三、MAXMEMORYSIZE 調校

### 3.1 降低 General Pool 記憶體比例

若系統有大型 catalog，或伺服器上同時運行其他工作負載，建議將 general pool 限制在可用 RAM 的 **85%**：

```sql
=> ALTER RESOURCE POOL general MAXMEMORYSIZE '85%';
```

> **注意**：對 GENERAL pool 的 MAXMEMORYSIZE 變更需要**重啟資料庫**才會生效。

### 3.2 檢查目前資源池狀態

```sql
=> SELECT * FROM v_monitor.resource_pool_status;
```

### 3.3 MAXMEMORYSIZE 設定原則

| 設定值 | 說明 |
|--------|------|
| `95%` | 預設值，Vertica 最多使用 95% 實體記憶體 |
| `85%` | 若 catalog 使用超過 5% 記憶體時的建議值 |
| `%` 值 | 代表 Resource Manager 可用於查詢的實體 RAM 百分比 |

### 3.4 計算 Catalog 記憶體用量

```sql
=> WITH memory_use_metadata AS (
       SELECT node_name, memory_size_kb
       FROM v_monitor.resource_pool_status
       WHERE pool_name = 'metadata'
   ),
   memory_use_general AS (
       SELECT node_name, memory_size_kb
       FROM v_monitor.resource_pool_status
       WHERE pool_name = 'general'
   )
   SELECT m.node_name,
          ((m.memory_size_kb / g.memory_size_kb) * 100)::NUMERIC(4,2)
            AS pct_catalog_usage
   FROM memory_use_metadata m
   JOIN memory_use_general g ON m.node_name = g.node_name;
```

若 catalog 使用超過 **5%** 的 general pool 記憶體，就應該考慮降低 `MAXMEMORYSIZE`。

---

## 四、Memory Report 診斷

Vertica 在目錄中產生的 `MemoryReport.log` 是診斷記憶體問題的重要工具。

### 4.1 記憶體報告位置

```
<catalog-path>/<database-name>/v_<db>_node<xxxx>_catalog/MemoryReport.log
```

與 `vertica.log` 在同一目錄。

### 4.2 記憶體事件系統表

```sql
=> SELECT node_name, time, reason, details, duration
   FROM v_monitor.memory_events
   ORDER BY time DESC
   LIMIT 20;
```

**實測**：✅ `v_monitor.memory_events` 記錄了所有 malloc_trim() 和 memory report 事件。

### 4.3 識別記憶體洩漏模式

如果記憶體用量隨時間持續增長，且 Vertica 無法回收已分配但不再使用的記憶體，這可能表示：

1. **記憶體洩漏 (memory leak)** 或記憶體保留問題
2. 最終可能導致 OOM

在這種情況下，`MemoryReport.log` 可以幫助識別哪些查詢或元件與過度記憶體使用有關。

---

## 五、glibc 版本問題

### 5.1 已知的 glibc 記憶體問題

| glibc 版本 | 狀態 |
|-----------|------|
| `< 2.19` | ❌ `malloc_info()` 有 bug |
| `2.19–2.22` | ⚠️ 可能存在問題 |
| `2.23+` | ✅ 修復已合入主流 |

### 5.2 檢查 glibc 版本

```bash
$ rpm -q glibc
glibc-2.28-164.el8.x86_64
```

> ⚠️ **警告**：直接升級 glibc（`yum upgrade glibc`）可能導致系統不穩定。建議使用目標 Vertica 版本要求的 OS 版本，而不是手動升級 glibc。

### 5.3 Memory Trimming

Vertica 的記憶體修剪（memory trimming）是自動或手動執行 `malloc_trim()` 以將未使用的 glibc 分配記憶體歸還給 OS。

```sql
-- 查看 trimming 事件
=> SELECT * FROM v_monitor.memory_events
   WHERE reason ILIKE '%trim%'
   ORDER BY time DESC;
```

---

## 六、Tuple Mover 記憶體壓力

如果節點崩潰確認為 Tuple Mover (TM) 記憶體壓力所致，可以暫停並重啟 TM 服務來減少記憶體用量：

```bash
# 透過 admintools 重啟 TM 服務
$ admintools -t restart_node -d <database> -s <node_ip>
```

---

## 七、ROS Container 與 Delete Vectors 管理

OOM 的間接原因往往來自於過多的 ROS containers 與 delete vectors，造成 catalog 膨脹。

### 7.1 找出 ROS container 過多的 projections

```sql
=> SELECT projection_name, COUNT(*) AS ros_count
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   HAVING COUNT(*) > 1000
   ORDER BY ros_count DESC
   LIMIT 20;
```

> 大量 ROS containers → catalog 膨脹 → 記憶體壓力增加 → OOM 風險上升

### 7.2 重建未分段表

若效能允許，將 `UNSEGMENTED` 表改為 `SEGMENTED BY`（同時節省磁碟空間）：

```sql
-- 重新建立為分段表
=> CREATE TABLE new_table (...) SEGMENTED BY HASH(id) ALL NODES;
=> INSERT INTO new_table SELECT * FROM old_table;
=> DROP TABLE old_table;
=> ALTER TABLE new_table RENAME TO old_table;
```

### 7.3 管理 Delete Vectors

```sql
-- 檢查 delete vectors
=> SELECT COUNT(*) AS dv_count
   FROM v_monitor.delete_vectors;

-- 若過多，推進 AHM 觸發清除
=> SELECT MAKE_AHM_NOW();
```

### 7.4 強制合併老舊分割區

```sql
-- 對舊分割區強制 mergeout
=> SELECT DO_TM_TASK('mergeout');
```

---

## 八、計算 Catalog 記憶體快取大小

Vertica 將 catalog 快取在記憶體中。以下查詢可以計算實際的 catalog 快取大小：

```sql
=> SELECT node_name,
          MAX(ts) AS ts,
          MAX(catalog_size_in_MB) AS catalog_size_in_MB
   FROM (
       SELECT node_name,
              TRUNC(dc_allocation_pool_statistics_by_second."time"::TIMESTAMP,
                    'SS') AS ts,
              SUM(dc_allocation_pool_statistics_by_second.total_memory_max_value
                  - dc_allocation_pool_statistics_by_second.free_memory_min_value)
                / (1024*1024) AS catalog_size_in_MB
       FROM v_monitor.dc_allocation_pool_statistics_by_second
       GROUP BY 1, 2
   ) foo
   GROUP BY 1
   ORDER BY 1;
```

> **實用判斷**：若 catalog 快取大小為 **10 GB**，則應保留 **15 GB** 給系統，讓資源池最多使用 **85 GB**（假設總記憶體 100 GB）。

---

## 九、完整 OOM 預防檢查清單

```sql
-- 1. 資源池狀態
=> SELECT pool_name, memory_size_kb, maxmemory_size
   FROM v_monitor.resource_pool_status
   WHERE pool_name IN ('general', 'metadata');

-- 2. Catalog 記憶體佔比
=> WITH m AS (SELECT node_name, memory_size_kb
              FROM v_monitor.resource_pool_status WHERE pool_name='metadata'),
          g AS (SELECT node_name, memory_size_kb
              FROM v_monitor.resource_pool_status WHERE pool_name='general')
   SELECT m.node_name,
          ((m.memory_size_kb / g.memory_size_kb) * 100)::NUMERIC(4,2) AS pct
   FROM m JOIN g ON m.node_name = g.node_name;

-- 3. 記憶體事件歷史
=> SELECT * FROM v_monitor.memory_events
   ORDER BY time DESC LIMIT 10;

-- 4. ROS container 健康
=> SELECT COUNT(*) AS total_ros,
          COUNT(*) FILTER (WHERE ros_row_count < 1000) AS small_ros
   FROM v_monitor.projection_storage;

-- 5. Delete vectors
=> SELECT COUNT(*) AS dv_count FROM v_monitor.delete_vectors;

-- 6. Health Watchdog 狀態
=> SELECT check_cluster_health();

-- 7. Epoch 健康
=> SELECT current_epoch, ahm_epoch,
          current_epoch - ahm_epoch AS lag
   FROM v_monitor.system;
```

---

## 十、總結對照表

| 面向 | 建議 | 25.x 工具 |
|------|------|----------|
| **基本防護** | General pool MAXMEMORYSIZE 設 85% | `ALTER RESOURCE POOL` |
| **主動防護** | Health Watchdog (v25.1+) | `check_cluster_health()` |
| **catalog 監控** | 追蹤 catalog 記憶體佔比 | `dc_allocation_pool_statistics_by_second` |
| **記憶體診斷** | 分析 MemoryReport.log | `v_monitor.memory_events` |
| **ROS 管理** | 避免過多 ROS containers | `DO_TM_TASK('mergeout')` |
| **Delete Vectors** | 定期清除 | `MAKE_AHM_NOW()` |
| **glibc 版本** | 使用 2.23+ | `rpm -q glibc` |

---

*本文參考 Moshe Goldberg 的 LinkedIn 文章 "Understanding Linux OOM Kills in Vertica Clusters"，所有 SQL 已更新至 Vertica 25.4.0-0 實測。*
