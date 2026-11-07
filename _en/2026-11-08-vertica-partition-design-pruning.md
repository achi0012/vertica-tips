---
layout: post_en
title: "Vertica Partition Table Design and Partition Pruning in Practice"
date: 2026-11-08 05:00:00 +0800
categories: vertica partition data-management
tags: [vertica, partition, pruning, drop-partition, ahm, purge, ros]
description: "Benchmark Vertica partitioned table design with PARTITION BY and GROUP BY, partition pruning, v_monitor.partitions, DROP PARTITION versus DELETE, and how AHM and purge affect the data lifecycle."
---

<!--more-->

---

## 1. The Short Version

Partitioning is one of the few Vertica features that costs almost nothing to maintain once you get it right — and one of the easiest to misunderstand. People treat it as an index, assume `DELETE` frees disk space, or run a monthly `DELETE` and then wonder where the space went.

On a single-node Vertica 25.4.0-0 instance I built a partitioned table, a flat table, a daily-partitioned table, and a hierarchically grouped table over 6 million rows and 24 months of data. Six findings:

1. **Partitioning costs almost nothing.** The same 6,000,000 rows: the monthly-partitioned table used 91,065,421 bytes, the flat table 91,088,745 bytes (a 23,324-byte difference, −0.03%). `COPY ... DIRECT` of a 313 MB CSV took 2,967.2 ms versus 2,974.6 ms. **Partitioning does not store a second copy of anything.**
2. **Pruning is real, and you can count the rows it saves.** For a query on January 2026, the partitioned table's `Scan` processed 250,000 rows; the flat table processed 6,000,000 (24×). Initiator memory dropped from 47,014 KB to 10,436 KB.
3. **But wall-clock time does not improve 24×.** The same heavy aggregate query ran in 24.9–37.9 ms on the partitioned table and 28.1–38.5 ms on the flat table — medians of 25.7 versus 28.2 ms, about 9%. At 6M fully cached rows, **the aggregate dominates, not the scan**.
4. **Partition granularity has its own cost.** A table with 120 daily partitions took a median of 42.6 ms for a full aggregate; the same 600,000 rows in one container took 28.6 ms — **49% slower**.
5. **Partition grouping trades rows read for ROS containers.** `CALENDAR_HIERARCHY_DAY` collapsed 120 partition keys into 4 ROS containers, but a single-day query went from reading 5,000 rows to 155,000 (31×).
6. **`DROP PARTITION` is the right way to retire old data.** Dropping January 2025 took 13.1 ms and freed 3,795,575 bytes immediately. `DELETE` of the same 250,000 rows took 35.0 ms and freed **zero bytes** at delete time, then needed a 1,604.8 ms mergeout to actually return the space.

This is written for anyone designing time-series tables, fighting "I deleted the data but the disk is still full", or simply wanting proof that pruning actually fires. Every number comes from a real run, and the `article_lab_part` schema was dropped with `DROP SCHEMA ... CASCADE` when the lab finished.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Nodes | 1 (Community Edition, `v_monitor.system.node_count = 1`) |
| Database / user | `testdb` / `dbadmin` |
| Test schema | `article_lab_part` (dropped after the lab; 0 leftover objects verified) |
| Fact table | 6,000,000 rows × 5 columns, 24 months |
| Columns | `sale_id`, `event_ts`, `region`, `amount`, `payload` |
| Time range | 2024-11-01 through 2026-10-30 |
| Load method | `COPY ... DIRECT`, one 313 MB file |
| Measurement date | 2026-11-08 |

Three details affect reproducibility:

- **This connection runs with `autocommit` set to `off`.** `CREATE` and `COPY` commit on their own; `INSERT` does not. After inserting one 2026-12 row, the next session still returned `COUNT(*) = 5,500,000` — the missing row was simply never committed. Every test here ran inside a single `vsql -f` session.
- **A single node has only one segment**, so nothing here says anything about cross-node skew.
- **In 25.4 the AHM is not in `v_monitor.epochs`.** On this box `SELECT * FROM v_monitor.epochs` returns `ERROR 4568: Relation "v_monitor.epochs" does not exist`; `v_catalog.epochs` now has only `epoch_number` and `epoch_close_time`. The AHM lives in `v_monitor.system`:

