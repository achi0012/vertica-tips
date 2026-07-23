---
layout: post
title: "Vertica 刪除資料最佳實踐 — DELETE、TRUNCATE、Partition 完整指南"
date: 2026-06-21 08:00:00 +0800
categories: vertica delete data-management best-practice
tags: [vertica, delete, TRUNCATE, partition, delete-vectors, mergeout, purge, AHM, data-management]
description: "Vertica 刪除資料的完整最佳實踐 — DELETE 運作原理、五種刪除方式對比 (Single Row / Bulk / Drop Partition / Truncate)、Delete Vectors 管理、PurgeMergeoutPercent 調校、以及替代 DELETE 的實戰技巧。"
---

Vertica 作為高效能欄位式分析資料庫，在刪除資料上與傳統資料庫有兩個關鍵差異：

1. **DELETE 不會立即從磁碟刪除資料**，而是建立 delete vector 記錄被刪除資料的位置與 epoch
2. **UPDATE 執行兩項任務**：寫入新資料 + 標記舊資料為刪除

本文整理 Vertica KB 的最佳實踐，並更新至 **25.x**。

<!--
AI Summary: Vertica 刪除資料的完整最佳實踐指南。涵蓋五種刪除方式對比（Single Row/Bulk DELETE/DROP PARTITION/TRUNCATE）、Delete Vector 運作原理、Purge 政策設定（HistoryRetentionTime/HistoryRetentionEpochs/PurgeMergeoutPercent）、替代 DELETE 的 Partition 操作技巧、Bulk DELETE 實作、Delete Vectors 監控與三種處理選項（dvmergeout/DROP_PARTITION/PURGE_TABLE）。
AI Keywords: Vertica, delete, TRUNCATE, partition, delete vectors, mergeout, purge, AHM, bulk delete, data management
-->
<!--more-->

---

## 一、五種刪除方式對比

| 方式 | 建議載入目標 | 可否 Rollback | 效能 | 使用時機 |
|------|------------|-------------|------|---------|
| **Single Row DELETE** | WOS | ✅ 可 | 取決於 projection 設計 | 少量單行刪除，建議在 WOS 中執行 |
| **Trickle Load** | WOS | ✅ 可 | 取決於 projection 設計 | 小批次高頻刪除 |
| **Bulk DELETE** | Direct ROS | ✅ 可 | 取決於 projection 設計 | **建議方式**，每個 ROS 只產生一個 delete vector |
| **DROP PARTITION** | N/A | ❌ 否 | 快速（背景移除儲存） | **清理歷史資料的最佳方式** |
| **TRUNCATE TABLE** | N/A | ❌ 否 | 快速（目錄變更 + 背景儲存移除） | 清除整個表內容但保留表結構 |

> **25.x 注意**：WOS 已在 25.x 中移除，所有操作直接載入 ROS。但 delete vector 機制不變。

---

## 二、DELETE 的運作原理

### Delete Vector 機制

當執行 DELETE 時，Vertica **不會**從磁碟移除資料。而是建立一個 **delete vector**，記錄：

- 被刪除記錄的位置
- 刪除提交時的 epoch

```sql
=> SELECT COUNT(*) AS delete_vector_count
   FROM v_monitor.delete_vectors;
```

### 永久移除已刪除資料

磁碟空間何時真正釋放，取決於 **Ancient History Mark (AHM)**。AHM 代表保留歷史的時間點，任何早於 AHM 的歷史資料都可被永久移除。

```sql
=> SELECT current_epoch, ahm_epoch,
          (current_epoch - ahm_epoch) AS lag
   FROM v_monitor.system;
```

---

## 三、設定清除政策 (Purge Policy)

Tuple Mover 的 mergeout 操作會根據清除政策決定何時永久移除已刪除資料。

### 3.1 HistoryRetentionTime

以時間為基礎保留歷史資料：

```sql
-- 保留 7 天 (604800 秒)
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionTime', 604800);

-- 停用以時間為基礎的保留 (改用 epoch)
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionTime', -1);
```

### 3.2 HistoryRetentionEpochs

以 epoch 數量為基礎保留歷史：

```sql
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionTime', -1);
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionEpochs', 200);
```

### 3.3 PurgeMergeoutPercent

控制 mergeout 何時清除已刪除資料。當 ROS container 中刪除行數的百分比超過此閾值時，mergeout 會清除這些資料：

```sql
=> SELECT SET_CONFIG_PARAMETER('PurgeMergeoutPercent', 20);
```

**行為差異**：

| 表類型 | PurgeMergeoutPercent 行為 |
|--------|--------------------------|
| **未分割表** | 對符合閾值的 ROS containers 永久移除已刪除資料 |
| **分割表** | 僅對**非活躍分割區**執行清除，活躍分割區不處理 |

---

## 四、尋找 DELETE 的替代方案

### 4.1 使用分割區管理替代 DELETE

若資料表已分割，用 partition 操作替代 DELETE 效率更高：

```sql
-- 範例：重新載入上週的錯誤資料
/* Step 1: 建立 staging 表 */
=> CREATE TABLE store.staging_store_orders_fact
   LIKE store.store_orders_fact INCLUDING PROJECTIONS;

/* Step 2: 保留正確資料 */
=> INSERT /*+ direct */ INTO store.staging_store_orders_fact
   SELECT * FROM store.store_orders_fact
   WHERE date_ordered BETWEEN '2005-11-01' AND '2005-11-19'
      OR date_ordered BETWEEN '2005-11-28' AND '2005-11-30';

/* Step 3: 交換分割區 */
=> SELECT SWAP_PARTITIONS_BETWEEN_TABLES(
       'store.staging_store_orders_fact', 200511, 200511,
       'store.store_orders_fact');
```

