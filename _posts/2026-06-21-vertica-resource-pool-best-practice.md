---
layout: post
title: "Vertica 資源池管理最佳實踐 — 從分類到串聯的完整調校指南"
date: 2026-06-21 10:00:00 +0800
categories: vertica resource-pool performance
tags: [vertica, resource-pool, workload-management, PRIORITY, cascading-pool, MEMORYSIZE, PLANNEDCONCURRENCY, query-budget]
description: "Vertica 資源池 (Resource Pool) 的完整管理指南 — 從五個核心參數、工作量分類方法、兩家企業的實戰演進（6 階段調校）、串聯資源池、執行時間路由、優先級排程，到監控與調校檢查清單。"
---

資源池是 Vertica 資源管理的核心機制。無論是 ETL 載入、BI 查詢還是批次分析，每個查詢都在某個資源池中執行。妥善管理資源池，能確保不同工作負載和平共存，避免資源飢餓。

本文整理 Vertica KB 的最佳實踐，並更新至 **25.x**。

<!--
AI Summary: Vertica Resource Pool 完整管理指南。涵蓋五個核心參數（MEMORYSIZE/MAXMEMORYSIZE/PLANNEDCONCURRENCY/MAXCONCURRENCY/EXECUTIONPARALLELISM）、Query Budget 計算公式、fastAnalytics 六階段實戰演進（從 ETL/SELECT 分割到 8 個專用 Pool）、串聯資源池（CASCADE TO）、執行時間路由（RUNTIMECAP/RUNTIMEPRIORITY）、優先級排程、以及監控調校檢查清單。
AI Keywords: Vertica, resource pool, workload management, cascading pool, query budget, PLANNEDCONCURRENCY, MEMORYSIZE, RUNTIMECAP, PRIORITY
-->
<!--more-->

---

## 一、五個核心參數

| 參數 | 說明 | 建議 |
|------|------|------|
| **MEMORYSIZE** | 初始分配給 pool 的記憶體 | 設定後會保留，即使未使用也無法被其他 pool 借用 |
| **MAXMEMORYSIZE** | pool 可獲得的記憶體上限 | 應設高於 pool 應得的配額，通常設 40-60% |
| **PLANNEDCONCURRENCY** | 預期並發查詢數，用於計算 query budget | query_budget = 可用記憶體 / PLANNEDCONCURRENCY |
| **MAXCONCURRENCY** | 最大並發查詢數，超過則排隊 | 設為空則不限制 |
| **EXECUTIONPARALLELISM** | 每個查詢使用的執行緒數 | `AUTO` = 核心數。特定工作負載可設固定值 |

### Query Budget 計算

```
query_budget ≈ MAXMEMORYSIZE / PLANNEDCONCURRENCY
```

- 調低 PLANNEDCONCURRENCY → 每個查詢獲得更多記憶體（適合大查詢）
- 調高 PLANNEDCONCURRENCY → 更多並發能力（適合小查詢）

---

## 二、工作量分類

建立資源池的第一步是了解工作負載。可依以下維度分類：

| 分類維度 | 說明 |
|----------|------|
| **查詢類型** | ETL vs SELECT vs DDL |
| **應用程式** | 儀表板、報表工具、批次排程 |
| **使用者群組** | 管理階層、資料分析師、業務人員 |
| **查詢複雜度** | 短查詢（秒級）、中查詢（數秒～1分）、長查詢（>1分） |

### 工作負載分析工具

```sql
-- 依使用者分析查詢時間分佈
=> SELECT user_name,
          AVG(request_duration_ms) AS avg_ms,
          PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY request_duration_ms)
            AS median_ms,
          MAX(request_duration_ms) AS max_ms,
          COUNT(*) AS query_count
   FROM v_monitor.query_requests
   WHERE start_timestamp >= NOW() - INTERVAL '7 days'
   GROUP BY user_name
   ORDER BY avg_ms DESC;

-- 依 pool 分析資源使用
=> SELECT pool_name,
          AVG(memory_inuse_kb) AS avg_mem_kb,
          MAX(memory_inuse_kb) AS max_mem_kb,
          COUNT(*) AS query_count,
          AVG(EXTRACT(EPOCH FROM (queue_entry_timestamp - transaction_id::VARCHAR::INT)))::INT
            AS avg_queue_sec
   FROM v_monitor.resource_acquisitions
   WHERE queue_entry_timestamp >= NOW() - INTERVAL '7 days'
   GROUP BY pool_name
   ORDER BY avg_mem_kb DESC;
```

---

## 三、實戰案例：fastAnalytics 的六階段演進

這是一家 24×7 執行 ETL、有 SLA 要求的企業。叢集：10-node，每節點 256GB RAM、48 核心。

### 階段 1：初始設定 — 分割 ETL 與 SELECT

```sql
=> CREATE RESOURCE POOL etl_pool
   MAXMEMORYSIZE '60%'
   PLANNEDCONCURRENCY 48
   EXECUTIONPARALLELISM AUTO;

=> CREATE RESOURCE POOL select_pool
   MAXMEMORYSIZE '60%'
   PLANNEDCONCURRENCY 48
   EXECUTIONPARALLELISM AUTO;
```

