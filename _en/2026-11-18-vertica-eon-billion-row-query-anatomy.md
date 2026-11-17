---
layout: post_en
title: "Anatomy of a Billion-Row Query on EON: How Shard Pruning and Depot Hits Shape EXPLAIN and Wall-Clock Time"
date: 2026-11-18 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, query-plan, explain, shard-pruning, depot, performance, profile]
description: "A read-only dissection of a 1,007,772,484-row table on a local three-node eondb (v25.4.0-0, K-Safe=0, four shards): count(*) in 0.42 s, 4.76 s once one aggregate column is added, 12.05 s for two. A filter on the sort-key prefix was slower than one that was not. A 1-billion by 2.93-million hash join took 90.9 s. EXPLAIN LOCAL shows a ScanStep on all three nodes — there is no plan-level shard pruning in EON, only nodes that scan nothing."
image: /assets/images/og/vertica-eon-billion-row-query-anatomy-en.png
faq:
  - q: "Does a 0.42-second count(*) on a billion rows really scan a billion rows?"
    a: "No. Three containers hold 336 million rows each; count(*) measured 420 ms (437 ms on the second run), which is 2.4 billion rows per second — far beyond any column scan. Changing the same table to sum(row_count) took 4,756 ms, eleven times slower. The difference is that count(*) only needs the row count stored with each container, not the column values."
  - q: "Why does Participating subscriptions list all three nodes in EXPLAIN?"
    a: "Because that block is the cluster's subscription map: the replica shard exists on all three nodes, and segment0001 through segment0003 live on node0001 through node0003. It is not the set of shards this query touches. To check that, use EXPLAIN LOCAL: even public.ujtest — 3,826 rows, all in one container on segment0003 — still gets a ScanStep on all three nodes."
  - q: "Why does adding sum() take the same table from 0.42 s to 4.76 s?"
    a: "Because count(*) can read the container row counts, while sum(row_count) has to read a billion integer values. Measured: sum(row_count) 4,756 ms, count(*)+sum(row_count) 7,844 ms, count+sum+min/max 12,047 ms. Each additional materialized column costs roughly five seconds — scan cost is driven by the number of columns, not the number of rows."
  - q: "Can you measure a depot miss or communal latency on this cluster?"
    a: "No. Each node's depot ceiling is 566,381,859,840 bytes and this table uses 2.95 GB per node, 0.52% of it, so everything is resident. v_monitor.depot_fetches held 30 stale records with a 3 ms maximum both before and after the whole test run — not one new row. The 4,162 ms to 1,437 ms difference between two runs of the same query is page cache, not the depot."
  - q: "Why does a hash join of a billion rows and 2.93 million rows take 90 seconds?"
    a: "EXPLAIN shows JOIN HASH Inner (BROADCAST), broadcasting the 2.93-million-row partitions_super to all three nodes, followed by GROUPBY HASH (GLOBAL RESEGMENT GROUPS) to redistribute 841,034,568,612 joined pairs. It measured 90,875 ms, most of it broadcast and resegment network and sort cost rather than reads."
---

<!--more-->

---

## 1. The Short Version

"A billion-row query is slow on EON because it has to fetch data from communal storage" is simply false on this local three-node eondb. This article takes one billion-row table apart with read-only queries and measures each piece. Everything below comes from the 2026-11-18 run:

