---
layout: post_en
title: "Vertica Statistics Management: Why the Optimizer Chooses the Wrong Plan"
date: 2026-11-06 05:00:00 +0800
categories: vertica statistics performance
tags: [vertica, statistics, analyze-statistics, query-plan, explain, optimizer, performance]
description: "Explain why the Vertica optimizer chooses the wrong plan when statistics are stale, benchmark EXPLAIN before and after ANALYZE_STATISTICS, compare row estimates and join order, and build a practical statistics maintenance strategy."
---

<!--more-->

---

## 1. The Short Version

The most common reason a query plan goes wrong is not a badly written SQL statement — it is that **the optimizer is working with statistics that are wrong or missing entirely**.

I measured this on a single-node Vertica 25.4.0-0 instance and came away with four findings that change how I run day-to-day operations:

1. **Creating a table and loading data does not create statistics.** A 1,000,000-row table built with `CREATE TABLE AS SELECT`, and a 100,000-row table filled with `COPY`, both had **no row at all** in `v_monitor.table_statistics` — and still had none 1.5 to 5 minutes later. `EXPLAIN` reports `Rows: 10K (NO STATISTICS)` for every one of them.
2. **Collecting statistics is cheap.** `ANALYZE_STATISTICS` on 1,000,000 rows × 6 columns took 67, 82, 90, and 122 ms across four runs; 100,000 rows took 19 ms. That is far cheaper than a 200 ms query. There is no excuse for skipping it.
3. **Fixing statistics changes the plan, but not always the wall clock.** A three-table join flipped from "`dim_customer` on the outer side, fact table on the inner side" to "fact table outer, `dim_customer` inner", and runtime went from 213–215 ms to 191–193 ms — about 10%. At one million rows on a single node, the value of statistics is **correct estimates**, not raw seconds.
4. **Statistics are not a magic wand.** Equality predicates on low-cardinality varchar columns stay unreliable: `status = 'CANCELLED'` matches 50,000 rows but the optimizer estimates 600,000 — a 12× error. Re-collecting with `percent = 100` still returns 600,000. "Has statistics" does not mean "estimates are accurate".

This article is for two kinds of people: those who see `NO STATISTICS` in `EXPLAIN` and do not know what to do next, and those who already run a scheduled `ANALYZE_STATISTICS` but still get strange plans. Every number here comes from a real run, and every SQL snippet can be re-executed.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `v25.4.0-0` |
| Nodes | 1 (single node, Community Edition) |
| Database | `testdb` |
| Connected user | `dbadmin` |
| Test schema | `article_lab_stats` (dropped with `DROP SCHEMA ... CASCADE` when the lab finished) |
| Fact tables | `fact_sales` 1,000,000 rows × 6 columns; `fact_join` 1,000,000 rows × 5 columns |
| Dimension tables | `dim_region` 20 rows, `dim_customer` 4,519 rows |
| Data distribution | `status`: CLOSED 700,000 / OPEN 250,000 / CANCELLED 50,000; `region_id = 1` covers 40% |
| Measurement date | 2026-11-06 |

Three environment details will affect your reproduction:

- **This connection runs with `autocommit` set to `off`** (`SHOW AUTOCOMMIT` returns `off`). After a `DELETE` you must issue an explicit `COMMIT`, otherwise a new session will not see the change. My first measurement round failed exactly this way and none of the numbers lined up.
- When generating test data, `row_number() OVER ()` combined with a `CROSS JOIN` **numbers each stream independently**. My first attempt produced a million rows with only 4,000 distinct `order_id` values. I switched to the arithmetic key `(a.i - 1) * 250 + b.i` to get a genuinely unique key. This is off-topic, but it will make you think "duplicate primary keys" is a Vertica bug.
- `DO_TM_TASK('mergeout')` with no table argument runs mergeout across the **entire database**. I triggered it once by accident on the test box; it ran for more than five minutes and I had to abort it with `CLOSE_SESSION()`. Do not run it casually in production.

---

