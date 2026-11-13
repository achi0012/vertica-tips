---
layout: post_en
title: "Shards and Segmentation in EON: Why Data Distribution Ignores Projection Segmentation"
date: 2026-11-14 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, shard, segmentation, hash-ring, node-subscription, data-distribution]
description: "Measured on a three-node eondb (v25.4.0-0, K-Safe=0, 4 shards): a row lands on the shard whose hash range contains hash(segmentation columns) mod 2^32 — not hash mod node count. On a 13,245,243-row table, bucketing by hash(id) % 4294967296 produced 4,413,479 / 4,419,435 / 4,412,329 rows, matching the three ROS containers exactly. The projection's segment_range in EON only says 33.3% per node; the real boundaries live in v_catalog.shards."
image: /assets/images/og/vertica-eon-shards-segmentation-en.png
faq:
  - q: "Does EON distribute data by projection segmentation or by shard?"
    a: "They divide the work: segmentation decides which columns feed the hash, and the shard decides which node owns that hash value. Bucketing `ADA.GCIS_Companyinfo_CmpB` (13,245,243 rows, segmented by `hash(id)`) with `hash(id) % 4294967296` gave 4,413,479 / 4,419,435 / 4,412,329 rows, matching the three ROS containers' `total_row_count` exactly. Those boundaries only exist in `v_catalog.shards`; `v_catalog.projections.segment_range` just reports \"implicit range: 33.3% per node\"."
  - q: "Why do the shard boundaries use 32 bits when hash() returns a 64-bit value?"
    a: "`v_catalog.shards.lower_hash_bound` and `upper_hash_bound` are 32-bit integers topping out at 4,294,967,295, while `hash()` returns a 64-bit BIGINT. Comparing `hash(id)` directly against the bounds put all 13,245,243 rows in `segment0003`, because 64-bit values almost always exceed 4.29e9. Applying `hash(id) % 4294967296` first is what reproduces the three container row counts."
  - q: "How do I read K-Safe straight out of the system tables?"
    a: "Count `v_catalog.node_subscriptions`: six rows here, three for `replica` (one primary, two secondary) and one each for `segment0001` through `segment0003`. No segment has a buddy copy, so K-Safe = 1 − 1 = 0, and the driver reports `WARNING 6046` on connect."
  - q: "Where do tables without segmentation (is_segmented = false) live in EON?"
    a: "On the replica shard, one copy per node. The 26 unsegmented projections here occupy 78 rows in `v_catalog.projections` (three each, with `node_name` populated) and 15 containers in `v_monitor.storage_containers`: 300,129 rows and 8,360,166 bytes, which is 5 containers, 100,043 rows and 2,786,722 bytes per node, identical across all three."
---

<!--more-->

---

## 1. The Short Version

Ask how EON distributes data and the usual answer is "by the projection's segmentation". That is not wrong, but it skips a layer — and skipping it makes capacity math, skew checks, and shard-pruning expectations all wrong. I worked the whole thing through with read-only queries on a local three-node eondb (v25.4.0-0, K-Safe=0, four shards). Seven findings:

1. **Placement is "which shard holds this hash value", not "hash mod node count".** The three segment shards cover `0`–`4,294,967,295` contiguously, totalling `4,294,967,296` = `2^32` with zero gap, and each is owned by exactly one node's primary subscription.
2. **Placement can be reproduced exactly.** `ADA.GCIS_Companyinfo_CmpB` (13,245,243 rows, segmented by `hash(id)`) bucketed with `hash(id) % 4294967296` yields `4,413,479` / `4,419,435` / `4,412,329` rows — identical to the three ROS containers' `total_row_count`. A second table with 1,093,169 rows reproduces the same way: `365,062` / `365,354` / `362,753`.
3. **Segmentation did not disappear; it only supplies the hash input.** 408 of the 486 rows in `v_catalog.projections` have `is_segmented = true` with a full `hash(...)` expression, yet `segment_range` in EON only says `implicit range: each node[33.3%]`. **The boundaries exist only in `v_catalog.shards`.**
4. **Unsegmented tables go to the replica shard, one copy per node.** The 26 `is_segmented = false` projections occupy 78 catalog rows (three each, `node_name` populated) and 15 containers — 5 per node, 100,043 rows and 2,786,722 bytes each.
5. **K-Safe is countable.** `v_catalog.node_subscriptions` has six rows: three for `replica` (1 primary + 2 secondary) and one per segment. No buddies, so the database is 0-safe and greets every connection with `WARNING 6046`.
6. **The three segments carry nearly identical load.** 366,727,810 / 366,526,751 / 366,396,866 rows — 33.34% / 33.32% / 33.31%, a spread of 0.03 percentage points.
7. **Shard pruning is real.** An equality predicate on the segmentation column produces `Execute on: v_eondb_node0003` and runs in 5.5–7.3 ms. The same table filtered on a non-segmentation column becomes `All Nodes` and takes 58.0–58.5 ms.

