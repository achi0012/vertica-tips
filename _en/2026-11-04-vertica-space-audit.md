---
layout: post_en
title: "Auditing Vertica Disk Space: Don't Count ROS Containers, Count Bytes (25.4 Measured)"
date: 2026-11-04 10:00:00 +0800
categories: vertica storage audit delete-vectors
tags: [vertica, storage, ROS, delete-vectors, projection-storage, purge, v_monitor, audit, mergeout]
description: "Measured on Vertica 25.4: 1,254 of 1,524 ROS containers hold just 0.03 GB. Four audit queries, empty-table detection, and a reclaim-action table."
---

The disk is filling up, and the reflex is "too many ROS containers, let mergeout clean it up". That instinct held up in older Vertica releases, but after auditing a 68.20 GB instance on 25.4 the picture is different: **container count and disk bytes are almost completely decoupled.** 1,254 of 1,524 containers (82.3%) are under 1 MB, and removing all of them would reclaim 0.03 GB — 0.04% of the database. The bytes actually live in the other 33 containers.

This is not a mergeout tutorial (for the mechanics, see [Tuple Mover best practices]({{ site.baseurl }}/2026/06/vertica-tuple-mover-best-practice/)). It is an **audit procedure**: four system tables to split "where did the space go" into three reconcilable ledgers, then two criteria to decide what is worth reclaiming and what would be wasted work.

<!--more-->

---

## 1. Reconcile First: Three Space Ledgers

Vertica exposes space in three places. Mixing them produces contradictory conclusions.

| Source | Meaning | Answers |
|--------|---------|---------|
| `v_monitor.projection_storage` | Per-projection, per-node ROS storage (`used_bytes`, `ros_count`) | Which table or projection holds the most bytes |
| `v_monitor.storage_containers` | One row per ROS container (size, row count, deleted row count) | Fragmentation and per-container health |
| `v_monitor.delete_vectors` | The delete markers themselves (row ranges + epoch) | "How many rows are marked deleted" — **not** a space report |

On the measured instance:

| Metric | Value |
|--------|-------|
| `projection_storage` SUM(`used_bytes`) | 68.20 GB |
| `projection_storage` SUM(`disk_size`) | 68.32 GB |
| `storage_containers` SUM(`used_bytes`) | 68.197 GB |
| `delete_vectors` SUM(`used_bytes`) | 0.65 MB |

Two takeaways:

1. `used_bytes` and `disk_size` differ by 0.12 GB — file allocation granularity and sparse files, not inconsistent data. Size capacity planning on `disk_size`; reason about data volume on `used_bytes`.
2. **`delete_vectors.used_bytes` being a few MB does not mean deletes waste a few MB.** A delete vector is an index of deleted row positions; the deleted rows themselves still sit in the ROS container, and their bytes are counted in `storage_containers`. This is the most common misread, and section 5 quantifies it.

```sql
-- Reconcile all three ledgers side by side
SELECT round(SUM(used_bytes)/1024^3, 2) AS used_gb,
       round(SUM(disk_size) /1024^3, 2) AS disk_gb
FROM v_monitor.projection_storage
WHERE projection_schema NOT IN ('v_catalog','v_monitor','v_internal');
```

---

## 2. Count Bytes, Not Containers

This is the most important table in the article. Same instance, same moment, 1,524 ROS containers bucketed by size:

| Container size | Containers | Share of count | Total GB | Share of bytes |
|----------------|-----------:|---------------:|---------:|---------------:|
| **< 1 MB** | **1,254** | **82.3%** | **0.03** | **0.04%** |
| 1 – 10 MB | 91 | 6.0% | 0.37 | 0.54% |
| 10 – 100 MB | 132 | 8.7% | 3.77 | 5.5% |
| 0.1 – 1 GB | 14 | 0.9% | 3.09 | 4.5% |
| **≥ 1 GB** | **33** | **2.2%** | **60.94** | **89.4%** |
| Total | 1,524 | 100% | 68.20 | 100% |

82.3% of containers carry 0.04% of the data; 2.2% of containers carry 89.4%.

So "too many containers" carries no information by itself — the question is whether **container count and byte count move together**. Here they do not:

- Containers ≥ 1 MB: 270
- Their total: **68.16 GB (99.95%)**
- The remaining 1,254 sub-1-MB containers: 0.03 GB (0.05%)

Merging them away would not free disk; it would remove 1,254 files. **That fragmentation is still worth fixing — for file count, mergeout overhead and query scan width, not to save space.** Those are different priorities with different remedies, and conflating them leads to the wrong decision.

