---
layout: post
title: "Vertica Health Watchdog 深度解析 — 叢集健康監控的自動守護神"
date: 2026-06-20 20:00:00 +0800
categories: vertica health-watchdog monitoring
tags: [vertica, health-watchdog, cluster-monitoring, DDL, DML, performance, troubleshooting]
description: "Vertica 24.4+ 引入的 Health Watchdog 功能深度解析 — 揭露四大監控模組（Truncation Version Lag、GCLX Queue、Mergeout Queue、Memory Pushback）的運作原理、閾值配置，以及 24 個常見 QA 總整理。"
---

Vertica 以高效能分析著稱，但在極端高並發負載下，資料庫可能進入降級狀態，導致查詢延長、鎖 timeout、ROS pushback，甚至崩潰。為了主動應對這些問題，Vertica 從 **24.4** 版開始引入了一個強大的新功能 — **Health Watchdog**（健康守護程序）。

這是 Vertica 從「被動容錯」邁向「主動自我修復」的一大步。

<!--more-->

---

## 一、Health Watchdog 是什麼？

Health Watchdog 是一個內建的智慧監控元件，持續觀察資料庫的內部狀態。當叢集面臨高並發壓力或內部瓶頸時，它能**在問題惡化前主動採取行動**。

### 核心功能

| 功能 | 說明 |
|------|------|
| **自動偵測** | 識別資料庫何時進入「不健康」狀態 |
| **查詢阻斷** | 阻斷 DDL / DML 操作，防止進一步惡化 |
| **查詢超時** | 被阻斷的操作若超過 5 分鐘仍未恢復，自動超時 |
| **自我恢復** | 系統穩定後自動解除阻斷，恢復正常運作 |

### 版本歷程

| 版本 | 變更 |
|------|------|
| **24.4** | Health Watchdog 首次引入 |
| **25.1+** | 改為 **timer-based 背景服務**，每 2 秒檢查一次 |
| 控制參數 | `WatchdogServiceInterval`（預設 2 秒） |

---

## 二、四大監控模組

Health Watchdog 監控四個關鍵指標，每個都有對應的配置參數與阻斷機制。

### 2.1 Truncation Version Lag（目錄版本滯後）

**監控對象**：`current_catalog_version` 與 `truncation_catalog_version` 之間的差距。

**背景知識**：
- **Catalog Truncation Version**：叢集崩潰、關機或休眠後會回退到的版本
- **current_catalog_version**：叢集當前活躍的目錄版本
- 兩者的差距代表 catalog 同步的延遲程度

**觸發條件**：
- 當差距超過 `TruncationVersionLag` 閾值（**預設 500**）時，模組標記為 unhealthy

**觸發後行為**：
- 所有非 superuser 的客戶端連線被**阻斷**
- 直到 catalog 同步完成、truncation version 傳播成功後自動恢復

```sql
-- 檢查 TruncationVersionLag 設定
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name = 'TruncationVersionLag';
```

### 2.2 GCLX Queue Bloat（GCLX 佇列膨脹）

**監控對象**：GCLX 佇列的大小。GCLX 負責處理 DDL、UPDATE、DELETE 等需要獨佔 catalog lock 的操作。

**問題情境**：
- 大量並發 DDL / DML 操作同時湧入
- 所有操作都在等待 catalog lock → 佇列迅速堆積
- 造成效能瓶頸與延遲

**觸發條件**：
- 當 GCLX 佇列大小超過 `GCLXBlockParameter`（**預設 100**）時，模組標記為 unhealthy

**觸發後行為**：
- 阻斷新交易進入 lock wait queue
- 直到當前佇列大小降至 `GCLXBlockParameter` 的 **10%** 以下才恢復

```sql
-- 檢查 GCLX 佇列狀態
=> SELECT node_name, transaction_id, object_name, mode,
          time, time - start_time AS queue_time
   FROM dc_lock_attempts
   WHERE object_name ILIKE '%global Catalog'
   ORDER BY queue_time DESC
   LIMIT 10;
```

