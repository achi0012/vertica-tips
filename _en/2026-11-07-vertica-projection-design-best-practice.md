---
layout: post_en
title: "Vertica Projection Design in Practice: Segmentation, Sort Order, Encoding, and ROS Pushback"
date: 2026-11-07 05:00:00 +0800
categories: vertica projection performance
tags: [vertica, projection, segmentation, sort-order, encoding, ros-pushback, performance]
description: "From segmentation and sort order to encoding and projection count, benchmark how Vertica projection design affects EXPLAIN plans, data skew, and ROS pushback, with a practical design checklist."
image: /assets/images/og/vertica-projection-design-best-practice-en.png
faq:
  - q: "Why is my new projection not used by queries?"
    a: "A new projection must be REFRESHed before the optimizer can use it; otherwise you get WARNING 4468. Confirm the chosen projection with EXPLAIN."
  - q: "Is a low-cardinality column a good segmentation key?"
    a: "No. With status as the segmentation key, HASH(status)%8 put 91.1% of rows in one bucket and left 6 of 8 buckets empty."
  - q: "Does forcing an encoding always reduce storage?"
    a: "No. Forcing BLOCK_DICT increased payload by 52.1% and amount by 29.5%; forcing every encoding was 75.9% larger than AUTO."
---

<!--more-->

---

## 1. The Short Version

A projection is the only place Vertica actually stores data, yet most people just run `CREATE TABLE` and let the database decide everything. That works fine at small scale. The bill arrives later, when queries slow down, loads get heavier, and disk usage suddenly triples because every decision has a cost.

On a single-node Vertica 25.4.0-0 instance I built several projections over the same 300,000-row data set and measured five things worth remembering:

1. **The sort key decides whether a query can skip rows, not just how well data compresses.** For a point lookup on `customer_id = 12345`, a projection with `ORDER BY customer_id` had an EXPLAIN cost of **193**, versus **657** without a sort key. PROFILE showed the sorted projection pruned **195,552 of 300,000 rows (65.2%)** through valindex; the unsorted one pruned **0**.
2. **Container-level min/max pruning has nothing to do with sort order — it follows load locality.** I loaded the data in 10 monthly batches. Even with no `ORDER BY` at all, a September query scanned only the September container (29,708 rows). The real difference showed up inside the container, where the sorted projection pruned another 22,413 rows (73.2%) via valindex.
3. **Segmenting on a low-cardinality column skews badly.** Simulating 8 segments with `HASH(status) % 8`, the `CLOSED` bucket held **273,280 rows (91.1%)**, and 6 of 8 buckets were completely empty. A single node has only one segment, so you cannot see node hot spots locally — but on a cluster this is a disaster.
4. **Forcing encodings usually makes things worse.** Same data, same sort order: AUTO was 17.06 bytes/row. `payload ENCODING BLOCK_DICT` became 25.94 (**+52.1%**), `amount ENCODING BLOCK_DICT` became 22.09 (**+29.5%**), and forcing every column was 30.00 (**+75.9%**). Only `event_ts ENCODING DELTAVAL` — a sorted key with nearby values — actually shrank (-3.9%).
5. **Projection count is a multiplier.** One projection stored 5,116,967 bytes; four projections stored 20,111,106 bytes (**3.93×**), load time went from 208.6–221.7 ms to 258.4–266.0 ms (**+22%**), and after 10 loads the ROS container count went from 10 to 40. A projection is not a free index.

This is written for anyone who designs projections, or who has ever wondered why adding a projection did not make a query faster. Every number comes from a real run, and the `article_lab_proj` schema was dropped with `DROP SCHEMA ... CASCADE` when the lab finished.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Nodes | 1 (Community Edition) |
| Database / user | `testdb` / `dbadmin` |
| Test schema | `article_lab_proj` (dropped after the lab) |
| Fact table | 300,000 rows × 6 columns |
| Columns | `event_id`, `customer_id`, `event_ts`, `status`, `amount`, `payload` |
| Distribution | `customer_id` 1–20,000; `status` CLOSED 270,248 / OPEN 26,720 / CANCELLED 3,032 |
| Time range | 2026-01-01 through 2026-10-30 |
| Load method | `COPY ... DIRECT`, one 300,000-row file |
| Measurement date | 2026-11-07 |

Three environment details affect reproducibility:

- **This connection runs with `autocommit` set to `off`.** Vertica commits DDL and `COPY` on its own, but if you `DELETE` or `INSERT` in one session and check from another, you will not see the change. Every test here ran inside a single `vsql -f` session.
- **A single node has only one segment.** `v_monitor.storage_containers` returned `segment_lower_bound = 0` and `segment_upper_bound = 4294967295` for every test projection, meaning one container spans the entire segment range. That means **this machine cannot observe inter-node skew**; I can only simulate it with `HASH()`, and the article says clearly which numbers are simulated.
- **`COPY` reads the file on the server side**, so it must be readable by `dbadmin`. I used `/tmp/opencode/` with mode 644.

---

## 3. Core Mechanics

### 3.1 One table, many projections

The table definition (`CREATE TABLE`) is only a logical shell. The data lives in projections. Each projection has its own:

| Design axis | What it decides | Main impact |
|---|---|---|
| Segmentation | Which segment gets each row | Multi-node distribution, join network traffic |
| Sort order (`ORDER BY`) | Row order inside a container | Compression, valindex pruning, pipelined GROUP BY |
| Encoding | Per-column compression | Disk usage, scan I/O |
| Projection count | How many copies of the data exist | Storage, load time, mergeout/ROS count |

These axes interact. Sorting makes encoding more effective but adds a sort during load. One more projection speeds up one query but makes every load write one more copy.

### 3.2 What segmentation actually looks like

If you write `SEGMENTED BY HASH(customer_id) ALL NODES`, `v_catalog.projections.segment_expression` shows exactly what happened:

```
hash(fact_cust.customer_id)
```

When you do not specify anything, the automatically created super projection uses **every column** as the segmentation key:

```
hash(fact_auto.event_id, fact_auto.customer_id, fact_auto.event_ts,
      fact_auto.amount, fact_auto.status, fact_auto.payload)
```

The semantics are: hash the value list, then take it modulo the number of segments. So the **cardinality and distribution** of the segmentation columns directly determine how many rows each segment receives. Using `status` (3 values) as the key effectively creates three piles, and the hash does not even split them proportionally.

### 3.3 Two jobs of the sort order

1. **Compression**: adjacent duplicate or close values give RLE and DELTAVAL something to work with.
2. **Pruning**: the `container rows pruned by valindex` counter in PROFILE appears only on projections with `ORDER BY`. On point lookups it skips most rows inside a container, which is where the 657 → 193 cost reduction comes from.

Important: **container-level min/max pruning does not require sorting**. Every ROS container records min/max for each column. As long as loads are range-local — monthly batches, for example — a single-month query scans only that one container even with no sort order. Sorting affects whether you can skip rows *inside* a container.

### 3.4 The cost model for projection count

Each additional projection means:

- One more full copy of the data on disk (with its own encoding differences)
- One more ROS write per `COPY`
- One more mergeout job for the tuple mover
- Its own accumulation of ROS containers, which eventually hits ROS pushback

The rule of thumb is "start with the fewest projections that satisfy the main queries, then add only when you find a bottleneck." The measurements below are that sentence with numbers attached.

---

## 4. Step-by-Step Lab

### 4.1 Four tables, same data, different designs

All four tables hold identical rows; only the projection differs:

```sql
CREATE TABLE article_lab_proj.fact_auto (
  event_id INT NOT NULL, customer_id INT NOT NULL, event_ts TIMESTAMP NOT NULL,
  status VARCHAR(12) NOT NULL, amount NUMERIC(12,2), payload VARCHAR(16));

CREATE TABLE article_lab_proj.fact_cust ( ...same columns... );
CREATE PROJECTION article_lab_proj.fact_cust_p (
  event_id, customer_id, event_ts, status, amount, payload
) AS SELECT * FROM article_lab_proj.fact_cust
ORDER BY customer_id, event_ts
SEGMENTED BY HASH(customer_id) ALL NODES;

CREATE TABLE article_lab_proj.fact_ts ( ...same columns... );
CREATE PROJECTION article_lab_proj.fact_ts_p (
  event_id, customer_id, event_ts, status, amount, payload
) AS SELECT * FROM article_lab_proj.fact_ts
ORDER BY event_ts
SEGMENTED BY HASH(event_id) ALL NODES;

CREATE TABLE article_lab_proj.fact_status ( ...same columns... );
CREATE PROJECTION article_lab_proj.fact_status_p (
  event_id, customer_id, event_ts, status, amount, payload
) AS SELECT * FROM article_lab_proj.fact_status
ORDER BY status
SEGMENTED BY HASH(status) ALL NODES;
```