```sql
=> SELECT current_epoch, ahm_epoch, last_good_epoch FROM v_monitor.system;
 current_epoch | ahm_epoch | last_good_epoch
---------------+-----------+-----------------
       2329826 |   2329825 |         2329825
```

That difference breaks monitoring queries copied from older documentation, which makes it the first practical trap in this article.

---

## 3. Core Mechanics

### 3.1 Partitioning is a table property, not an index

`PARTITION BY` goes in `CREATE TABLE` and applies to every projection of the table. Vertica splits the data into ROS containers by partition key: at least one container per key, more when a partition is large.

| What you write | What Vertica does | Effect on queries |
|---|---|---|
| `PARTITION BY (year*100+month)` | One partition key per month, each in its own ROS container | A single-month query scans only that container |
| `PARTITION BY event_ts::DATE` | One key per day | Single-day queries are fast, but container count explodes |
| `GROUP BY CALENDAR_HIERARCHY_DAY(...)` | Several keys merged into one partition group sharing a container | Fewer containers, but a single-day query reads the whole group |

The key point: **partitioning changes neither the storage format nor any index.** It changes how many pieces the data is cut into and where the boundaries sit. Storage cost is unchanged (−0.03% here); what changes is how many pieces a scan can skip.

### 3.2 When pruning fires

The optimizer needs to derive "which partition keys does this predicate cover". The measured behaviour is looser than the documentation implies:

| Predicate | Rows scanned | Pruned? |
|---|---|---|
| `event_ts >= '2026-01-01' AND event_ts < '2026-02-01'` | 250,000 | Yes |
| `EXTRACT(YEAR FROM event_ts)*100 + EXTRACT(MONTH FROM event_ts) = 202601` | 250,000 | Yes |
| `EXTRACT(MONTH FROM event_ts) = 1` | 500,000 | Yes (only 2025-01 and 2026-01) |
| `TO_CHAR(event_ts,'YYYYMM') = '202601'` | 5,500,000 | No (full table) |
| `(January 2026 range) OR region = 'ZZ'` | 250,000 | Yes |

So: **pruning works whenever the predicate lets the optimizer derive a range of partition keys — you do not have to repeat the `PARTITION BY` expression verbatim.** Wrapping the column in `TO_CHAR` (convert to string, then compare) destroys that information and falls back to a full scan.

(The `TO_CHAR` and `OR` rows above were measured after the delete tests below, when the table held 5,500,000 rows; the other rows were measured at 6,000,000.)

### 3.3 What happens after a DELETE

This is the most commonly misunderstood part of the topic. `DELETE` does not remove data immediately:

1. `DELETE` writes a **delete vector** onto the ROS container, marking which rows are gone.
2. `SELECT` filters them out (results are correct), but **disk usage does not change**.
3. Only a tuple mover mergeout that merges containers — and only once the **AHM has passed the epoch of the delete** — actually purges the expired rows.
4. `DROP PARTITION` removes the whole container from the catalog: **no mergeout, no AHM dependency**.

The measurements below quantify each step.

---

## 4. Step-by-Step Measurements

### 4.1 Create and load the partitioned table

```sql
=> CREATE TABLE article_lab_part.sales_p (
     sale_id  INT NOT NULL,
     event_ts TIMESTAMP NOT NULL,
     region   VARCHAR(8),
     amount   NUMERIC(12,2),
     payload  VARCHAR(64)
   )
   PARTITION BY (EXTRACT(YEAR FROM event_ts)::INT * 100 + EXTRACT(MONTH FROM event_ts)::INT);
```

Without a cast — `PARTITION BY (EXTRACT(YEAR FROM event_ts) * 100 + EXTRACT(MONTH FROM event_ts))` — you get an extra warning:

```
WARNING 6100:  Using PARTITION expression that returns a Numeric value
HINT:  This PARTITION expression may cause too many data partitions.
       Use of an expression that returns a more accurate value,
       such as a regular VARCHAR or INT, is encouraged
```

Casting the whole expression with `::INT` makes 6100 disappear (verified side by side: it appears without the cast, and does not with it). The statement above still emits `WARNING 9249`, a reminder that the partition key may contain NULL:

```
WARNING 9249:  Using PARTITION expression that may result in NULL values
HINT:  DML statements which produce NULL values for partition expressions will roll back
```

That is why `event_ts` is declared `NOT NULL`. After loading, the partition keys were `202411`, `202412` … `202610`: 24 of them, as intended.

