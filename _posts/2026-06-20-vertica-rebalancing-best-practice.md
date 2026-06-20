---
layout: post
title: "Vertica 集群重新平衡實戰指南 — Cluster Rebalancing (25.x)"
date: 2026-06-20 14:00:00 +0800
categories: vertica rebalancing cluster
tags: [vertica, rebalancing, cluster-management, scalability, performance]
description: "Vertica 集群增減節點後的完整 Rebalancing 指南 — 從架構流程、資源池調優、四階段搬運機制，到鎖衝突處理與分批排程策略。基於 Vertica 25.4.0-0 實測經驗。"
---

當 Vertica 集群需要擴充或縮減時，**Rebalancing** 是確保資料均勻分佈、發揮所有節點效能的關鍵操作。然而，Rebalancing 是 CPU、磁碟、網路密集的操作，若未妥善規劃，可能嚴重影響線上查詢。

本文完整拆解 Vertica 25.x 的 Rebalancing 機制，從事前準備到事後驗證，提供可直接套用的實戰指南。

<!--more-->

---

## 一、什麼是 Rebalancing

當 Vertica 集群增減節點後，資料在所有節點之間必須重新分配以達到最佳效能。這個過程稱為 **Rebalancing**。

### 何時需要 Rebalancing

| 情境 | 原因 |
|------|------|
| **增加節點** | 資料量增長、需要更多計算 / 儲存資源 |
| **移除節點** | 集群過度佈建、硬體需調配他用 |

### 啟動 Rebalancing 的三種方式

```sql
-- 方式 1: 完整集群 rebalance
=> SELECT REBALANCE_CLUSTER();

-- 方式 2: 單表 rebalance
=> SELECT REBALANCE_TABLE('table_name');

-- 方式 3: 透過管理主控台 (Management Console)
```

> **注意**：Rebalancing 是 CPU、磁碟、網路密集的操作，務必排程在離峰時間執行。

---

## 二、Rebalancing 的架構與流程

### 2.1 資源池

Rebalancing 永遠使用內建的 **REFRESH** 資源池：

```sql
=> SELECT * FROM resource_pools WHERE name = 'refresh';
```

| 欄位 | REFRESH pool 預設值 (25.4) | 說明 |
|------|---------------------------|------|
| memorysize | 0% | 從 general pool 借用 |
| plannedconcurrency | AUTO | 控制同時 rebalance 的 buddy group 數 |
| maxconcurrency | (空) | 對 REFRESH pool 無效 |
| priority | -10 | **低優先級**，不干擾查詢 |
| singleinitiator | t | 僅 initiator 節點執行 |
| queuetimeout | 00:05 | |

> **關鍵**：`PLANNEDCONCURRENCY` 決定 Vertica 一次可 rebalance 多少組 table/projection。
> - 調高 → 更多平行 rebalance，但佔用更多資源
> - 調低 → 減少資源競爭，但 rebalance 時間拉長

### 2.2 Rebalancing 的四個階段

```
Phase 1: 鎖定 (Lock)
  ├── 對每個未分段 projection 取 X lock (獨佔鎖)
  └── 對每個分段 projection 取 S lock (共享鎖)

Phase 2: 複製 (Copy)
  ├── 未分段 projection: 直接複製完整資料到新節點 (低 CPU 成本)
  └── 分段 projection: 讀取、split、寫入分段資料 (高 CPU/IO 成本)

Phase 3: 分離 ROS containers (Separate) ⚡️
  ├── 將 ROS containers 依照新節點映射 split
  └── 此階段耗時最長，可佔 rebalance 總時間的 80%

Phase 4: 傳輸 (Transfer)
  ├── 將 split 後的 ROS containers 傳送到目標節點
  └── 資料傳輸後，由 Tuple Mover 在下一次 mergeout 時合併
```

### 2.3 資料搬運量範例

**4 節點 → 5 節點**：
```
原始: 每個節點 1/4 資料
目標: 每個節點 1/5 資料
搬運量: 每個舊節點需搬出約 1/20 的資料
```

### 2.4 節點插入位置

Vertica 會自動選擇**最小化資料搬運**的新節點位置。

```
三節點集群加入第四個節點:
    [N1] ←→ [N2] ←→ [N3]
              ↓
             [N4]    ← 從 N2 搬資料
```

---

## 三、Rebalancing 前的準備工作

### ✅ 確認 Local Segmentation 已關閉

```sql
=> SELECT parameter_name, current_value
   FROM configuration_parameters
   WHERE parameter_name = 'EnableLocalSegmentCreation';
```
確認值為 `0`（預設即關閉）。

### ✅ 檢查 CPU 與網路頻寬

使用 Vertica 內建工具建立效能基線（**不要在 rebalance 期間執行**）：

