---
layout: post
title: "Vertica Tuple Mover 最佳實踐 — Moveout、Mergeout 與 STRATA 演算法完整指南"
date: 2026-06-21 09:00:00 +0800
categories: vertica tuple-mover mergeout performance
tags: [vertica, tuple-mover, moveout, mergeout, ROS, STRATA, WOS, delete-vectors, resource-pool, TM]
description: "Vertica Tuple Mover 的完整最佳實踐指南 — 從 Moveout/Mergeout 運作原理、STRATA 演算法、TM 資源池調優、Part 1 (9.1-) 與 Part 2 (9.2+) 的版本差異，到 ROS Pushback 預防與實戰監控。"
---

Tuple Mover 是 Vertica 的背景服務，負責管理資料從記憶體到磁碟的移動、ROS containers 的合併，以及已刪除資料的清除。雖然多數情況下預設配置就足夠，但特定工作負載需要調校才能達到最佳效能。

本文整合 Vertica KB 的兩篇 Tuple Mover 最佳實踐（Part 1: 9.1 以前、Part 2: 9.2+），並更新至 **25.x**。

<!--
AI Summary: Vertica Tuple Mover 完整最佳實踐指南，整合 KB Part 1 (≤9.1) 與 Part 2 (9.2+)。涵蓋 Moveout/Mergeout 運作原理、STRATA 演算法（ROSPerStratum=32）、TM 資源池調優（寬表增加記憶體、並發度 4）、Partition 與 Projection 設計原則、Mergeout 吞吐量診斷（< 1GB/分需調校）、ROS Pushback 預防、Replay Delete 處理，以及 25.x 版本差異對照。
AI Keywords: Vertica, Tuple Mover, moveout, mergeout, STRATA, ROS, TM resource pool, ROS pushback, replay delete, partition
-->
<!--more-->

---

## 一、Tuple Mover 概觀

### 歷史演進

| 版本 | 變更 |
|------|------|
| **≤ 9.1** (Part 1) | 有 WOS，Tuple Mover 負責 Moveout (WOS→ROS) + Mergeout (ROS 合併) |
| **9.2+** (Part 2) | WOS 已移除，直接載入 ROS，Tuple Mover 僅負責 Mergeout |
| **25.x** | WOS 完全移除，Moveout 不再需要 |

### 兩個操作

| 操作 | 說明 | 25.x 狀態 |
|------|------|----------|
| **Moveout** | 定期將資料從 WOS container 移到新 ROS container | ❌ WOS 已移除，不再需要 |
| **Mergeout** | 合併 ROS containers + 清除已刪除資料 | ✅ 核心功能 |

---

## 二、Moveout（舊版，僅供參考）

適用於 **Vertica 9.1 及更早版本**。

### Moveout 最佳實踐

| 實踐 | 說明 |
|------|------|
| **大檔案用 COPY DIRECT** | > 100 MB 的檔案直接載入 ROS，跳過 WOS |
| **MoveOutInterval** | 預設 300 秒，設為小於填滿一半 WOS 的時間 |
| **避免 WOS 中有未提交資料** | Tuple Mover 只移動已提交資料 |
| **勿用 WOS 載入大型暫存表** | > 50 MB 的暫存表會導致 WOS 溢出 |
| **MoveOutSizePct** | 觸發 moveout 的 WOS 使用率百分比 |
| **MoveOutMaxAgeTime** | 資料在 WOS 中的最大停留時間（預設 1800 秒） |

### 偵測 WOS 溢出

```sql
=> SELECT node_name, COUNT(*)
   FROM dc_execution_engine_events
   WHERE event_type = 'WOS_SPILL'
   GROUP BY node_name;
```

---

## 三、Mergeout 核心機制

### 3.1 Mergeout 的工作

Mergeout 是 Tuple Mover 的核心操作（25.x 唯一操作），負責：

1. **合併較小的 ROS containers** 成較大的 containers
2. **清除已刪除的資料**（purge deleted records）
3. **合併 delete vectors**

### 3.2 ROS Container 限制

```
每個 projection 每節點 ROS container 上限：1024 個
參數：ContainersPerProjectionLimit
```

超過此限制會出現 `TOO MANY ROS CONTAINERS`（ROS pushback）錯誤。

> ⚠️ **不建議**調高此數值，因為欄位資料分散在多個 ROS containers 時會導致效能下降。

### 3.3 STRATA 演算法

Vertica 9.2+ 引入的 STRATA 演算法決定哪些 ROS containers 需要合併：