## 3. How It Works

### 3.1 Where the statistics live

The entry point is `v_monitor.table_statistics`. It has only nine columns in 25.4:

| Column | Type | Meaning |
|--------|------|---------|
| `table_oid` | int | Joins to `v_catalog.tables.table_id` (**not** a column called `table_id`) |
| `table_name` | varchar(128) | Table name |
| `row_count` | int | Row count at collection time |
| `is_row_count_valid` | boolean | A flag that, in practice, **cannot be trusted** (see 4.6) |
| `stat_collection_time` | timestamptz | When the statistics were collected |
| `min_partition_key` / `max_partition_key` | varchar(128) | Partition boundaries |
| `logical_stats_oid` / `schema_oid` | int | Internal identifiers |

To get the schema name back you join `v_catalog.tables` yourself:

```sql
SELECT t.table_schema, ts.table_name, ts.row_count,
       ts.is_row_count_valid, ts.stat_collection_time
FROM v_monitor.table_statistics ts
JOIN v_catalog.tables t ON t.table_id = ts.table_oid
WHERE t.table_schema = 'article_lab_stats'
ORDER BY 2;
```

Note that the join uses `t.table_id`, not `t.table_oid` — `v_catalog.tables` has no `table_oid` column, and getting this wrong returns `ERROR 2624: Column "table_oid" does not exist`.

### 3.2 What the optimizer does with statistics

| Decision | Statistics it depends on | What goes wrong when they are stale |
|----------|--------------------------|-------------------------------------|
| Join order (which side is outer / build side) | `row_count` on both sides plus filter selectivity | A large table becomes the build side; the hash table spills or blows up memory |
| Projection choice | Projection sort order and row counts | Picks a projection without the useful sort order, adding a sort |
| `GROUPBY` type (PIPELINED vs HASH) | Input row count and group count | Something that could pipeline becomes a hash aggregate that spills |
| Network transfer (resegment) | Segmentation on both sides plus row counts | An extra broadcast |

With no statistics the optimizer does not stop. It substitutes a **default of 10,000 rows** and prints `(NO STATISTICS)` in `EXPLAIN`. That default is too large for a 20-row table and far too small for a 100-million-row fact table. Both directions are wrong.

### 3.3 When statistics go stale

| Operation | Measured result (25.4.0-0) |
|-----------|----------------------------|
| `CREATE TABLE AS SELECT` | **No** statistics row is created |
| `COPY` into a newly created table | **No** statistics row (still absent 1.5–5 minutes later) |
| Large `DELETE` | `row_count` **unchanged**, `is_row_count_valid` stays `t` |
| `TRUNCATE` | `row_count` becomes **1** (not 0), `stat_collection_time` unchanged |
| `ALTER TABLE ... ADD COLUMN` | `stat_collection_time` is refreshed, `row_count` is preserved |
| Background mergeout | Not guaranteed; in one round the old value was still there about 6 minutes later |

That table is the heart of this article. It says statistics are not something the database maintains for you; they are **a one-off snapshot that you have to trigger**. The documentation supports this: `ANALYZE_STATISTICS` is an alias of `ANALYZE_HISTOGRAM`, which is no longer documented separately, and both are meta-functions that must be called from a `SELECT`.

![Statistics lifecycle: how load, ANALYZE, DELETE, and TRUNCATE affect row_count and EXPLAIN estimates]({{ '/assets/images/stats-lifecycle.svg' | relative_url }}){: loading="lazy" }

The left side shows the state and the action that leads to it; the right side shows what `EXPLAIN` estimates for that same table in that state. Green means fresh statistics, amber means stale or missing.

---

## 4. Step by Step

### 4.1 Build the test data