```bash
# 磁碟 IO 效能測試
$ vioperf

# 網路吞吐量測試
$ vnetperf
```

### ✅ 確認磁碟空間充足

**至少需要資料庫大小 40% 的可用空間**，因 rebalance 需要大量暫存空間。

```bash
# 檢查各節點磁碟空間
$ df -h
```

```sql
=> SELECT SUM(used_bytes) / (1024^3) AS database_size_gb
   FROM v_monitor.projection_storage;
```

### ✅ 檢查 REFRESH 資源池設定

```sql
=> SELECT name, memorysize, plannedconcurrency, priority,
          queuetimeout, singleinitiator
   FROM resource_pools
   WHERE name = 'refresh';
```

若需調整：
```sql
-- 增加平行度 (加速但佔用更多資源)
=> ALTER RESOURCE POOL refresh PLANNEDCONCURRENCY 4;

-- 或降低平行度 (減少資源競爭)
=> ALTER RESOURCE POOL refresh PLANNEDCONCURRENCY 2;
```

### ✅ 檢查 LockTimeout

```sql
=> SELECT GET_CONFIG_PARAMETER('LockTimeout');
-- 預設: 300 秒
```

若 ETL 作業可能與 rebalance 衝突，可調高：
```sql
=> ALTER DATABASE testdb SET LockTimeout = 600;
```

> **重要**：Rebalance 完成後記得重設回預設值。

### ✅ 檢查 DMLCancelTM

```sql
=> SELECT GET_CONFIG_PARAMETER('DMLCancelTM');
```

- `true` (預設)：DML 可以取消 Tuple Mover 任務以取得鎖
- `false`：Tuple Mover 優先，DML 需等待

若 ETL 為關鍵任務，保持 `true`。

---

## 四、Rebalancing 中的監控

### 查看正在 rebalance 的表

```sql
=> SELECT * FROM v_monitor.rebalance_table_status
   WHERE is_latest = true
   ORDER BY start_timestamp;
```

關鍵欄位：`table_name`, `rebalance_method`, `separated_percent`, `transferred_percent`

### 查看每個 projection 的進度

```sql
=> SELECT projection_name, projection_schema, rebalance_method,
          separated_percent, transferred_percent
   FROM v_monitor.rebalance_projection_status
   WHERE is_latest = true
     AND (separated_percent <> 100 OR transferred_percent <> 100)
   ORDER BY projection_name;
```

### 整體進度摘要

```sql
=> SELECT rebalance_method,
          CASE
            WHEN (separated_percent = 100 AND transferred_percent = 100)
              THEN 'Completed'
            WHEN (separated_percent <> 0 AND separated_percent <> 100)
              OR (transferred_percent <> 0 AND transferred_percent <> 100)
              THEN 'In Progress'
            ELSE 'Queued'
          END AS status,
          COUNT(*) AS count
   FROM v_monitor.rebalance_projection_status
   WHERE is_latest = true
   GROUP BY 1, 2
   ORDER BY 1, 2;
```

### 查看執行中的 rebalance session

```sql
=> SELECT node_name, session_id, session_start_timestamp, description
   FROM system_sessions
   WHERE session_type = 'REBALANCE_CLUSTER'
     AND is_active;
```

### 查看各操作階段耗時

```sql
=> SELECT node_name, object_name, operation_name,
          operation_start_timestamp, operation_end_timestamp,
          (operation_end_timestamp - operation_start_timestamp) AS duration,
          operation_status
   FROM v_monitor.rebalance_operations
   WHERE is_latest = true
   ORDER BY operation_start_timestamp;
```

### 查看 Tuple Mover 的 ROS 分離進度

```sql
=> SELECT tm.projection_name, tm.node_name,
          tm.operation_start_timestamp
   FROM v_monitor.tuple_mover_operations tm
   JOIN system_sessions USING (session_id)
   WHERE system_sessions.is_active
     AND session_type = 'REBALANCE_CLUSTER'
     AND operation_status = 'Running';
```

### 查看每個 projection 的 rebalance 時間

```sql
=> SELECT node_name, projection_schema, projection_name,
          start_time, (time - start_time) AS duration
   FROM dc_rebalanced_projections
   ORDER BY duration DESC;
```

---

## 五、Rebalancing 後的檢查

### 確認所有表已 rebalance

```sql
=> SELECT table_name,
          CASE
            WHEN separated_percent + transferred_percent = 200
              THEN 'REBALANCED'
            WHEN (separated_percent + transferred_percent) < 200
              AND (separated_percent + transferred_percent) > 0
              THEN 'REBALANCING'
            ELSE 'NOT REBALANCED YET'
          END AS status
   FROM v_monitor.rebalance_table_status
   WHERE is_latest = true
   ORDER BY table_name;
```

