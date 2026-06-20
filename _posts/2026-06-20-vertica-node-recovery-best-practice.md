---
layout: post
title: "Vertica 節點故障恢復實戰指南 — Node Down Recovery (25.x)"
date: 2026-06-20 12:00:00 +0800
categories: vertica node-recovery HA
tags: [vertica, node-recovery, high-availability, K-safety, troubleshooting]
description: "Vertica 節點故障後的完整恢復指南 — 從節點狀態轉換、Recovery 兩階段流程、資源池調優，到 Dirty Transaction 處理與 5 大常見問題排解。基於 Vertica 25.4.0-0 實測經驗。"
---

資料庫叢集中最不想遇到的場景之一，就是節點突然 **DOWN** 了。當 Vertica 集群中一個節點離線，它不參與任何從 down 機時間點之後提交的 transaction。重新啟動後，必須從 **buddy nodes** 恢復遺失的資料，才能重新上線服務。

本文將完整拆解 Vertica 25.x 的節點恢復流程，並提供實戰排解指南。

<!--more-->

---

## 一、節點狀態轉換

當節點發生故障並重新啟動時，它會經歷以下狀態轉換：

```
DOWN → (重啟) → INITIALIZING → RECOVERING → READY → UP
```

| 狀態 | 說明 |
|------|------|
| **DOWN** | 節點離線，不參與任何 transaction |
| **INITIALIZING** | 節點重新加入集群，接收新的 global catalog |
| **RECOVERING** | 正在從 buddy node 恢復資料；可參與資料載入 |
| **READY** | 恢復完成，等待轉換 |
| **UP** | 完全恢復，接受連線與查詢 |

### 25.x 關鍵特性

- **多表並行恢復**：Vertica 7.2.2+ 支援同時恢復多張表（取決於 RECOVERY pool 的 MAXCONCURRENCY）
- **Tuple Mover 在 Recovery 期間仍可執行**：mergeout 和 moveout 在 recovery 期間繼續運作
- **可指定恢復順序**：透過 `recover_priority` 控制表的恢復優先級

---

## 二、Recovery 兩階段流程

### 2.1 第一階段：Pre-Recovery（恢復前）

從節點重啟開始，到重新加入集群為止。日誌位置：`startup.log` 與 `vertica.log`。

| 步驟 | 說明 |
|------|------|
| **1. 讀取 catalog** | 讀取 catalog checkpoint → 應用 transaction log → 建立 catalog objects 索引 |
| **2. 啟動 spread daemon** | 啟動並連接 spread daemon |
| **3. 讀取 DataCollector** | 建立 DataCollector 檔案的 inventory |
| **4. 檢查資料儲存** | 驗證 catalog 中所有資料檔案的存在與正確性；移除未引用的檔案 |
| **5. 載入 UDx** | 載入 User Defined Extension libraries |
| **6. 準備集群邀請** | 加入 spread group → 廣播訊息 → 等待其他節點邀請 |
| **7. 加入集群** | 收到邀請後加入集群 → 狀態變為 INITIALIZING |
| **8. 接收新 global catalog** | UP 節點以 1GB chunks 傳送 global catalog；接收並安裝新 catalog |

**監控方式**：使用 `tail -f startup.log` 查看 recovery phase json 訊息。

```json
{
  "goal": 477688606,
  "node": "v_db_node0003",
  "progress": 146732903,
  "stage": "Read DataCollector",
  "text": "Inventory files (bytes)",
  "timestamp": "2016-03-16 17:28:32.016"
}
```

### 2.2 第二階段：Recovery（恢復）

從節點加入集群開始，到完全恢復為 UP。

| 步驟 | 說明 |
|------|------|
| **1. 回放遺失的 catalog events** | 按 commit 順序回放 catalog events（ALTER PARTITION, RESTORE TABLE 等） |
| **2. 標記 dirty transactions** | 標記在 RECOVERY 階段開始前啟動但未提交的 transaction |
| **3. 載入 UDx** | 從 UP 節點接收並載入 UDx |
| **4. 建立 recovery 表清單** | 列出需要恢復的所有 table |
| **5. 執行 table recovery** | 從 buddy node 恢復資料（最多重試 20 次） |
| **6. 檢查清單 → READY** | 清單為空 → 狀態變為 READY |
| **7. 轉為 UP** | 開始接受連線，參與所有 database plan |

---

## 三、Recovery 資源池調優

Recovery 使用內建的 **RECOVERY** 資源池：