This is for three audiences: people who assume shard count equals node count and mis-size their depot; people staring at the 33.3% in `segment_range` and failing to find hash boundaries; and people who need to know whether a query will hit one node or all three. **Every number labelled "measured" comes from the 2026-11-14 run; mechanism explanations come from the documentation linked at the end. Everything was read-only — nothing on the cluster was changed.**

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Database | `eondb` (EON mode) |
| Nodes | 3 (`v_eondb_node0001/0002/0003`, `node_type = PERMANENT`, all `UP`) |
| Shards | 4 (1 replica + 3 segment), `default_shard_count = 3` |
| Node subscriptions | 6 (`replica` 3, one per `segment*`) |
| K-Safe | 0 (`WARNING 6046` on connect) |
| Communal storage | `s3://verticadb/eondb` |
| Containers and rows | 1,381 ROS containers, 221 projections, 1,099,951,556 rows, `disk_size` 12,047,766,999 bytes |
| Baseline | Local `testdb` (Enterprise mode, single node, same v25.4.0-0) |
| Access | `eondb` via `vertica_python` (endpoints 1533/2533/3533 all point at the same cluster), `testdb` via `vsql-testdb`; only `SELECT` and `EXPLAIN` |

The baseline is single-node, so **I could not reproduce Enterprise's multi-node hash-range split locally**. What I could measure is that Enterprise has no shard layer at all, and that a single node owns the entire 32-bit ring — numbers in section 6.

---

## 3. The Core Model: Three Layers, and You Need All Three

| Layer | Answers | Source | Numbers here |
|-------|---------|--------|--------------|
| segmentation | which columns feed a row's **hash input** | `v_catalog.projections.segment_expression` | 408 segmented projections, e.g. `hash(GCIS_Companyinfo_CmpB.id)` |
| shard | how many pieces the hash space is cut into, **and where the cuts are** | `v_catalog.shards` | 4 rows (3 segment + 1 replica), bounds at 0 / 1,431,655,739 / 2,866,311,478 |
| node subscription | which **node** owns which shard | `v_catalog.node_subscriptions` | 6 rows |
| ROS container | which files on which node belong to which shard | `v_monitor.storage_containers` | 1,381 rows, `shard_name` populated |

In one sentence: **segmentation decides what a row's hash is; the shard decides who owns that hash.** Once those are separate questions, several things fall into place:

- Adding a node does not change segmentation, but it does redistribute shards (the docs call this rebalancing shards).
- `K-Safe` is a property of a shard (how many subscriptions it has), not of a projection.
- To find out where data sits, `v_catalog.projections` will not help; `v_catalog.shards` will.

![EON data placement: three segment ranges covering 2^32, each owned by one node, plus a replica shard held by all three nodes]({{ '/assets/images/eon-shard-hash-ring.svg' | relative_url }}){: loading="lazy" }

Every number in that diagram comes from this run: the three hash-range sizes, the per-node container and row counts, and the three copies of the replica shard.

---

## 4. Step by Step: Reconstructing Placement

### 4.1 Shard boundaries and gaps