### 4.2 重建表 (Table Recreation)

若需要清除大量資料，直接重建表可能是最快的：

```sql
=> CREATE TABLE store.store_orders_fact_new AS
   SELECT * FROM store.store_orders_fact
   WHERE date_ordered >= '2025-01-01';
=> DROP TABLE store.store_orders_fact CASCADE;
=> ALTER TABLE store.store_orders_fact_new
   RENAME TO store_orders_fact;
```

---

## 五、Bulk DELETE 最佳實踐

若無法避免使用 DELETE，請使用 **Bulk DELETE** 而非多次單行刪除。

### 建立暫存表存放要刪除的 ID

```sql
=> CREATE LOCAL TEMP TABLE data_to_delete (emp_id INT);
=> COPY data_to_delete FROM '/tmp/employee_to_delete.txt';

=> DELETE /*+ direct */ FROM store.store_orders_fact
   WHERE employee_key IN (SELECT * FROM data_to_delete);

=> DROP TABLE data_to_delete;
```

> **關鍵原則**：一個 bulk DELETE 對每個包含刪除資料的 ROS container 只產生**一個 delete vector**。

---

## 六、Delete Vectors 管理

### 6.1 監控 Delete Vectors

```sql
=> SELECT schema_name, projection_name,
          COUNT(*) AS num_ros,
          SUM(total_row_count) AS num_rows,
          SUM(deleted_row_count) AS num_deld_rows,
          SUM(delete_vector_count) AS num_dv,
          (SUM(deleted_row_count) / SUM(total_row_count) * 100)::INT
            AS pct_del_rows
   FROM v_monitor.storage_containers
   WHERE node_name = (SELECT local_node_name())
   GROUP BY 1, 2
   HAVING SUM(deleted_row_count) > 0
   ORDER BY num_deld_rows DESC;
```

### 6.2 依分割區查看刪除資料

```sql
=> SELECT p.node_name, p.table_schema, p.projection_name,
          p.partition_key,
          COUNT(DISTINCT p.ros_id) AS num_ros,
          SUM(p.ros_size_bytes) AS used_bytes,
          SUM(p.ros_row_count) AS num_rows,
          SUM(p.deleted_row_count) AS num_del,
          SUM(p.delete_vector_count) AS cdv,
          (SUM(p.deleted_row_count) / SUM(p.ros_row_count) * 100)::INT
            AS pct_del_rows
   FROM v_monitor.partitions p
   JOIN v_monitor.storage_containers sc ON p.ros_id = sc.storage_oid
   WHERE p.node_name = (SELECT local_node_name())
     AND sc.node_name = (SELECT local_node_name())
   GROUP BY 1, 2, 3, 4
   HAVING SUM(p.delete_vector_count) > 0
   ORDER BY pct_del_rows DESC;
```

### 6.3 三種處理選項

根據查詢結果，可選擇以下三種處理方式：

| 情況 | 處理方式 | 指令 |
|------|---------|------|
| **delete vectors 過多，但刪除行數佔比不大** | 合併 delete vectors | `SELECT DO_TM_TASK('dvmergeout');` |
| **分割區內大量已刪除資料** | 清除分割區（partial purge） | `SELECT DROP_PARTITION(...)` |
| **整個表有大量已刪除資料** | 手動清除整個表 | `SELECT PURGE_TABLE('schema.table');` |

#### 選項 1：合併 Delete Vectors

```sql
=> SELECT DO_TM_TASK('dvmergeout');
```

當 delete vectors 過多但刪除行數佔比不大時使用。合併 delete vectors 比清除整個表更有效率。

#### 選項 2：清除分割區

```sql
=> SELECT DROP_PARTITION('store.store_orders_fact', 200511, 200511);
```

#### 選項 3：清除整個表

```sql
=> SELECT PURGE_TABLE('store.store_orders_fact');
```

> ⚠️ `PURGE_TABLE` 會重寫整個 ROS containers 資料集（不含已刪除的行）。若刪除行數佔比不大，**不建議**使用此方式。

---

## 七、建議的工作流程

```
遇到需要刪除資料的需求
    ↓
可以不用 DELETE 嗎？
    ├── 分割表 → 用 DROP/SWAP PARTITION
    ├── 大量資料 → 用 CREATE TABLE AS + DROP
    └── 全部清除 → 用 TRUNCATE
    ↓
必須用 DELETE？
    ├── 用 Bulk DELETE（單一語句）
    └── 在 Temp Table 中準備要刪除的 ID
    ↓
監控 Delete Vectors
    ├── pct_del_rows 小 → dvmergeout
    ├── pct_del_rows 大（分割區）→ DROP_PARTITION
    └── pct_del_rows 大（整表）→ PURGE_TABLE
    ↓
定期清除政策
    ├── 設定 HistoryRetentionTime
    └── 設定 PurgeMergeoutPercent
```

---

## 八、版本差異

| 項目 | 舊版 (≤10.x) | 25.x |
|------|-------------|------|
| `storage_containers` | 裸表 | `v_monitor.storage_containers` |
| `partitions` | 裸表 | `v_monitor.partitions` |
| `delete_vectors` | 裸表 | `v_monitor.delete_vectors` |
| `DO_TM_TASK('dvmergeout')` | 可用 | ✅ 仍然可用 |
| `PURGE_TABLE()` | 可用 | ✅ 仍然可用 |
| WOS | WOS 存在 | 已移除，直接 ROS |
| DELETE 載入目標 | 建議 WOS | 直接 ROS |

---

*本文基於 Vertica KB "Best Practices for Deleting Data"，所有 SQL 已更新至 Vertica 25.4.0-0。*