| Pool | MEMORYSIZE | MAXMEMORYSIZE | PLANNEDCONCURRENCY | Query Budget |
|------|-----------|--------------|-------------------|-------------|
| general | — | 95% (Special) | 120 | ~2 GB |
| etl_pool | — | 60% | 48 | ~3 GB |
| select_pool | — | 60% | 48 | ~3 GB |

> **關鍵原則**：使用者定義 pool 的 MAXMEMORYSIZE 總和不應超過可用實體記憶體的 **120%**。

### 階段 2：依查詢時間細分 SELECT

分析後發現三種 SELECT 使用者：

| 類型 | 使用情境 | 執行時間 |
|------|---------|---------|
| **短查詢** | 儀表板 | < 1 秒 |
| **中查詢** | 資料分析 | 數秒～1 分鐘 |
| **長查詢** | 批次報表 | > 1 分鐘 |

```sql
=> CREATE RESOURCE POOL short_pool
   MAXMEMORYSIZE '40%' PLANNEDCONCURRENCY 48
   EXECUTIONPARALLELISM 6;
=> CREATE RESOURCE POOL medium_pool
   MAXMEMORYSIZE '30%' PLANNEDCONCURRENCY 18
   EXECUTIONPARALLELISM 12;
=> CREATE RESOURCE POOL large_pool
   MAXMEMORYSIZE '10%' PLANNEDCONCURRENCY 3
   EXECUTIONPARALLELISM 24;

=> DROP RESOURCE POOL select_pool CASCADE;
```

| Pool | MAXMEMORYSIZE | PLANNEDCONCURRENCY | Query Budget | EXECUTIONPARALLELISM |
|------|-------------|-------------------|-------------|---------------------|
| etl_pool | 40% | 48 | ~3 GB | 4 |
| **short_pool** | 40% | 48 | ~2 GB | 6 |
| **medium_pool** | 30% | 18 | ~4 GB | 12 |
| **large_pool** | 10% | 3 | ~8 GB | 24 |

### 階段 3：拆分 ETL 管道

將 ETL 拆分為兩個 pipeline：

```sql
=> CREATE RESOURCE POOL trickle_pool
   MAXMEMORYSIZE '30%' PLANNEDCONCURRENCY 28
   EXECUTIONPARALLELISM 4;
=> CREATE RESOURCE POOL bulk_pool
   MAXMEMORYSIZE '10%' PLANNEDCONCURRENCY 4
   EXECUTIONPARALLELISM 4;
```

- **trickle_pool**：小批次載入，query budget = 2.5 GB
- **bulk_pool**：寬表（> 250 columns）批次載入，query budget = 6 GB

### 階段 4：管理階層專用 Standalone Pool

```sql
=> CREATE RESOURCE POOL management_pool
   MEMORYSIZE '6GB'
   MAXMEMORYSIZE '6GB'
   PLANNEDCONCURRENCY 6
   EXECUTIONPARALLELISM 6;
```

> ⚠️ MEMORYSIZE = MAXMEMORYSIZE 稱為 **standalone pool**，確保永不排隊，但若未使用會浪費記憶體。需定期檢查使用率。

### 最終資源池配置

| Pool | MEMORYSIZE | MAXMEMORYSIZE | PLANNEDCONCURRENCY | EXECUTIONPARALLELISM | Query Budget |
|------|-----------|-------------|-------------------|---------------------|-------------|
| general | — | 95% | 120 | AUTO | ~2 GB |
| trickle_pool | — | 30% | 28 | 4 | ~2.5 GB |
| bulk_pool | — | 10% | 4 | 4 | ~6 GB |
| etl_pool | — | 40% | 48 | 4 | ~3 GB |
| short_pool | — | 40% | 48 | 6 | ~2 GB |
| medium_pool | — | 30% | 18 | 12 | ~4 GB |
| large_pool | — | 10% | 3 | 24 | ~8 GB |
| **management_pool** | 6 GB | 6 GB | 6 | 6 | ~1 GB |

---

## 四、串聯資源池 (Cascading Pools)

適合 **ad-hoc 工作負載**（如 quickAnalytics 公司），查詢類型不可預測。

### 運作方式

```sql
=> CREATE RESOURCE POOL adhoc_short_pool
   MAXMEMORYSIZE '20%'
   PLANNEDCONCURRENCY 30
   RUNTIMECAP '10 seconds'
   CASCADE TO adhoc_medium_pool;

=> CREATE RESOURCE POOL adhoc_medium_pool
   MAXMEMORYSIZE '20%'
   PLANNEDCONCURRENCY 10
   RUNTIMECAP '1 minute'
   CASCADE TO adhoc_long_pool;

=> CREATE RESOURCE POOL adhoc_long_pool
   MAXMEMORYSIZE '20%'
   PLANNEDCONCURRENCY 4;
```

### 流程