1. **`count(*)` takes 0.42 s, and that is not a scan.** On 1,007,772,484 rows it measured 420 ms and 437 ms — 2.4 billion rows per second. Switching to `sum(row_count)` takes 4,756 ms, eleven times longer.
2. **Cost is driven by how many columns you read, not how many rows there are.** `sum(row_count)` 4,756 ms, `count(*)+sum(row_count)` 7,844 ms, `count+sum+min/max(disk_space_bytes)` 12,047 ms. Every extra materialized column adds roughly five seconds.
3. **There is no plan-level shard pruning in EXPLAIN.** `EXPLAIN LOCAL` shows a `ScanStep` on each of node0001, node0002, and node0003. Even `public.ujtest` — 3,826 rows, one container, entirely on segment0003 — gets a `ScanStep` on all three nodes.
4. **To find where data lives, query `v_monitor.storage_containers`, not EXPLAIN.** Three containers, one per segment shard: 336,125,677 / 335,881,586 / 335,765,221 rows, 8.86 GB on disk, 92.58 GB of payload (10.45x compression), 8.79 bytes per row.
5. **A filter on the sort-key prefix is not faster.** `column_name = 'epoch'` (2,725,727 matches) took 5,809 ms and 5,146 ms; the non-prefix `partition_key = '-1'` (4,421,986 matches) took 4,162 ms and 1,437 ms. With one container per segment shard there is no container-level range to skip.
6. **Cost does not predict time.** The 722K plan took 12.0 s, the 2M plan took 5.8 s, the 1B plan took 4.2 s.
7. **The depot absorbs everything; communal fetches are unmeasurable.** Each node's depot ceiling is 566,381,859,840 bytes and this table uses 2.95 GB per node (0.52%). `v_monitor.depot_fetches` held 30 records with a 3 ms maximum before and after the entire run.
8. **The join is where the cost actually is.** A billion by 2.93 million hash join plans as `Inner (BROADCAST)` plus `GLOBAL RESEGMENT GROUPS` and measured 90,875 ms.

This is for three kinds of reader: people who can read only the Cost number out of an EXPLAIN; people who assume a slow EON query must be communal I/O; and anyone who has to explain to a manager why a billion-row count beats a 900,000-row table. **Everything is read-only: SELECT and EXPLAIN only, no CREATE, no INSERT, no parameter changes.**

![One EXPLAIN shipped to three nodes; each ScanStep reads only the containers on its own shard, and storage_containers is what tells you which node holds the data]({{ '/assets/images/eon-query-anatomy-layers.svg' | relative_url }}){: loading="lazy" }

---

## 2. The Test Environment

| Item | Value (verified in this run) |
|------|------------------------------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Database | `eondb` (EON mode) |
| Nodes | 3 (`v_eondb_node0001` to `0003`, all `PERMANENT`, `is_primary = true`) |
| Shards | 4 (`replica` plus `segment0001` to `segment0003`), K-Safe = 0 |
| Hash bounds | `0–1,431,655,738` / `1,431,655,739–2,863,311,477` / `2,863,311,478–4,294,967,295` |
| Table under test | `mtk.partition_columns` (1,007,772,484 rows) |
| Projection | `mtk.partition_columns_super` (segmented, eight-column sort key) |
| Containers | 3 (one per segment shard) |
| Depot ceiling | 566,381,859,840 bytes per node (`v_catalog.storage_locations.max_size`) |
| Connection | `connect_eondb.py` (`vertica_python`), endpoints 1533 / 2533 / 3533 |
| Run window | 2026-11-18 05:05–06:05 (Asia/Taipei) |

The `mtk` schema holds partition metadata from another system: every row describes one ROS container of one partition of one projection on one node. `partition_columns` has 13 columns, and `partition_columns_super` is segmented by `hash(column_id, projection_id, row_count, deleted_row_count, disk_space_bytes, statistics_updated_timestamp, column_name, table_name)`, sorted by `column_name, column_id, table_name, projection_name, projection_id, node_name, partition_key, grouped_partition_key`. Keep that sort key in mind; section 4 comes back to it.

The three containers, as measured:

```sql
=> SELECT node_name, shard_name, total_row_count, used_bytes, payload_size,
          segment_lower_bound, segment_upper_bound
   FROM v_monitor.storage_containers
   WHERE projection_name = 'partition_columns_super' ORDER BY shard_name;

node_name        | shard_name  | total_row_count | used_bytes | payload_size | lower      | upper
-----------------+-------------+-----------------+------------+--------------+------------+------------
v_eondb_node0001 | segment0001 |     336,125,677 | 2954595943 |  30879805419 |          0 | 1431655738
v_eondb_node0002 | segment0002 |     335,881,586 | 2952943511 |  30857616937 | 1431655739 | 2863311477
v_eondb_node0003 | segment0003 |     335,765,221 | 2952191804 |  30846258727 | 2863311478 | 4294967295
```

Add them up: 1,007,772,484 rows, 8,859,730,258 bytes on disk (8.86 GB), 92,583,681,083 bytes of payload (92.58 GB), 10.45x compression, 8.79 bytes per row. **That is the physical basis for every timing below: any query can parallelize across at most three machines, and each reads 2.95 GB.**

---