```sql
-- Container size histogram
SELECT CASE WHEN used_bytes <   1048576 THEN 'a  <1MB'
            WHEN used_bytes <  10485760 THEN 'b  1-10MB'
            WHEN used_bytes < 104857600 THEN 'c  10-100MB'
            WHEN used_bytes < 1073741824 THEN 'd  0.1-1GB'
            ELSE                             'e  >=1GB' END AS bucket,
       COUNT(*)                          AS containers,
       round(SUM(used_bytes)/1024^3, 2)  AS gb
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
GROUP  BY 1
ORDER  BY 1;
```

---

## 3. What Real Fragmentation Looks Like: MB per Container

Push the histogram down to the table level and measure **average MB per container**. Both extremes coexist in one database:

| Table (masked) | Size | Containers | Projections | MB / container | Verdict |
|----------------|-----:|-----------:|------------:|---------------:|---------|
| `schema_a.t1` | 41.19 GB | 57 | 1 | 739.9 | Healthy, large containers |
| `schema_a.t2` | 12.57 GB | 1 | 1 | 12,875 | Healthy, single large container |
| `schema_a.t3` | 7.03 GB | 1 | 1 | 7,196 | Healthy |
| `schema_b.t1` | 4.63 GB | 136 | 2 | 34.9 | Watch, frequent micro-batches |
| `schema_c.t1` | 0.13 GB | 267 | 1 | **0.49** | **Fragmented** |

`schema_b.t1` spreads 4.63 GB across 136 containers at 34.9 MB each — normal for a table loaded in frequent increments; mergeout catches up on its own.

`schema_c.t1` holds only 131 MB but scatters it across 267 containers, **255 of them under 1 MB**. Queries on that table open 267 containers one by one, and the fixed cost of metadata plus file opens dominates. It is also closing in on `ContainersPerProjectionLimit` (default 1024).

```sql
-- MB per container, per table (find fragmentation)
SELECT anchor_table_schema                             AS table_schema,
       anchor_table_name                               AS table_name,
       round(SUM(used_bytes)/1024^3, 2)                AS gb,
       SUM(ros_count)                                  AS ros,
       COUNT(*)                                        AS projections,
       round(SUM(used_bytes)/NULLIF(SUM(ros_count),0)/1048576, 1) AS mb_per_ros
FROM   v_monitor.projection_storage
WHERE  projection_schema NOT IN ('v_catalog','v_monitor','v_internal')
GROUP  BY 1, 2
ORDER  BY 3 DESC
LIMIT  20;
```

```sql
-- Fragmentation ranking: many containers, mostly under 1 MB
SELECT schema_name, projection_name,
       COUNT(*)                                                  AS ros,
       SUM(CASE WHEN used_bytes < 1048576 THEN 1 ELSE 0 END)      AS tiny,
       round(SUM(used_bytes)/1048576, 1)                          AS mb
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
GROUP  BY 1, 2
HAVING COUNT(*) > 20
ORDER  BY 4 DESC
LIMIT  20;
```

Top three by `tiny`:

| Projection | Containers | Under 1 MB | Total |
|------------|-----------:|------------:|------:|
| `schema_c.t1_super` | 267 | 255 | 131 MB |
| `schema_d.t1_super` | 46 | 46 | ~0 (empty) |
| `schema_a.t4_super` | 42 | 42 | 3 MB |

The second and third entries matter: **46 and 42 containers holding essentially nothing.** These are empty projections (created but never loaded, or fully deleted); mergeout has nothing to merge, yet they still appear in `storage_containers`. A container-count ranking alone is polluted by exactly this — which is why bytes must always be read alongside count.

---

## 4. 164 Empty Tables: The Easiest Win in the Audit

Of 409 user tables, **164 (40.1%) consume 0 bytes.** They cost no disk, but they do:

- add catalog load and slow `v_catalog` queries
- make "is this table still in use?" unanswerable
- become landmines during `DROP SCHEMA ... CASCADE` or migrations

Use `NOT EXISTS` rather than `SUM(used_bytes) = 0` — a table with no projections at all never appears in `projection_storage`:

```sql
SELECT t.table_schema, t.table_name
FROM   v_catalog.tables t
WHERE  t.table_schema NOT IN ('v_catalog','v_monitor','v_internal','v_func','v_txtindex')
  AND  NOT EXISTS (
         SELECT 1 FROM v_monitor.projection_storage ps
         WHERE  ps.anchor_table_schema = t.table_schema
           AND  ps.anchor_table_name   = t.table_name
           AND  ps.used_bytes > 0)
ORDER  BY 1, 2;
```