```sql
=> SELECT * FROM resource_pools WHERE name = 'recovery';
```

### RECOVERY pool 預設值 (25.4)

| 參數 | 預設值 | 說明 |
|------|--------|------|
| memorysize | 0% | 從 general pool 借用 |
| plannedconcurrency | AUTO | 自動 |
| **maxconcurrency** | **5** | **關鍵**：控制同時可恢復的 buddy group 數 |
| priority | 107 | 高優先級 |
| runtimepriority | MEDIUM | |
| queuetimeout | 00:05 | |
| singleinitiator | t | |
| executionparallelism | AUTO | |

### 調優建議

```sql
-- 查看當前 RECOVERY pool
=> SELECT name, maxconcurrency, plannedconcurrency,
          priority, queuetimeout, memorysize
   FROM resource_pools
   WHERE name = 'recovery';

-- 增加平行度 → 加快 recovery (但佔用更多資源)
=> ALTER RESOURCE POOL recovery MAXCONCURRENCY 8;

-- 降低平行度 → 減少對線上查詢的影響
=> ALTER RESOURCE POOL recovery MAXCONCURRENCY 2;
```

> **注意**：每個 table 可能有 b0/b1 兩個 buddy projections，所以實際並行恢復的表數約為 maxconcurrency / 2。

---

## 四、Recovery 方法

`PROJECTION_RECOVERIES` 表的 `method` 欄位顯示使用的恢復方法：

| 方法 | 說明 | 適用場景 |
|------|------|----------|
| **recovery-by-container** | 直接複製整個 ROS container | 小資料量、少量變更 |
| **incremental** | 增量恢復，只複製差異部分 | 大量資料、大部分已在本地 |
| **incremental-replay-delete** | 增量恢復 + 回放 DELETE 操作 | 有大量 DELETE 操作後的恢復 |

```sql
=> SELECT node_name, projection_name, method, status, progress,
          detail, start_time, end_time
   FROM v_monitor.projection_recoveries
   ORDER BY start_time DESC
   LIMIT 20;
```

輸出範例：
```
recovery-by-container   → 直接複製 container
incremental             → Scan:XX% Sort:XX% Write:XX% (增量，階段進度)
incremental-replay-delete → Delete: X/Y (回放 DELETE 操作)
```

---

## 五、監控 Recovery 進度

### 整體恢復狀態

```sql
=> SELECT node_name, is_running, recovery_phase,
          current_completed, current_total,
          historical_completed, historical_total,
          splits_completed, splits_total,
          epoch, recover_epoch
   FROM v_monitor.recovery_status;
```

| 欄位 | 說明 |
|------|------|
| recovery_phase | 當前階段 (Pre-Recovery, Recovery 等) |
| current_completed / current_total | 當前資料恢復進度 |
| historical_completed / historical_total | 歷史資料恢復進度 |
| splits_completed / splits_total | ROS container split 進度 |

### 各表恢復狀態

```sql
-- 當前正在恢復哪張表
=> SELECT node_name, recovering_table_name, tables_remain,
          node_recovery_start_time, recover_epoch
   FROM v_monitor.table_recovery_status
   WHERE is_running = true;
```

### 各 Projection 恢復詳細資料

```sql
=> SELECT node_name, projection_name, method, status, progress,
          detail, start_time, end_time, runtime_priority
   FROM v_monitor.projection_recoveries
   ORDER BY start_time DESC
   LIMIT 50;
```

### phase 欄位解讀

| phase 值 | 說明 |
|----------|------|
| (empty) | 等待中或已完成 |
| historical | 正在恢復歷史資料 |
| historical dirty | 正在處理 dirty transactions 的歷史部分 |
| current replay delete | 正在回放 DELETE 操作 |

### 檢查 recovery 錯誤

```sql
=> SELECT node_name, table_name, status, phase, start_time,
          end_time, recover_error, recover_priority
   FROM v_monitor.table_recoveries
   WHERE status = 'error-retry'
   ORDER BY start_time DESC;
```

### 日誌監控

```bash
# Pre-Recovery 階段日誌
$ tail -f catalog-path/database-name/v_db_node_catalog/startup.log

# 一般日誌
$ tail -f catalog-path/database-name/v_db_node_catalog/vertica.log

# 檢查 recovery 重試次數
$ grep "incrCatchUpFailureCount" vertica.log
# 範例: [Recover] <INFO> incrCatchUpFailureCount: 17 failures, max 20
```

---

## 六、Dirty Transaction 處理