```sql
=> SELECT shard_name, shard_type, lower_hash_bound, upper_hash_bound,
          upper_hash_bound - lower_hash_bound + 1 AS hash_values
   FROM v_catalog.shards ORDER BY shard_name;

  shard_name  | shard_type | lower_hash_bound | upper_hash_bound | hash_values
--------------+------------+------------------+------------------+-------------
 replica      | Replica    |                  |                  |
 segment0001  | Segment    | 0                | 1431655738       |  1431655739
 segment0002  | Segment    | 1431655739       | 2863311477       |  1431655739
 segment0003  | Segment    | 2863311478       | 4294967295       |  1431655818

=> SELECT shard_name, lo, hi, (hi - lo + 1) AS span,
          sum(hi - lo + 1) OVER () AS total_span,
          lo - lag(hi) OVER (ORDER BY lo) - 1 AS gap_before
   FROM (SELECT shard_name, lower_hash_bound lo, upper_hash_bound hi
         FROM v_catalog.shards WHERE shard_type = 'Segment') s
   ORDER BY lo;

  shard_name  |     lo     |     hi     |   span    | total_span | gap_before
--------------+------------+------------+-----------+------------+------------
 segment0001  |          0 | 1431655738 | 1431655739 | 4294967296 | 
 segment0002  | 1431655739 | 2863311477 | 1431655739 | 4294967296 |          0
 segment0003  | 2863311478 | 4294967295 | 1431655818 | 4294967296 |          0
```

Both `gap_before` values are `0`, and `total_span` is `4,294,967,296` — exactly `2^32`. **Bounds are inclusive**: `segment0001` is `0 ≤ h ≤ 1,431,655,738` and the next range starts at `1,431,655,739`, so lengths need `hi - lo + 1`, not `hi - lo`.

These boundaries match the ones in the previous article (the EON architecture overview) exactly, but **the container counts and row counts moved since then**: yesterday 1,394 containers at 450/484/445; today 1,381 at 456/461/449. Shard boundaries are configuration; containers are files that mergeout rewrites. They do not change on the same timescale.

### 4.2 Which node owns which shard

```sql
=> SELECT node_name, shard_name, is_primary, subscription_state
   FROM v_catalog.node_subscriptions ORDER BY node_name, shard_name;

     node_name     |  shard_name  | is_primary | subscription_state
-------------------+--------------+------------+--------------------
 v_eondb_node0001  | replica      | True       | ACTIVE
 v_eondb_node0001  | segment0001  | True       | ACTIVE
 v_eondb_node0002  | replica      | False      | ACTIVE
 v_eondb_node0002  | segment0002  | True       | ACTIVE
 v_eondb_node0003  | replica      | False      | ACTIVE
 v_eondb_node0003  | segment0003  | True       | ACTIVE
```

The shape of those six rows is the answer. Each node is primary for its own segment; for `replica`, node0001 is primary and the other two are secondaries. **K-Safe is "subscriptions per segment, minus one"**, and every segment here has exactly one:

```sql
=> SELECT shard_name, count(*) subs,
          sum(CASE WHEN is_primary THEN 1 ELSE 0 END) primaries
   FROM v_catalog.node_subscriptions GROUP BY 1 ORDER BY 1;

  shard_name  | subs | primaries
--------------+------+-----------
 replica      |    3 |         1
 segment0001  |    1 |         1
 segment0002  |    1 |         1
 segment0003  |    1 |         1
```

### 4.3 Reconstructing the hash from real data

Pick a table with a single-column segmentation and enough rows to matter: `ADA.GCIS_Companyinfo_CmpB`, segmented by `hash(id)`, 13,245,243 rows. Its three containers hold 4,413,479 / 4,419,435 / 4,412,329 rows. Now bucket the table in SQL and see whether the same three numbers come out:

```sql
=> SELECT CASE WHEN (hash(id) % 4294967296) <= 1431655738 THEN 'segment0001'
               WHEN (hash(id) % 4294967296) <= 2863311477 THEN 'segment0002'
               ELSE 'segment0003' END AS shard_from_hash,
          count(*) AS rows_
   FROM ADA.GCIS_Companyinfo_CmpB GROUP BY 1 ORDER BY 1;

 shard_from_hash |  rows_
-----------------+---------
 segment0001     | 4413479
 segment0002     | 4419435
 segment0003     | 4412329
```

Against `v_monitor.storage_containers`:

