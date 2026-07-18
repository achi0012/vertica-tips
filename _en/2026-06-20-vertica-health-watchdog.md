---
layout: post_en
title: "Vertica Health Watchdog Deep Dive — Automatic Cluster Monitoring (24.4+)"
date: 2026-06-20 20:00:00 +0800
categories: vertica health-watchdog monitoring
tags: [vertica, health-watchdog, cluster-monitoring, DDL, DML, performance, GCLX, mergeout]
description: "Deep dive into Vertica's Health Watchdog (24.4+) — four monitoring modules (Truncation Version Lag, GCLX Queue, Mergeout Queue, Memory Pushback), threshold configuration, and 24 FAQ answers."
---

Under extreme concurrent load, even Vertica can enter a degraded state. To proactively address this, Vertica **24.4** introduced the **Health Watchdog** — an intelligent monitoring component that detects early signs of stress and takes corrective action before minor issues escalate.

This marks Vertica's evolution from "passive fault tolerance" to "active self-healing."

<!--more-->

---

## 1. What is Health Watchdog?

A built-in monitoring component that continuously observes the database's internal state and proactively blocks DDL/DML operations when the cluster faces pressure.

### Core Functions

| Function | Description |
|----------|-------------|
| **Auto Detection** | Identifies "bad health" state |
| **Query Blocking** | Blocks DDL/DML to prevent further degradation |
| **Query Timeout** | Blocked operations timeout after 5 min |
| **Self Recovery** | Auto-unblocks when system stabilizes |

### Version History

| Version | Change |
|---------|--------|
| **24.4** | Health Watchdog introduced |
| **25.1+** | Timer-based background service, checks every 2 seconds |

---

## 2. Four Monitoring Modules

### 2.1 Truncation Version Lag

Monitors the gap between `current_catalog_version` and `truncation_catalog_version`.

**Trigger**: Gap exceeds `TruncationVersionLag` (default: **500**)

**Action**: Blocks all non-superuser client connections until catalog sync completes.

```sql
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name = 'TruncationVersionLag';
```

### 2.2 GCLX Queue Bloat

Monitors the GCLX queue size (handles DDL, UPDATE, DELETE needing exclusive catalog locks).

**Trigger**: Queue exceeds `GCLXBlockParameter` (default: **100**)

**Action**: Blocks new transactions until queue drops to **10%** of threshold.

```sql
=> SELECT node_name, transaction_id, object_name, mode,
          time, (time - start_time) AS queue_time
   FROM dc_lock_attempts
   WHERE object_name ILIKE '%global Catalog'
   ORDER BY queue_time DESC LIMIT 10;
```

### 2.3 Mergeout Queue Bloat

Monitors Tuple Mover mergeout request queue size.

**Trigger**: Queue exceeds `MergeoutBlockParameter` (default: **100**)

**Action**: Blocks new DML until queue drops to **25%** of max.

**Common causes**: High-frequency inserts, limited TM threads, small TM pool, slow disk I/O.

```sql
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name = 'MergeoutBlockParameter';
```

### 2.4 Memory Pushback

Monitors global pool utilization when general pool is low.

**Trigger**: Global pool utilization exceeds `GlobalPoolMaxUtilizationRatio` (default: **0.9**)

**Action**: Blocks new DML until utilization drops to **50%** of threshold.

```sql
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name ILIKE '%globalpool%';
```

---

## 3. Default Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `TruncationVersionLag` | Max catalog version lag | 500 |
| `GCLXBlockParameter` | GCLX queue block threshold | 100 |
| `MergeoutBlockParameter` | Mergeout queue block threshold | 100 |
| `GlobalPoolMaxUtilizationRatio` | Max global pool utilization | 0.9 |
| `WatchdogTimeoutInterval` | Blocked transaction timeout (s) | 300 (5 min) |
| `WatchdogServiceInterval` | Watchdog check interval (s, 25.1+) | 2 |

```sql
=> SELECT parameter_name, current_value, default_value
   FROM configuration_parameters
   WHERE parameter_name ILIKE '%watchdog%'
      OR parameter_name ILIKE '%blockparameter%';
```

---

## 4. Management & Monitoring

### Check Cluster Health

```sql
=> SELECT check_cluster_health();
```

### View Blocked Transactions

```sql
=> SELECT * FROM health_watchdog_blocked_transactions;
```

### View History

```sql
=> SELECT * FROM health_watchdog_blocked_events
   ORDER BY time DESC LIMIT 50;
```

---

## 5. FAQ Summary

| Q | Answer |
|---|--------|
| What gets blocked? | DDL/DML only. **SELECT queries are NOT affected** |
| Are superusers affected? | **No**. Only non-superuser client queries |
| Auto-retry after timeout? | **No**. Must manually reissue the query |
| Can I manually unblock? | **No**. Wait for cluster to recover |
| Version introduced? | 24.4 (timer-based in 25.1+) |

---

## 6. Best Practices

```sql
-- Monitor blocked transactions regularly
=> SELECT COUNT(*) FROM health_watchdog_blocked_transactions;

-- Adjust thresholds based on workload
-- Frequent mergeout blocking → increase MergeoutBlockParameter
-- Frequent GCLX blocking → reduce concurrent DDL/DML
-- Memory pushback → increase node memory or adjust ratio

-- Regular health check
=> SELECT NOW() AS check_time, check_cluster_health() AS health;
```

---

## 7. Summary

| Module | Metric | Blocks | Recovery |
|--------|--------|--------|----------|
| **Truncation Version Lag** | Catalog gap > 500 | All non-superuser | Sync complete |
| **GCLX Queue** | Queue > 100 | New lock queue entries | Queue < 10 |
| **Mergeout Queue** | Queue > 100 | New DML | Queue < 25 |
| **Memory Pushback** | Global pool > 90% | New DML | Utilization < 45% |

---

*Based on Vertica 25.4.0-0 and OpenText Community article on Health Watchdog.*