### 檢查是否有過期 projection

```sql
=> SELECT projection_name, anchor_table_name, is_prejoin, is_up_to_date
   FROM projections
   WHERE is_up_to_date = false;
```

若找到過期 projection，手動刪除：
```sql
=> DROP PROJECTION schema_name.projection_name;
```

### 檢查 ROS container 數量

Rebalancing 後可能產生大量 ROS containers（特別是多小檔案的環境）：

```sql
=> SELECT projection_name, COUNT(*) AS ros_count
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   ORDER BY ros_count DESC
   LIMIT 20;
```

等待 Tuple Mover 完成 mergeout 後數量應恢復正常。

---

## 六、鎖衝突與並發問題處理

### 常見鎖衝突情境

| 操作 | 衝突類型 | 錯誤訊息 |
|------|----------|----------|
| DDL（ALTER TABLE, 增刪 column） | 與 rebalance 的 X lock 衝突 | `ERROR 3007: DDL statement interfered` |
| DML（INSERT/UPDATE/DELETE） | 與 rebalance 的 S lock 衝突 | `ERROR 5157: Locking failure - Timed out` |
| SWAP / MOVE PARTITION | projection 定義不一致 | `ERROR 7121: Tables do not match` |
| 鎖等待 timeout | 超過 LockTimeout | `Unavailable: S lock table - timeout error` |

### 鎖衝突診斷

**查詢哪些時段 ETL 持有鎖超過 5 分鐘**：

```sql
=> SELECT DATE_TRUNC('hour', grant_time) AS hour,
          node_name,
          COUNT(*) AS number_of_tx,
          MAX(time - grant_time) AS max_time_lock_held
   FROM dc_lock_releases
   WHERE (time - grant_time) > INTERVAL '5 min'
     AND mode IN ('X', 'S', 'O')
     AND object_name NOT LIKE 'ElasticCluster'
   GROUP BY 1, 2
   ORDER BY 4 DESC;
```

### 解決方案

#### 解法 A：調高 LockTimeout

```sql
=> SELECT GET_CONFIG_PARAMETER('LockTimeout');  -- 預設 300 秒
=> ALTER DATABASE testdb SET LockTimeout = 600;  -- 調高
-- Rebalance 完成後記得重設
=> ALTER DATABASE testdb SET LockTimeout = 300;
```

#### 解法 B：讓 Rebalance 優先

若 rebalance 需要優先於 ETL，可暫時關閉 DMLCancelTM：

```sql
=> SELECT SET_CONFIG_PARAMETER('DMLCancelTM', false);
=> SELECT REBALANCE_CLUSTER();
-- 完成後恢復
=> SELECT SET_CONFIG_PARAMETER('DMLCancelTM', true);
```

> **注意**：`DMLCancelTM = false` 時，DML 無法中斷 Tuple Mover 任務，可能導致 DML 超時。請謹慎使用。

#### 解法 C：手動分批 Rebalance（見下一節）

---

## 七、手動控制 Rebalancing

### 為什麼需要手動分批

若集群有大量資料和多張表，完整 rebalance 可能需要多個晚上或週末才能完成。手動分批可：
- 每次只 rebalance 少量表
- 避開 ETL 執行時段
- 減少鎖衝突視窗

### 手動 Rebalance 單表

```sql
=> SELECT REBALANCE_TABLE('table_name');
```

### 確認哪些表已/未 Rebalance

```sql
=> SELECT table_name,
          CASE
            WHEN separated_percent + transferred_percent = 200
              THEN 'REBALANCED'
            WHEN (separated_percent + transferred_percent) < 200
              AND (separated_percent + transferred_percent) > 0
              THEN 'REBALANCING'
            ELSE 'NOT REBALANCED YET'
          END AS status
   FROM v_monitor.rebalance_table_status
   WHERE is_latest = true
   ORDER BY status, table_name;
```

### 手動排程策略

```
週一 22:00: REBALANCE_TABLE('table_A'), REBALANCE_TABLE('table_B')
週二 22:00: REBALANCE_TABLE('table_C'), REBALANCE_TABLE('table_D')
...
持續直到所有表完成
```

---

## 八、常見錯誤與排除

### ERROR 3007: DDL statement interfered

**原因**：DDL 操作與 rebalance 鎖衝突。

**解法**：
- 暫停 DDL 直到 rebalance 完成
- 或先完成 DDL 再啟動 rebalance
- 使用 `REBALANCE_TABLE()` 分批避開衝突表

### ERROR 5157: Locking failure - Timed out

**原因**：等待鎖超過 LockTimeout（預設 300 秒）。

