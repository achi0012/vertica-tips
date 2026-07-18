---
layout: post_en
title: "Vertica Performance Slow? — 13-Step Diagnostic Checklist (25.x Updated)"
date: 2026-06-20 23:00:00 +0800
categories: vertica performance troubleshooting
tags: [vertica, performance, slow-database, troubleshooting, delete-vectors, epoch, AHM, locks, resource-pool]
description: "A systematic 13-step diagnostic checklist when Vertica performance degrades — updated from a 2018 article to Vertica 25.x with version-specific system table locations."
---

When Vertica performance slows down, a systematic diagnostic approach helps pinpoint the root cause. This 13-step checklist was originally published in 2018 — this version is **updated to Vertica 25.x** with all system table locations verified.

<!--more-->

---

## Diagnostic Flow

```
Step  1: Single query slow?    → Query Performance check
Step  2: Entire DB slow?       → Continue
Step  3: Node status           → Verify all nodes UP
Step  4: Delete Vectors        → > 1000 needs attention
Step  5: Epoch progression     → AHM stuck?
Step  6: Node performance skew → One node slower?
Step  7: Load balancing        → Workload evenly distributed?
Step  8: Resource rejections   → Resource rejections
Step  9: Session queue         → Queries queued?
Step 10: Long-running queries  → Resource hogs
Step 11: Lock conflicts        → Transaction lock waits
Step 12: Catalog memory        → Catalog > 5% of memory
Step 13: Memory usage          → Resident/Virtual Memory
```

---

## Step 1: Single Query Slow?

```sql
=> EXPLAIN SELECT ...;
```

Check for: incorrect JOIN order, missing projections, excessive partition scans, data skew.

> **25.x+**: Use `QUERY_EVENTS` for query tracing.
> ```sql
> => SELECT * FROM v_monitor.query_events WHERE is_executing = true;
> ```

---

## Step 2: Is the Entire Database Slow?

```sql
=> SELECT AVG(request_duration_ms) AS avg_duration,
          MAX(request_duration_ms) AS max_duration
   FROM v_monitor.query_requests
   WHERE is_executing = false AND request_duration_ms > 0
     AND start_timestamp >= NOW() - INTERVAL '1 hour';
```

---

## Step 3: Check Node Status

```sql
=> SELECT node_name, node_address, node_state
   FROM v_catalog.nodes WHERE node_state != 'UP';
```

If a node is DOWN:
```bash
$ admintools -t restart_node -d <database> -s <node_ip>
```

---

## Step 4: Check Delete Vectors

```sql
=> SELECT COUNT(*) AS delete_vector_count
   FROM v_monitor.delete_vectors;
```

> **Version diff**: 2018 used bare `delete_vectors`. **25.x: `v_monitor.delete_vectors`** ✅

**Threshold**: `< 1000` normal, `> 1000` needs mergeout:
```sql
=> SELECT DO_TM_TASK('mergeout');
```

---

## Step 5: Check Epoch Progression

```sql
=> SELECT current_epoch, ahm_epoch, last_good_epoch
   FROM v_monitor.system;
```

> **Version diff**: 2018 used bare `system`. **25.x: `v_monitor.system`** ✅

If AHM isn't advancing:
```sql
=> SELECT * FROM v_monitor.tuple_mover_operations
   ORDER BY operation_start DESC LIMIT 20;
```

---

## Step 6: Check for Node Performance Skew

```bash
#!/bin/bash
grep -P "^v_" /opt/vertica/config/admintools.conf | \
  awk '{print $3}' | awk -F, '{print $1}' | \
while read host; do
  echo "----- $host -----"
  /opt/vertica/bin/vsql -h $host -c "SELECT /*+kV*/ 1;"
done
```

---

## Step 7: Check Load Balancing

```sql
=> SELECT node_name, COUNT(*) AS query_count
   FROM v_monitor.dc_requests_issued
   WHERE time > SYSDATE() - 1
   GROUP BY node_name ORDER BY node_name;
```

---

## Step 8: Check Resource Rejections

```sql
=> SELECT * FROM v_monitor.resource_rejections
   ORDER BY last_rejected_timestamp DESC LIMIT 20;
```

> **Version diff**: 2018 used bare `resource_rejections`. **25.x: `v_monitor.resource_rejections`** ✅

---

