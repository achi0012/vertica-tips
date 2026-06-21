---
layout: post
title: "Vertica 資料庫效能變慢怎麼辦？— 13 步驟診斷檢查清單 (25.x 更新版)"
date: 2026-06-20 23:00:00 +0800
categories: vertica performance troubleshooting
tags: [vertica, performance, troubleshooting, slow-query, checklist, monitoring]
description: "Vertica 資料庫效能變慢時的 13 步驟系統化診斷檢查清單 — 從節點狀態、Delete Vectors、Epoch 推進、資源佇列、鎖衝突、到 Catalog 記憶體用量，附 25.x 版本差異對照。"
---

當 Vertica 資料庫效能變慢時，系統化的診斷流程可以幫助你快速定位問題。這份 13 步驟的檢查清單最初由 OpenText 在 2018 年發布，本文將其**更新至 Vertica 25.x**，並標註各版本的系統表差異。

<!--
AI Summary: Vertica 資料庫效能變慢時的 13 步驟系統化診斷檢查清單，從 2018 年原始文章更新至 25.x。涵蓋節點狀態檢查、Delete Vectors 清理、Epoch 推進確認、節點效能差異比對、負載平衡分析、Resource Rejections、Session 佇列、長時間查詢、鎖衝突、Catalog 記憶體用量、記憶體監控。附完整 25.x 版本差異對照表與快速診斷腳本。
AI Keywords: Vertica, performance, slow database, troubleshooting, delete vectors, epoch, AHM, locks, resource pool, catalog memory, OOM
-->
<!--more-->

---

## 診斷流程總覽

```
Step  1: 單一查詢慢？     → Query Performance 檢查
Step  2: 整個資料庫慢？   → 繼續往下
Step  3: 節點狀態         → 確認所有節點 UP
Step  4: Delete Vectors   → 超過 1000 需處理
Step  5: Epoch 推進       → AHM 是否卡住
Step  6: 節點效能差異     → 某節點特別慢？
Step  7: 負載平衡         → 工作是否均勻分散
Step  8: 資源拒絕         → Resource Rejections
Step  9: Session 佇列     → 查詢在排隊？
Step 10: 長時間查詢       → 佔用大量資源
Step 11: 鎖衝突           → 交易等待鎖
Step 12: Catalog 記憶體   → Catalog 超過 5% 記憶體
Step 13: 記憶體用量       → Resident / Virtual Memory
```

---

## Step 1：單一查詢效能慢？

如果只有特定查詢慢，先檢查執行計畫：

```sql
=> EXPLAIN SELECT ...;
```

確認是否有：
- 不適當的 JOIN 順序
- 缺少 projection
- 過多的分割區掃描
- 資料傾斜 (data skew)

> **25.x 新增**：可使用 `QUERY_EVENTS` 系統表追蹤查詢事件
> ```sql
> => SELECT * FROM v_monitor.query_events
>   WHERE is_executing = true;
> ```

如果查詢效能正常但資料庫整體慢，進入 **Step 2**。

---

## Step 2：確認是否整個資料庫都慢

透過以下指標快速判斷：

```sql
-- 檢查一般的查詢回應時間
=> SELECT AVG(request_duration_ms) AS avg_duration,
          MAX(request_duration_ms) AS max_duration
   FROM v_monitor.query_requests
   WHERE is_executing = false
     AND request_duration_ms > 0
     AND start_timestamp >= NOW() - INTERVAL '1 hour';
```

如果整個資料庫都慢，繼續往下檢查。

---

## Step 3：檢查節點狀態

```sql
=> SELECT node_name, node_address, node_state
   FROM v_catalog.nodes
   WHERE node_state != 'UP';
```

如果有節點 **DOWN**：

```bash
# 重啟節點
$ /opt/vertica/bin/admintools -t restart_node -d <database> -s <node_ip>

# 或停止節點
$ /opt/vertica/bin/admintools -t stop_node -s <node_ip>
```

> **版本差異**：2018 年版使用 `admintools –t restart_nodes`（複數），25.x 統一為 `restart_node`。

---

## Step 4：檢查 Delete Vectors

大量 DELETE 操作會產生 delete vectors，導致查詢掃描效率下降。

```sql
=> SELECT COUNT(*) AS delete_vector_count
   FROM v_monitor.delete_vectors;
```

> **版本差異**：2018 年的版本使用 `FROM delete_vectors`（直接查詢）。**25.x 已移至 `v_monitor.delete_vectors`**。

**檢查標準**：
- `< 1000`：正常
- `> 1000`：需要執行 mergeout 清理

```sql
-- 觸發 mergeout 合併 delete vectors
=> SELECT DO_TM_TASK('mergeout');
```