```sql
=> SELECT node_name, shard_name, total_row_count, deleted_row_count,
          segment_lower_bound, segment_upper_bound, disk_size
   FROM v_monitor.storage_containers
   WHERE projection_name = 'GCIS_Companyinfo_CmpB_super' ORDER BY shard_name;

     node_name     |  shard_name  | total_row_count | deleted_row_count | segment_lower_bound | segment_upper_bound | disk_size
-------------------+--------------+-----------------+-------------------+---------------------+---------------------+-----------
 v_eondb_node0001  | segment0001  |         4413479 |                 0 |                   0 |          1431655738 | 114183018
 v_eondb_node0002  | segment0002  |         4419435 |                 0 |          1431655739 |          2863311477 | 114384873
 v_eondb_node0003  | segment0003  |         4412329 |                 0 |          2863311478 |          4294967295 | 113996312
```

**The three numbers match exactly**, and the containers' `segment_lower_bound` / `segment_upper_bound` are identical to the shard bounds. A second table confirms it (`ADA.GCIS_Companyinfo_Person`, 1,093,169 rows, `hash(id)`): bucketing gives 365,062 / 365,354 / 362,753, and the containers hold 365,062 / 365,354 / 362,753.

That is the accurate version of "don't look at projection segmentation alone": **segmentation supplies the hash input, and placement is the hash value matched against a shard range.** Change the shard boundaries — a rebalance, a different shard count — and the data moves, while the projection DDL stays untouched.

### 4.4 Container ranges always sit inside shard ranges

If shards really are the upper-level split, no container's hash range should escape the shard it belongs to. Check the whole cluster:

```sql
=> SELECT count(*) AS violations
   FROM v_monitor.storage_containers sc
   JOIN v_catalog.shards s ON s.shard_name = sc.shard_name
   WHERE sc.segment_lower_bound IS NOT NULL
     AND (sc.segment_lower_bound < s.lower_hash_bound
       OR sc.segment_upper_bound > s.upper_hash_bound);

 violations
------------
          0
(1 row)
```

Zero. Of 1,381 containers, 1,366 carry hash ranges (the other 15 are replica-shard containers for unsegmented projections, with NULL bounds), and none crosses its shard boundary.

### 4.5 Cluster-wide distribution

```sql
=> SELECT shard_name, count(*) containers, count(DISTINCT projection_id) projs,
          sum(total_row_count) rows_, sum(disk_size) bytes_,
          round(100.0 * sum(total_row_count) / sum(sum(total_row_count)) OVER (), 3) AS pct_rows
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

  shard_name  | containers | projs |   rows_    |   bytes_   | pct_rows
--------------+------------+-------+------------+------------+----------
 replica      |         15 |     5 |     300129 |    8360166 |    0.027
 segment0001  |        456 |   183 |  366727810 | 4014640911 |   33.340
 segment0002  |        461 |   188 |  366526751 | 4012538061 |   33.322
 segment0003  |        449 |   180 |  366396866 | 4012227861 |   33.310
```

The container counts (456/461/449) differ because mergeout is at different stages, but the row counts barely do: 33.34% / 33.32% / 33.31%. **That is what a healthy shard layout looks like**: container counts wobble, row counts do not. If one segment holds 40% and another 20%, that is not "containers haven't merged yet" — that is genuine hash skew, and the first thing to inspect is the segmentation column.

The `projs` column counts projections with containers on that shard: 183, 188, 180, plus 5 on replica. Together they form 551 (projection, shard) pairs, 556 including replica, mapping to 221 distinct projections that actually hold containers. Comparing this to the catalog is a mistake waiting to happen; compare `count(DISTINCT projection_id)` instead.

### 4.6 Turning Shards Into Capacity

```sql
=> SELECT shard_name, sum(disk_size) bytes_,
          round(100.0 * sum(disk_size) / sum(sum(disk_size)) OVER (), 3) AS pct_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

  shard_name  |   bytes_   | pct_bytes
--------------+------------+-----------
 replica      |    8360166 |     0.069
 segment0001  | 4014640911 |    33.323
 segment0002  | 4012538061 |    33.305
 segment0003  | 4012227861 |    33.303
```

Each segment holds about 4.01 GB; the replica shard holds 8.36 MB, or 0.069%. `disk_size` is compressed bytes actually stored in containers, not raw data size, so these numbers map to disk footprint per node rather than to ingest volume.

