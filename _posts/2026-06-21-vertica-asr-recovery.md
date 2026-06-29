---
layout: post
title: "Vertica 資料庫崩潰後啟動失敗 — ASR 日期異常的完整復原指南"
date: 2026-06-21 07:00:00 +0800
categories: vertica disaster-recovery ASR troubleshooting
tags: [vertica, ASR, database-recovery, crash, epoch, AHM, abortrecovery, disaster-recovery]
description: "Vertica 資料庫因斷電或檔案系統問題崩潰後啟動失敗，ASR 顯示無效日期（2001 年或更早）時的完整復原步驟 — 從 Unsafe Mode 啟動、epoch 分析、abortrecovery 到資料搶救。"
---

當資料中心斷電或檔案系統出問題時，Vertica catalog 可能參照到遺失或損壞的資料檔案。在這種情況下，節點的 computed recovery epoch 可能落在 ancient history mark (AHM) 之後。當兩個 buddy nodes 都遇到這問題時，資料庫的 expected recovery epoch 會小於 AHM epoch，導致 ASR (Auto Synchronous Recovery) 建議的 recovery epoch 為 **0**，recovery timestamp 落在 **1999 年**。

本文整理自 Micro Focus KB KM03449287（2019），所有 SQL 已更新至 **Vertica 25.x**。

<!--
AI Summary: Vertica 資料庫因斷電或檔案系統問題崩潰後 ASR 顯示無效日期（2001 年或更早）的完整復原指南。涵蓋 Unsafe Mode 啟動、get_ahm_epoch/get_expected_recovery_epoch 診斷、projection_checkpoint_epochs 分析、abortrecovery 執行、資料不一致清理（分割表/非分割表），以及預防措施。基於 Micro Focus KB KM03449287 更新至 25.x。
AI Keywords: Vertica, ASR, database recovery, crash, abortrecovery, epoch, AHM, disaster recovery, unsafe mode
-->
<!--more-->

---

## 問題徵兆

嘗試啟動資料庫時出現類似錯誤：

```
ASR showing invalid date: 2001-01-01 00:00:00
Database start failed
```

或使用 Last Good Epoch (LGE) 復原也失敗：

```bash
$ admintools -t start_db -d mydb
Error: recovery epoch is behind ancient history mark (AHM)
```

---

## 復原流程總覽

```
Step 1: 嘗試從備份還原（最安全）
   ↓ 沒有備份？
Step 2: Unsafe Mode 啟動資料庫
   ↓
Step 3: 找出 AHM epoch 與受影響節點
   ↓
Step 4: 找出 checkpoint epoch 低於 AHM 的 projections
   ↓
Step 5: 執行 abortrecovery
   ↓
Step 6: 重新啟動資料庫
   ↓
Step 7: 清理資料不一致
   ├── 可 truncate 的表 → TRUNCATE TABLE
   └── 重要的表 → 資料搶救 (Step 8)
```

---

## Step 1：從備份還原（最安全）

這是最安全的選項。先確認是否有最近的資料庫備份：

```bash
# 查看 vbr 備份狀態
$ /opt/vertica/bin/vbr -t list -c backup.ini
```

若有備份，建議直接還原：

```bash
$ /opt/vertica/bin/vbr -t restore -c backup.ini
```

> **建議**：復原完成後，務必設定定期備份機制（如使用 AWS S3 作為備份目標）。

---

## Step 2：Unsafe Mode 啟動資料庫

若沒有備份，使用 **unsafe mode** 啟動資料庫以識別受影響的 projections：

```bash
$ admintools -t start_db -d mydb -U
```

> ⚠️ **警告**：Unsafe mode 應在 Vertica 技術支援工程師的指導下使用，僅用於上述特定問題的資料庫復原。

---

## Step 3：找出 AHM 與需要關注的節點

啟動後查詢 AHM epoch 與 expected recovery epoch：

```sql
=> SELECT get_ahm_epoch();
 get_ahm_epoch
───────────────
           150

=> SELECT get_expected_recovery_epoch();
INFO 4544: Recovery Epoch Computation:
Node Dependencies:
00011 - cnt: 10
00110 - cnt: 10
...

Nodes certainly in the cluster:
    Node 0(v_mydb_node0001), epoch 170
    Node 1(v_mydb_node0002), epoch 170
    Node 2(v_mydb_node0003), epoch 170
Filling more nodes to satisfy node dependencies:
    Node 3(v_mydb_node0004), epoch 149
Data dependencies fulfilled, remaining nodes LGEs don't matter:
    Node 4(v_mydb_node0005), epoch 100
```

**判讀方式**：
- `Nodes certainly in the cluster`：這些節點的 LGE 足夠高，不需處理
- `Filling more nodes to satisfy node dependencies`：**這些是需要關注的節點**，它們的 LGE 低於 AHM
- 記錄這些節點的名稱（如上例中的 `v_mydb_node0004`）

---

