---
layout: post_en
title: "Understanding Linux OOM Kills in Vertica Clusters — Prevention Guide"
date: 2026-06-21 06:00:00 +0800
categories: vertica OOM memory troubleshooting
tags: [vertica, OOM, memory, Linux, kernel, catalog, resource-pool, glibc, Health-Watchdog, ROS]
description: "Complete guide to Linux OOM killer causes and prevention in Vertica clusters — MAXMEMORYSIZE tuning, catalog memory calculation, MemoryReport.log diagnostics, glibc versions, and ROS/Delete Vector management."
---

Out-of-Memory (OOM) conditions in Vertica are less common on modern, up-to-date systems but can still occur with large catalogs, competing workloads, or specific query patterns. When the Linux **OOM Killer** terminates a Vertica process, a systematic investigation approach is needed.

This guide covers causes, diagnostics, and prevention — with Health Watchdog (v25.1+) as an additional safeguard.

<!--more-->

---

## 1. OOM Warning Signs

```bash
$ dmesg | grep -i "out of memory\|killed process\|oom-killer"
```

```
[12345.678901] Out of memory: Killed process 12345 (vertica) ...
```

---

## 2. Health Watchdog — First Defense (v25.1+)

Health Watchdog automatically blocks non-superuser DDL/DML during extreme load:

```sql
=> SELECT check_cluster_health();
=> SELECT * FROM health_watchdog_blocked_transactions;
```

> **Note**: Watchdog is a safeguard, but does NOT replace proper memory configuration and resource pool management.

---

## 3. MAXMEMORYSIZE Tuning

### Reduce General Pool to 85%

```sql
=> ALTER RESOURCE POOL general MAXMEMORYSIZE '85%';
```

> Requires database restart to take effect.

### Check Pool Status

```sql
=> SELECT * FROM v_monitor.resource_pool_status;
```

### Calculate Catalog Memory Usage

```sql
=> WITH m AS (SELECT node_name, memory_size_kb
              FROM v_monitor.resource_pool_status WHERE pool_name='metadata'),
          g AS (SELECT node_name, memory_size_kb
              FROM v_monitor.resource_pool_status WHERE pool_name='general')
   SELECT m.node_name,
          ((m.memory_size_kb / g.memory_size_kb) * 100)::NUMERIC(4,2) AS pct
   FROM m JOIN g ON m.node_name = g.node_name;
```

If catalog > 5% of general pool, reduce MAXMEMORYSIZE.

---

## 4. Memory Report Diagnostics

### MemoryReport.log Location

```
<catalog-path>/<database-name>/v_<db>_node<xxxx>_catalog/MemoryReport.log
```

### Memory Events System Table

```sql
=> SELECT node_name, time, reason, details, duration
   FROM v_monitor.memory_events
   ORDER BY time DESC LIMIT 20;
```

### Identifying Memory Leak Patterns

If memory usage grows continuously without reclamation, check MemoryReport.log to identify which queries or components are responsible.

---

## 5. glibc Version Issues

| glibc Version | Status |
|--------------|--------|
| `< 2.19` | ❌ `malloc_info()` bug |
| `2.19–2.22` | ⚠️ Potential issues |
| `2.23+` | ✅ Fixed |

```bash
$ rpm -q glibc
```

> ⚠️ Direct glibc upgrade (`yum upgrade glibc`) can cause system instability. Use OS versions recommended by your Vertica release.

### Memory Trimming

```sql
=> SELECT * FROM v_monitor.memory_events
   WHERE reason ILIKE '%trim%' ORDER BY time DESC;
```

---

## 6. ROS & Delete Vector Management

Indirect OOM causes often come from catalog bloat due to excessive ROS containers and delete vectors.

### ROS Container Check

```sql
=> SELECT projection_name, COUNT(*) AS ros_count
   FROM v_monitor.projection_storage
   GROUP BY projection_name HAVING COUNT(*) > 1000;
```

### Delete Vectors

```sql
=> SELECT COUNT(*) AS dv_count FROM v_monitor.delete_vectors;
=> SELECT MAKE_AHM_NOW();
```

### Mergeout Old Partitions

```sql
=> SELECT DO_TM_TASK('mergeout');
```

---

## 7. Catalog Cache Size

```sql
=> SELECT node_name, MAX(catalog_size_in_MB) AS catalog_size_in_MB
   FROM (
       SELECT node_name,
              SUM(total_memory_max_value - free_memory_min_value) / (1024*1024)
                AS catalog_size_in_MB
       FROM v_monitor.dc_allocation_pool_statistics_by_second
       GROUP BY 1, TRUNC(time::TIMESTAMP, 'SS')
   ) t GROUP BY 1 ORDER BY 1;
```

**Rule of thumb**: If catalog cache = 10 GB, leave 15 GB for system, let pools use 85 GB.

---

## 8. Prevention Checklist

```sql
-- 1. Pool status
SELECT * FROM v_monitor.resource_pool_status;
-- 2. Catalog memory %
SELECT m.memory_size_kb / g.memory_size_kb * 100
FROM (SELECT memory_size_kb FROM resource_pool_status WHERE pool_name='metadata') m,
     (SELECT memory_size_kb FROM resource_pool_status WHERE pool_name='general') g;
-- 3. Memory events
SELECT * FROM v_monitor.memory_events ORDER BY time DESC LIMIT 10;
-- 4. ROS health
SELECT COUNT(*) FROM v_monitor.projection_storage;
-- 5. Delete vectors
SELECT COUNT(*) FROM v_monitor.delete_vectors;
-- 6. Health Watchdog
SELECT check_cluster_health();
-- 7. Epoch health
SELECT current_epoch, ahm_epoch FROM v_monitor.system;
```

---

## Summary

| Area | Recommendation | 25.x Tool |
|------|---------------|-----------|
| **Basic protection** | General pool MAXMEMORYSIZE 85% | `ALTER RESOURCE POOL` |
| **Active defense** | Health Watchdog (v25.1+) | `check_cluster_health()` |
| **Catalog monitoring** | Track catalog memory ratio | `dc_allocation_pool_statistics_by_second` |
| **Memory diagnostics** | Analyze MemoryReport.log | `v_monitor.memory_events` |
| **ROS management** | Avoid excessive ROS containers | `DO_TM_TASK('mergeout')` |
| **Delete Vectors** | Clear periodically | `MAKE_AHM_NOW()` |
| **glibc version** | Use 2.23+ | `rpm -q glibc` |

---

*Based on Moshe Goldberg's LinkedIn article "Understanding Linux OOM Kills in Vertica Clusters." SQL updated to Vertica 25.4.0-0.*