**But "0 bytes" does not mean "safe to drop".** An empty table may be:

1. a partition table whose current period is not loaded yet
2. a shell left behind by a failed load
3. a schema template someone deliberately keeps
4. referenced by a view or external job that is simply idle

I would not use `query_requests` text matching as sole evidence — retention is limited and the `request` column does not reliably contain table names, so it produces false kills. The procedure I recommend:

| Step | Action | Purpose |
|------|--------|---------|
| 1 | Export `CREATE TABLE` DDL and dependent view definitions | Preserve rebuild ability |
| 2 | `ALTER TABLE ... RENAME TO ..._deprecated_YYYYMM` | Nothing deleted; watch for application errors |
| 3 | Observe 2–4 weeks (including month/quarter close) | Catch low-frequency users |
| 4 | `DROP TABLE` only if clean | This step is irreversible |

Step 2 is the point: **rename is reversible, drop is not.** On this instance I dropped none of the 164 — the deliverable of an audit is a list and a recommendation, not an execution.

---

## 5. 288,920 Rows Marked Deleted: A Delete Vector Is Not Wasted Space

Measured in `v_monitor.delete_vectors`: **310 entries across 19 projections, 288,920 rows total.**

The distribution is extremely concentrated — two super projections account for 94.5%:

| Projection (masked) | DV entries | Deleted rows | DV size |
|---------------------|-----------:|-------------:|--------:|
| `schema_a.t1_super` | 111 | 143,151 | 0.4 MB |
| `schema_a.t5_super` | 4 | 129,733 | 0.3 MB |
| `schema_a.t6_super` | 3 | 6,079 | < 0.1 MB |
| `schema_a.t7_super` | 1 | 3,042 | < 0.1 MB |
| `schema_c.t1_super` | 156 | 1,864 | < 0.1 MB |

```sql
-- Unpurged deletes: find where they concentrate
SELECT schema_name, projection_name,
       COUNT(*)                          AS dv_entries,
       SUM(deleted_row_count)            AS deleted_rows,
       round(SUM(used_bytes)/1048576, 1) AS dv_mb
FROM   v_monitor.delete_vectors
GROUP  BY 1, 2
ORDER  BY 4 DESC
LIMIT  20;
```

Now the number that gets misread most often:

```sql
-- How many bytes sit in containers carrying delete markers
SELECT COUNT(*)                             AS containers_with_dv,
       round(SUM(used_bytes)/1024^3, 2)     AS gb
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
  AND  deleted_row_count > 0;
```

Result: **243 containers, 43.38 GB** — 63.6% of the database.

That figure **must not** be read as "43 GB is reclaimable". It means "these 43.38 GB of containers carry delete markers"; the vast majority of rows inside them are still live. The truly reclaimable amount equals the `used_bytes` delta before and after purge rewrites those containers, and that delta **can only be measured by running the purge** — which this audit did not do (it is a state-changing action), so it is recorded here as **not measured**.

When do deletes actually get cleaned up?

| Trigger | Behaviour | Controllable |
|---------|-----------|--------------|
| Mergeout | Applies DV and discards deleted rows while rewriting a container | Background, timing not yours |
| `PURGE TABLE` | Rewrites affected containers, forcing deleted rows out | You choose when |
| Dropping a whole partition | Discards that partition's containers | Fastest, cleanest |

Whether to purge proactively depends on the **deleted-row ratio**, not the DV count. `schema_a.t1_super` has 111 DV entries, but with 57 containers over 41.19 GB its `deleted_row_count / total_row_count` is only 0.63% — mergeout absorbs that; no human needed. The highest ratio in the database is a different projection: `schema_a.t5_super` has 129,733 deleted out of 1,827,715 rows — **7.10%** — and that is the purge candidate, even though it holds only 4 DV entries versus 111. **Ranking by DV count picks the wrong target.** Purge only when a projection's deleted ratio stays above roughly 10%, or when disk pressure demands immediate reclamation. It is I/O heavy: schedule off-peak.

---

## 6. Reclaim Actions Compared

The audit is done; now, do we act? Five options, and note the fourth column — only projecting rebuild is reversible:

| Action | Reclaims | Impact / locks | Reversible | When |
|--------|----------|----------------|------------|------|
| `DROP TABLE` | Data + all projections | Short DDL lock; dependent views break | ❌ backup only | After confirmed unused |
| `TRUNCATE TABLE` | All rows | Fast, discards containers, keeps schema | ❌ | Keep shell, clear data |
| `PURGE TABLE` | Deleted-row space + DVs | Rewrites containers; heavy I/O, scales with data | Rows were already deleted; no data risk | Off-peak |
| Rebuild projection | Fragmentation, sort order | `CREATE PROJECTION` + background refresh, needs extra disk | ✅ old projection can stay | Fragmented with no cheaper fix |
| Drop partition | That partition's containers | Fastest | ❌ | Tables designed with partitions |