## Step 4：找出受影響的 Projections

查詢 checkpoint epoch (CPE) 低於 AHM epoch 的 projections：

```sql
=> SELECT e.node_name, t.table_schema, t.table_name,
          e.projection_schema, e.projection_name,
          e.checkpoint_epoch
   FROM v_monitor.projection_checkpoint_epochs e
   JOIN projections p ON e.projection_id = p.projection_id
   JOIN tables t ON p.anchor_table_id = t.table_id
   WHERE NOT (t.is_temp_table)
     AND e.is_behind_ahm
     AND e.is_up_to_date
     AND e.node_name IN ('v_mydb_node0004')  -- Step 3 找出的節點
   ORDER BY t.table_schema, t.table_name;
```

> **版本差異**：2019 年版使用裸表 `projection_checkpoint_epochs`。在 **25.x 中已移至 `v_monitor.projection_checkpoint_epochs`**。

---

## Step 5：執行 Abortrecovery

對 Step 4 找出的每張表執行 `abortrecovery`。這會將 CPE 更新為 **-1**，表示重新啟動時不進行復原：

```sql
=> SELECT DO_TM_TASK('abortrecovery', 'schema.table_name');
```

> 對每一張受影響的表重複執行。

---

## Step 6：重新啟動資料庫

停止並重新啟動資料庫：

```bash
$ admintools -t stop_db -d mydb
$ admintools -t start_db -d mydb
```

此時資料庫會成功啟動，但被標記 `abortrecovery` 的表會有**資料不一致**的問題。需要在開放給一般使用者使用前清理。

---

## Step 7：清理資料不一致

對 Step 4 找出的每張表，判斷是否可以 truncate：

### 可以直接 Truncate 的表

```sql
=> TRUNCATE TABLE schema.table_name;
```

### 需要保留資料的重要表

請進入 **Step 8** 進行資料搶救。

---

## Step 8：重要資料表的資料搶救

### 8.1 分割表 (Partitioned Table)

**檢查 buddy projections 之間的 partition count mismatch**：

```sql
=> SELECT partition_key, diff
   FROM (
       SELECT a.partition_key,
              SUM(a.ros_row_count - a.deleted_row_count)
                - SUM(b.ros_row_count - b.deleted_row_count) AS diff
       FROM partitions a
       JOIN partitions b ON a.partition_key = b.partition_key
       WHERE a.projection_name IN (
             'schema.table_projection',
             'schema.table_projection_b0'
       )
       GROUP BY 1
   ) AS sub
   WHERE diff <> 0;
```

**修復方式**：

對有 count mismatch 的 partition，可以：
1. **直接 drop 該 partition 並重新載入資料**
2. 或使用 `MOVE_PARTITIONS_TO_TABLE()` 將受影響的 partition 移到新表，再用 `INSERT...SELECT` 載回原表

> 受影響的 partitions 會有一些資料損失，但最終兩個 buddy projections 之間不會有 count mismatch。

**驗證修復完成**：重新執行上述查詢，應回傳空結果。

**留下註記**：

```sql
=> COMMENT ON TABLE schema.table_name IS
   'abortrecovery was run on <日期> by <管理員>';
```

### 8.2 非分割表 (Non-Partitioned Table)

```sql
-- 建立新表並從受影響的事實表插入資料
=> CREATE TABLE schema.table_name_new AS
   SELECT * FROM schema.table_name;

-- 新表可能缺少部分資料，但兩個 buddy projections 之間不會有 count mismatch

-- 刪除原表並重新命名
=> DROP TABLE schema.table_name CASCADE;
=> ALTER TABLE schema.table_name_new RENAME TO table_name;
```

---

## 預防措施

| 面向 | 建議 |
|------|------|
| **定期備份** | 使用 `vbr` 設定自動備份到 S3 或 NFS |
| **UPS 電源** | 資料中心配備 UPS，避免意外斷電 |
| **K-Safety** | 確保 `K-safety >= 1`，容忍節點故障 |
| **Epoch 監控** | 定期檢查 `current_epoch` 與 `ahm_epoch` 的差距 |
| **還原測試** | 定期演練備份還原流程 |

---

## 版本差異對照

| 2019 年 (Micro Focus KB) | 25.x | 狀態 |
|-------------------------|------|------|
| `projection_checkpoint_epochs` | `v_monitor.projection_checkpoint_epochs` | ✅ 已確認 |
| `get_ahm_epoch()` | 同左 | ✅ 系統函數 |
| `get_expected_recovery_epoch()` | 同左 | ✅ 系統函數 |
| `DO_TM_TASK('abortrecovery', ...)` | 同左 | ✅ |
| `admintools -t start_db -d <db> -U` | 同左 | ✅ |
| `partitions` 表 | `v_monitor.partitions` | ✅ |

---

*本文基於 Micro Focus KB KM03449287（2019）撰寫，所有 SQL 已更新至 Vertica 25.4.0-0。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