## 3. The Mechanics: One Logical Plan, Three Local Plans

### 3.1 The EXPLAIN you normally read is only the logical plan

```sql
=> EXPLAIN SELECT count(*) AS rows_, sum(row_count) AS ros_rows,
            min(disk_space_bytes) AS min_b, max(disk_space_bytes) AS max_b
   FROM mtk.partition_columns;

+-GROUPBY NOTHING [Cost: 722K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*), sum(partition_columns.row_count),
|              min(partition_columns.disk_space_bytes), max(partition_columns.disk_space_bytes)
|  Execute on: All Nodes
| +---> STORAGE ACCESS for partition_columns [Cost: 591K, Rows: 1B (ROW COUNT ONLY)] (PATH ID: 2)
| |      Projection: default_namespace.mtk.partition_columns_super
| |      Materialize: partition_columns.row_count, partition_columns.disk_space_bytes
| |      Execute on: All Nodes
```

Nothing here tells you who does how much — `Execute on: All Nodes` means exactly that. The two useful pieces sit in the GraphViz header:

```
All Nodes Vector:
  node[0]=v_eondb_node0001 (initiator) Up
  node[1]=v_eondb_node0002 (executor) Up
  node[2]=v_eondb_node0003 (executor) Up

Participating subscriptions:
  replica: [<0, 1, 2>]
  segment0001: [<0>]
  segment0002: [<1>]
  segment0003: [<2>]
```

### 3.2 "Participating subscriptions" is not this query's shard list

Many people — including this author, once — read that block as "the shards this query will use." It is not. It is **the cluster's subscription map**: the replica shard sits on all three nodes, and each segment shard sits on one. Every query prints the same block, whether it scans a billion rows or 3,826.

To prove it, find a table with exactly one container:

```sql
=> SELECT p.anchor_table_name, p.projection_name, sc.node_name, sc.shard_name, sc.total_row_count
   FROM v_catalog.projections p
   JOIN (SELECT projection_id FROM v_monitor.storage_containers
         GROUP BY 1 HAVING count(*) = 1) x ON x.projection_id = p.projection_id
   JOIN v_monitor.storage_containers sc ON sc.projection_id = p.projection_id
   ORDER BY sc.total_row_count DESC LIMIT 3;

anchor_table_name | projection_name  | node_name        | shard_name  | total_row_count
------------------+------------------+------------------+-------------+-----------------
ujtest            | ujtest_super     | v_eondb_node0003 | segment0003 |            3826
watch_seen        | watch_seen_super | v_eondb_node0001 | segment0001 |               4
cx_t              | cx_t_super       | v_eondb_node0002 | segment0002 |               3
```

`public.ujtest` holds 3,826 rows, all in node0003's segment0003. If shard pruning worked at the plan level, only one node would be involved. Here is `EXPLAIN LOCAL`:

```sql
=> EXPLAIN LOCAL SELECT count(*) FROM public.ujtest;
...
-----------------------------------------------
PLAN: v_eondb_node0001 (GraphViz Format)
...
9[label = "ScanStep: ujtest_super\nsales_quantity (not emitted)", shape = "box"];
...
-----------------------------------------------
PLAN: v_eondb_node0002 (GraphViz Format)
...
6[label = "ScanStep: ujtest_super\nsales_quantity (not emitted)", shape = "box"];
...
-----------------------------------------------
PLAN: v_eondb_node0003 (GraphViz Format)
...
6[label = "ScanStep: ujtest_super\nsales_quantity (not emitted)", shape = "box"];
```

**All three nodes carry a `ScanStep`.** node0001 and node0002 hold no container for `ujtest_super` at all; their scans return nothing. So on this 25.4.0-0 cluster:

- There is no plan-level shard pruning. One logical plan is shipped to every node (`Send`/`Recv`, net id 1000).
- The actual pruning happens in the storage layer: a node without a container has no I/O to do and returns an empty partial result.
- The only reliable source for data placement is `v_monitor.storage_containers`.

Two practical consequences. First, **the parallelism in EXPLAIN is not trustworthy**: a plan may say `Parallelism: 3`, but if the table sits on one shard, one machine does the work. Second, **small tables do not get faster on EON**: `count(*)` on 3,826 rows measured 13 ms and 9 ms, exactly what a single-node database would do, because it was always one node's job.