Two practical consequences. First, **each node's depot has to hold its 4.01 GB plus the 8.36 MB replica copy**; anything that does not fit gets evicted and the query has to go back to communal storage. Second, **on this cluster, sizing by node count and sizing by shard count happen to agree** (three and three), so the distinction is invisible here. Once the shard count differs from the node count — say `default_shard_count = 6` on three nodes — capacity must be computed per shard, because what is being divided is data, not machines.

---

## 5. So How Many Projections Are Those 486 Rows?

```sql
=> SELECT is_segmented, count(*) rows_, count(DISTINCT projection_id) ids
   FROM v_catalog.projections GROUP BY 1;

 is_segmented | rows_ | ids
--------------+-------+-----
 False        |    78 |  26
 True         |   408 | 408

=> SELECT n, count(*) AS proj_ids
   FROM (SELECT projection_id, count(*) n FROM v_catalog.projections GROUP BY 1) x
   GROUP BY 1 ORDER BY 1;

 n | proj_ids
---+----------
 1 |      408
 3 |       26
```

Read those two results and the catalog stops misleading you:

- **A segmented projection has exactly one row**, with `node_name` empty — it belongs to no single node, because its data is spread across all three shards by hash.
- **An unsegmented projection has three rows**, one per node with `node_name` populated — because its data sits on the replica shard, one copy per node.

So 486 rows = 408 segmented projections + 26 unsegmented projections × 3. `SELECT count(*) FROM v_catalog.projections` does **not** return a projection count; it returns projection × placement rows.

Now `segment_range`:

```sql
=> SELECT DISTINCT segment_range FROM v_catalog.projections WHERE is_segmented LIMIT 1;

 implicit range: v_eondb_node0001[33.3%] v_eondb_node0002[33.3%] v_eondb_node0003[33.3%]
```

It tells you each node takes 33.3%, and **nothing about where the cuts are**. For the cuts you have to read `v_catalog.shards`. That is the point of this article's title: distribution is not something you read off a projection's segmentation — the projection tells you the hash input, the shard table tells you the destination.

### 5.1 Four Numbers That Get Mixed Up

```sql
=> SELECT (SELECT count(*) FROM v_catalog.projections) AS catalog_rows,
          (SELECT count(DISTINCT projection_id) FROM v_catalog.projections) AS catalog_projs,
          (SELECT count(*) FROM v_monitor.storage_containers) AS containers,
          (SELECT count(DISTINCT projection_id) FROM v_monitor.storage_containers) AS projs_with_data;

 catalog_rows | catalog_projs | containers | projs_with_data
--------------+---------------+------------+-----------------
          486 |           434 |       1381 |             221
```

Four numbers, four questions: 486 is projection × placement rows; 434 is the real projection count in the catalog; 1,381 is the number of ROS containers; 221 is the number of projections that currently hold files. Put all four in the same health-check output and "why does the projection count keep changing?" stops being a mystery — the number that moves is usually 221, because mergeout and new projections both touch it.

The unsegmented inventory is worth looking at daily too:

```sql
=> SELECT projection_name, count(*) containers, count(DISTINCT node_name) nodes,
          sum(total_row_count) rows_
   FROM v_monitor.storage_containers WHERE shard_name = 'replica'
   GROUP BY 1 ORDER BY 4 DESC;

         projection_name          | containers | nodes |  rows_
----------------------------------+------------+-------+--------
 customer_dimension_flex2_1_unseg |          3 |     3 | 150000
 xxx_unseg                        |          3 |     3 | 150000
 lowi_user_save_source_unseg      |          3 |     3 |    114
 scheduled_task_unseg             |          3 |     3 |      9
 lowi_user_save_channel_unseg     |          3 |     3 |      6
```

Five projections, 15 containers, 300,129 rows — 100,043 rows per node. `containers = 3` together with `nodes = 3` is the evidence that all three nodes hold a copy; a projection showing `nodes = 2` means one node's replica subscription is missing or still syncing, and that is worth investigating the same day.

---

## 6. The Enterprise Baseline: No Shard Layer at All

`testdb` on the same machine (Enterprise mode, single node, same version):

