---
layout: post_en
title: "Vertica Tuple Mover Best Practices — Mergeout, STRATA & TM Pool Tuning (25.x)"
date: 2026-06-21 09:00:00 +0800
categories: vertica tuple-mover mergeout performance
tags: [vertica, tuple-mover, moveout, mergeout, STRATA, ROS, TM, resource-pool, ROS-pushback, partition]
description: "Complete Vertica Tuple Mover best practices combining KB Part 1 (≤9.1) and Part 2 (9.2+). Covers Mergeout mechanics, STRATA algorithm, TM resource pool tuning, ROS pushback prevention, and version differences up to 25.x."
---

The Tuple Mover is Vertica's background service that manages ROS container consolidation and deleted data purging. While defaults work for most cases, specific workloads require tuning for optimal performance.

This article combines both KB articles ("Tuple Mover Best Practices: Part 1" for ≤9.1 and "Part 2" for 9.2+), updated for **25.x**.

<!--more-->

---

## 1. Tuple Mover Overview

### Historical Evolution

| Version | Change |
|---------|--------|
| **≤ 9.1** (Part 1) | WOS existed. TM handled Moveout (WOS→ROS) + Mergeout |
| **9.2+** (Part 2) | WOS removed. TM handles only Mergeout |
| **25.x** | WOS fully removed. Only Mergeout remains |

### Two Operations

| Operation | Description | 25.x Status |
|-----------|-------------|-------------|
| **Moveout** | Moved data from WOS to ROS containers | ❌ WOS removed, no longer needed |
| **Mergeout** | Consolidates ROS containers + purges deleted data | ✅ Core function |

---

## 2. Moveout (Legacy, Reference Only)

Applies to **Vertica 9.1 and earlier**.

| Practice | Description |
|----------|-------------|
| **Use COPY DIRECT for large files** | > 100 MB: bypass WOS, load directly to ROS |
| **MoveOutInterval** | Default 300s. Set lower than time to fill half WOS |
| **Avoid uncommitted data in WOS** | TM only moves committed data |
| **MoveOutSizePct** | WOS usage % to trigger moveout |
| **MoveOutMaxAgeTime** | Max time data sits in WOS (default 1800s) |

### Detect WOS Spillover

```sql
=> SELECT node_name, COUNT(*)
   FROM dc_execution_engine_events
   WHERE event_type = 'WOS_SPILL'
   GROUP BY node_name;
```

---

## 3. Mergeout Mechanics

### What Mergeout Does

1. **Merges small ROS containers** into larger ones
2. **Purges deleted data**
3. **Merges delete vectors**

### ROS Container Limit

```
Max ROS containers per projection per node: 1024
Parameter: ContainersPerProjectionLimit
```

Exceeding this triggers `TOO MANY ROS CONTAINERS` (ROS pushback).

### The STRATA Algorithm (9.2+)

```
Stratum 3: [large containers]
Stratum 2: [medium containers]
Stratum 1: [small containers]
Stratum 0: [negligible containers] ← mergeout starts here
```

- Max ROS per stratum: **32** (parameter: `ROSPerStratum`)
- When a stratum fills up, it becomes eligible for mergeout
- **Stratum 0** special handling: merges **all** eligible containers into one

---

## 4. TM Resource Pool Tuning

### Default Values

```sql
=> SELECT * FROM resource_pools WHERE name = 'tm';
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| memorysize | 5% | TM pool memory |
| plannedconcurrency | AUTO | Default 3 (1 moveout + 2 mergeout legacy) |
| maxconcurrency | 7 | Max concurrent, don't exceed 6 |

### Wide Table Tuning

If you have tables with **100+ columns**, increase TM memory:

```sql
=> ALTER RESOURCE POOL tm MEMORYSIZE '10%';
```

### Increase Mergeout Threads

```sql
=> ALTER RESOURCE POOL tm PLANNEDCONCURRENCY 4;
=> ALTER RESOURCE POOL tm MAXCONCURRENCY 4;
```

> **Version note**: In 10.0+, half of threads work on both active+inactive partitions, the other half on active partitions only.

### Mergeout Throughput Diagnostics

When throughput is below **1 GB/minute**:

```sql
=> SELECT dtme.projection, dtme.total_size_in_gb,
          dtme.mergeout_time, dtme.mergeout_throughput,
          dtme.include_replay_delete, dra.memory_mb
   FROM (SELECT s.node_name, s.schema_name||'.'||s.projection_name AS projection,
                (s.total_size_in_bytes/1024^3)::NUMERIC(10,2) AS total_size_in_gb,
                DATEDIFF(MINUTE, s.time, c.time) AS mergeout_time,
                TRUNC(s.total_size_in_bytes/1024^3/NULLIFZERO(DATEDIFF(MINUTE,s.time,c.time)),3)
                  AS mergeout_throughput
         FROM dc_tuple_mover_events s
         JOIN dc_tuple_mover_events c USING (node_name, projection_oid, transaction_id)
         WHERE s.operation='Mergeout' AND c.operation='Mergeout'
           AND s.event='Start' AND c.event='Complete') dtme
   JOIN (SELECT node_name, transaction_id, (MAX(memory_kb)/1024)::INT AS memory_mb
         FROM dc_resource_acquisitions WHERE pool_name='tm' GROUP BY 1,2) dra
     USING (node_name, transaction_id)
   ORDER BY mergeout_throughput ASC LIMIT 20;
