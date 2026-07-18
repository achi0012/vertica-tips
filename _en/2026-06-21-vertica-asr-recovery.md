---
layout: post_en
title: "Vertica Database Fails to Start After Crash — ASR Invalid Date Recovery Guide"
date: 2026-06-21 07:00:00 +0800
categories: vertica disaster-recovery ASR troubleshooting
tags: [vertica, ASR, database-recovery, crash, epoch, AHM, abortrecovery, disaster-recovery]
description: "Complete recovery steps when Vertica fails to start after a crash or power outage with ASR showing invalid dates — Unsafe Mode, epoch analysis, abortrecovery, and data salvage."
---

When a power outage or filesystem issue causes Vertica catalog to reference missing or corrupted data files, the database may fail to start with ASR (Auto Synchronous Recovery) showing epoch **0** and a timestamp in **1999**.

This guide covers the complete recovery process, based on Micro Focus KB KM03449287 (2019) updated for **Vertica 25.x**.

<!--more-->

---

## Symptoms

```
ASR showing invalid date: 2001-01-01 00:00:00
Database start failed
```

Last Good Epoch (LGE) recovery also fails:

```bash
$ admintools -t start_db -d mydb
Error: recovery epoch is behind ancient history mark (AHM)
```

---

## Recovery Flow

```
Step 1: Restore from backup (safest)
   ↓ no backup?
Step 2: Start in Unsafe Mode
   ↓
Step 3: Identify AHM epoch & affected nodes
   ↓
Step 4: Find projections with checkpoint epoch < AHM
   ↓
Step 5: Run abortrecovery
   ↓
Step 6: Restart database
   ↓
Step 7: Clean up data inconsistencies
   ├── Truncatable tables → TRUNCATE TABLE
   └── Critical tables → Data salvage
```

---

## Step 1: Restore from Backup

```bash
$ /opt/vertica/bin/vbr -t list -c backup.ini
$ /opt/vertica/bin/vbr -t restore -c backup.ini
```

---

## Step 2: Start in Unsafe Mode

```bash
$ admintools -t start_db -d mydb -U
```

> ⚠️ Unsafe mode should only be used under guidance of a senior technical support engineer.

---

## Step 3: Identify AHM & Affected Nodes

```sql
=> SELECT get_ahm_epoch();
 get_ahm_epoch
───────────────
           150

=> SELECT get_expected_recovery_epoch();
INFO 4544: Recovery Epoch Computation:
...
Nodes certainly in the cluster:
    Node 0(v_mydb_node0001), epoch 170
    Node 1(v_mydb_node0002), epoch 170
Filling more nodes to satisfy node dependencies:
    Node 3(v_mydb_node0004), epoch 149
```

Focus on nodes in "Filling more nodes" section — their LGE is below AHM.

---

## Step 4: Identify Impacted Projections

```sql
=> SELECT e.node_name, t.table_schema, t.table_name,
          e.checkpoint_epoch
   FROM v_monitor.projection_checkpoint_epochs e
   JOIN projections p ON e.projection_id = p.projection_id
   JOIN tables t ON p.anchor_table_id = t.table_id
   WHERE NOT t.is_temp_table
     AND e.is_behind_ahm
     AND e.is_up_to_date
     AND e.node_name IN ('v_mydb_node0004')
   ORDER BY t.table_schema, t.table_name;
```

> **Version diff**: 25.x uses `v_monitor.projection_checkpoint_epochs`.

---

## Step 5: Run Abortrecovery

```sql
=> SELECT DO_TM_TASK('abortrecovery', 'schema.table_name');
```

Repeat for each table found in Step 4. This sets CPE to -1, meaning no recovery on restart.

---

## Step 6: Restart Database

```bash
$ admintools -t stop_db -d mydb
$ admintools -t start_db -d mydb
```

Database will start, but tables with abortrecovery will have data inconsistencies.

---

## Step 7: Clean Up

### Truncatable Tables

```sql
=> TRUNCATE TABLE schema.table_name;
```

### Critical Tables — Partitioned

Check for count mismatch between buddy projections:

```sql
=> SELECT partition_key, diff
   FROM (
       SELECT a.partition_key,
              SUM(a.ros_row_count - a.deleted_row_count)
                - SUM(b.ros_row_count - b.deleted_row_count) AS diff
       FROM v_monitor.partitions a
       JOIN v_monitor.partitions b ON a.partition_key = b.partition_key
       WHERE a.projection_name IN ('proj_name', 'proj_name_b0')
       GROUP BY 1
   ) sub WHERE diff <> 0;
```

For mismatched partitions, either drop and reload, or use `MOVE_PARTITIONS_TO_TABLE()`.

### Critical Tables — Non-Partitioned

```sql
=> CREATE TABLE schema.table_name_new AS SELECT * FROM schema.table_name;
=> DROP TABLE schema.table_name CASCADE;
=> ALTER TABLE schema.table_name_new RENAME TO table_name;
```

---

## Prevention

| Area | Recommendation |
|------|---------------|
| **Regular backups** | Auto backup to S3/NFS via vbr |
| **UPS power** | Protect against power loss |
| **K-Safety** | Ensure K-safety >= 1 |
| **Epoch monitoring** | Track current_epoch vs ahm_epoch |
| **Restore drills** | Practice backup restore regularly |

---

## Version Differences

| 2019 KB (Micro Focus) | 25.x | Status |
|-----------------------|------|--------|
| `projection_checkpoint_epochs` | `v_monitor.projection_checkpoint_epochs` | ✅ |
| `get_ahm_epoch()` | Same | ✅ |
| `get_expected_recovery_epoch()` | Same | ✅ |
| `DO_TM_TASK('abortrecovery')` | Same | ✅ |
| `admintools -t start_db -U` | Same | ✅ |

---

*Based on Micro Focus KB KM03449287. SQL updated to Vertica 25.4.0-0.*