### 2.3 Mergeout Queue Bloat（Mergeout 佇列膨脹）

**監控對象**：Tuple Mover 的 mergeout 請求佇列大小。

**背景知識**：
- Tuple Mover 是背景元件，負責合併 ROS containers 與清除已刪除資料
- 高頻插入下，mergeout 請求可能大量堆積

**觸發條件**：
- 當 mergeout 佇列大小超過 `MergeoutBlockParameter`（**預設 100**）時，模組標記為 unhealthy

**觸發後行為**：
- 阻斷**新的 DML 交易**
- 直到當前佇列大小降至閾值的 **25%** 以下才恢復

**Mergeout 堆積的常見原因**：

| 原因 | 說明 |
|------|------|
| 高頻插入 | 大量 INSERT / COPY 操作 |
| TM 執行緒不足 | `TM` 資源池的 maxconcurrency 不夠 |
| TM 資源池過小 | 記憶體不足，mergeout 被延遲 |
| 磁碟 IO 緩慢 | 合併 ROS containers 需要大量 IO |

```sql
-- 檢查 MergeoutBlockParameter 設定
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name = 'MergeoutBlockParameter';
```

### 2.4 Memory Pushback（記憶體回推）

**監控對象**：global pool（全域記憶體池）的使用率。

**背景知識**：
- **general pool**：系統主要記憶體資源，受 `GeneralPoolMinFreeMemoryRatio`（預設 0.25）管理
- **global pool**：內部記憶體池，主要用於 catalog 物件

**觸發條件**：
- 當 general pool 低於閾值時，Watchdog 檢查 global pool
- 若 global pool 使用率超過 `GlobalPoolMaxUtilizationRatio`（**預設 0.9**，即 90%），交易被阻斷

**觸發後行為**：
- 新的 DML 操作被阻斷
- 直到 global pool 使用率降至閾值的 **50%** 以下（即 45%）才恢復

```sql
-- 檢查記憶體相關參數
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name ILIKE '%globalpool%'
      OR parameter_name ILIKE '%pushback%';
```

---

## 三、預設配置參數總表

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `TruncationVersionLag` | 最大允許的 catalog 版本滯後 | 500 |
| `GCLXBlockParameter` | GCLX 佇列阻斷閾值 | 100 |
| `MergeoutBlockParameter` | Mergeout 佇列阻斷閾值 | 100 |
| `GlobalPoolMaxUtilizationRatio` | Global pool 最大使用率 | 0.9 (90%) |
| `WatchdogTimeoutInterval` | 被阻斷交易的超時時間（秒） | 300 (5 min) |
| `WatchdogServiceInterval` | Watchdog 檢查間隔（秒，25.1+） | 2 |

```sql
-- 查詢所有 Health Watchdog 相關參數
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name ILIKE '%watchdog%'
      OR parameter_name ILIKE '%blockparameter%'
      OR parameter_name ILIKE '%globalpool%'
      OR parameter_name ILIKE '%versionlag%';
```

---

## 四、管理與監控

### 檢查叢集健康狀態

```sql
=> SELECT check_cluster_health();
```

### 查看當前被阻斷的交易

```sql
=> SELECT * FROM health_watchdog_blocked_transactions;
```

### 查看歷史阻斷事件

```sql
=> SELECT * FROM health_watchdog_blocked_events
   ORDER BY time DESC LIMIT 50;
```

### 調整 Watchdog 檢查頻率

```sql
-- 調整檢查間隔 (25.1+)
=> ALTER DATABASE default SET WatchdogServiceInterval = 5;

-- 調整阻斷超時時間
=> ALTER DATABASE default SET WatchdogTimeoutInterval = 600;
```

---

## 五、FAQ 重點整理

### Q1: 哪些操作會被阻斷？