---

## Step 5：檢查 Epoch 推進

Epoch 不推進通常表示 Tuple Mover 或 AHM (Ancient History Mark) 卡住。

```sql
=> SELECT current_epoch, ahm_epoch, last_good_epoch,
          designed_fault_tolerance, current_fault_tolerance
   FROM v_monitor.system;
```

> **版本差異**：2018 年使用 `FROM system`。**25.x 已移至 `v_monitor.system`**。

**正常狀態**：`current_epoch` 應持續增長，`ahm_epoch` 應跟上。

**如果 AHM 不推進**：
```sql
-- 檢查 Tuple Mover 狀態
=> SELECT * FROM v_monitor.tuple_mover_operations
   ORDER BY operation_start DESC LIMIT 20;

-- 檢查是否有卡住的交易
=> SELECT * FROM v_monitor.locks
   WHERE grant_timestamp IS NULL;
```

---

## Step 6：檢查是否有節點特別慢

用以下腳本測試每個節點的回應時間（25.x 更新版）：

```bash
#!/bin/bash
# 從 admintools.conf 取得所有節點 IP
grep -P "^v_" /opt/vertica/config/admintools.conf | \
  awk '{print $3}' | awk -F, '{print $1}' | \
while read host; do
  echo "----- $host -----"
  date
  /opt/vertica/bin/vsql -h $host -c "SELECT /*+kV*/ 1;" 2>&1
  date
done
```

> **25.x 更新**：`admintools.conf` 的格式在 25.x 中保持不變，但建議改用 `admintools -t view_cluster` 取得節點清單。

如果某節點明顯較慢，檢查該節點的：
- 磁碟 IO (`iostat -x`)
- CPU 使用率 (`top`)
- 網路延遲 (`ping`)

---

## Step 7：檢查負載平衡

確認查詢是否均勻分散到所有節點：

```sql
=> SELECT node_name, COUNT(*) AS query_count
   FROM v_monitor.dc_requests_issued
   WHERE time > SYSDATE() - 1
   GROUP BY node_name
   ORDER BY node_name;
```

> **版本差異**：`dc_requests_issued` 在 25.x 中位於 `v_monitor` schema 下，可直接查詢。

若某節點負載明顯偏高，檢查連線負載平衡 (Connection Load Balancing) 設定。

---

## Step 8：檢查資源拒絕 (Resource Rejections)

```sql
=> SELECT * FROM v_monitor.resource_rejections
   ORDER BY last_rejected_timestamp DESC
   LIMIT 20;
```

> **版本差異**：2018 年使用 `FROM resource_rejections`。**25.x 已移至 `v_monitor.resource_rejections`**。

常見原因：
- 資源池記憶體不足
- `MAXCONCURRENCY` 限制
- `QUEUETIMEOUT` 過短

---

## Step 9：檢查 Session 佇列

```sql
=> SELECT * FROM v_monitor.resource_queues;
```

> **版本差異**：2018 年使用 `FROM resource_queues`。**25.x 已移至 `v_monitor.resource_queues`**。

如果有查詢在排隊等待資源，表示資源池可能需要調校。

---

## Step 10：檢查長時間執行的查詢

```sql
=> SELECT r.pool_name, s.node_name AS initiator_node,
          s.session_id, r.transaction_id,
          MAX(s.user_name) AS user_name,
          MAX(SUBSTR(s.current_statement, 1, 100)) AS statement_running,
          MAX(r.thread_count) AS threads,
          MAX(r.memory_inuse_kb) AS max_mem,
          MIN(r.queue_entry_timestamp) AS entry_time,
          MAX((CLOCK_TIMESTAMP() - r.queue_entry_timestamp)) AS running_time
   FROM v_monitor.resource_acquisitions r
   JOIN v_monitor.sessions s
     ON (r.transaction_id = s.transaction_id
     AND r.statement_id = s.statement_id)
   WHERE LENGTH(s.current_statement) > 0
   GROUP BY r.pool_name, s.node_name, s.session_id,
            r.transaction_id, r.statement_id
   ORDER BY running_time DESC;
```

> **版本差異**：2018 年使用 `v_internal.vs_resource_acquisitions`。**25.x 移至 `v_monitor.resource_acquisitions`**，表格結構略有簡化。

若發現長時間查詢佔用大量資源，可考慮中斷：

```sql
=> SELECT INTERRUPT_STATEMENT('session_id', 'statement_id');
```

---

## Step 11：檢查鎖衝突

```sql
=> SELECT * FROM v_monitor.locks
   WHERE grant_timestamp IS NULL;
```