### 3.3 Three EXPLAIN forms, three different questions

| Command | What it adds | When to use it |
|---------|--------------|----------------|
| `EXPLAIN` | Logical plan, Cost, materialized columns | Day to day: which projection was chosen, is there a resegment |
| `EXPLAIN VERBOSE` | Per-operator `nodeSet`, resource estimates (Scratch Memory MB, Worker Threads), sort keys | Deciding "how many nodes run this operator" |
| `EXPLAIN LOCAL` | One local plan per node (initiator plus two executors) | Confirming whether a particular node has a `ScanStep` |

`EXPLAIN LOCAL` is the more honest tool on EON: it prints distinct `PLAN: v_eondb_node0001`, `PLAN: v_eondb_node0002`, and `PLAN: v_eondb_node0003` sections.

---

## 4. Step by Step: Seven Ways to Ask a Billion-Row Question

Every query below ran twice in the same session, back to back. Times are milliseconds; `run1` is the first execution, `run2` the immediate second. That "twice" matters, and section 6 explains why.

### 4.1 `count(*)`: 0.42 seconds

```sql
=> SELECT count(*) FROM mtk.partition_columns;
```

| Run | Time |
|-----|------|
| run1 | 420 ms |
| run2 | 437 ms |

1,007,772,484 divided by 0.42 s is about **2.4 billion rows per second**. No column scan does that — the row count is already stored with each ROS container, so `count(*)` never reads values. The plan says so twice: `Materialize:` is empty, and the whole plan costs **2** (`STORAGE ACCESS` is 1, annotated `1 RLE`). The optimizer itself is telling you it only reads metadata row counts.

### 4.2 Adding one aggregate column: 4.76 seconds

```sql
=> SELECT sum(row_count) FROM mtk.partition_columns;
```

| Run | Time |
|-----|------|
| run1 | 4,756 ms |
| run2 | 4,769 ms |

Same session, same table. All it takes is touching one integer column to go from 0.42 s to 4.76 s — **eleven times slower**. That is what a scan costs: a billion rows in 4.76 s is about 212 million rows per second across three nodes, roughly 70 million per node.

### 4.3 Adding a second column: 12.05 seconds

```sql
=> SELECT count(*), sum(row_count), min(disk_space_bytes), max(disk_space_bytes)
   FROM mtk.partition_columns;
```

| Run | Time |
|-----|------|
| run1 | 12,047 ms |
| run2 | 12,174 ms |

Materializing both columns costs about 2.5 times the single-column query. The full ladder:

| Query | run1 | run2 |
|-------|------|------|
| `count(*)` | 420 ms | 437 ms |
| `sum(row_count)` | 4,756 ms | 4,769 ms |
| `count(*) + sum(row_count)` | 7,844 ms | 9,060 ms |
| `count + sum + min/max(disk_space_bytes)` | 12,047 ms | 12,174 ms |

**Every extra column costs roughly five seconds.** Conversely, if a report only needs a row count, do not casually add a `sum()` — that `sum()` is 4.3 seconds.

### 4.4 Filtering on the sort-key prefix: 5.8 seconds, no benefit

The first sort key column is `column_name`, so `column_name = 'epoch'` should be the ideal prefix filter:

```sql
=> SELECT count(*), sum(row_count) FROM mtk.partition_columns WHERE column_name = 'epoch';
-- 2,725,727 rows
```

| Run | Time |
|-----|------|
| run1 | 5,809 ms |
| run2 | 5,146 ms |

A 0.27% hit rate (2.72 million out of a billion) still took five seconds. The reason is that **each segment shard holds exactly one container**: sorting only orders rows inside a container, and with no second container there is no range to skip wholesale. The `column_name` column is read end to end and then filtered.

### 4.5 Filtering on a non-prefix column: 4.16 s, then 1.44 s

```sql
=> SELECT count(*), sum(row_count) FROM mtk.partition_columns WHERE partition_key = '-1';
-- 4,421,986 rows
```

| Run | Time |
|-----|------|
| run1 | 4,162 ms |
| run2 | 1,437 ms |

4.4 million matches (0.44%), 4.16 s the first time and 1.44 s the second — **2.9 times faster for the identical query**. That is not the optimizer improving; it is that column's data now sitting in page cache. Section 6 ties this back to the depot.