```
查詢提交 → adhoc_short_pool
    ↓ 超過 10 秒？
重新排隊 → adhoc_medium_pool
    ↓ 超過 1 分鐘？
重新排隊 → adhoc_long_pool
```

> **注意**：串聯過程中查詢已經獲得的記憶體會被釋放，在下一層重新取得。

---

## 五、執行時間路由 (RUNTIMEPRIORITY + RUNTIMECAP)

### RUNTIMEPRIORITY

根據查詢執行的已用時間動態調整優先級：

```sql
=> ALTER RESOURCE POOL etl_pool RUNTIMEPRIORITY HIGH;
=> ALTER RESOURCE POOL etl_pool RUNTIMEPRIORITY FIXED 10;
```

| 值 | 說明 |
|----|------|
| HIGH | 隨執行時間增加而提高優先級 |
| MEDIUM | 預設值 |
| LOW | 隨執行時間增加而降低優先級 |
| FIXED n | 固定優先級（0-11） |

### RUNTIMECAP

設定查詢在此 pool 中的執行時間上限：

```sql
=> ALTER RESOURCE POOL short_pool RUNTIMECAP '30 seconds';
=> ALTER RESOURCE POOL medium_pool RUNTIMECAP '5 minutes';
```

---

## 六、優先級與排程

### PRIORITY

資源池之間的排程優先級（數字越大越優先）：

```sql
=> ALTER RESOURCE POOL management_pool PRIORITY 10;
=> ALTER RESOURCE POOL bulk_pool PRIORITY 5;
=> ALTER RESOURCE POOL large_pool PRIORITY 0;
```

### QUEUETIMEOUT

查詢在佇列中的最長等待時間：

```sql
=> ALTER RESOURCE POOL bulk_pool QUEUETIMEOUT '30 minutes';
=> ALTER RESOURCE POOL short_pool QUEUETIMEOUT '30 seconds';
```

### CPUAFFINITY

綁定特定 CPU 核心：

```sql
=> ALTER RESOURCE POOL etl_pool CPUAFFINITYMODE EXCLUSIVE
   CPUAFFINITYSET '0-7';
```

---

## 七、監控資源池

### 即時狀態

```sql
=> SELECT pool_name, running_query_count, plannedconcurrency,
          query_budget_kb, memory_inuse_kb,
          (memory_inuse_kb * 100.0 / NULLIF(query_budget_kb, 0))::NUMERIC(5,1)
            AS mem_util_pct,
          queue_timeout_threshold
   FROM v_monitor.resource_pool_status
   WHERE pool_name IN ('general', 'etl_pool', 'short_pool',
                       'medium_pool', 'large_pool', 'management_pool');
```

### 排隊查詢

```sql
=> SELECT pool_name, node_name, transaction_id,
          priority, queue_entry_timestamp,
          (CLOCK_TIMESTAMP() - queue_entry_timestamp) AS queue_time
   FROM v_monitor.resource_queues
   ORDER BY queue_entry_timestamp;
```

### 資源拒絕事件

```sql
=> SELECT pool_name, reason, COUNT(*) AS reject_count
   FROM v_monitor.resource_rejections
   WHERE last_rejected_timestamp >= NOW() - INTERVAL '1 day'
   GROUP BY pool_name, reason;
```

---

## 八、調校檢查清單

| 步驟 | 說明 | SQL |
|------|------|-----|
| 1. 分析工作負載 | 了解查詢時間分佈與使用者群組 | `v_monitor.query_requests` 依 user_name 分組 |
| 2. 建立自訂 Pool | 依查詢類型分割資源 | `CREATE RESOURCE POOL` |
| 3. 設定 EXECUTIONPARALLELISM | 避免 AUTO = 48 造成資源浪費 | 短查詢 6、中查詢 12、大查詢 24 |
| 4. 設定 RUNTIMECAP | 防止 runaway queries | `ALTER POOL ... RUNTIMECAP` |
| 5. 設定 PRIORITY | 確保重要查詢優先 | `ALTER POOL ... PRIORITY` |
| 6. 監控記憶體使用 | 確認 query_budget 是否合理 | `resource_pool_status` |
| 7. 檢查 queue | 確認無異常排隊 | `resource_queues` |
| 8. 定期覆核 | 工作負載變化時調整配置 | 回步驟 1 |

---

## 九、重要提醒

| 原則 | 說明 |
|------|------|
| **MAXMEMORYSIZE 總和 ≤ 120%** | 使用者定義 pool 的 MAXMEMORYSIZE 總和不應超過可用記憶體的 120% |
| **Standalone pool 需監控** | MEMORYSIZE = MAXMEMORYSIZE 的 pool 若未使用會浪費記憶體 |
| **串聯 pool 的排隊時間** | 每次串聯重新排隊，總等待時間 = 各層 queue_time + runtime |
| **EXECUTIONPARALLELISM AUTO** | 通常等於 CPU 核心數，對混合負載可能過高 |
| **CASCADE TO** | 可以鏈式串聯多層 |

---

*本文基於 Vertica KB "Best Practices for Managing Resource Pools"，所有 SQL 已更新至 Vertica 25.4.0-0。*
