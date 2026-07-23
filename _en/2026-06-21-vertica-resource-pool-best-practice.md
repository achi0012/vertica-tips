---
layout: post_en
title: "Vertica Resource Pool Best Practices — From Custom Pools to Cascading (25.x)"
date: 2026-06-21 10:00:00 +0800
categories: vertica resource-pool performance
tags: [vertica, resource-pool, workload-management, cascading-pool, MEMORYSIZE, PLANNEDCONCURRENCY, query-budget, PRIORITY]
description: "Complete Vertica resource pool management guide — five core parameters, workload classification, fastAnalytics 6-phase evolution, cascading pools, runtime routing, and the tuning checklist."
---

Resource pools are Vertica's core resource management mechanism. Every query runs in some pool — whether it's an ETL load, a BI dashboard query, or a batch analytics job. Proper pool management ensures workloads coexist without resource starvation.

This article covers Vertica KB's best practices, updated for **25.x**.

<!--more-->

---

## 1. Five Core Parameters

| Parameter | Description | Recommendation |
|-----------|-------------|----------------|
| **MEMORYSIZE** | Initial memory assigned to the pool | Reserved even if unused |
| **MAXMEMORYSIZE** | Cap on total memory acquired | Set higher than entitled share (40-60%) |
| **PLANNEDCONCURRENCY** | Expected concurrent queries; calculates query budget | query_budget = available_memory / PLANNEDCONCURRENCY |
| **MAXCONCURRENCY** | Max concurrent queries; excess queue | Empty = unlimited |
| **EXECUTIONPARALLELISM** | Threads per query | `AUTO` = CPU cores. Set fixed for specific workloads |

### Query Budget Calculation

```
query_budget ≈ MAXMEMORYSIZE / PLANNEDCONCURRENCY
```

- Lower PLANNEDCONCURRENCY → more memory per query (good for large queries)
- Higher PLANNEDCONCURRENCY → more concurrency (good for small queries)

---

## 2. Workload Classification

Classify along these dimensions:

| Dimension | Description |
|-----------|-------------|
| **Query type** | ETL vs SELECT vs DDL |
| **Application** | Dashboard, reporting, batch scheduler |
| **User group** | Management, analysts, operators |
| **Query complexity** | Short (<1s), Medium (seconds-1min), Long (>1min) |

### Analysis Tools

```sql
-- Query distribution by user
=> SELECT user_name, AVG(request_duration_ms) AS avg_ms,
          PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY request_duration_ms) AS median_ms,
          MAX(request_duration_ms) AS max_ms, COUNT(*) AS cnt
   FROM v_monitor.query_requests
   WHERE start_timestamp >= NOW() - INTERVAL '7 days'
   GROUP BY user_name ORDER BY avg_ms DESC;

-- Resource usage by pool
=> SELECT pool_name, AVG(memory_inuse_kb) AS avg_mem_kb,
          MAX(memory_inuse_kb) AS max_mem_kb, COUNT(*) AS cnt
   FROM v_monitor.resource_acquisitions
   WHERE queue_entry_timestamp >= NOW() - INTERVAL '7 days'
   GROUP BY pool_name ORDER BY avg_mem_kb DESC;
```

---

## 3. Case Study: fastAnalytics — 6-Phase Evolution

Environment: 10-node cluster, 256GB RAM, 48 cores per node, 24×7 ETL with SLAs.

### Phase 1: Split ETL and SELECT

```sql
=> CREATE RESOURCE POOL etl_pool MAXMEMORYSIZE '60%'
   PLANNEDCONCURRENCY 48 EXECUTIONPARALLELISM AUTO;
=> CREATE RESOURCE POOL select_pool MAXMEMORYSIZE '60%'
   PLANNEDCONCURRENCY 48 EXECUTIONPARALLELISM AUTO;
```

### Phase 2: Subdivide by Query Duration

| Pool | MAXMEMORYSIZE | PLANNEDCONCURRENCY | Query Budget | EXECUTIONPARALLELISM |
|------|-------------|-------------------|-------------|---------------------|
| short_pool | 40% | 48 | ~2 GB | 6 |
| medium_pool | 30% | 18 | ~4 GB | 12 |
| large_pool | 10% | 3 | ~8 GB | 24 |

### Phase 3: Split ETL Pipeline

```sql
=> CREATE RESOURCE POOL trickle_pool MAXMEMORYSIZE '30%'
   PLANNEDCONCURRENCY 28 EXECUTIONPARALLELISM 4;
=> CREATE RESOURCE POOL bulk_pool MAXMEMORYSIZE '10%'
   PLANNEDCONCURRENCY 4 EXECUTIONPARALLELISM 4;
```