```sql
CREATE SCHEMA article_lab_stats;
CREATE TABLE article_lab_stats.seed AS
SELECT row_number() OVER () AS i FROM v_catalog.columns;   -- 4,519 rows

CREATE TABLE article_lab_stats.fact_sales AS
SELECT k.id AS order_id,
       CASE WHEN k.id % 10 < 4 THEN 1 ELSE (k.id % 19) + 2 END AS region_id,
       (k.id % 4519) + 1 AS customer_id,
       CASE WHEN k.id % 100 < 70 THEN 'CLOSED'
            WHEN k.id % 100 < 95 THEN 'OPEN'
            ELSE 'CANCELLED' END AS status,
       ((k.id % 1000)::numeric / 10) AS amount,
       TIMESTAMP '2025-01-01 00:00:00' + ((k.id % 365) * INTERVAL '1 day') AS order_ts
FROM (SELECT (a.i - 1) * 250 + b.i AS id
      FROM (SELECT i FROM article_lab_stats.seed WHERE i <= 4000) a
      CROSS JOIN (SELECT i FROM article_lab_stats.seed WHERE i <= 250) b) k;
```

Loading one million rows took 549–884 ms. The `status` distribution came out as 700,000 / 250,000 / 50,000, and `count(DISTINCT order_id)` returned 1,000,000.

### 4.2 The plan with no statistics

Querying the statistics table immediately after the load:

```
 table_name | row_count | is_row_count_valid
------------+-----------+--------------------
 dim_customer |      4519 | t
 dim_region   |        20 | t
(2 rows)
```

`fact_sales` is simply absent. `EXPLAIN` reacts exactly as you would expect:

```
+-SORT [Cost: 2K, Rows: 20 (NO STATISTICS)] (PATH ID: 1)
| +---> JOIN HASH [Cost: 2K, Rows: 400 (NO STATISTICS)] (PATH ID: 3)
| | +-- Outer -> STORAGE ACCESS for f [Cost: 1K, Rows: 10K (NO STATISTICS)] (PATH ID: 4)
| | |      Filter: (f.status = 'CLOSED')
| | +-- Inner -> STORAGE ACCESS for r [Cost: 34, Rows: 20] (PATH ID: 5)
```

Three things to notice:

- `fact_sales` is estimated at **10K** against an actual 1,000,000 — off by 100×.
- `dim_region` shows **20** with no `NO STATISTICS` marker, because it was analyzed earlier. **A single plan can contain a mix of tables with and without statistics.** Do not assume everything is fine just because some numbers look real.
- The `status = 'CLOSED'` predicate does not appear in the estimate at all; `Rows: 10K` is just the table-level default.

### 4.3 ANALYZE_STATISTICS: cost and effect

```sql
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_sales');
```

The return value is `0`. It is neither an error code nor a row count. Measured durations:

| Table | Rows × columns | Duration |
|-------|----------------|----------|
| `t_copy` | 100,000 × 2 | 19 ms |
| `t_sel` | 1,000,000 × 3 | 74 ms |
| `fact_join` | 1,000,000 × 5 | 99 ms |
| `fact_sales` | 1,000,000 × 6 | 67 / 82 / 90 / 122 ms (four runs) |
| `fact_sales` (single column `status`) | 1,000,000 × 1 | 52 ms |

`v_monitor.table_statistics` fills in immediately:

```
 table_name | row_count | is_row_count_valid |     stat_collection_time
------------+-----------+--------------------+-------------------------------
 fact_sales |   1000000 | t                  | 2026-11-06 05:12:27.843561+08
```

The `EXPLAIN` estimate moves from 10K to 905K and the cost from 2K to 8K:

```
+-SORT [Cost: 8K, Rows: 20] (PATH ID: 1)
| +---> JOIN HASH [Cost: 7K, Rows: 905K] (PATH ID: 3)
| | +-- Outer -> STORAGE ACCESS for f [Cost: 3K, Rows: 905K] (PATH ID: 4)
| | +-- Inner -> STORAGE ACCESS for r [Cost: 34, Rows: 20] (PATH ID: 5)
```

Note that it says **905K, not 700K**, which is the actual count for `status = 'CLOSED'`. That is not a rounding difference; it is 29%. The next section explains why.