Each `COPY ... DIRECT` loaded 300,000 rows in 174.0–236.5 ms (215.4, 212.3, 236.5, 174.0 ms across four runs). Load time is roughly the same at this stage because each table has only one projection.

Projections and storage:

```
 projection_name | segment_expression                          | used_bytes | bytes/row
-----------------+---------------------------------------------+------------+-----------
 fact_auto_super | hash(event_id, customer_id, event_ts, ...)   |  5,098,062 |     16.99
 fact_cust_p     | hash(customer_id)                            |  5,116,967 |     17.06
 fact_ts_p       | hash(event_id)                               |  4,885,658 |     16.29
 fact_status_p   | hash(status)                                 |  5,018,078 |     16.73
```

The `ORDER BY event_ts` projection is the smallest (16.29 bytes/row) because DELTAVAL works well on a sorted timestamp. `fact_status_p` also compresses decently (16.73) despite the low cardinality key. The spread is under 5% — **at this data size, the storage difference from sorting and encoding is not the point; query behavior is**.

### 4.2 Segmentation: single node shows no skew, so simulate with HASH()

First confirm the segment layout on a single node:

```sql
SELECT projection_name, segment_lower_bound, segment_upper_bound,
       count(*) AS containers, sum(total_row_count) AS rows
FROM v_monitor.storage_containers
WHERE schema_name = 'article_lab_proj'
GROUP BY 1,2,3;
```

```
 fact_auto_super | 0 | 4294967295 | 1 | 300000
 fact_cust_p     | 0 | 4294967295 | 1 | 300000
 fact_status_p   | 0 | 4294967295 | 1 | 300000
 fact_ts_p       | 0 | 4294967295 | 1 | 300000
```

Everything is "one container covering 0–4294967295", which means one segment. **This machine simply cannot demonstrate node skew**, and I will not pretend otherwise.

Instead I computed `MOD(HASH(key), 8)` over the real 300,000 rows to simulate eight segments (this is a simulation computed from real data, not a multi-node observation):

| Segmentation key | Largest bucket | Smallest bucket | max/min | Empty buckets |
|---|---|---|---|---|
| `status` | 273,280 (91.1%) | 26,720 | 10.23 | 6 / 8 |
| `customer_id` | 38,988 | 36,149 | 1.08 | 0 / 8 |
| `event_id` | 37,791 | 37,285 | 1.01 | 0 / 8 |

`status` has only CLOSED / OPEN / CANCELLED. After hashing it lands in just buckets 5 and 7, with 91.1% of rows in bucket 5. On a cluster, one node would do nine-tenths of the work while the rest sit idle — the classic "the query is not slow, it is waiting on one node" pattern.

`customer_id` and `event_id` stay within ±4%, which is what a segmentation key should look like.

> This section is a `MOD(HASH(...), 8)` simulation. Vertica decides the real segment count from node count and K-safety; single-node CE is 1, so `v_monitor` cannot confirm an eight-segment distribution.

### 4.3 Sort order: a 3.4× difference in EXPLAIN cost

Look up one customer:

```sql
EXPLAIN SELECT count(*), sum(amount)
FROM article_lab_proj.fact_cust WHERE customer_id = 12345;
```

`STORAGE ACCESS` cost across the three tables:

| Table | Projection | STORAGE ACCESS cost | Total cost |
|---|---|---|---|
| `fact_auto` | `fact_auto_super` (no ORDER BY) | 656 | 657 |
| `fact_cust` | `fact_cust_p` (ORDER BY customer_id) | 192 | **193** |
| `fact_ts` | `fact_ts_p` (ORDER BY event_ts) | 656 | 657 |

Only 9 rows actually match. All three EXPLAIN plans estimate 2K rows (statistics were collected), so the entire difference comes from whether the projection can use the sort key to locate rows.

The `fact_cust` cost is 70.6% lower, but **actual runtime is only about 26% lower**:

```
cust_sorted    4.10 / 4.21 / 4.39 ms
cust_unsorted  5.44 / 5.77 / 5.72 ms
```

The reason is simple: 300,000 rows is small for a single node, everything fits in memory, and pruning saves CPU rather than disk I/O. The gap widens as data grows.

### 4.4 PROFILE: how many rows valindex actually prunes

Run the same query with `PROFILE`, then read `v_monitor.execution_engine_profiles`:

```sql
PROFILE SELECT count(*), sum(amount)
FROM article_lab_proj.cust_sorted WHERE customer_id = 12345;
```

```
 operator      | counter                                   | value
---------------+-------------------------------------------+--------
 StorageUnion  | container rows total                      | 300,000
 StorageUnion  | container rows pruned by valindex         | 149,998
 StorageUnion  | container rows pruned by query predicates  |  45,554
 Scan          | rows pruned by valindex                   | 195,552
```

The unsorted `cust_unsorted`:

```
 StorageUnion  | container rows total                      | 300,000
 StorageUnion  | container rows pruned by valindex         |       0
 StorageUnion  | container rows pruned by query predicates  |       0
 Scan          | rows pruned by valindex                   |       0
```

Note that 195,552 + 45,554 = 241,106 does not reconcile with 300,000, while 149,998 + 45,554 = 195,552 does. The `Scan` valindex counter and the two `StorageUnion` counters come from different stages; do not add them together. The point is simpler: **a projection without ORDER BY prunes zero rows through valindex**.

### 4.5 Container min/max is independent of sorting: the monthly load test

I split the same data into 10 monthly files and loaded each table with 10 separate `COPY` statements. Querying September:

```
ts_sorted    3.71 / 3.78 / 4.40 ms    container rows total 29,708
ts_unsorted  3.76 / 3.82 / 3.80 ms    container rows total 29,708
```

**Both tables scanned only 29,708 rows**, because each ROS container holds a single month, so min/max eliminated the other nine containers. Container pruning comes from load locality, not sorting.

Query a three-day range (3/1–3/4) and the difference appears:

```
ts_sorted    container rows total 30,605
             container rows pruned by valindex 21,418
             container rows pruned by query predicates 995
             Scan rows pruned by valindex 22,413   (73.2%)
ts_unsorted  container rows total 30,605
             container rows pruned by valindex 0
```

Both were first pruned by min/max down to the March container (30,605 rows), but the sorted projection then skipped 22,413 rows inside it using valindex. Runtime was 3.42–3.46 ms versus 3.59–3.69 ms — again a small difference because the row count is small.

The takeaway: **sort order pays off inside a container; pruning between containers is a load-strategy win.** If you bulk load once and every query spans the full time range, sorting buys you much less than a monthly load pattern does.

### 4.6 Plan shape for GROUP BY

Sorting also changes how aggregation runs. For `GROUP BY status`:

```
fact_auto    GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 419]
             STORAGE ACCESS [Cost: 272, Rows: 300K]

fact_status  GROUPBY PIPELINED [Cost: 339]
             STORAGE ACCESS [Cost: 192, Rows: 300K]
```

`ORDER BY status` lets `GROUP BY status` use pipelined aggregation instead of building a hash table and resegmenting. Cost drops 419 → 339 (**-19.1%**). That is the concrete payoff for aligning the sort key with the GROUP BY prefix.

### 4.7 Encoding: AUTO is usually fine, and forcing it usually hurts

Same data, same `ORDER BY customer_id, event_ts`, same segmentation, only the column encodings change:

| Encoding | used_bytes | bytes/row | vs AUTO |
|---|---|---|---|
| AUTO (baseline) | 5,116,967 | 17.06 | — |
| All columns `ENCODING NONE` | 5,461,646 | 18.21 | +6.7% |
| `event_ts ENCODING DELTAVAL` | 4,916,531 | 16.39 | **-3.9%** |
| `status ENCODING RLE` | 5,116,967 | 17.06 | 0.0% |
| `amount ENCODING BLOCK_DICT` | 6,627,774 | 22.09 | +29.5% |
| `payload ENCODING BLOCK_DICT` | 7,781,972 | 25.94 | **+52.1%** |
| Everything forced (DELTAVAL×3 + RLE + BLOCK_DICT×2) | 8,998,702 | 30.00 | **+75.9%** |

Two observations:

1. **AUTO does not mean "no compression".** Turning all encodings off added only 6.7%, which means AUTO already picked reasonably well for this data set.
2. **`BLOCK_DICT` backfires on high-cardinality columns.** `payload` has 5,000 distinct values and `amount` is almost unique per row; the dictionary plus its index costs more than the space it saves.

Sorting plus RLE, on the other hand, works. After changing the table to `ORDER BY status, event_ts`:

| Setting | used_bytes | bytes/row | vs `fact_status` |
|---|---|---|---|
| AUTO (`ORDER BY status`) | 5,018,078 | 16.73 | — |
| `status ENCODING RLE` | 4,831,850 | 16.11 | -3.7% |
| `status RLE` + other columns `DELTAVAL` | 4,756,913 | 15.86 | **-5.2%** |

RLE only helps when identical values appear consecutively, so it must be paired with putting that column at the front of the sort key.

### 4.8 Projection count: 3.93× storage, +22% load, 4× ROS

Two tables with identical data: one projection versus four projections (sorted by customer_id, event_ts, status, and amount respectively). Each loaded 300,000 rows twice:

| Table | Projections | Load time | Total storage |
|---|---|---|---|
| `single_a` | 1 | 221.7 ms | 5,116,967 |
| `single_b` | 1 | 208.6 ms | 5,116,967 |
| `multi_a` | 4 | 258.4 ms | 20,111,106 |
| `multi_b` | 4 | 266.0 ms | 20,112,739 |

Average 215.2 ms → 262.2 ms (**+21.9%**), with 3.93× the storage.

Then load the same data in 10 batches into `single_b` and `multi_b`:

```
single_b  single_b_p    ros_count 10   row_count 569,168   used_bytes  9,630,816
multi_b   multi_b_p1    ros_count 10   row_count 569,168   used_bytes  9,630,816
multi_b   multi_b_p2    ros_count 10   row_count 569,168   used_bytes  9,269,325
multi_b   multi_b_p3    ros_count 10   row_count 569,168   used_bytes  9,313,602
multi_b   multi_b_p4    ros_count 10   row_count 569,168   used_bytes  9,443,703
```

One projection accumulates 10 ROS containers; four projections accumulate **40**. Total storage is 37,657,446 bytes (3.91×). More ROS containers mean more mergeout work for the tuple mover. If you load frequently and every projection keeps accumulating containers, that is where ROS pushback begins.

### 4.9 Creating a projection on a populated table requires REFRESH

```sql
CREATE PROJECTION article_lab_proj.fact_auto_p2 (...)
AS SELECT ... FROM article_lab_proj.fact_auto
ORDER BY event_ts SEGMENTED BY HASH(event_id) ALL NODES;
```

Actual output:

```
WARNING 4468:  Projection <article_lab_proj.fact_auto_p2> is not available for
query processing. Execute the select start_refresh() function to copy data into
this projection.
```

`v_catalog.projections` shows `is_up_to_date = f`. It only flips to `t` after `REFRESH`, which took **233.3 ms** for 300,000 rows (refresh method `scratch`). Until then the optimizer will not use the projection, and you cannot measure any speedup from it — an easy way to conclude "the new projection did not help."

---

## 5. Performance and Cost Analysis

The measurements as a decision table:

| Action | Benefit (measured) | Cost (measured) | When it pays off |
|---|---|---|---|
| Add `ORDER BY` matching point/range filters | cost 657 → 193; valindex prunes 65.2% of rows | one extra sort at load; storage +0.5% | When filter columns are clear and data is large |
| Align with GROUP BY prefix | GROUPBY HASH 419 → PIPELINED 339 | same as above | When reports aggregate the same dimensions |
| Segment on a high-cardinality uniform column | max/min 1.01–1.08 | none | Almost always |
| Segment on a low-cardinality column | none | 91.1% lands in one bucket | Do not do it |
| `DELTAVAL` on a sorted timestamp | -3.9% | negligible | Time-series data |
| `BLOCK_DICT` on a whole payload column | none | +52.1% | Only when cardinality is truly low |
| One extra projection | Certain queries get faster | storage ×3.93, load +22%, ROS ×4 | When one query consistently dominates resources |

Two costs people overlook:

- **EXPLAIN cost is not runtime.** At 300,000 rows, 657 → 193 bought only 26% wall-clock improvement. Cost is a relative signal, not seconds.
- **Container pruning and sorting are separate mechanisms.** Monthly loads let an unsorted table prune containers too, so "I added ORDER BY and felt nothing" does not mean sorting is useless — it means container min/max already covered your query.

---

## 6. Common Mistakes and Anti-Patterns

### 6.1 Segmenting on a low-cardinality column

```sql
SEGMENTED BY HASH(status) ALL NODES
```

Measured `HASH(status) % 8`: 273,280 rows in bucket 5, 26,720 in bucket 7, the other six buckets empty, max/min = 10.23. On a cluster this is exactly how you get one node working while the others watch. Use a high-cardinality, evenly distributed column that also appears in joins.