### 什麼是 Dirty Transaction

在 RECOVERY 階段開始前已啟動但**未提交**的 transaction。Vertica 會：

1. **標記** dirty transactions
2. **等待** 最多 5 分鐘讓它們 commit（受 `RecoveryDirtyTxnWait` 控制）
3. **終止** 如果 5 分鐘後仍未 commit

### 哪些操作會引發 Dirty Transaction

| 操作類型 | 範例 |
|----------|------|
| TRUNCATE TABLE | 清除資料但不影響 catalog 結構 |
| ADD COLUMN | DDL 操作未完成 |
| ALTER PARTITION | 分割區變更 |
| DROP / RESTORE TABLE | 表結構變更 |
| MOVE / SWAP PARTITION | 分割區移動 |
| REBALANCE TABLE | 資料重新平衡 |
| REPLACE NODE | 節點替換 |
| MERGE PROJECTION | projection 合併 |

### Dirty Transaction 的影響

- Dirty transactions **不會**持有 lock
- 恢復時資料透過 `incremental` 或 `incremental-replay-delete` 方法恢復
- 若 dirty transaction 造成恢復失敗，Vertica 會自動重試（最多 20 次）

---

## 七、常見問題排解

### 7.1 節點無法重新加入集群

**症狀**：節點卡在 INITIALIZING 狀態

| 步驟 | 指令 / 說明 |
|------|-----------|
| 1. 檢查 startup.log | `tail -f <catalog-path>/startup.log` |
| 2. 檢查 vertica.log | `tail -f <catalog-path>/vertica.log` |
| 3. 檢查 spread.conf | 確認所有節點的 spread.conf 一致 |
| 4. 檢查 IP 位址 | 確認 recovering node 的 IP 在 spread.conf 中正確 |
| 5. 清除暫存檔案 | 若看到 ERROR 4803，刪除 `/tmp/` 下的 spread socket 檔案 |
| 6. 檢查防火牆 | `nc -zv <node-ip> <port>` 測試連通性 |
| 7. 檢查子網域 | 若在不同子網，設定 spread point-to-point mode |

```bash
# 確認 spread daemon 設定
$ cat /opt/vertica/config/spread.conf

# 測試網路連通性
$ nc -zv 192.168.100.10 4803

# 強制重啟節點
$ /opt/vertica/bin/admintools -t restart_node -d <db_name> --hosts <ip> --force
```

### 7.2 Recovery 重複失敗 (incrCatchUpFailureCount)

**症狀**：`grep "incrCatchUpFailureCount" vertica.log` 顯示接近 20 次失敗

**可能原因**：
- Catalog events 太頻繁（大量 DDL / ETL）
- DML 操作持有鎖 timeout
- LockTimeout 太低

**解法**：
```sql
-- 檢查 LockTimeout
=> SELECT GET_CONFIG_PARAMETER('LockTimeout');

-- 若太低，調高
=> ALTER DATABASE testdb SET LockTimeout = 600;

-- 取消長時間運行的 DML 操作
=> SELECT * FROM sessions WHERE is_active = true;
```

### 7.3 Catalog 過大 / ROS 檔案過多

**症狀**：Pre-Recovery 階段的「檢查資料儲存」步驟極慢

**原因**：數百萬個 ROS 檔案需要 catalog 逐一驗證

**解法**：
- 定期執行 Tuple Mover mergeout 減少 ROS container 數量
- 避免大量小檔案載入（合併 COPY statements）
- 檢查 ROS container 數量：

```sql
=> SELECT projection_name, COUNT(*) AS ros_count
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   HAVING COUNT(*) > 1000
   ORDER BY ros_count DESC;
```

### 7.4 Table Recovery 卡住 — Event apply failed

**症狀**：TABLE_RECOVERIES 顯示 `status = 'error-retry'`、`recover_error = 'Event apply failed'`

```sql
=> SELECT node_name, table_name, status, phase, start_time,
          end_time, recover_error
   FROM v_monitor.table_recoveries
   WHERE status = 'error-retry';
```

**解法**：
- 暫停相關的 ETL / DML 操作
- 增加 RECOVERY pool 的 maxconcurrency 以加快恢復
- 手動重試（最多 20 次自動重試）

### 7.5 常見問題速查