### 4.6 `LIMIT 5`: 19 ms

```sql
=> SELECT column_name, row_count, disk_space_bytes FROM mtk.partition_columns LIMIT 5;
```

| Run | Time |
|-----|------|
| run1 | 19 ms |
| run2 | 15 ms |

A billion-row table answers `LIMIT 5` in 19 ms because it stops after the fifth row. **That is the special case with no ORDER BY and no GROUP BY.** Add either and you are back to a 5–12 second full scan. Treating `LIMIT` as a general accelerator fails on exactly the aggregate reports people care about.

### 4.7 The join: 90.9 seconds

```sql
=> SELECT p.table_schema, count(*) AS n
   FROM mtk.partition_columns pc
   JOIN mtk.partitions p ON pc.projection_id = p.projection_id
   GROUP BY 1 ORDER BY n DESC LIMIT 5;
```

The plan:

```
+-SELECT  LIMIT 5 [Cost: 36M, Rows: 5 (ROW COUNT ONLY)]
| +---> SORT [TOPK] [Cost: 36M]
| | +---> GROUPBY HASH (GLOBAL RESEGMENT GROUPS) (LOCAL RESEGMENT GROUPS) [Cost: 36M]
| | | +---> JOIN HASH [Cost: 879K, Rows: 1B (ROW COUNT ONLY)] Inner (BROADCAST)
| | | |      Join Cond: (pc.projection_id = p.projection_id)
| | | | +-- Outer -> STORAGE ACCESS for pc [Cost: 295K, Rows: 1B]
| | | | +-- Inner -> STORAGE ACCESS for p [Cost: 7K, Rows: 3M (PUSHED GROUPING)]
```

| Run | Time | Result |
|-----|------|--------|
| run1 | 90,875 ms | `eda` / 841,034,568,612 |

Two details worth remembering:

1. **`Inner (BROADCAST)`**: `mtk.partitions` holds only 2,929,345 rows (three containers, about 976,000 each), so the optimizer broadcasts it to all three nodes rather than resegmenting the billion-row side.
2. **`GLOBAL RESEGMENT GROUPS`**: the 841 billion joined pairs then have to be redistributed by `table_schema` before the `GROUP BY`, and that step is cross-node network traffic.

Of the 90.9 seconds, reads account for the first few. The rest is broadcast, join, resegment, and sort. **On EON, join network cost is far harder to predict than scan cost.**

---

## 5. Reading EXPLAIN Correctly: Cost Is Not Time

Put this run's plans side by side with their measured times:

| Query | EXPLAIN Cost | Measured run1 | Rows per second |
|-------|--------------|---------------|-----------------|
| `count(*)` | 2 | 420 ms | 2.4 B |
| `sum(row_count)` | 361K | 4,756 ms | 212 M |
| `count+sum+min/max` | 722K | 12,047 ms | 84 M |
| `filter column_name='epoch'` | 2M | 5,809 ms | 173 M (scan) |
| `filter partition_key='-1'` | 1B | 4,162 ms | 242 M (scan) |
| `join` | 36M (top) / 879K (join) | 90,875 ms | 11 M (per billion rows) |

![Seven ways to ask the same table: from count(*) at 0.42 s to the join at 90.88 s, a 216x spread on a log scale]({{ '/assets/images/eon-query-time-bars.svg' | relative_url }}){: loading="lazy" }

Two things are clear:

- **Cost ranking does not match time ranking.** The 722K plan took 12 seconds; the 1B plan took 4.2. Cost is a unitless relative number the optimizer uses to compare candidate plans, not a linear predictor.
- **Cost is estimated from row counts, not column counts.** `count+sum+min/max` and `count(*)` both show 722K, yet differ by 28x in wall-clock time.

Three questions actually predict runtime: **how many columns are read** (4.3), **how many nodes hold the data** (3.2), and **does anything have to cross the network** (4.7).

---

## 6. The Depot: Fully Resident, So Communal Fetches Are Unmeasurable

Query time on EON also depends on whether data is in the depot (local SSD) or must be fetched from communal storage. Here is the local reality:

| Item | Value |
|------|-------|
| Depot ceiling per node | 566,381,859,840 bytes (about 527.5 GiB) |
| This table per node | 2,954,595,943 bytes (2.95 GB) |
| Share | **0.52%** |
| `v_monitor.depot_fetches` rows (before) | 30 |
| `v_monitor.depot_fetches` rows (after) | 30 |
| `fetch_time_ms` maximum | 3 ms |