Loading 6 million rows (313 MB):

```
=> COPY article_lab_part.sales_p FROM '/tmp/opencode/sales_part.csv' DELIMITER '|' DIRECT;
 Rows Loaded
-------------
     6000000
Time: First fetch (1 row): 2967.224 ms.
```

The same data into the flat table `sales_flat` took 2,974.6 ms; loading a **shuffled** copy into `sales_flat_shuf` took 3,879.3 ms (+30.3%). **Partitioning itself adds no load cost; whether the data arrives sorted is what matters** — sorted input compresses and writes more efficiently.

Storage:

```sql
=> SELECT projection_name, SUM(used_bytes) AS used_bytes, SUM(total_row_count) AS rows,
          COUNT(*) AS ros
   FROM v_monitor.storage_containers
   WHERE schema_name='article_lab_part' GROUP BY 1 ORDER BY 1;
    projection_name    | used_bytes |  rows   | ros
-----------------------+------------+---------+-----
 sales_flat_shuf_super |   91088745 | 6000000 |   1
 sales_flat_super      |   91088745 | 6000000 |   1
 sales_p_super         |   91065421 | 6000000 |  24
```

24 partitions, 24 ROS containers, and 23,324 fewer bytes than the flat table.

### 4.2 What pruning actually saves: counting rows with PROFILE

In 25.4, `EXPLAIN` does not print a "Partition pruning" line; the plan shows the same `Filter:` either way. The direct way to confirm pruning is `PROFILE` (these are my own lab tables, not production ones):

```sql
=> PROFILE SELECT COUNT(*), SUM(amount) FROM article_lab_part.sales_p
   WHERE event_ts >= '2026-01-01' AND event_ts < '2026-02-01';
```

Reading `rows processed` for the Scan operator from `v_monitor.execution_engine_profiles`:

| Query | Rows scanned | Initiator memory |
|---|---|---|
| Partitioned table, one month | 250,000 | 10,436 KB |
| Flat table, one month | 6,000,000 (6 Scans summed) | 47,014 KB |
| Partitioned table, `TO_CHAR`-wrapped | 5,500,000 | 47,020 KB |
| Partitioned table, `(one month) OR region='ZZ'` | 250,000 | 10,468 KB |

250,000 is exactly one month (6,000,000 / 24), which means **one container was scanned**. The flat table was loaded in date order, but a single `COPY` produced a single container, so container-level min/max pruning could not help at all: 6,000,000 rows.

One counter-intuitive result: `OR` did not disable pruning. Documentation and community posts often claim it does, but in this shape the optimizer still pushed the partition predicate down (250,000 rows). **Do not assume general rules — measure.**

### 4.3 Why only 9% faster: breaking down the cost

The same heavy single-month aggregate (`GROUP BY region` plus `SUM(LENGTH(payload))`), three runs each:

```
Partitioned: 37.890 ms / 25.658 ms / 24.882 ms   (median 25.7)
Flat:        38.518 ms / 28.209 ms / 28.111 ms   (median 28.2)
```

A 24× difference in rows scanned, a 9% difference in time. The reason is simple: all 91 MB fit in memory and page cache, so the scan itself took 0.9 ms (PROFILE reported 885 µs of `Scan` execution time). The rest is `SUM(NUMERIC)` and `GROUP BY` CPU work. **Pruning saves I/O and rows scanned; it does not make aggregation faster.**

On a production system the ratio looks very different — the scan comes from disk, there are more columns, and each month holds far more data, so the scan share is larger. But I will not extrapolate a production speedup from a lab measurement of 9%; that would be fabrication. What is certain: **the ceiling for pruning is the scan work you skipped, not a multiplier on the whole query.**

### 4.4 Granularity: 120 daily partitions made full scans 49% slower

A daily-partitioned table (120 days, 5,000 rows per day, 600,000 rows total):

```sql
=> CREATE TABLE article_lab_part.sales_day (...) PARTITION BY (event_ts::DATE);
=> SELECT COUNT(DISTINCT partition_key) AS keys, COUNT(DISTINCT ros_id) AS ros
   FROM v_monitor.partitions p
   JOIN v_catalog.projections pr ON pr.projection_id = p.projection_id
   WHERE pr.anchor_table_name='sales_day';
 keys | ros
------+-----
  120 | 120
```