```
Stratum 3: [large containers]
Stratum 2: [medium containers]
Stratum 1: [small containers]
Stratum 0: [negligible containers] ← mergeout 從這裡開始
```

- 每個 stratum 預設最大 ROS 數量：**32**（參數 `ROSPerStratum`）
- 當某個 stratum 達到上限時，該 projection 的該 stratum 被標記為 eligible for mergeout
- 合併後的 ROS container 通常進入下一個 stratum
- **Stratum 0** 的特殊處理：合併**所有** eligible ROS containers 為一個 container

**目的**：確保每個 tuple 經歷 mergeout 的次數是有限且可控的，無論資料載入模式為何。

---

## 四、TM 資源池調優

### 4.1 預設值

```sql
=> SELECT name, memorysize, maxmemorysize, plannedconcurrency,
          maxconcurrency, executionparallelism, priority
   FROM resource_pools WHERE name = 'tm';
```

| 參數 | 預設值 | 說明 |
|------|--------|------|
| memorysize | 5% | TM 資源池記憶體 |
| plannedconcurrency | AUTO | 預設 3（1 moveout + 2 mergeout） |
| maxconcurrency | 7 | 最大並發數，不建議超過 6 |

### 4.2 寬表調優

若資料庫有**寬表（超過 100 個 columns）**，增加 TM 資源池的記憶體：

```sql
=> ALTER RESOURCE POOL tm MEMORYSIZE '10%';
=> ALTER RESOURCE POOL tm MAXMEMORYSIZE '10%';
```

### 4.3 增加 Mergeout 執行緒

若需要更多 mergeout threads：

```sql
=> ALTER RESOURCE POOL tm PLANNEDCONCURRENCY 4;
=> ALTER RESOURCE POOL tm MAXCONCURRENCY 4;
```

> **版本差異**：
> - **9.3 以前**：只有第一個和最後一個 thread 處理非活躍分割區
> - **10.0+**：一半 thread 處理活躍+非活躍分割區，另一半只處理活躍分割區

### 4.4 Mergeout 吞吐量診斷

當 mergeout 吞吐量低於 **1 GB/分鐘** 時需要調校：

```sql
=> SELECT dtme.node_name, dtme.projection,
          dtme.total_size_in_gb,
          dtme.mergeout_time,
          dtme.mergeout_throughput,
          dtme.include_replay_delete,
          dra.memory_mb,
          des.bytes_spilled
   FROM (
       SELECT s.node_name, s.schema_name||'.'||s.projection_name AS projection,
              (s.total_size_in_bytes / 1024^3)::NUMERIC(10,2) AS total_size_in_gb,
              DATEDIFF(MINUTE, s.time, c.time) AS mergeout_time,
              TRUNC(s.total_size_in_bytes / 1024^3 /
                    NULLIFZERO(DATEDIFF(MINUTE, s.time, c.time)), 3)::NUMERIC(5,3)
                AS mergeout_throughput,
              CASE WHEN c.plan_type = 'Replay Delete' THEN true ELSE false END
                AS include_replay_delete
       FROM dc_tuple_mover_events s
       JOIN dc_tuple_mover_events c
         ON s.node_name = c.node_name
        AND s.projection_oid = c.projection_oid
        AND s.transaction_id = c.transaction_id
        AND s.operation = 'Mergeout' AND c.operation = 'Mergeout'
        AND s.event = 'Start' AND c.event = 'Complete'
   ) dtme
   JOIN (SELECT node_name, transaction_id,
                (MAX(memory_kb) / 1024)::INT AS memory_mb
         FROM dc_resource_acquisitions WHERE pool_name='tm' GROUP BY 1, 2) dra
     USING (node_name, transaction_id)
   JOIN dc_execution_summaries des USING (node_name, transaction_id)
   WHERE dtme.mergeout_time > 1
   ORDER BY dtme.mergeout_throughput ASC
   LIMIT 20;
```

---

## 五、Mergeout 最佳實踐

### 5.1 分割區設計

| 實踐 | 說明 |
|------|------|
| **每個表 ≤ 50 個分割區** | Vertica 不跨分割區合併 ROS containers |
| **ActivePartitionCount = 2** | 若頻繁寫入最近的非活躍分割區，設為 2（預設 1） |
| **歸檔舊分割區** | 使用 `MOVE_PARTITION_TO_TABLE()` 歸檔不再存取的舊分割區 |

```sql
=> ALTER TABLE store.store_orders_fact SET ACTIVE_PARTITION_COUNT 2;
```