**The test run performed more than 30 scans of a billion rows and `depot_fetches` did not gain a single row.** No read needed a file from communal storage — the data was already in the depot. Therefore:

- This cluster **cannot** demonstrate a depot miss or communal latency. Any claim that "cold reads on EON are slower" has no supporting data here.
- The 4,162 ms to 1,437 ms swing in section 4.5 comes from the operating system page cache (or the depot's in-memory cache), not from depot eviction.
- A cluster whose depot is smaller than its hot set behaves very differently — but that requires a dataset the depot cannot hold, which this machine does not have.

That is also why the previous article (`vertica-eon-ros-storage-containers`) discussed container counts and scan cost without discussing temperature: on this machine, a cold/warm comparison cannot be constructed.

---

## 7. Common Mistakes and Anti-Patterns

**Mistake 1: reading `Participating subscriptions` as "the shards this query uses."**
It is the cluster subscription map, and all three nodes are always present. For placement, query `v_monitor.storage_containers`; for which node has a `ScanStep`, run `EXPLAIN LOCAL`.

**Mistake 2: prioritizing by Cost.**
Measured here: 722K to 12.0 s, 2M to 5.8 s, 1B to 4.2 s. Cost compares plans for the same query; it cannot compare queries, and it certainly is not an SLA.

**Mistake 3: assuming a sort-key prefix always helps.**
`column_name = 'epoch'` is the leading sort column, hit rate 0.27%, and it still took 5.8 s — while the non-prefix `partition_key = '-1'` took 1.4 s on its second run. With a single container per shard there is no container-level range to skip; sort order saves comparisons inside a container, not I/O.

**Mistake 4: "count(*) is cheap, so a few more aggregates will not hurt."**
`count(*)` 0.42 s, one `sum()` 4.76 s, two columns 12.05 s. That is not 10% — it is 28x. Casually added aggregates are the most common performance tax on these reports.

**Mistake 5: treating `LIMIT` as a universal accelerator.**
`LIMIT 5` runs in 19 ms without ORDER BY or GROUP BY because it stops early; with either, it is a 5–12 second full scan. `LIMIT` does not reduce what has to be read.

**Mistake 6: assuming EON spreads every table evenly.**
`public.ujtest` keeps all 3,826 rows in one container on node0003. A small table's hash values land in one segment range, so two of the three `ScanStep`s are empty. **"EON spreads data across three nodes" is false for small tables.**

---

## 8. Risks and Recommendations

**Do now (read-only, low risk)**

1. Build a projection-to-shard-to-node-to-row-count map from `v_monitor.storage_containers` and check it before every performance tuning session.
2. Run `EXPLAIN LOCAL` on slow queries to see how many nodes have a `ScanStep` and how many are scanning nothing.
3. Split reports into "row count only" and "aggregate values" layers: let `count(*)` take the metadata path and keep aggregates on a separate query. Do not mix them.
4. Always measure twice and record both runs: the first is the cold/warm boundary, the second is the steady state. The 4,162 to 1,437 ms case is the illustration.

**Needs a change window and approval**

1. If the depot genuinely cannot hold the hot set, raise `MaxDepotSizePercent` or add depot disks — a cluster configuration change, not tested here.
2. If large joins routinely exceed 60 seconds, pre-join in ETL or build a projection with aligned segmentation; that means DDL and redistribution.
3. K-Safe is currently 0. Every discussion of "slow queries" and "data redistribution" here assumes no replicas. Raising K-Safe requires rebuilding shards and subscriptions and is not something to do casually.

---

## 9. References

- Vertica 25.4 EXPLAIN command: <https://docs.vertica.com/25.4.x/en/sql-reference/commands/explain/>
- `v_monitor.storage_containers`: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-storage-containers/>
- `v_catalog.storage_locations`: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-storage-locations/>
- EON architecture (communal storage, depot, shards): <https://docs.vertica.com/25.4.x/en/admin/architecture/eon/>

---

*Every figure in this article comes from read-only queries against the local `eondb` (Vertica v25.4.0-0, three nodes, K-Safe=0) on 2026-11-18. Nothing on the cluster was changed.*