A full aggregate with no pruning predicate:

```
120 partitions: 41.762 / 42.552 / 44.354 ms  (median 42.6)
Single container: 38.605 / 26.202 / 28.598 ms (median 28.6)
```

**49% slower.** Every container has to be opened, its header read, and a scan initialized; 120 of them is 120 times that fixed cost. This is exactly why the documentation keeps warning against unbounded subdivision.

With a pruning predicate, fine granularity pays off: for `2025-03-15` the daily table scanned only 5,000 rows.

### 4.5 Partition grouping: 31× more rows read for 30× fewer containers

After a few years, one partition per day means thousands of containers. Vertica's answer is partition grouping:

```sql
=> CREATE TABLE article_lab_part.sales_hier (...)
   PARTITION BY event_ts::DATE
   GROUP BY CALENDAR_HIERARCHY_DAY(event_ts::DATE, 2, 2);
```

After loading the same 120 days and 600,000 rows:

```sql
=> SELECT COUNT(DISTINCT p.partition_key) AS partition_keys, COUNT(DISTINCT p.ros_id) AS ros
   FROM v_monitor.partitions p JOIN v_catalog.projections pr ON pr.projection_id=p.projection_id
   WHERE pr.anchor_table_name='sales_hier';
 partition_keys | ros
----------------+-----
            120 |   4
```

**120 partition keys in only 4 ROS containers** (roughly one month per group). The price shows up in queries:

| Querying a single day, 2025-03-15 | Rows scanned |
|---|---|
| Daily partitions (ungrouped) | 5,000 |
| Partition groups (monthly) | 155,000 |

Same SQL, same `WHERE event_ts >= '2025-03-15' AND event_ts < '2025-03-16'`; the grouped table reads **31× more rows**, because the smallest prunable unit is the group, not the day.

Grouping also coarsens `DROP`. Trying to drop one day inside a group fails outright:

```
=> SELECT DROP_PARTITIONS('article_lab_part.sales_hier', '2025-03-15', '2025-03-15');
ERROR 8430:  Some storage containers must be split before the specified partition range
             can be dropped
```

Dropping the whole month group succeeds:

```
=> SELECT DROP_PARTITIONS('article_lab_part.sales_hier', '2025-03-01', '2025-03-31');
 Partition dropped
```

**Grouping ties "how finely can I drop" to "how many containers do I have".** Decide both together.

### 4.6 DROP PARTITION vs DELETE vs mergeout

This is the most valuable set of numbers. First, `DELETE` the 250,000 January 2025 rows:

```
=> DELETE FROM article_lab_part.sales_p
   WHERE event_ts >= '2025-01-01' AND event_ts < '2025-02-01';
 OUTPUT
--------
 250000
Time: 35.033 ms
```

Storage immediately after:

```sql
=> SELECT projection_name, SUM(used_bytes) AS used_bytes, SUM(total_row_count) AS rows,
          SUM(delete_vector_count) AS dvs, SUM(deleted_row_count) AS deleted
   FROM v_monitor.storage_containers
   WHERE schema_name='article_lab_part' AND projection_name='sales_p_super' GROUP BY 1;
 projection_name | used_bytes |  rows   | dvs | deleted
-----------------+------------+---------+-----+---------
 sales_p_super   |   91065421 | 6000000 |   1 |  250000
```

**Bytes are unchanged (91,065,421), plus one delete vector.** `SELECT COUNT(*)` already returns 5,750,000 — results are correct — but those 3.8 MB are still on disk.

Now `DROP_PARTITION`:

```
=> SELECT DROP_PARTITION('article_lab_part.sales_p', 202501);
WARNING 8461:  drop_partition has been deprecated and will be removed in a future version
HINT:  Use drop_partitions and a single value for both the min and max range value
 Partition dropped
Time: 13.128 ms

=> -- used_bytes: 91,065,421 → 87,269,846, a 3,795,575-byte drop
```

**13.1 ms, and 3,795,575 bytes returned immediately**, with the delete vector gone (`dvs = 0`, `deleted = 0`). That number matches the `ros_size_bytes` of partition 202501 in `v_monitor.partitions` exactly.

`DELETE` of the same 250,000 rows on the flat table took 35.5 ms (essentially the same as 35.0 ms on the partitioned table), and again freed nothing. Reclaiming it required a mergeout:

```
=> SELECT DO_TM_TASK('mergeout', 'article_lab_part.sales_flat');
Time: 1604.794 ms
=> -- used_bytes: 91,088,745 → 87,167,347, 3,921,398 bytes freed, delete vector cleared
```

Summary:

| Operation | Duration | When space is freed | Bytes freed |
|---|---|---|---|
| `DELETE` 250k rows (partitioned) | 35.0 ms | Never, waits for mergeout | 0 |
| `DELETE` 250k rows (flat) | 35.5 ms | Never, waits for mergeout | 0 |
| `DROP_PARTITION` 202501 | **13.1 ms** | **Immediately** | **3,795,575** |
| `DO_TM_TASK('mergeout')` on flat table | 1,604.8 ms | On completion | 3,921,398 |

`DROP_PARTITION` was **122× faster** than the mergeout and needs no AHM wait. That is why "partition your time-series tables by time" is close to mandatory: **not to make queries faster, but to make data cheap to throw away.**

There is a related cost people forget: a `DELETE` without a partition predicate is still a full scan. I ran `DELETE ... WHERE region = 'ZZ'` (matching nothing) and PROFILE reported initiator memory of **4,212,454 KB**; the partition-predicate `DELETE` used **10,436 KB**, a 400× difference. Delete memory is bounded by the resource pool, so this shape can fail outright in production.

### 4.7 Monitoring partitions: three traps I actually hit

**(1) `v_monitor.partitions` has no `table_name`.** It exposes `table_schema`, `projection_name`, `partition_key`, `ros_id`, `ros_row_count` and friends. To query by table you must join:

```sql
=> SELECT pr.anchor_table_name, COUNT(DISTINCT p.partition_key) AS keys,
          COUNT(DISTINCT p.ros_id) AS ros
   FROM v_monitor.partitions p
   JOIN v_catalog.projections pr ON pr.projection_id = p.projection_id
   WHERE p.table_schema = 'article_lab_part'
   GROUP BY 1 ORDER BY 1;
```

**(2) `SUM(ros_row_count)` inflates under partition grouping.** `v_monitor.partitions` has one row per partition key, but `ros_row_count` holds the **container's** row count. One container in the grouped table held 30 keys:

```sql
=> SELECT ros_id, COUNT(*) AS keys, SUM(ros_row_count) AS summed
   FROM v_monitor.partitions p JOIN v_catalog.projections pr ON pr.projection_id=p.projection_id
   WHERE pr.anchor_table_name='sales_hier' GROUP BY 1;
      ros_id       | keys |  summed
-------------------+------+----------
 45035996322353409 |   30 |  4500000

=> SELECT COUNT(*) FROM article_lab_part.sales_hier WHERE event_ts < '2025-02-01';
 155000
```

Summing gives 4,500,000 against an actual 155,000 — **29× too high**. Count rows with `v_monitor.storage_containers` (one row per container) or `COUNT(DISTINCT ros_id)`.

**(3) A new partition is invisible until data reaches ROS.** I loaded two rows for 2026-12:

```sql
=> COPY article_lab_part.sales_p FROM '/tmp/opencode/newpart.csv' DELIMITER '|' DIRECT;
 Rows Loaded: 2
=> -- partition keys went from 22 to 23; 202612 appeared immediately with ros_row_count = 2
```

`COPY ... DIRECT` writes straight to ROS, so it shows up at once. With `INSERT` (which goes to WOS), the key appears in `v_monitor.partitions` only after a moveout. The row I inserted with `INSERT` did not even show up in `COUNT(*)` — because `autocommit` was `off` and I never issued `COMMIT`.

Partition-level statistics live in `v_monitor.partition_columns`. Running `ANALYZE_STATISTICS` on the partitioned table returns 0, but each partition's `statistics_type` becomes `FULL`:

```sql
=> SELECT ANALYZE_STATISTICS('article_lab_part.sales_p');
 ANALYZE_STATISTICS
--------------------
                  0
=> SELECT p.partition_key, p.column_name, p.statistics_type
   FROM v_monitor.partition_columns p
   JOIN v_catalog.projections pr ON pr.projection_id=p.projection_id
   WHERE pr.anchor_table_name='sales_p' AND p.column_name='amount'
   ORDER BY p.partition_key LIMIT 3;
 partition_key | column_name | statistics_type
---------------+-------------+-----------------
 202411        | amount      | FULL
 202412        | amount      | FULL
 202503        | amount      | FULL
```