### 6.2 Assuming table-level ENCODING applies to every projection

```sql
CREATE TABLE t (payload VARCHAR(16) ENCODING NONE, ...);
CREATE PROJECTION t_p (...) AS SELECT ... FROM t ORDER BY ...;
```

Measured result: the automatically created `t_super` was `NONE`, but the manually created `t_p` was `AUTO` on every column. **Table-level ENCODING only affects the automatic projection**; manual projections need their own encoding clauses. This happens silently, with no error.

### 6.3 `ENCODING DELTA` is a syntax error in 25.4

```
ERROR 4856:  Syntax error at or near "DELTA" at character 82
LINE 1: ... (i ENCODING DELTAVAL, ts ENCODING DELTA, v ENCODING RLE)
```

Replacing it with `ENCODING DELTAVAL` works (for both INT and TIMESTAMP). Do not copy `DELTA` from old documentation or intuition; validate syntax on a small table first.

### 6.4 Creating a projection and forgetting REFRESH

```
WARNING 4468:  Projection <...> is not available for query processing.
```

Along with `is_up_to_date = f`. Queries still use the old projection, so you conclude "the new projection did not help." Run `SELECT REFRESH('schema.table')` first, then compare EXPLAIN and timings.

### 6.5 Dropping a projection that was already replaced

After `CREATE PROJECTION` on an empty table, Vertica removes the original automatic super projection:

```
ROLLBACK 4451:  Projection "fact_enc_none_super" does not exist
```

If an old script then tries `DROP PROJECTION ..._super`, you get this error. After creating a projection, check `v_catalog.projections` to see what actually exists.

### 6.6 Adding a projection for every query

Four projections mean 3.93× storage, +22% load time, and ROS containers going from 10 to 40. Early on this may feel free. Once the table reaches tens of millions of rows and loads run several times a day, the costs all come back at once. Identify whether the bottleneck is scanning, aggregation, or joins before deciding to add a projection instead of fixing SQL or partitioning.

---

## 7. Risks and Recommendations

### Safe now (read-only / low risk)

1. Inspect current projections and sort keys:
   ```sql
   SELECT anchor_table_name, projection_name, is_segmented, segment_expression
   FROM v_catalog.projections
   WHERE projection_schema = 'your_schema'
   ORDER BY 1,2;
   ```
2. Find fat or fragmented projections by actual size and ROS count:
   ```sql
   SELECT anchor_table_name, projection_name, node_name,
          row_count, ros_count, used_bytes
   FROM v_monitor.projection_storage
   WHERE anchor_table_schema = 'your_schema'
   ORDER BY used_bytes DESC
   LIMIT 20;
   ```
3. Run `EXPLAIN` on slow queries to see which projection was chosen and at what cost. Only run `PROFILE` against **your own test tables** when you need more detail.
4. Check candidate segmentation keys with `HASH()` (replace `8` with your segment count):
   ```sql
   SELECT MOD(HASH(status), 8) AS bucket, COUNT(*)
   FROM your_table GROUP BY 1 ORDER BY 1;
   ```

### Requires change (needs a window / approval)

1. **Adding or changing projections**: create the new projection, `REFRESH` it, and validate with EXPLAIN and real timings before considering removal of the old one. Run during off-peak hours and confirm free disk space — one projection is one full copy of the data.
2. **Changing segmentation**: triggers data redistribution; on a cluster it affects network and disk I/O. First confirm the new key's `MOD(HASH(...), N)` distribution is within ±10%.
3. **Changing encoding**: validate bytes/row on a small table or sample before applying broadly. Do not change every column at once; the all-forced test was 75.9% larger than AUTO.
4. **Dropping redundant projections**: confirm no query depends on it (`v_monitor.query_profiles` or `EXPLAIN`), then `DROP PROJECTION`. Dropping a projection does not delete table data, but it removes one plan option.

---

## 8. References

- [CREATE PROJECTION](https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-statements/create-projection/)
- [Projections](https://docs.vertica.com/25.4.x/en/admin/projections/)
- [v_catalog.projections](https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-projections/)
- [ALTER TABLE (encoding syntax)](https://docs.vertica.com/25.4.x/en/sql-reference/statements/alter-statements/alter-table/)
- [Projection best practices](https://docs.vertica.com/25.4.x/en/best-practices/projections/)
- [REFRESH](https://docs.vertica.com/25.4.x/en/sql-reference/functions/refresh/)