## Step 9: Check Session Queues

```sql
=> SELECT * FROM v_monitor.resource_queues;
```

> **Version diff**: 2018 used bare `resource_queues`. **25.x: `v_monitor.resource_queues`** ✅

---

## Step 10: Check Long-Running Queries

```sql
=> SELECT r.pool_name, s.node_name, s.session_id,
          MAX(SUBSTR(s.current_statement, 1, 100)) AS statement,
          MAX(r.memory_inuse_kb) AS max_mem,
          MAX((CLOCK_TIMESTAMP() - r.queue_entry_timestamp)) AS running_time
   FROM v_monitor.resource_acquisitions r
   JOIN v_monitor.sessions s
     ON (r.transaction_id = s.transaction_id AND r.statement_id = s.statement_id)
   WHERE LENGTH(s.current_statement) > 0
   GROUP BY 1, 2, 3 ORDER BY running_time DESC;
```

> **Version diff**: 2018 used `v_internal.vs_resource_acquisitions`. **25.x: `v_monitor.resource_acquisitions`** ✅

```sql
=> SELECT INTERRUPT_STATEMENT('session_id', 'statement_id');
```

---

## Step 11: Check Lock Conflicts

```sql
=> SELECT * FROM v_monitor.locks WHERE grant_timestamp IS NULL;
```

> **Version diff**: 2018 used bare `locks`. **25.x: `v_monitor.locks`** ✅

Advanced lock tracing:
```sql
=> SELECT node_name, object_name, mode,
          (time - start_time) AS queue_time
   FROM dc_lock_attempts
   WHERE object_name ILIKE '%global Catalog'
   ORDER BY queue_time DESC LIMIT 20;
```

---

## Step 12: Check Catalog Memory Usage

```sql
=> SELECT node_name, MAX(catalog_size_in_MB) AS catalog_size_in_MB
   FROM (
     SELECT node_name,
            SUM(total_memory_max_value - free_memory_min_value) / (1024*1024)
              AS catalog_size_in_MB
     FROM v_monitor.dc_allocation_pool_statistics_by_second
     GROUP BY node_name, TRUNC(time::TIMESTAMP, 'SS')
   ) t GROUP BY 1;
```

If catalog > 5% of host memory, adjust resource pools:
```sql
=> ALTER RESOURCE POOL metadata MAXMEMORYSIZE '2G';
=> ALTER RESOURCE POOL general PLANNEDCONCURRENCY 4;
```

---

## Step 13: Check Resident/Virtual Memory

```sql
=> SELECT time, node_name, virtual_size, resident_size,
          thread_count, map_count
   FROM dc_process_info
   ORDER BY time DESC LIMIT 10;
```

---

## Version Differences Summary

| 2018 Original | 25.x | Status |
|--------------|------|--------|
| `FROM system` | `FROM v_monitor.system` | ✅ |
| `FROM delete_vectors` | `FROM v_monitor.delete_vectors` | ✅ |
| `FROM resource_rejections` | `FROM v_monitor.resource_rejections` | ✅ |
| `FROM resource_queues` | `FROM v_monitor.resource_queues` | ✅ |
| `FROM locks` | `FROM v_monitor.locks` | ✅ |
| `v_internal.vs_resource_acquisitions` | `v_monitor.resource_acquisitions` | ✅ |
| `admintools -t restart_nodes` | `admintools -t restart_node` | ✅ |

---

## Quick Diagnostic Script

```sql
-- 1. Node status
SELECT node_name, node_state FROM v_catalog.nodes WHERE node_state != 'UP';
-- 2. Delete Vectors
SELECT COUNT(*) FROM v_monitor.delete_vectors;
-- 3. Epoch health
SELECT current_epoch, ahm_epoch FROM v_monitor.system;
-- 4. Resource rejections
SELECT * FROM v_monitor.resource_rejections ORDER BY last_rejected_timestamp DESC LIMIT 10;
-- 5. Lock waits
SELECT * FROM v_monitor.locks WHERE grant_timestamp IS NULL;
-- 6. Memory
SELECT time, node_name, virtual_size, resident_size FROM dc_process_info ORDER BY time DESC LIMIT 5;
```

---

*Updated from the 2018 OpenText article "What Should I do if the Database Performance is Slow?" to Vertica 25.4.0-0.*