---

## 5. Performance and Cost Analysis

The decision table from these measurements:

| Design choice | Benefit (measured) | Cost (measured) |
|---|---|---|
| Monthly partitions | Single-month query scans 250,000 rows (vs 6,000,000) | Storage +0% (−0.03%), load +0% |
| Daily partitions (120 days) | Single-day query scans 5,000 rows | Full scan 49% slower (42.6 vs 28.6 ms), 120 containers |
| Partition grouping (monthly) | 120 keys → 4 containers | Single-day query reads 31× more (155,000 vs 5,000); cannot drop a single day (ERROR 8430) |
| `DROP PARTITION` for one month | 13.1 ms, frees 3.8 MB immediately | Whole-block only; verify no partition-spanning dependencies |
| `DELETE` + mergeout | Can delete partial rows | 35.0 ms delete + 1,604.8 ms mergeout, no space freed in between |

Rules of thumb:

- **Clear time axis and bulk expiry** → partition, with granularity equal to the smallest unit you can legitimately discard (usually a month).
- **Queries always carry a time range** → partitioning pays off. If most workload is full-table aggregation, partitioning only adds the per-container fixed cost (+49% here).
- **More than a year of data, one partition per day** → use `CALENDAR_HIERARCHY_DAY`, but first confirm single-day queries are not the main workload; grouping makes them 31× more expensive.
- **Need to reclaim space** → do not rely on `DELETE`; use `DROP PARTITION`.

---

## 6. Common Errors and Anti-Patterns

### 1. Assuming `ALTER TABLE ... DROP PARTITION` exists

```
=> ALTER TABLE article_lab_part.sales_p DROP PARTITION 202503;
ERROR 4856:  Syntax error at or near "202503" at character 53
```

There is no such syntax; use the `DROP_PARTITION` / `DROP_PARTITIONS` meta-functions. In 25.4 the singular form reports deprecation (WARNING 8461), so new code should call `DROP_PARTITIONS(table, min, max)`.

### 2. Calling `DROP_PARTITION` on an unpartitioned table

```
=> SELECT DROP_PARTITION('article_lab_part.sales_flat', 0);
ERROR 4881:  Table "sales_flat" is not partitioned
```

Harmless on its own, but an automation script that "creates the table, then drops partitions" will only discover a missing partition clause here.

### 3. A NULL partition key

```sql
=> CREATE TABLE t_null (d TIMESTAMP, v INT)
   PARTITION BY (EXTRACT(YEAR FROM d)::INT * 100 + EXTRACT(MONTH FROM d)::INT);
WARNING 9249:  Using PARTITION expression that may result in NULL values
HINT:  DML statements which produce NULL values for partition expressions will roll back

=> INSERT INTO t_null VALUES (NULL, 1);
ERROR 4195:  NULL value detected in data partitioning expression
```

**The whole DML statement rolls back**, not just the offending row. Declare partition key columns `NOT NULL`; if NULLs must be allowed, wrap the expression in `ZEROIFNULL()` — I verified that `PARTITION BY (ZEROIFNULL(a))` accepts NULL and puts it in key 0.

### 4. Wrapping the partition column in a function

```sql
=> SELECT COUNT(*), SUM(amount) FROM article_lab_part.sales_p
   WHERE TO_CHAR(event_ts, 'YYYYMM') = '202601';
-- Scan rows processed: 5,500,000 (full table), memory 47,020 KB
```

The equivalent `event_ts >= '2026-01-01' AND event_ts < '2026-02-01'` scans 250,000 rows. `TO_CHAR`, casts to a different type, and string concatenation all destroy the range information the optimizer needs.

### 5. A non-deterministic partition expression

```
=> CREATE TABLE t_e (d DATE, v INT) PARTITION BY RANDOM();
ERROR 2552:  Cannot use meta function or non-deterministic function in PARTITION BY expression
```

`RANDOM()` and `NOW()` cannot appear in the partition expression. `CALENDAR_HIERARCHY_DAY` references `NOW()` internally, which is why the documentation only ever shows it in `GROUP BY`; this lab only exercised it in `GROUP BY` and did not test it in `PARTITION BY`.

### 6. Writing a custom `GROUP BY` expression incorrectly