```

---

## 5. Mergeout Best Practices

### Partition Design

| Practice | Description |
|----------|-------------|
| **≤ 50 partitions per table** | Vertica doesn't merge across partitions |
| **ActivePartitionCount = 2** | If frequently writing to recent inactive partitions |
| **Archive old partitions** | Use `MOVE_PARTITION_TO_TABLE()` |

```sql
=> ALTER TABLE store.orders SET ACTIVE_PARTITION_COUNT 2;
```

### Projection Design

| Practice | Description |
|----------|-------------|
| **Sort order ≤ 8 columns** | Too many columns slow mergeout |
| **Avoid wide VARCHAR in sort** | Significantly increases mergeout time |
| **Max 2 projection sets** | Per table |

### Delete & Replay Delete Optimization

```sql
-- If mergeout stuck on replay delete
=> SELECT close_session('<session_id>');
=> SELECT make_ahm_now();
=> SELECT PURGE_TABLE('schema.table_name');
```

> **Tip**: Use a high-cardinality column as the **last column** in sort order to avoid long replay deletes.

### Disable Mergeout on Temp Tables (11.0+)

```sql
=> ALTER TABLE temp_table SET MERGEOUT 0;
```

### Partition Reorganization

```sql
=> SELECT * FROM v_monitor.partition_status;
=> ALTER TABLE table_name REORGANIZE;
```

---

## 6. Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MergeOutInterval` | 600 | Mergeout trigger interval (s) |
| `ActivePartitionCount` | 1 | Active partition count |
| `PurgeMergeoutPercent` | 20 | Delete % threshold for purge |
| `ROSPerStratum` | 32 | ROS count per stratum |
| `MaxDVROSPerContainer` | 10 | Delete vector limit per ROS |
| `ContainersPerProjectionLimit` | 1024 | ROS limit per projection |

---

## 7. Monitoring

### Currently Executing

```sql
=> SELECT * FROM v_monitor.tuple_mover_operations
   WHERE operation_status = 'Running';
```

### Mergeout Performance History

```sql
=> SELECT projection_name, operation_type,
          COUNT(*), AVG(EXTRACT(EPOCH FROM (operation_end - operation_start))) AS avg_sec
   FROM v_monitor.tuple_mover_operations
   WHERE operation_type = 'Mergeout' AND operation_end IS NOT NULL
   GROUP BY 1, 2 ORDER BY avg_sec DESC LIMIT 20;
```

### ROS Pushback Risk

```sql
=> SELECT projection_name, COUNT(*) AS ros_count
   FROM v_monitor.projection_storage
   GROUP BY projection_name
   HAVING COUNT(*) > 500 ORDER BY ros_count DESC;
```

---

## 8. Part 1 vs Part 2 Differences

| Feature | Part 1 (≤ 9.1) | Part 2 (9.2+) / 25.x |
|---------|---------------|---------------------|
| **Storage** | WOS + ROS | ROS only |
| **TM operations** | Moveout + Mergeout | Mergeout only |
| **Merge algorithm** | Basic | **STRATA** |
| **Temp table mergeout off** | N/A | ✅ 11.0+ |
| **dc_tuple_mover_events** | Available | ✅ Still available |
| **TM pool default threads** | 1 moveout + 2 mergeout | All for mergeout |

---

*Combined from Vertica KB "Tuple Mover Best Practices" Part 1 & Part 2. SQL updated to Vertica 25.4.0-0.*