### Phase 4: Standalone Management Pool

```sql
=> CREATE RESOURCE POOL management_pool
   MEMORYSIZE '6GB' MAXMEMORYSIZE '6GB'
   PLANNEDCONCURRENCY 6 EXECUTIONPARALLELISM 6;
```

> **Key principle**: Sum of user-defined MAXMEMORYSIZE should not exceed **120%** of available physical memory.

---

## 4. Cascading Resource Pools

Best for **ad-hoc workloads** where query patterns are unpredictable.

### Example: 3-Level Cascade

```sql
=> CREATE RESOURCE POOL adhoc_short_pool
   MAXMEMORYSIZE '20%' PLANNEDCONCURRENCY 30
   RUNTIMECAP '10 seconds' CASCADE TO adhoc_medium_pool;

=> CREATE RESOURCE POOL adhoc_medium_pool
   MAXMEMORYSIZE '20%' PLANNEDCONCURRENCY 10
   RUNTIMECAP '1 minute' CASCADE TO adhoc_long_pool;

=> CREATE RESOURCE POOL adhoc_long_pool
   MAXMEMORYSIZE '20%' PLANNEDCONCURRENCY 4;
```

```
Query → adhoc_short_pool
   ↓ exceeds 10s?
Re-queue → adhoc_medium_pool
   ↓ exceeds 1min?
Re-queue → adhoc_long_pool
```

---

## 5. Runtime Routing

### RUNTIMEPRIORITY

Adjusts priority dynamically based on elapsed query time:

```sql
=> ALTER RESOURCE POOL etl_pool RUNTIMEPRIORITY HIGH;
=> ALTER RESOURCE POOL etl_pool RUNTIMEPRIORITY FIXED 10;
```

### RUNTIMECAP

```sql
=> ALTER RESOURCE POOL short_pool RUNTIMECAP '30 seconds';
=> ALTER RESOURCE POOL medium_pool RUNTIMECAP '5 minutes';
```

---

## 6. Priority & Scheduling

```sql
=> ALTER RESOURCE POOL management_pool PRIORITY 10;
=> ALTER RESOURCE POOL bulk_pool QUEUETIMEOUT '30 minutes';
=> ALTER RESOURCE POOL etl_pool CPUAFFINITYMODE EXCLUSIVE
   CPUAFFINITYSET '0-7';
```

---

## 7. Monitoring

### Real-Time Status

```sql
=> SELECT pool_name, running_query_count, plannedconcurrency,
          query_budget_kb, memory_inuse_kb,
          (memory_inuse_kb * 100.0 / NULLIF(query_budget_kb, 0))::NUMERIC(5,1) AS mem_pct
   FROM v_monitor.resource_pool_status
   WHERE pool_name IN ('general', 'short_pool', 'medium_pool', 'large_pool');
```

### Queued Queries

```sql
=> SELECT pool_name, transaction_id,
          (CLOCK_TIMESTAMP() - queue_entry_timestamp) AS queue_time
   FROM v_monitor.resource_queues ORDER BY queue_entry_timestamp;
```

### Resource Rejections

```sql
=> SELECT pool_name, reason, COUNT(*) AS cnt
   FROM v_monitor.resource_rejections
   WHERE last_rejected_timestamp >= NOW() - INTERVAL '1 day'
   GROUP BY pool_name, reason;
```

---

## 8. Tuning Checklist

| Step | Action |
|------|--------|
| 1. Analyze workload | Query distribution by user/type |
| 2. Create custom pools | Subdivide by workload |
| 3. Set EXECUTIONPARALLELISM | Short=6, Medium=12, Large=24 |
| 4. Set RUNTIMECAP | Prevent runaway queries |
| 5. Set PRIORITY | Critical queries first |
| 6. Monitor memory | Check query_budget in resource_pool_status |
| 7. Check queue | Monitor resource_queues |
| 8. Review regularly | Adjust as workload changes |

---

## Key Principles

| Principle | Description |
|-----------|-------------|
| **MAXMEMORYSIZE sum ≤ 120%** | Prevent pools from starving each other |
| **Monitor standalone pools** | MEMORYSIZE = MAXMEMORYSIZE can waste memory |
| **Cascade re-queuing** | Each cascade level involves re-queuing |
| **AUTO parallelism may be too high** | = CPU core count; often excessive for mixed workloads |

---

*Based on Vertica KB "Best Practices for Managing Resource Pools." SQL updated to Vertica 25.4.0-0.*