```
=> ... PARTITION BY (year*100+month) GROUP BY (year)
ERROR 8363:  Partition GROUP BY expression can only reference columns by way of
             inclusion of the PARTITION BY expression
```

`GROUP BY` must be derived **from the partition expression**, not from the raw columns. For a custom hierarchy, wrap the same partition expression in a `CASE` (the docs have an example), or just use `CALENDAR_HIERARCHY_DAY`.

### 7. Expecting the disk to shrink after `DELETE`

After deleting 250,000 rows, `used_bytes` did not move by a single byte. Monitoring only `df` makes it look like the delete never ran; monitoring only `SELECT COUNT(*)` makes it look like the space is already back. **Watch both, and understand the roles of mergeout and the AHM.**

### 8. Unbounded daily partitioning

120 days meant 120 containers. This test only reached 120; production accumulates thousands over a few years, and the fixed per-container cost grows linearly (120 containers already made full aggregation 49% slower here). **Treat partition count as a capacity parameter, not as "finer is always better".**

---

## 7. Risks and Recommendations

### Safe now (read-only / low risk)

1. **Inventory current partitions:**

```sql
=> SELECT pr.anchor_table_name, COUNT(DISTINCT p.partition_key) AS keys,
          COUNT(DISTINCT p.ros_id) AS ros
   FROM v_monitor.partitions p
   JOIN v_catalog.projections pr ON pr.projection_id = p.projection_id
   GROUP BY 1 ORDER BY ros DESC LIMIT 20;
```

2. **Find tables carrying delete vectors that never got purged** — direct evidence of unreclaimed disk:

```sql
=> SELECT projection_name, SUM(used_bytes) AS bytes,
          SUM(delete_vector_count) AS dvs, SUM(deleted_row_count) AS deleted
   FROM v_monitor.storage_containers
   WHERE delete_vector_count > 0
   GROUP BY 1 ORDER BY deleted DESC LIMIT 20;
```

3. **Check whether the AHM is lagging** (on 25.4, query `v_monitor.system`, not `v_monitor.epochs`):

```sql
=> SELECT current_epoch, ahm_epoch, current_epoch - ahm_epoch AS lag FROM v_monitor.system;
```

4. **Use `EXPLAIN` to confirm hot queries include the partition predicate**, and `PROFILE` in a test environment to verify the Scan's `rows processed`.

### Requires change (needs a window / approval)

1. **Adding partitioning to an existing time-series table**: `ALTER TABLE ... PARTITION BY ... REORGANIZE` rewrites the entire table (I did not run `REORGANIZE` on the lab tables; this is flagged from the documented semantics), so time and space cost is roughly a full reload. Confirm disk headroom, a maintenance window, and a rollback plan (worst case: rebuild and reload the table).
2. **Switching daily partitions to monthly, or adding `CALENDAR_HIERARCHY_DAY`**: also `REORGANIZE`, and it changes `DROP` granularity. Any downstream job that calls `DROP_PARTITIONS` per day will start failing with ERROR 8430.
3. **Replacing a `DELETE` cleanup job with `DROP_PARTITION`**: verify nothing queries across partitions, and that no view or external process assumes row-level deletion. Rehearse once outside production.
4. **Tuning `ActivePartitionCount` or retention policy**: a table- or cluster-level setting; change one table at a time and watch ROS counts and mergeout behaviour for a week before widening.

---

## 8. References

- Vertica documentation: Partition clause (syntax and restrictions for `PARTITION BY` / `GROUP BY`) — <https://docs.vertica.com/26.1.x/en/sql-reference/statements/create-statements/create-table/partition-clause/>
- Vertica documentation: Partition grouping (group keys and `CALENDAR_HIERARCHY_DAY`) — <https://docs.rocketsoftware.com/bundle/vertica_24.2/page/admin/partitioning-tables/defining-partitions/partition-grouping.html>
- Vertica documentation: Hierarchical partitioning (dynamic grouping and tuple mover behaviour) — <https://docs.rocketsoftware.com/bundle/vertica_11.1/page/admin/partitioning-tables/hierarchical-partitioning.html>
- Vertica documentation: `DROP_PARTITIONS` — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/drop_partitions/>
- Vertica Forum: Partitioning strategy (worked examples of grouped `DROP_PARTITIONS` and forceSplit) — <https://forum.vertica.com/discussion/243329/partitioning-strategey>