### 5.2 Projection 設計

| 實踐 | 說明 |
|------|------|
| **Sort order ≤ 8 個 columns** | 過多 sort columns 拖慢 mergeout |
| **避免在 sort order 中使用寬 VARCHAR** | 會大幅增加 mergeout 時間 |
| **不超過 2 個 projection sets** | 每個表不要超過 2 組 projections |

### 5.3 DELETE 與 Replay Delete 最佳化

```sql
-- 若 mergeout 卡在 replay delete，先關閉 session
=> SELECT close_session('<session_id>');

-- 推進 AHM，讓 deletes 不需要 replay
=> SELECT make_ahm_now();

-- 或手動清除已刪除資料
=> SELECT PURGE_TABLE('schema.table_name');
```

> **sort order 建議**：使用高基數 (high-cardinality) 欄位作為 sort order 的**最後一個欄位**，可以避免長時間的 replay delete。

### 5.4 MergeOutInterval

觸發 mergeout 操作的間隔時間（秒）：

```sql
-- 預設 600 秒，若 ROS containers 累積過快可調低
=> SELECT SET_CONFIG_PARAMETER('MergeOutInterval', 300);
```

### 5.5 臨時表停用 Mergeout

對於建立後很快就被刪除的臨時表，可以停用 mergeout（11.0+）：

```sql
=> ALTER TABLE temp_table SET MERGEOUT 0;
```

### 5.6 分割區重組

變更分割區表達式後，若重組未成功，ROS containers 無法被 mergeout 處理：

```sql
=> SELECT * FROM v_monitor.partition_status;
-- 若 reorganize 未完成
=> ALTER TABLE table_name REORGANIZE;
```

---

## 六、Mergeout 相關配置參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `MergeOutInterval` | 600 | mergeout 觸發間隔（秒） |
| `ActivePartitionCount` | 1 | 活躍分割區數量 |
| `PurgeMergeoutPercent` | 20 | 刪除資料佔比超過此值才清除 |
| `ROSPerStratum` | 32 | 每個 stratum 的 ROS 數，達到後觸發 mergeout |
| `MaxDVROSPerContainer` | 10 | 單一 ROS container 的 delete vector 上限 |
| `ContainersPerProjectionLimit` | 1024 | 每個 projection 的 ROS container 上限 |

---

## 七、監控 Tuple Mover

### 正在執行的 TM 操作

```sql
=> SELECT * FROM v_monitor.tuple_mover_operations
   WHERE operation_status = 'Running';
```

### Mergeout 效能歷史

```sql
=> SELECT projection_name, operation_type,
          COUNT(*) AS count,
          AVG(EXTRACT(EPOCH FROM (operation_end - operation_start)))
            AS avg_duration_sec
   FROM v_monitor.tuple_mover_operations
   WHERE operation_type = 'Mergeout'
     AND operation_end IS NOT NULL
   GROUP BY 1, 2
   ORDER BY avg_duration_sec DESC
   LIMIT 20;
```

### 檢查 ROS Pushback 風險

```sql
=> SELECT projection_name, COUNT(*) AS ros_count,
          SUM(ros_row_count) AS total_rows,
          SUM(ros_used_bytes) / (1024^3) AS size_gb
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   HAVING COUNT(*) > 500
   ORDER BY ros_count DESC;
```

---

## 八、Part 1 vs Part 2 版本差異

| 項目 | Part 1 (≤ 9.1) | Part 2 (9.2+) / 25.x |
|------|---------------|---------------------|
| **儲存** | WOS + ROS | 僅 ROS |
| **Mover 操作** | Moveout + Mergeout | 僅 Mergeout |
| **合併演算法** | 基礎 mergeout | **STRATA 演算法** |
| **WOS 管理** | 需要 MoveOutInterval 等參數 | 不適用 |
| **TM pool 預設 threads** | 1 moveout + 2 mergeout | 全部用於 mergeout |
| **ROS pushback limit** | 1024 | 1024（可調 ContainersPerProjectionLimit） |
| **臨時表停用 mergeout** | 不支援 | ✅ 11.0+ `ALTER TABLE SET MERGEOUT 0` |
| **dc_tuple_mover_events** | 可用 | ✅ 仍可用 |
| **v_monitor.tuple_mover_operations** | 可用 | ✅ 仍可用 |

---

*本文整合 Vertica KB "Tuple Mover Best Practices" Part 1 與 Part 2，所有 SQL 已更新至 Vertica 25.4.0-0。*