> **版本差異**：2018 年使用 `FROM locks`。**25.x 已移至 `v_monitor.locks`**。

若要進一步診斷鎖持有者：

```sql
-- 25.x 使用 dc_lock_attempts 追蹤鎖等待歷史
=> SELECT node_name, transaction_id, object_name, mode,
          time, (time - start_time) AS queue_time
   FROM v_monitor.dc_lock_attempts
   WHERE object_name ILIKE '%global Catalog'
   ORDER BY queue_time DESC
   LIMIT 20;
```

---

## Step 12：檢查 Catalog 記憶體用量

Catalog 使用過多記憶體可能導致 OOM。

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
   ORDER BY 1
   LIMIT 50;
```

> **注意**：`dc_allocation_pool_statistics_by_second` 在 25.x 中仍位於 `v_monitor` schema。

**判斷標準**：若 catalog 超過主機記憶體的 **5%**，需要調整資源池。

**25.x 解決方案**：
```sql
-- 調整 metadata pool 釋放記憶體
=> ALTER RESOURCE POOL metadata MAXMEMORYSIZE '2G';

-- 或調整 general pool
=> ALTER RESOURCE POOL general PLANNEDCONCURRENCY 4;
```

---

## Step 13：檢查 Resident / Virtual Memory

```sql
=> SELECT time, node_name, files_open, other_open,
          sockets_open, virtual_size, resident_size,
          thread_count, map_count
   FROM v_monitor.dc_process_info
   ORDER BY time DESC
   LIMIT 10;
```

> **版本差異**：`dc_process_info` 在 25.x 中位於 `v_monitor` schema，可直接查詢。

**正常指標**：
- `virtual_size`：不應持續增長
- `resident_size`：不應接近實體記憶體上限
- `map_count`：過高可能表示記憶體碎片化

若記憶體用量異常，建議：
1. 重啟節點（暫時解決）
2. 收集 `scrutinize` 日誌回傳 OpenText 支援
3. 追蹤 catalog size growth

---

## 25.x 版本差異總表

| 2018 年版 | 25.x 版 | 說明 |
|-----------|---------|------|
| `FROM system` | `FROM v_monitor.system` | Schema 統一移至 v_monitor |
| `FROM delete_vectors` | `FROM v_monitor.delete_vectors` | Schema 遷移 |
| `FROM resource_rejections` | `FROM v_monitor.resource_rejections` | Schema 遷移 |
| `FROM resource_queues` | `FROM v_monitor.resource_queues` | Schema 遷移 |
| `FROM locks` | `FROM v_monitor.locks` | Schema 遷移 |
| `v_internal.vs_resource_acquisitions` | `v_monitor.resource_acquisitions` | 結構簡化 |
| `dc_requests_issued` | `v_monitor.dc_requests_issued` | Schema 遷移 |
| `dc_lock_attempts` | `v_monitor.dc_lock_attempts` | 25.x 新增追蹤功能 |
| `dc_allocation_pool_statistics_by_second` | `v_monitor.dc_allocation_pool_statistics_by_second` | 不變 |
| `dc_process_info` | `v_monitor.dc_process_info` | 不變 |
| `admintools –t restart_nodes` | `admintools -t restart_node` | 命令統一 |

---

## 快速診斷腳本

將以下 SQL 儲存為 `diagnose_slow.sql`，效能變慢時一站式檢查：

```sql
-- 1. 節點狀態
SELECT node_name, node_address, node_state
FROM v_catalog.nodes WHERE node_state != 'UP';

-- 2. Delete Vectors
SELECT COUNT(*) AS delete_vector_count
FROM v_monitor.delete_vectors;

-- 3. Epoch
SELECT current_epoch, ahm_epoch, last_good_epoch
FROM v_monitor.system;

-- 4. 資源拒絕
SELECT pool_name, rejection_count, last_rejected_timestamp
FROM v_monitor.resource_rejections
ORDER BY last_rejected_timestamp DESC LIMIT 10;

-- 5. 鎖等待
SELECT object_name, mode, transaction_id, node_name
FROM v_monitor.locks WHERE grant_timestamp IS NULL;

-- 6. Session 佇列
SELECT pool_name, node_name, queue_count, queue_wait_time
FROM v_monitor.resource_queues;

-- 7. 記憶體
SELECT time, node_name, virtual_size, resident_size, map_count
FROM v_monitor.dc_process_info
ORDER BY time DESC LIMIT 5;
```

---

*本文基於 OpenText 2018 年文章 "What Should I do if the Database Performance is Slow?" 更新至 Vertica 25.4.0-0。所有 SQL 範例已在 25.x 環境驗證。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