### 4.4 Histograms: accurate for integers, poor for strings

On the same one-million-row table I compared estimate against reality for each predicate:

| Predicate | `EXPLAIN` estimate | Actual rows | Error |
|-----------|--------------------|-------------|-------|
| `region_id = 1` | 400K | 400,000 | 0% |
| `status = 'CLOSED'` | 905K | 700,000 | +29% |
| `status = 'OPEN'` | 143K | 250,000 | −43% |
| `status = 'CANCELLED'` | 600K | 50,000 | **+1,100%** |
| `status = 'ZZZ'` (no such value) | 1 | 0 | — |
| `id <= 1000` (on the 100K table) | 1K | 1,000 | 0% |

Integer equality is exact to the row. **Low-cardinality varchar equality is completely unreliable** — `CANCELLED` is off by 12×. And it is not a sampling problem: I re-collected with the three-argument form at 100% sampling:

```sql
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status', 100);
```

After that, `CLOSED` moved from 905K to 923K, `OPEN` stayed at 143K, and `CANCELLED` stayed at 600K — **essentially unchanged**. So this is how the string histogram interpolation works, not a sample-size issue. The practical consequence: if join order or projection choice hinges on the selectivity of a varchar predicate, do not trust the `Rows` number in `EXPLAIN` — verify with your own `count(*)`.

### 4.5 Join order: the plan shape really does change

With the same one million rows in `fact_join`, here is the three-table join. **Without statistics**:

```
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 2K, Rows: 100 (NO STATISTICS)]
| +---> JOIN HASH [Cost: 2K, Rows: 201 (NO STATISTICS)]
| | +-- Outer -> STORAGE ACCESS for c [Cost: 18, Rows: 5K]
| | +-- Inner -> JOIN HASH [Cost: 2K, Rows: 200 (NO STATISTICS)] (PUSHED GROUPING)
| | | +-- Outer -> STORAGE ACCESS for r [Cost: 17, Rows: 20]
| | | +-- Inner -> STORAGE ACCESS for f [Cost: 2K, Rows: 10K (NO STATISTICS)]
```

After `ANALYZE_STATISTICS`:

```
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 9K, Rows: 100]
| +---> JOIN HASH [Cost: 7K, Rows: 905K]
| | +-- Outer -> JOIN HASH [Cost: 4K, Rows: 905K]
| | | +-- Outer -> STORAGE ACCESS for f [Cost: 3K, Rows: 905K]
| | | +-- Inner -> STORAGE ACCESS for c [Cost: 40, Rows: 5K]
| | +-- Inner -> STORAGE ACCESS for r [Cost: 34, Rows: 20]
```

The shape changed. Without statistics the optimizer believed the fact table held only 10K rows, so it made `dim_customer` (5K) the outer side. With statistics it knows the fact table has 905K rows and flips it to the outer side with the dimensions as build inputs — and pushes both runtime filters (`SIP1` / `SIP2`) into the scan.

![The same three-table join before and after statistics: the fact table moves from the inner side to the outer side]({{ '/assets/images/stats-plan-shape.svg' | relative_url }}){: loading="lazy" }

The left plan has no statistics; the right plan follows `ANALYZE_STATISTICS`. Blue nodes are the outer side, grey nodes are the inner (build) side.

Runtime for the same statement, three runs each:

| Scenario | Run 1 | Run 2 | Run 3 |
|----------|-------|-------|-------|
| No statistics | 294.8 ms | 215.0 ms | 212.7 ms |
| With statistics | 192.2 ms | 192.8 ms | 191.5 ms |

Discounting the first run's warm-up, **215 → 192 ms, roughly 10%**. The two-table join (fact table plus a 20-row dimension) went from 120–145 ms to 120–125 ms — **within noise**. In other words, the benefit of statistics depends on whether a bad estimate produces a bad plan shape. When the shape does not change, collecting statistics only makes `EXPLAIN` look better; it does not make the query faster.