```sql
=> SELECT count(*) AS shard_rows FROM v_catalog.shards;

 shard_rows
------------
          0

=> SELECT count(*) containers,
          sum(CASE WHEN segment_lower_bound = 0 AND segment_upper_bound = 4294967295 THEN 1 ELSE 0 END) full_ring
   FROM v_monitor.storage_containers;

 containers | full_ring
------------+-----------
       1494 |      1483
```

Two clear signals:

1. **Enterprise has no shards.** `v_catalog.shards` returns zero rows, and `v_monitor.storage_containers.shard_name` is an empty string for every row.
2. **A single node owns the whole 32-bit ring.** 1,483 containers span `0`–`4294967295`; the remaining 11 are unsegmented projections with NULL bounds.

The catalog agrees: `testdb` has 433 rows and 433 distinct `projection_id` values (one row per projection, because there is one node), 411 of them segmented. **The same `count(*)` means "number of projections" in Enterprise and something else entirely in EON** — an easy trap when writing health-check SQL.

I have no multi-node Enterprise environment, so I will not quote measured numbers for how Enterprise splits segmentation ranges across nodes. Mechanically: Enterprise maps a projection's segmentation directly onto nodes, so adding a node requires re-segmenting; EON maps shards onto nodes, so adding a node means moving shards. The documentation links at the end cover the mechanics.

Three things break most often in health-check SQL when you move from Enterprise to EON: `SELECT count(*) FROM v_catalog.projections` (EON counts the per-node copies of unsegmented projections as well), `WHERE shard_name IS NULL` (Enterprise stores an empty string, not NULL, so the predicate matches nothing), and any join that assumes one projection maps to one node. The fixes are the same everywhere: count projections with `count(DISTINCT projection_id)`, read node ownership from `v_catalog.node_subscriptions`, and read placement from `v_monitor.storage_containers.shard_name`.

---

## 7. Query Side: What Shard Pruning Looks Like

An equality predicate on the segmentation column:

```sql
=> EXPLAIN SELECT count(*) FROM ADA.GCIS_Companyinfo_CmpB
   WHERE id = '3cc5cd9e-253e-11eb-9cb0-005056b1498c';

+-GROUPBY NOTHING [Cost: 19K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*)
|  Execute on: v_eondb_node0003
| +---> STORAGE ACCESS for GCIS_Companyinfo_CmpB [Cost: 19K, Rows: 13M ...]
| |      Projection: default_namespace.ADA.GCIS_Companyinfo_CmpB_super
| |      Filter: (GCIS_Companyinfo_CmpB.id = '3cc5cd9e-...')
| |      Execute on: v_eondb_node0003
```

`Execute on:` names one node. A row's hash lands on exactly one shard, so only one node has to work. Switch to a non-segmentation column:

```sql
=> EXPLAIN SELECT count(*) FROM ADA.GCIS_Companyinfo_CmpB
   WHERE business_seq_no = '0021';

+-GROUPBY NOTHING [Cost: 6K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*)
|  Execute on: All Nodes
| +---> STORAGE ACCESS for GCIS_Companyinfo_CmpB [Cost: 6K, Rows: 13M ...]
| |      Filter: (GCIS_Companyinfo_CmpB.business_seq_no = '0021')
| |      Execute on: All Nodes
```

Same table, `All Nodes`. Timings (same connection, warm depot, three runs each, minimum shown):

| Query | Nodes scanned | Rows returned | Best time |
|-------|---------------|---------------|-----------|
| `WHERE id = '3cc5cd9e-…'` (segmentation column) | 1 (node0003) | 1 | 5.5 ms |
| `WHERE id = 'ffffffff-…'` (nonexistent key) | 1 | 0 | 4.8 ms |
| `WHERE business_seq_no = '0021'` (not segmentation) | 3 (All Nodes) | 174,498 | 58.0 ms |
| `SELECT count(*)` (whole table) | 3 | 13,245,243 | 15.3 ms |

Two caveats. First, **this is not an apples-to-apples benchmark**: the row counts differ by five orders of magnitude, and the 58 ms is mostly the cost of scanning 13M rows, not the node count. Second, **`count(*)` is not a performance metric**: 13M rows in 15.3 ms works because the plan answers from ROS statistics — it says `ROW COUNT ONLY` — without touching the data.

### 7.1 `Participating subscriptions` Is Not the Pruned Set

The GraphViz block at the end of the plan lists this:

```text
All Nodes Vector:
  node[0]=v_eondb_node0001 (initiator) Up
  node[1]=v_eondb_node0002 (executor) Up
  node[2]=v_eondb_node0003 (remote initiator) Up

Participating subscriptions:
  replica: [<0, 1, 2>]
  segment0001: [<0>]
  segment0002: [<1>]
  segment0003: [<2>]
```

It is **byte-for-byte identical** in the single-key query and in the full-table `count(*)` — because it lists which subscriptions exist in the cluster, not which ones this query used. Pruning shows up in the Access Path's `Execute on:` line: `v_eondb_node0003` for the point lookup, `All Nodes` for the full scan. Confusing the two leads straight to "EON does no shard pruning at all", followed by pointless segmentation changes — when the real problem was misreading the plan.

---

## 8. Common Mistakes and Anti-Patterns

**1. Comparing `hash()` directly against shard bounds.** My first attempt was `CASE WHEN hash(id) <= 1431655738 …`, which put **all** 13,245,243 rows in `segment0003`, because `hash()` returns a 64-bit BIGINT (the minimum value in that table was 293,223,774,726) and is always greater than the 4,294,967,295 ceiling. The working form is `hash(id) % 4294967296`.

**2. Treating `count(*)` on `v_catalog.projections` as a projection count.** In EON, 486 rows means 434 projections (408 + 26×3). Use `count(DISTINCT projection_id)` for projections, `count(*)` for placement rows.

**3. Assuming shard count equals node count.** Three nodes, four shards, and `default_shard_count = 3` counts segments only. Size a depot with one number and validate with the other, or you will spend an afternoon wondering which table lies.

**4. Comparing the projection count in `v_monitor.storage_containers` with the catalog.** The view lists projections that have files (221 here); the catalog lists 434. The gap is projections with no data yet (`DELAYED CREATION`) plus in-flight mergeout containers. Both numbers are correct answers to different questions.

**5. Expecting hash boundaries inside `segment_range`.** EON's `segment_range` reports node percentages only. Boundaries live in `v_catalog.shards`.

**6. Using `node_name` to decide where data is.** It is empty for segmented projections, and populated on all three nodes for unsegmented ones. Read `v_monitor.storage_containers.shard_name` instead.

---

## 9. Risks and Recommendations

**Do now (read-only, low risk)**

- Add "shard boundaries plus per-segment rows" to the daily health check: the three `span` values must sum to `4,294,967,296` and every `gap_before` must be 0.
- Track row share per `shard_name` from `v_monitor.storage_containers`; anything more than two percentage points off 33% deserves a look at segmentation skew.
- Check `v_catalog.node_subscriptions`: subscriptions per segment equal K-Safe + 1. Fewer than expected means a copy is missing.
- For slow queries, read `Execute on:` in `EXPLAIN`. If it says `All Nodes` when you expected one, the predicate is not on the segmentation column.

**Needs a change window**

- Changing the shard count triggers a rebalance that moves data between nodes — not a read-only operation.
- Raising K-Safe adds subscriptions and needs spare nodes and depot space; this cluster has three nodes at K-Safe=0, so moving to 1 needs planning.
- Changing a projection's segmentation requires rebuilding it, which rewrites the data.

---

## 10. References

- Vertica 25.4 documentation: Shards and subscriptions — https://docs.vertica.com/25.4.x/en/architecture/eon-mode-architecture/shards-and-subscriptions/
- Vertica 25.4 documentation: `v_catalog.shards` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-tables/shards/
- Vertica 25.4 documentation: `v_catalog.node_subscriptions` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-tables/node-subscriptions/
- Vertica 25.4 documentation: Segmentation clause (`CREATE PROJECTION`) — https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-projection/segmentation-clause/
- Vertica 25.4 documentation: Projection segmentation — https://docs.vertica.com/25.4.x/en/architecture/projection-design/segmentation/
- Vertica 25.4 documentation: `HASH` function — https://docs.vertica.com/25.4.x/en/sql-reference/functions/hash/

> Note: `docs.vertica.com` currently 302-redirects to `docs.rocketsoftware.com/bundle/vertica_25.4/` with the same paths; all links above returned 200 when checked on 2026-11-14.
