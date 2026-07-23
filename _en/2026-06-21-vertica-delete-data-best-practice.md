---
layout: post_en
title: "Best Practices for Deleting Data in Vertica — DELETE, TRUNCATE, Partition Guide (25.x)"
date: 2026-06-21 08:00:00 +0800
categories: vertica delete data-management best-practice
tags: [vertica, delete, TRUNCATE, partition, delete-vectors, mergeout, purge, AHM, data-management]
description: "Complete best practices for deleting data in Vertica — how DELETE works, five delete methods compared, delete vector management, purge policy tuning (PurgeMergeoutPercent), and alternatives to DELETE."
---

Vertica differs from traditional databases in two key ways when it comes to deleting data:

1. **DELETE does not physically remove data from disk** — it creates a delete vector recording the position and epoch of deleted records
2. **UPDATE performs two tasks** — writes new data and marks old data for deletion

This article covers Vertica's best practices for deleting data, updated for **25.x**.

<!--more-->

---

## 1. Five Delete Methods Compared

| Method | Load Target | Rollback | Performance | Use Case |
|--------|-------------|----------|-------------|----------|
| **Single Row DELETE** | WOS (legacy) / ROS | ✅ Yes | Depends on projection | Few single-row deletes |
| **Bulk DELETE** | Direct ROS | ✅ Yes | Depends on projection | **Recommended** — one delete vector per ROS |
| **DROP PARTITION** | N/A | ❌ No | Fast (background) | **Best for historical data cleanup** |
| **TRUNCATE TABLE** | N/A | ❌ No | Fast (catalog change) | Clear entire table, keep schema |

> **Note for 25.x**: WOS has been removed. All operations go directly to ROS. The delete vector mechanism remains unchanged.

---

## 2. How DELETE Works

### Delete Vector Mechanism

DELETE creates a **delete vector** recording:
- Position of the deleted record
- Epoch when the delete was committed

```sql
=> SELECT COUNT(*) FROM v_monitor.delete_vectors;
```

### Permanent Removal

Space is released based on the **Ancient History Mark (AHM)** — data older than AHM is eligible for permanent removal.

```sql
=> SELECT current_epoch, ahm_epoch FROM v_monitor.system;
```

---

## 3. Purge Policy Configuration

### HistoryRetentionTime

```sql
-- Retain 7 days (604800 seconds)
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionTime', 604800);
-- Disable time-based retention
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionTime', -1);
```

### HistoryRetentionEpochs

```sql
=> SELECT SET_CONFIG_PARAMETER('HistoryRetentionEpochs', 200);
```

### PurgeMergeoutPercent

Controls when mergeout purges deleted data:

```sql
=> SELECT SET_CONFIG_PARAMETER('PurgeMergeoutPercent', 20);
```

| Table Type | Behavior |
|------------|----------|
| **Non-partitioned** | Purges ROS containers meeting threshold |
| **Partitioned** | Purges **inactive** partitions only |

---

## 4. Alternatives to DELETE

### Use Partition Management

```sql
-- Example: Reload last week's erroneous data
=> CREATE TABLE store.staging_fact
   LIKE store.store_orders_fact INCLUDING PROJECTIONS;
=> INSERT /*+ direct */ INTO store.staging_fact
   SELECT * FROM store.store_orders_fact
   WHERE date_ordered BETWEEN '2005-11-01' AND '2005-11-19'
      OR date_ordered BETWEEN '2005-11-28' AND '2005-11-30';
=> SELECT SWAP_PARTITIONS_BETWEEN_TABLES(
       'store.staging_fact', 200511, 200511,
       'store.store_orders_fact');
```

### Recreate Table

```sql
=> CREATE TABLE store.orders_new AS
   SELECT * FROM store.store_orders_fact WHERE date_ordered >= '2025-01-01';
=> DROP TABLE store.store_orders_fact CASCADE;
=> ALTER TABLE store.orders_new RENAME TO store_orders_fact;
```

---

## 5. Bulk DELETE Best Practices

Use **Bulk DELETE** instead of multiple single row deletes:

```sql
=> CREATE LOCAL TEMP TABLE data_to_delete (emp_id INT);
=> COPY data_to_delete FROM '/tmp/employee_to_delete.txt';
=> DELETE /*+ direct */ FROM store.store_orders_fact
   WHERE employee_key IN (SELECT * FROM data_to_delete);
=> DROP TABLE data_to_delete;
```

> **Key principle**: One bulk DELETE creates **one delete vector** per affected ROS container.

---

## 6. Managing Delete Vectors

### Monitor Delete Vectors

```sql
=> SELECT schema_name, projection_name,
          SUM(deleted_row_count) AS num_deld_rows,
          SUM(delete_vector_count) AS num_dv,
          (SUM(deleted_row_count)/SUM(total_row_count)*100)::INT AS pct_del
   FROM v_monitor.storage_containers
   WHERE node_name = (SELECT local_node_name())
   GROUP BY 1, 2
   HAVING SUM(deleted_row_count) > 0
   ORDER BY num_deld_rows DESC;
```

### Per-Partition View

```sql
=> SELECT p.partition_key,
          SUM(p.deleted_row_count) AS num_del,
          SUM(p.delete_vector_count) AS cdv,
          (SUM(p.deleted_row_count)/SUM(p.ros_row_count)*100)::INT AS pct_del
   FROM v_monitor.partitions p
   JOIN v_monitor.storage_containers sc ON p.ros_id = sc.storage_oid
   WHERE p.node_name = (SELECT local_node_name())
   GROUP BY 1
   HAVING SUM(p.delete_vector_count) > 0
   ORDER BY pct_del DESC;
```

### Three Resolution Options

| Scenario | Solution | Command |
|----------|----------|---------|
| Too many DVs, low deleted % | Merge DVs | `SELECT DO_TM_TASK('dvmergeout');` |
| Many deleted rows in partition | Drop partition | `SELECT DROP_PARTITION(...)` |
| Many deleted rows in whole table | Purge table | `SELECT PURGE_TABLE('schema.tbl');` |

---

## 7. Recommended Workflow

```
Need to delete data?
    ↓
Can you avoid DELETE?
    ├── Partitioned → DROP/SWAP PARTITION
    ├── Large dataset → CREATE TABLE AS + DROP
    └── Full clear → TRUNCATE
    ↓
Must use DELETE?
    ├── Bulk DELETE (single statement)
    └── Prepare IDs in temp table
    ↓
Monitor Delete Vectors
    ├── pct_del low → dvmergeout
    ├── pct_del high (partition) → DROP_PARTITION
    └── pct_del high (table) → PURGE_TABLE
    ↓
Regular Purge Policy
    ├── Set HistoryRetentionTime
    └── Set PurgeMergeoutPercent
```

---

## Version Differences

| Item | Legacy (≤10.x) | 25.x |
|------|---------------|------|
| `storage_containers` | Bare table | `v_monitor.storage_containers` |
| `partitions` | Bare table | `v_monitor.partitions` |
| `delete_vectors` | Bare table | `v_monitor.delete_vectors` |
| `DO_TM_TASK('dvmergeout')` | Available | ✅ Still available |
| `PURGE_TABLE()` | Available | ✅ Still available |
| WOS | Existed | Removed, direct ROS |

---

*Based on Vertica KB "Best Practices for Deleting Data." SQL updated to Vertica 25.4.0-0.*