That 294.8 ms first run deserves a note: all three runs were the same SQL over the same data, yet only the first was slow, with the next two steady at 212–215 ms. That is the cost of pulling ROS containers into memory on the first scan, not a statistics effect. When you benchmark a plan change, **always throw away the first run** — otherwise it is very easy to read a warm-up effect as "statistics made the query 40% slower".

### 4.6 After a DELETE, `is_row_count_valid` lies to you

This is the most overlooked scenario. `fact_sales` has one million rows and fresh statistics. Now delete 95% of them:

```sql
DELETE FROM article_lab_stats.fact_sales WHERE order_id > 50000;
COMMIT;
```

`DELETE` reports `OUTPUT 950000` and `count(*)` leaves 50,000. But the statistics table says:

```
 row_count | is_row_count_valid |     stat_collection_time
-----------+--------------------+-------------------------------
   1000000 | t                  | 2026-11-06 05:14:04.431932+08
```

**`row_count` is still 1,000,000, `is_row_count_valid` is still `t`, and the timestamp has not moved.** I checked again about six minutes later and it was still 1,000,000 (during that window I accidentally triggered a database-wide mergeout and aborted it, which may have disturbed background work, but the conclusion holds: you cannot rely on it).

The plan is stale too:

| Stage | `EXPLAIN` estimate | Cost | Actual rows | Query time |
|-------|--------------------|------|-------------|------------|
| After DELETE, before re-collect | 905K | 8K | 50,000 | 34–40 ms |
| After `ANALYZE_STATISTICS` (221 ms) | 45K | 6K | 50,000 | 33 ms |

After re-collecting, the estimate is 45K (the actual CLOSED count is 35,000, so it still overestimates by 29% — the same string problem as 4.4). Query time barely moved, because only 50,000 rows remain. **But the estimate was wrong by 18×, and that error propagates upward through every join.** In a real three- or five-table join, that is exactly how you end up on the wrong join order.

One more measured detail: after `TRUNCATE`, `row_count` shows **1** (not 0) and `stat_collection_time` is unchanged. So `row_count = 1` means neither "the table has one row" nor "the statistics are fresh".

### 4.7 Argument variants and common forms

```sql
-- one column only
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status');

-- several columns as one comma-separated string
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status, region_id');

-- explicit sampling percentage (third argument)
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status', 100);

-- every column in the table
SELECT ANALYZE_HISTOGRAM('article_lab_stats.fact_join');
```

All four succeed and return `0`. Re-collecting a single column on a one-million-row table took 52 ms; the 100% sample took 67 ms.

### 4.8 Finding tables with no statistics, or stale ones

This is a read-only catalog query with no table scans, safe to run in production:

```sql
SELECT count(*) AS user_tables,
       count(ts.table_oid) AS tables_with_stats,
       count(*) - count(ts.table_oid) AS tables_without_stats
FROM v_catalog.tables t
LEFT JOIN v_monitor.table_statistics ts ON ts.table_oid = t.table_id
WHERE t.is_system_table = false;

SELECT count(*) AS stats_older_than_30d
FROM v_monitor.table_statistics
WHERE stat_collection_time < now() - interval '30 days';
```

On this `testdb`, after dropping the lab schema:

```
 user_tables | tables_with_stats | tables_without_stats
-------------+-------------------+----------------------
         409 |               372 |                   37

 stats_older_than_30d
----------------------
                  211
```

Of 409 user tables, 37 have no statistics at all; of the 372 that do, **211 are more than 30 days old**. That is not a quirk of a test database — it is what "no schedule, no owner" looks like.

### 4.9 Partitioned tables: `ANALYZE_STATISTICS_PARTITION`

The documentation also lists `ANALYZE_STATISTICS_PARTITION`, which collects statistics for a range of partitions only — a good fit for a fact table that gains one partition per day, so you do not rescan history every time. None of the tables on this test box are partitioned, so **I did not test how this function behaves**. I did confirm it exists in this build by deliberately passing a bad table name:

```
ERROR 3457:  Function ANALYZE_STATISTICS_PARTITION(unknown) does not exist ...
HINT:  Candidates are:
ANALYZE_STATISTICS_PARTITION(database.schema.table::varchar, min‑range‑value::varchar,
  max‑range‑value::varchar, column‑list::varchar, percent::float)
```

The error is a type-resolution failure (`unknown` cannot become varchar), not a missing function — the hint prints the full five-argument signature. If your tables use `PARTITION BY`, first confirm with `EXPLAIN` that partition pruning actually happens, then decide whether to move the schedule from whole-table to per-partition.

---

## 5. Performance and Cost

Here is the decision table built from the numbers above:

| Action | Cost | What you get |
|--------|------|--------------|
| `ANALYZE_STATISTICS` on 100K rows | 19 ms | Estimates move from 10K to 100K; `id <= 1000` estimated at 1K (exact) |
| `ANALYZE_STATISTICS` on 1M rows × 6 columns | 67–122 ms | Corrected join order, three-table query −10% |
| `ANALYZE_STATISTICS` on a single column | 52 ms | Fixes selectivity for one predicate |
| Doing nothing | 0 ms | Estimates off by 100×, join order chosen by guesswork |

A few rules of thumb:

- **Always collect after a load.** On this 25.4 instance neither CTAS nor COPY produces a statistics row, and `EXPLAIN` says `NO STATISTICS` outright. This is the cheapest, highest-return fix available.
- **Always collect after a large DELETE.** `row_count` does not come back on its own, and `is_row_count_valid` will not flip to `f`.
- **Do not blindly schedule the whole database.** `ANALYZE_STATISTICS` is a meta-function that takes one table (or a column list on one table). There is no schema-level syntax. Whole-database means a loop, and large and small tables should be scheduled separately.
- **Prioritise columns that appear in joins and WHERE clauses.** A single column costs 52 ms versus 122 ms for the whole table; across 100 tables that is 5 seconds versus 12 seconds.
- **Order of operations:** first check whether the plan shape will change (compare join order in `EXPLAIN`), then decide whether finer statistics are worth paying for. When the shape does not change, collecting statistics only makes the numbers look nicer.

Translating that into scheduling cost on this box: 409 tables at an estimated 50 ms each is about 20 seconds per pass. If you only cover the 30 core tables that join the fact table, a pass takes under 2 seconds. The expensive part is not `ANALYZE_STATISTICS` — it is the extra hash join and re-segmentation that a query runs when statistics are missing, and that burns seconds.

---

## 6. Common Mistakes and Anti-Patterns

### 6.1 Assuming a load creates statistics

Symptom: `EXPLAIN` shows `Rows: 10K (NO STATISTICS)` even though the table clearly has millions of rows.

```
+-SORT [Cost: 3K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
```

I measured both CTAS and COPY into a new table and neither produced a `table_statistics` row. Do not assume statistics exist because the load succeeded; `EXPLAIN` is the only trustworthy check.

### 6.2 Using `is_row_count_valid` to judge freshness

Symptom: after deleting 95% of the data, `row_count` still reads 1,000,000 and `is_row_count_valid` is still `t`.

```
 row_count | is_row_count_valid
-----------+--------------------
   1000000 | t
```

The flag only means "this number was once considered valid", not "it matches the current data volume". Judge freshness from `stat_collection_time`, and **compare against `count(*)`**.

### 6.3 Calling `ANALYZE_STATISTICS` on a schema

Symptom:

```
ERROR 2065:  analyze_statistics: Invalid table/projection/column article_lab_stats
```

The same error code shows up for a mistyped table, a mistyped column, and a schema that does not exist:

```
ERROR 2065:  analyze_statistics: Invalid table/projection/column article_lab_stats.no_such_table
ERROR 2065:  analyze_statistics: Invalid table/projection/column article_lab_stats.t_copy.no_such_col
```

`ANALYZE_STATISTICS` has no schema-level form. For a whole database you have to generate the SQL yourself:

```sql
SELECT 'SELECT ANALYZE_STATISTICS(''' || table_schema || '.' || table_name || ''');'
FROM v_catalog.tables
WHERE is_system_table = false AND table_schema = 'your_schema';
```

### 6.4 Wrong arguments, cryptic error

Symptom:

```
ERROR 3457:  Function ANALYZE_STATISTICS() does not exist, or permission is denied for ANALYZE_STATISTICS()
HINT:  No function matches the given name and argument types. Candidates are:
ANALYZE_STATISTICS(database.schema.table::varchar, column‑list::varchar, percent::float)
```

That hint is actually useful: it tells you 25.4 has a **three-argument** form (sampling percentage), which is rarely mentioned in tutorials.

### 6.5 Believing varchar equality estimates

Symptom: `status = 'CANCELLED'` is estimated at 600K against an actual 50,000. Even with `percent = 100` it stays at 600K.

This is not "statistics were not collected"; it is that string histogram estimation is too coarse. For low-cardinality, heavily skewed varchar predicates, run your own `count(*)`, or validate the estimate against actual execution numbers — do not read `Rows` and stop there.

### 6.6 Treating `DO_TM_TASK('mergeout')` as a universal fix

Symptom:

```
ERROR 6809:  Inappropriate usage of meta-function ("do_tm_task")
```

I first wrote `SELECT DO_TM_TASK('mergeout') FROM (SELECT 1) x;` and got that error. Changing it to `SELECT DO_TM_TASK('mergeout');` worked — but it runs mergeout across the **whole database**. On the test box it ran for more than five minutes without finishing and I aborted it with `CLOSE_SESSION()`. If you use it, scope it to specific objects, and never during peak hours.

---

## 7. Risks and Recommendations

### Safe to do now (read-only, low risk)

1. Search every `EXPLAIN` output for `NO STATISTICS`. Any query that shows it is planning on the assumption of 10,000 rows.
2. Run the two catalog queries from 4.8 and count how many tables have no statistics and how many are older than 30 days. This box reported 37 and 211; your numbers may be worse.
3. Compare `Rows` in `EXPLAIN` against `count(*)` for your important queries. A gap of more than 10× is a strong signal that the statistics are wrong.
4. Remove `DO_TM_TASK` from your "quick fix" list and use `ANALYZE_STATISTICS` instead.

### Requires change (needs a window and approval)

1. **Build a statistics schedule**: daily for fact tables, weekly for dimensions. At 100 ms per million rows, a full-database loop fits comfortably in an off-peak window.
2. **Batch large tables** using the three-argument `percent` form (for example 10) to keep each table under 20 ms — but remember the finding from 4.4: the sampling rate does not fix the string estimation bias.
3. **Validate with `EXPLAIN`**: compare join order and cost after the change. "The job succeeded" is not a result.
4. **Monitor `stat_collection_time`**: turn "not updated in N days" into an alert instead of relying on memory.

---

## 8. References

- Vertica documentation: `ANALYZE_STATISTICS` (including the note that it is an alias of `ANALYZE_HISTOGRAM`)  
  https://docs.rocketsoftware.com/bundle/vertica_25.3/page/sql-reference/functions/performance-analysis-functions/statistics-management-functions/analyze-statistics.html
- Vertica documentation: `v_monitor.table_statistics` column reference  
  https://docs.rocketsoftware.com/bundle/vertica_25.2/page/sql-reference/system-tables/v-monitor-schema/table-statistics.html
- Vertica documentation: Collecting database statistics (`ANALYZE_STATISTICS` and `ANALYZE_STATISTICS_PARTITION`)  
  https://docs.rocketsoftware.com/bundle/vertica_26.3/page/admin/collecting-db-statistics/index.html
- Vertica documentation: `ANALYZE_STATISTICS_PARTITION`  
  https://docs.rocketsoftware.com/bundle/vertica_25.3/page/sql-reference/functions/performance-analysis-functions/statistics-management-functions/analyze-statistics-partition.html