**解法**：
- 調高 LockTimeout
- 在離峰時間執行 rebalance
- 分批 rebalance 減少鎖競爭

### ERROR 7121: Projections definition mismatch

**原因**：對已 rebalance 和未 rebalance 的表執行 SWAP PARTITION。

**解法**：SWAP PARTITION 只能在**兩邊都已 rebalance 完成**或**兩邊都未 rebalance** 的表之間執行。

### Rebalance 失敗後重試

若 rebalance 因錯誤失敗或被 DML 取消，Vertica 會自動重試。解決根本原因後：

```sql
=> SELECT REBALANCE_CLUSTER();
```

Rebalancing 會從上次失敗的地方繼續，不會從頭開始。

### 檢查 rebalance 錯誤日誌

```sql
=> SELECT time, session_id, error_level, node_name, log_message
   FROM dc_errors
   WHERE session_id IN (
     SELECT DISTINCT session_id
     FROM dc_session_starts
     WHERE session_type = 'REBALANCE_CLUSTER'
   )
   ORDER BY time DESC;
```

---

## 九、快速檢查清單

### Rebalance 前

```sql
-- [ ] 檢查 local segmentation 是否關閉
=> SELECT parameter_name, current_value
   FROM configuration_parameters
   WHERE parameter_name = 'EnableLocalSegmentCreation';

-- [ ] 檢查 REFRESH resource pool
=> SELECT name, memorysize, plannedconcurrency, priority
   FROM resource_pools WHERE name = 'refresh';

-- [ ] 檢查 LockTimeout (建議調高)
=> SELECT GET_CONFIG_PARAMETER('LockTimeout');

-- [ ] 預估所需磁碟空間 (至少 40%)
=> SELECT SUM(used_bytes) / (1024^3) AS database_size_gb
   FROM v_monitor.projection_storage;
```

### Rebalance 中

```sql
-- [ ] 查看整體進度
=> SELECT rebalance_method, COUNT(*) AS total,
          SUM(CASE WHEN separated_percent + transferred_percent = 200
                THEN 1 ELSE 0 END) AS completed
   FROM v_monitor.rebalance_projection_status
   WHERE is_latest = true
   GROUP BY rebalance_method;

-- [ ] 查看仍在進行的 projection
=> SELECT projection_name, rebalance_method,
          separated_percent, transferred_percent
   FROM v_monitor.rebalance_projection_status
   WHERE is_latest = true
     AND (separated_percent <> 100 OR transferred_percent <> 100);

-- [ ] 查看操作時間
=> SELECT node_name, object_name, operation_name,
          (operation_end_timestamp - operation_start_timestamp) AS duration
   FROM v_monitor.rebalance_operations
   WHERE is_latest = true
   ORDER BY duration DESC
   LIMIT 20;
```

### Rebalance 後

```sql
-- [ ] 確認所有表已 rebalance
=> SELECT COUNT(*) AS not_rebalanced
   FROM v_monitor.rebalance_table_status
   WHERE is_latest = true
     AND separated_percent + transferred_percent <> 200;

-- [ ] 檢查過期 projection
=> SELECT projection_name, anchor_table_name
   FROM projections
   WHERE is_up_to_date = false;

-- [ ] 重設 LockTimeout (若有調高)
=> ALTER DATABASE testdb SET LockTimeout = 300;

-- [ ] 檢查 ROS container 健康
=> SELECT projection_name, COUNT(*) AS ros_count
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   HAVING COUNT(*) > 1000
   ORDER BY ros_count DESC;
```

---

## 十、黃金法則總結

| 階段 | 要點 |
|------|------|
| **事前準備** | 確保至少 40% 額外磁碟空間；檢查 REFRESH pool 設定；調高 LockTimeout（建議 600s）；排程在離峰時間 |
| **執行中** | 避免同時執行 DDL / SWAP PARTITION；監控 separated_percent 和 transferred_percent；確認 Tuple Mover 正常運作 |
| **效能調優** | 增加 REFRESH pool 的 PLANNEDCONCURRENCY → 加速；降低 PLANNEDCONCURRENCY → 減少資源競爭 |
| **衝突處理** | LockTimeout 調高 → ETL 不易 timeout；DMLCancelTM = false → rebalance 優先；分批 REBALANCE_TABLE() → 最小化鎖衝突 |
| **完成後** | 檢查過期 projection（`is_up_to_date = false`）；重設 LockTimeout；建立新效能基線（vioperf / vnetperf） |
| **不建議** | Rebalance 期間執行 vioperf / vnetperf；對已/未 rebalance 的表做 SWAP PARTITION；同時做大量 DDL |

---

*本文基於 Vertica 25.4.0-0 實測撰寫，SQL 範例皆在實際環境驗證通過。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