| 問題 | 可能原因 | 解決方式 |
|------|----------|----------|
| 卡在 INITIALIZING | spread.conf 不一致 | 比對所有節點的 spread.conf |
| ERROR 4803 | spread socket 被佔用 | 刪除 `/tmp/4803` 並重啟 |
| 節點無法連線 | 防火牆 / 不同子網 | 設定 spread point-to-point mode |
| Recovery 極慢 | catalog events 太頻繁 | 暫停 ETL / 調高 MAXCONCURRENCY |
| Dirty txn 卡住 | 未提交的交易超過 5 分鐘 | 自動終止，或手動 commit / rollback |
| ROS 過多 | 大量小檔案載入 | 合併 COPY 語句 / 觸發 mergeout |

---

## 八、事前預防措施

### RECOVERY 資源池預先調校

```sql
-- 建議：若資料量大，調高 maxconcurrency
=> ALTER RESOURCE POOL recovery MAXCONCURRENCY 8;
```

### ROS Container 管理

```sql
-- 定期檢查 ROS container 數量
=> SELECT COUNT(*) AS total_ros_containers
   FROM v_monitor.projection_storage;

-- 若數量過高，觸發 mergeout
=> SELECT DO_TM_TASK('mergeout');
```

### LockTimeout 設定

```sql
=> SELECT GET_CONFIG_PARAMETER('LockTimeout');
-- 建議：設定 300-600 秒
```

### K-Safety 確保資料冗餘

```sql
=> SELECT get_vertica_options('KSAFETY');
-- K-safety = 1: 可容忍 1 個節點 down
-- K-safety = 2: 可容忍 2 個節點 down
```

### 定期備份

```bash
$ /opt/vertica/bin/vbr -t backup -c backup.ini
```

---

## 九、快速檢查清單

### Recovery 前

```bash
# [ ] 檢查節點狀態
$ /opt/vertica/bin/admintools -t view_cluster
# [ ] 檢查 spread.conf 一致性
$ cat /opt/vertica/config/spread.conf
# [ ] 確認網路連通性 (所有節點)
$ nc -zv <node-ip> 4803
# [ ] 檢查磁碟空間
$ df -h
```

### Recovery 中

```sql
-- [ ] 檢查整體 recovery 狀態
=> SELECT node_name, is_running, recovery_phase,
          current_completed, current_total,
          historical_completed, historical_total
   FROM v_monitor.recovery_status;

-- [ ] 檢查正在恢復哪張表
=> SELECT node_name, recovering_table_name, tables_remain
   FROM v_monitor.table_recovery_status
   WHERE is_running = true;

-- [ ] 檢查 recovery 重試次數
-- 在 vertica.log 中搜尋 incrCatchUpFailureCount
```

### Recovery 後

```sql
-- [ ] 確認節點已 UP
=> SELECT node_name, node_state FROM v_catalog.nodes;

-- [ ] 確認所有表已恢復
=> SELECT COUNT(*) AS tables_not_recovered
   FROM v_monitor.table_recoveries
   WHERE status NOT IN ('recovered', 'finished');

-- [ ] 檢查過期 projection
=> SELECT projection_name, is_up_to_date
   FROM projections
   WHERE is_up_to_date = false;

-- [ ] 檢查 ROS container 數量
=> SELECT COUNT(*) AS total_ros_containers
   FROM v_monitor.projection_storage;
```

---

## 十、黃金法則總結

| 階段 | 要點 |
|------|------|
| **事前預防** | 確保 K-safety ≥ 1；預先調校 RECOVERY pool 的 MAXCONCURRENCY；控制 ROS container 數量；設定合理的 LockTimeout (300-600s)；定期備份 |
| **診斷優先** | 先看 `recovery_status` 取得整體概況 → 再看 `table_recovery_status` 找出卡住的表 → 最後看 `projection_recoveries` 檢查方法與進度 |
| **加速 recovery** | 增加 MAXCONCURRENCY；暫停不必要的 ETL / catalog events；確保 buddy node 的磁碟 IO 和網路頻寬足夠 |
| **完成後** | 確認所有節點 UP；檢查無 error-retry；檢查無過期 projection |

### 監控系統表對照 (25.x)

| 系統表 (v_monitor) | 用途 |
|--------------------|------|
| `recovery_status` | 整體 recovery 進度摘要 |
| `table_recovery_status` | 當前正在恢復的表 |
| `table_recoveries` | 各表的 recovery 詳細資訊（含錯誤） |
| `projection_recoveries` | 各 projection 的 recovery 方法與進度 |

---

*本文基於 Vertica 25.4.0-0 實測撰寫，SQL 範例皆在實際環境驗證通過。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