Three practical rules:

1. **If you can drop a partition, don't purge.** Partition drop discards containers, O(1); purge rewrites, scaling with data volume.
2. **For fragmented tables, rebuild the projection rather than hammering mergeout.** Mergeout has a throughput ceiling; 267 tiny containers take a long time to absorb, while a rebuild reorders in one pass.
3. **Concurrency:** purge and rebuild both compete with live queries for I/O. On a single node there is nowhere to hide — go off-peak and watch `v_monitor.resource_acquisitions`.

---

## 7. Turn the Audit Into One Query

Rewriting queries at every disk alert is waste. This produces five metrics in one shot (read-only, safe to schedule):

```sql
WITH sz AS (
  SELECT t.table_schema, t.table_name,
         COALESCE(SUM(ps.used_bytes), 0) AS bytes
  FROM   v_catalog.tables t
  LEFT   JOIN v_monitor.projection_storage ps
         ON ps.anchor_table_schema = t.table_schema
        AND ps.anchor_table_name   = t.table_name
  WHERE  t.table_schema NOT IN ('v_catalog','v_monitor','v_internal','v_func','v_txtindex')
  GROUP  BY 1, 2
)
SELECT 'total_gb'            AS metric,
       round(SUM(bytes)/1024^3, 2)::varchar AS value
FROM   sz
UNION ALL
SELECT 'empty_tables', COUNT(*)::varchar FROM sz WHERE bytes = 0
UNION ALL
SELECT 'containers_total', COUNT(*)::varchar
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
UNION ALL
SELECT 'containers_lt_1mb', COUNT(*)::varchar
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
  AND  used_bytes < 1048576
UNION ALL
SELECT 'gb_in_containers_with_dv', round(SUM(used_bytes)/1024^3, 2)::varchar
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
  AND  deleted_row_count > 0;
```

Thresholds calibrated on this measurement (not universal standards):

| Metric | Measured | Alert at | Reading |
|--------|---------:|----------|---------|
| `total_gb` | 68.20 | Disk usage > 80% | Capacity issue, see section 6 |
| `empty_tables` | 164 (40%) | > 30% | Cleanup candidates, section 4 |
| `containers_lt_1mb` | 1,254 | > 2,000, or > 500 per projection | Fragmentation / pushback risk |
| Worst MB / container | 0.49 | < 5 on a table > 1 GB | Real fragmentation, consider rebuild |
| `gb_in_containers_with_dv` | 43.38 | Abnormal growth rate | Delete-path design problem |

The last row is a slope, not an absolute: 63.6% of bytes sitting in containers with DVs looks alarming but is often perfectly normal (a large table with steady small deletes). **How fast it grows in a week matters more than how large it is today.**

---

## 8. Environment and Reproducibility

| Item | Value |
|------|-------|
| Vertica version | 25.4.0-0 |
| Nodes | 1 (single node) |
| Database | Test instance (table names masked) |
| User schemas / tables / projections | 27 / 409 / 394 |
| `used_bytes` / `disk_size` | 68.20 GB / 68.32 GB |
| ROS containers | 1,524 (45.8 MB average) |
| Measured on | 2026-11-04 |

Three notes:

1. **All figures are single-node measurements.** On a cluster, `storage_containers` expands per `node_name`; totals and fragmentation distribution differ, but the reading method does not.
2. **Table names are rewritten** (`schema_a.t1` style) while numbers are preserved. Run the SQL against your own instance to get your own figures.
3. **No changes were executed**: no purge, no drops, no projection rebuilds. "How much can be reclaimed" is therefore answered as **not measured**, with the measurement method in section 5.

---

*All figures come from a single-node Vertica 25.4.0-0 instance measured on 2026-11-04; every query is read-only and safe on production. For mergeout and STRATA internals see [Tuple Mover best practices]({{ site.baseurl }}/2026/06/vertica-tuple-mover-best-practice/); for delete syntax choices see [deleting data best practices]({{ site.baseurl }}/2026/06/vertica-delete-data-best-practice/).*

**References** (Vertica 25.4.x documentation): the `PURGE` statement, the `PURGE_TABLE()` function, the `TRUNCATE` statement, Working with Native Tables → Removing Table Data, and the system tables `v_monitor.storage_containers`, `v_monitor.projection_storage`, `v_monitor.delete_vectors`.