僅 **DDL / DML**（寫入操作）。**唯讀查詢 (SELECT) 完全不受影響**。

### Q2: superuser 會被阻斷嗎？

**不會**。Health Watchdog 只監控非 superuser 的客戶端查詢。使用 `dbadmin` 或其他 superuser 不受此限制。

### Q3: 交易被阻斷後會自動重試嗎？

**不會**。如果交易因被阻斷而超時，需要**手動重新執行**。

### Q4: 可以手動解除阻斷嗎？

**不行**。必須等待叢集恢復健康狀態後，Watchdog 自動解除阻斷。

### Q5: 如何在 Management Console 中設定警示？

目前 **Management Console 不支援 Health Watchdog 的警示功能**。

### Q6: 可以完全關閉 Health Watchdog 嗎？

可以透過將所有 block parameter 設為極大值來實質停用，但不建議在生產環境這麼做。

---

## 六、實戰建議

### 6.1 監控 Blocked Transactions

```sql
-- 定期檢查是否有交易被阻斷
=> SELECT COUNT(*) AS blocked_count
   FROM health_watchdog_blocked_transactions;

-- 若有大量阻斷，進一步分析原因
=> SELECT module_name, blocked_count, avg_blocked_time_seconds
   FROM (
     SELECT 'mergeout' AS module_name, COUNT(*) AS blocked_count,
            AVG(EXTRACT(EPOCH FROM (NOW() - block_time))) AS avg_blocked_time_seconds
     FROM health_watchdog_blocked_transactions
     WHERE module = 'MERGEOUT'
     UNION ALL
     SELECT 'gclx', COUNT(*),
            AVG(EXTRACT(EPOCH FROM (NOW() - block_time)))
     FROM health_watchdog_blocked_transactions
     WHERE module = 'GCLX'
   ) t;
```

### 6.2 調整閾值的策略

- **Mergeout 頻繁觸發** → 調高 `MergeoutBlockParameter` 或增加 TM 資源池容量
- **GCLX 頻繁觸發** → 減少並發 DDL/DML 操作，或調高 `GCLXBlockParameter`
- **Memory pushback 頻繁觸發** → 增加節點記憶體，或調低 `GlobalPoolMaxUtilizationRatio`
- **Truncation version lag** → 檢查 catalog 同步是否有異常

### 6.3 預防性檢查

```sql
-- 建立定期健康檢查腳本
=> SELECT NOW() AS check_time,
          check_cluster_health() AS cluster_health;

-- 檢查是否有堆積的 mergeout 請求
=> SELECT COUNT(*) AS pending_mergeout
   FROM v_monitor.tuple_mover_operations
   WHERE operation_status = 'Running'
     AND operation_type = 'Mergeout';
```

---

## 七、總結

Health Watchdog 是 Vertica 從 24.4 開始提供的一個**低成本、高價值的自動化保護機制**。它讓 Vertica 從被動等待問題發生，進化到**主動偵測並預防**叢集降級。

| 模組 | 監控指標 | 阻斷對象 | 恢復條件 |
|------|---------|---------|---------|
| **Truncation Version Lag** | catalog 版本差距 > 500 | 所有非 superuser 交易 | 同步完成 |
| **GCLX Queue** | 佇列大小 > 100 | 新交易進入 lock queue | 佇列 < 10 |
| **Mergeout Queue** | 佇列大小 > 100 | 新 DML 交易 | 佇列 < 25 |
| **Memory Pushback** | global pool 使用率 > 90% | 新 DML 交易 | 使用率 < 45% |

對於維運團隊來說，定期檢查 `check_cluster_health()` 和 `health_watchdog_blocked_transactions` 可以及早發現叢集瓶頸，在影響用戶之前就採取行動。

---

*本文基於 Vertica 25.4.0-0 與 OpenText Community 文章 "Inside Opentext Vertica's Health Watchdog: A Deep Dive into Cluster Monitoring" 撰寫。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
