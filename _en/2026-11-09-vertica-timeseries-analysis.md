---
layout: post_en
title: "Vertica Time Series Analysis: TIMESERIES Interpolation, Gap Filling, and Event Windows"
date: 2026-11-09 05:00:00 +0800
categories: vertica timeseries analytics
tags: [vertica, timeseries, interpolation, gap-filling, window-functions, analytics]
description: "Vertica's TIMESERIES clause resamples data onto a fixed time grid and interpolates the holes. On 98,784 five-minute readings it produced 103,680 slices - 4,896 of them synthetic - and over a 15-day outage linear interpolation invented a smooth 23.89 to 24.00 ramp. The cost is in slicing and interpolation, not scanning: PROFILE shows Scan at 1,061 us and the Analytical node at 23,446 us."
image: /assets/images/og/vertica-timeseries-analysis-en.png
faq:
  - q: "Does TIMESERIES create rows for timestamps with no data?"
    a: "Yes, it fills the whole grid. 12 sensors over 30 days at 5-minute intervals produced 98,784 raw rows but 103,680 slices, 4,896 of which had no source row. Sensor 9 was offline for 15 days and still emitted 8,640 slices from only 4,320 readings."
  - q: "Is linear interpolation safe across a long gap?"
    a: "No. Sensor 9 jumps from 23.89 at 2026-01-04 23:55 to 24.00 at 2026-01-20 00:00, so all 4,320 slices in between are computed (2026-01-12 12:00 returns 23.9450127285351). The default constant policy just repeats 23.89. Neither is a measurement."
  - q: "Is TIMESERIES faster than GROUP BY TIME_SLICE?"
    a: "No. At 5-minute granularity TIMESERIES took 45.5/40.2/34.7 ms against 15.8/15.2/15.3 ms for GROUP BY TIME_SLICE. PROFILE shows Scan at 1,061 us and the Analytical node (slicing plus interpolation) at 23,446 us. TIMESERIES buys gap filling, not speed."
  - q: "Why can't I use AVG or SUM with TIMESERIES?"
    a: "On 25.4.0-0 it fails immediately with ERROR 2640, and GROUP BY fails with ERROR 3500. Wrap TS_FIRST_VALUE / TS_LAST_VALUE in a subquery first, then aggregate in the outer query."
---

<!--more-->

---

## 1. The Short Version

Vertica's `TIMESERIES` clause exists to solve gaps: it resamples your data onto a fixed time grid and emits a row even for intervals that contain nothing. But it fills the **grid**, not the **values** - and the values it produces may be invented by interpolation.

I took a single-node Vertica 25.4.0-0 instance, loaded 98,784 real rows, and took `TIMESERIES` apart. Six findings:

1. **The output is a time grid, not your rows.** Four raw readings (00:00, 00:05, 00:15, 00:30) become 7 slices. 98,784 readings become 103,680 slices, of which **4,896 (4.7%) contain no source data at all**.
2. **Slice boundaries align to the calendar, not to your first row.** `'1 week'` placed data from 2026-01-01 into a slice starting **2025-12-27, a Saturday**; `'90 seconds'` starts slicing at 00:03:00. The first slice often lands before your first row, and its value is `NULL`.
3. **`TS_FIRST_VALUE` samples the start of the slice, `TS_LAST_VALUE` the end.** The default policy repeats the previous value (constant behaviour); `'linear'` is the only scheme you can name explicitly - and it only accepts integer or float types (`ERROR 5005`). Writing `'CONSTANT'` explicitly is **rejected with `ERROR 3604`**.
4. **Linear interpolation fabricates data across long gaps.** Sensor 9 was offline for 15 days; interpolation drew a smooth ramp from 23.89 to 24.00, with 2026-01-12 12:00 returning 23.9450127285351. **All 4,320 slices in that window are computed.** Constant just repeats 23.89 for 15 days. Neither is a measurement.
5. **The cost is slicing and interpolation, not scanning.** PROFILE: `Scan` processed 98,784 rows in **1,061 us**; the `Analytical` (Timeseries) node emitted 103,680 rows in **23,446 us** and reserved 10,660,392 bytes. `GROUP BY TIME_SLICE()` at the same granularity needs 15.3 ms; `TIMESERIES` needs 40.2 ms.
6. **It refuses ordinary aggregate functions.** `AVG()`, `SUM()`, `COUNT(*)` all fail with `ERROR 2640`, and `GROUP BY` fails with `ERROR 3500`. To average anything you must sample with `TS_*` first and aggregate in an outer query.

This is for anyone building sensor, monitoring, or financial time-series reports - and for anyone who has watched a chart draw a beautiful straight line through a period when the device was switched off. Every number comes from a real run, and the `article_lab_ts` schema was dropped with `DROP SCHEMA ... CASCADE` when the lab finished.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Nodes | 1 (Community Edition) |
| Database / user | `testdb` / `dbadmin` |
| Lab schema | `article_lab_ts` (dropped after the lab) |
| Main table | `readings`: 98,784 rows x 5 columns |
| Shape | 12 sensors x 30 days x one reading every 5 minutes (103,680 theoretical) |
| Range | 2026-01-01 00:00:00 to 2026-01-30 23:55:00 |
| Deliberate gaps | Sensor 5 missing 2026-01-10 to 01-11, sensor 9 missing 2026-01-05 to 01-19 |
| Load | `COPY ... DIRECT`, 3.8 MB CSV |
| Measured on | 2026-11-09 |

Three details that matter for reproducing this:

- **`readings` uses the default sort order `(sensor_id, ts)`**, so `TIMESERIES ... ORDER BY ts` can consume storage order directly. I also built `readings_unsorted` (`ORDER BY hum`) with identical rows as a control.
- **A single-node cluster has one segment**, so this article says nothing about cross-node segmentation or skew.
- **Everything ran inside one `vsql -f` session**, with explicit `COMMIT` after every `INSERT`, so there is no "missing row because autocommit was off" artefact.

---

## 3. How It Works

### 3.1 What `TIMESERIES` looks like in the plan

```sql
=> EXPLAIN SELECT slice_time, sensor_id,
            TS_FIRST_VALUE(temp,'linear') AS f,
            TS_LAST_VALUE(temp,'linear')  AS l
   FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts);

 +-ANALYTICAL [Cost: 1K, Rows: 99K] (PATH ID: 1)
 |  Analytic Timeseries Group
 |   Functions: time_slice(), ts_first_value(), ts_last_value()
 | +---> STORAGE ACCESS for readings [Cost: 741, Rows: 99K] (PATH ID: 2)
 | |      Projection: article_lab_ts.readings_super
```

It is not a function but an **analytical node** (`ANALYTICAL`, internally `Analytic Timeseries Group`) that does three things: cut the `ORDER BY` timestamp column into slices, apply that per `PARTITION BY` group, and evaluate the `TS_*` functions. The GraphViz form spells it out:

```
3[label = "Timeseries:
PartitionBy: sensor_id
OrderBy: ts
Analytical Functions:
  time slice alias
  ts_first_value(readings.temp, false, 'linear') ...
```

Sorted input is the catch. Because `readings_super` is sorted on `(sensor_id, ts)`, the plan feeds the node straight from `StorageMergeStep`. Switch to `readings_unsorted` (`ORDER BY hum`) and an extra step appears:

```
 |   Group Local Resegment: readings_unsorted.sensor_id
 |   Group Sort: readings_unsorted.sensor_id ASC, readings_unsorted.ts ASC NULLS FIRST
 ...
 5[label = "Sort: (keys = A,A,N)" ...]
```

That is a **full extra sort**, and it is the first thing to check when you design projections for time-series queries.

### 3.2 Slice boundaries align to the calendar, not your data

This is the most misunderstood part. `slice_time` is not `first row + n x interval`; it snaps to a calendar or fixed grid:

| Interval | First slice start (measured) | Note |
|---|---|---|
| `'5 minutes'` | 00:00:00 | snaps to whole 5 minutes |
| `'90 seconds'` | 00:03:00 | snaps to the 90-second grid, not to 00:03:30 |
| `'1 hour'` | 00:00:00 | snaps to the hour |
| `'1 day'` | 00:00:00 | snaps to midnight |
| `'1 week'` | **2025-12-27** | snaps to Saturday (next slices 01-03, 01-10, 01-17) |

The weekly case is the trap: the data starts 2026-01-01, but the first slice is labelled 2025-12-27 - **five days before any data exists** - and its value is `NULL`. A weekly report will read that as "no data that week" when in fact you simply have no history there.

There is a second, equally important limit: **the grid only spans the first and last rows.** Truncating sensor 9 to 2026-01-02 yields exactly 288 five-minute slices (one day x 288); Vertica will not pad to 30 days. So you cannot ask `TIMESERIES` to invent future slices or an arbitrary range, and `generate_series()` does not exist in Vertica (`ERROR 3457: Function generate_series(int, int) does not exist`).

### 3.3 `TS_FIRST_VALUE`, `TS_LAST_VALUE`, and the two interpolation policies

Four rows (00:00=20, 00:05=21, 00:15=23, 00:30=26) with 5-minute slices produce 7 output rows:

```sql
=> SELECT slice_time, TS_FIRST_VALUE(temp) f_def, TS_LAST_VALUE(temp) l_def,
          TS_FIRST_VALUE(temp,'linear') f_lin, TS_LAST_VALUE(temp,'linear') l_lin
   FROM article_lab_ts.tiny
   TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
     slice_time      | f_def | l_def | f_lin | l_lin
---------------------+-------+-------+-------+-------
 2026-01-01 00:00:00 |    20 |    20 |    20 |    21
 2026-01-01 00:05:00 |    21 |    21 |    21 |    22
 2026-01-01 00:10:00 |    21 |    21 |    22 |    23
 2026-01-01 00:15:00 |    23 |    23 |    23 |    24
 2026-01-01 00:20:00 |    23 |    23 |    24 |    25
 2026-01-01 00:25:00 |    23 |    23 |    25 |    26
 2026-01-01 00:30:00 |    26 |    26 |    26 |
(7 rows)
```

![TIMESERIES gap filling and interpolation: four raw readings, constant is a step, linear is a ramp]({{ '/assets/images/timeseries-interpolation-shape.svg' | relative_url }}){: loading="lazy" }

How to read it:

- **`TS_FIRST_VALUE` returns the value at the slice start**, `TS_LAST_VALUE` at the slice end (start + interval).
- **The default policy repeats the previous value**: the empty slices at 00:10, 00:20 and 00:25 all carry 21, 23, 23 forward.
- **`'linear'` interpolates between neighbours**: 00:10 becomes (21+23)/2 = 22, 00:20 becomes 24, 00:25 becomes 25.
- The final `l_lin` is `NULL` because 00:35 lies past the last reading - **`TIMESERIES` does not extrapolate**.

Two measured details worth knowing:

- `'linear'` only works on `INTEGER` or `FLOAT`; on `VARCHAR` it fails with `ERROR 5005: Time Series Aggregate Function with interpolation scheme LINEAR may only have an INTEGER or FLOAT type as its first argument`. `NUMERIC(6,2)` works fine.
- Writing `'CONSTANT'` explicitly fails: `ERROR 3604: Interpolation scheme constant for Time Series Aggregate Function ts_first_value is not supported`. **The default behaviour is constant, but you cannot spell it** - on 25.4.0-0, `'linear'` is the only accepted literal (`'nearest'` and `'ignore_null'` are rejected the same way).

---

## 4. Step by Step

### 4.1 Create and load

```sql
=> CREATE TABLE article_lab_ts.readings (
     sensor_id INT NOT NULL,
     ts        TIMESTAMP NOT NULL,
     temp      NUMERIC(6,2),
     hum       NUMERIC(6,2),
     status    VARCHAR(8)
   );
=> COPY article_lab_ts.readings FROM '/tmp/opencode/ts_readings.csv' DELIMITER '|' DIRECT;
 Rows Loaded
-------------
       98784
Time: First fetch (1 row): 168.253 ms.
```

The identical CSV loaded into the `ORDER BY hum` control table took **94.7 ms** - the time-sorted table was **78% slower** (168.3 vs 94.7 ms). I did not isolate the cause (sorting, encoding and compression during load are all mixed together), but the direction matches the partitioning article: **load cost depends on whether input order matches projection sort order, not on row count**.

### 4.2 How much was actually filled in: 4,896 synthetic slices

```sql
=> SELECT COUNT(*) FROM (
     SELECT slice_time, sensor_id, TS_FIRST_VALUE(temp,'linear') f
     FROM article_lab_ts.readings
     TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)) x;
 103680
```

The theoretical maximum is 12 sensors x 8,640 slices = 103,680, so the grid is complete. There are only 98,784 real rows; the difference of **4,896** is entirely synthetic:

| Sensor | Raw rows | Slices out | Synthetic | Reason |
|---|---|---|---|---|
| 1-4, 6-8, 10-12 (ten) | 8,640 | 8,640 | 0 | no gaps |
| 5 | 8,064 | 8,640 | 576 | 2 days missing |
| 9 | 4,320 | 8,640 | **4,320** | 15 days missing |
| Total | 98,784 | 103,680 | **4,896** | |

Switch to 1-minute granularity:

```sql
=> -- 1 minute slices
 518352
```

518,352 = 12 x 43,196 (the last slice is 23:55, hence not 43,200). **Output grew 5.2x while the source data did not change.** A single 30-day table with 12 sensors already emits half a million rows - do that arithmetic before wiring `TIMESERIES` into a BI tool.

### 4.3 Interpolation invents data: a 15-day gap becomes a ramp

Sensor 9's gap runs from 2026-01-05 00:00 to 2026-01-19 23:55. The real readings on either side:

```sql
=> SELECT ts, temp FROM article_lab_ts.readings
   WHERE sensor_id=9 AND ts BETWEEN '2026-01-04 23:40' AND '2026-01-05 00:10'
    OR sensor_id=9 AND ts BETWEEN '2026-01-19 23:45' AND '2026-01-20 00:10' ORDER BY ts;
         ts          | temp
---------------------+-------
 2026-01-04 23:40:00 | 23.56
 2026-01-04 23:45:00 | 23.67
 2026-01-04 23:50:00 | 23.78
 2026-01-04 23:55:00 | 23.89
 2026-01-20 00:00:00 | 24.00
```

Wrapping the `TIMESERIES` output in a subquery and picking a few timestamps:

| slice_time | linear | default (constant) |
|---|---|---|
| 2026-01-04 23:50 | 23.78 | 23.78 |
| 2026-01-05 00:00 | 23.8900254570701 | 23.89 |
| **2026-01-12 12:00** | **23.9450127285351** | 23.89 |
| 2026-01-19 23:55 | 23.9999745429299 | 23.89 |
| 2026-01-20 00:00 | 24 | 24.00 |

`linear` draws a perfect ramp from 23.89 to 24.00 across 15 days, with more than a dozen digits of precision. **All 4,320 slices in that window are computed - not one of them is a measurement.** Point a temperature alert at that view and you get a line that climbs forever and never crosses a threshold.

Constant looks safer, but it lies differently: 23.89 repeated for 15 days reads as "the sensor reported steadily" when in reality "the sensor was offline". **Neither policy can answer "was there data in this window"** - that has to be tracked separately.

### 4.4 Marking which rows are synthetic

The `TIMESERIES` output carries no "real or filled" flag. Join back to the raw counts:

```sql
=> SELECT v.slice_time, ROUND(v.temp_at_start,3) AS t, COALESCE(r.n,0) AS real_rows
   FROM article_lab_ts.v5 v
   LEFT JOIN (SELECT TIME_SLICE(ts,5,'MINUTE') AS b, sensor_id, COUNT(*) AS n
              FROM article_lab_ts.readings GROUP BY 1,2) r
     ON v.slice_time = r.b AND v.sensor_id = r.sensor_id
   WHERE v.sensor_id = 9
     AND v.slice_time IN ('2026-01-04 23:55','2026-01-12 12:00','2026-01-20 00:00');
     slice_time      |   t    | real_rows
---------------------+--------+-----------
 2026-01-04 23:55:00 |  23.89 |         1
 2026-01-12 12:00:00 | 23.945 |         0
 2026-01-20 00:00:00 |     24 |         1
```

`real_rows = 0` marks a filled row. Scanning the whole view, the count of such rows is exactly **4,896**, matching section 4.2. The price is latency: 25.2 ms becomes **97.5 ms** (a hash join plus a 98,784-row aggregate).

Incidentally, `TIMESERIES` **can be wrapped in a view** (`CREATE VIEW` succeeds), and you filter `slice_time` when querying:

```sql
=> CREATE VIEW article_lab_ts.v5 AS
   SELECT slice_time, sensor_id, TS_FIRST_VALUE(temp,'linear') AS temp_at_start,
          TS_LAST_VALUE(temp,'linear') AS temp_at_end
   FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts);
CREATE VIEW
```

But a `WHERE slice_time = ...` outside the view will **not** push down - the view materialises 103,680 rows first. For narrow time ranges on large tables, put the filter *before* `TIMESERIES`, on the raw rows:

```sql
=> -- one day only, and gaps stay gaps
   SELECT slice_time, TS_FIRST_VALUE(temp,'linear') AS f
   FROM article_lab_ts.readings
   WHERE ts >= '2026-01-10' AND ts < '2026-01-11'
   TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
```

### 4.5 A NULL data point breaks carry-forward

```sql
=> SELECT slice_time, TS_FIRST_VALUE(v,'linear') lin, TS_FIRST_VALUE(v) c
   FROM article_lab_ts.nulls TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
     slice_time      | lin |  c
---------------------+-----+------
 2026-02-01 00:00:00 |   1 | 1.00
 2026-02-01 00:05:00 |     |
 2026-02-01 00:10:00 |   5 | 5.00
 2026-02-01 00:15:00 |   6 | 5.00
 2026-02-01 00:20:00 |   7 | 7.00
```

The source rows are `(00:00,1) (00:05,NULL) (00:10,5) (00:20,7)`. The 00:05 slice *does* contain a row, but its value is `NULL`, so **both `lin` and `c` are `NULL`** - it does not fall back to 1.00. Recovery happens at 00:10. In other words: **an empty slice carries the previous value forward; a slice whose value is NULL does not.** The two kinds of "no data" look different in the output, and both are routinely misread downstream as either zero or missing.

### 4.6 SQL you cannot write

Every one of these was hit for real; the messages are verbatim:

```sql
=> SELECT slice_time, AVG(temp) FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
ERROR 2640:  Column "readings.ts" must appear in the GROUP BY clause or be used in an aggregate function

=> SELECT slice_time, sensor_id, COUNT(*) FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts);
ERROR 2640:  Column "readings.sensor_id" must appear in the GROUP BY clause or be used in an aggregate function

=> SELECT slice_time, COUNT(*) FROM ... TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)
   GROUP BY slice_time, sensor_id;
ERROR 3500:  HAVING / GROUP BY not allowed with Time Series query
```

`AVG()`, `SUM()`, `COUNT(*)` and `MIN()` are all rejected, and so is `GROUP BY`. **The select list of a `TIMESERIES` query may only contain `slice_time`, partition columns, and `TS_*` results.** To average, wrap it:

```sql
=> SELECT slice_time, AVG(temp) FROM (
     SELECT slice_time, sensor_id, TS_FIRST_VALUE(temp,'linear') AS temp
     FROM article_lab_ts.readings
     TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)) x
   GROUP BY 1;
```

Three more syntax traps:

```sql
=> ... TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id);
ERROR 4856:  Syntax error at or near ")" at character 136        -- ORDER BY is mandatory

=> ... TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts) WHERE sensor_id = 9;
ERROR 4856:  Syntax error at or near "WHERE" at character 224    -- no WHERE after OVER

=> ... TIMESERIES slice_time AS 5 OVER (ORDER BY ts);
ERROR 5012:  Time slice length must be an interval constant       -- not a plain integer

=> ... TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY sensor_id);
ERROR 4297:  ORDER BY column in timeseries OVER clause must be Timestamp type

=> -- using TS_* outside the TIMESERIES subquery
   SELECT slice_time, TS_FIRST_VALUE(f) FROM (SELECT ... TS_FIRST_VALUE(temp) AS f ... TIMESERIES ...) x;
ERROR 2100:  A query with Time Series Aggregate Function ts_first_value must have a timeseries clause
```

`WHERE` may only appear **before** `TIMESERIES` (filtering raw rows); filtering `slice_time` requires a subquery.

### 4.7 Tumbling slice sampling versus sliding window aggregation

`TIMESERIES` takes one sample per slice boundary; **it does not aggregate over a range**. For "the average of the last 15 minutes" you need a window function:

```sql
=> SELECT slice_time, ROUND(f,3) AS slice_start_temp FROM (
     SELECT sensor_id, slice_time, TS_FIRST_VALUE(temp,'linear') AS f
     FROM article_lab_ts.readings WHERE sensor_id=1 AND ts < '2026-01-01 01:00'
     TIMESERIES slice_time AS '15 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)
   ) x WHERE slice_time IN ('2026-01-01 00:15','2026-01-01 00:30') ORDER BY 1;
     slice_time      | slice_start_temp
 --------------------+------------------
 2026-01-01 00:15:00 |            21.33
 2026-01-01 00:30:00 |            21.65

=> SELECT ts, rolling15, n FROM (
     SELECT ts,
            ROUND(AVG(temp) OVER (ORDER BY ts RANGE BETWEEN INTERVAL '14 minutes' PRECEDING
                                  AND CURRENT ROW),3) AS rolling15,
            COUNT(*) OVER (ORDER BY ts RANGE BETWEEN INTERVAL '14 minutes' PRECEDING
                           AND CURRENT ROW) AS n
     FROM article_lab_ts.readings WHERE sensor_id=1) y
   WHERE ts IN ('2026-01-01 00:15','2026-01-01 00:30') ORDER BY 1;
          ts          | rolling15 | n
 ---------------------+-----------+---
  2026-01-01 00:15:00 |    21.22 | 3
  2026-01-01 00:30:00 |    21.543 | 3
```

The raw data is one reading every 5 minutes from 00:00 (21.00 / 21.11 / 21.22 / 21.33 / 21.44 / 21.54 / 21.65). So 21.33 is "the value at 00:15" while 21.22 is "the mean of the three rows between 00:01 and 00:15". **One is a sample, the other an aggregate; they are not interchangeable.** The `RANGE` frame excludes the 00:00 row (it starts at 00:01), which is why `n = 3` rather than 4.

One gotcha I hit for real: putting `WHERE ts IN (...)` in the same query level makes `n` collapse to **1**, because `WHERE` removes the other rows before the window is computed. **Compute the window in a subquery; filter outside it.**

---

## 5. Performance and Cost

Same data, same 5-minute granularity, three shapes, three runs each (`vsql \timing`, output wrapped in `COUNT(*)` to exclude transfer cost):

| Shape | Three runs (ms) | Median | Rows out |
|---|---|---|---|
| `TIMESERIES` (table sorted `(sensor_id, ts)`) | 45.512 / 40.199 / 34.675 | **40.2** | 103,680 |
| `TIMESERIES` (`ORDER BY hum` table) | 22.637 / 21.314 / 22.530 | **22.5** | 103,680 |
| `GROUP BY TIME_SLICE(ts,5,'MINUTE')` | 15.760 / 15.219 / 15.286 | **15.3** | 98,784 |
| `TIMESERIES` at 1-minute granularity | 60.791 / 61.942 / 59.999 | **60.8** | 518,352 |
| `LAG()` gap detection (no filling) | 20.345 | **20.3** | 2 rows (the gaps) |

`TIMESERIES` is **2.6x slower** than `GROUP BY TIME_SLICE`, because it does two things the other does not: emit 4,896 empty slices and interpolate each one. Conversely, `GROUP BY TIME_SLICE` never fills gaps - it returns only the 98,784 buckets that exist.

`readings_unsorted` came out *faster* than the sorted table (22.5 vs 40.2 ms). That runs against intuition and I would not generalise from it: both tables have a single ROS container, roughly 100 MB of data, and everything sits in page cache, so sort cost is diluted by scan and node setup. What is safe to say: **at this size the sort is not the bottleneck; at larger sizes it will be.** Judge by plan shape (an extra `Sort` node) rather than by these 18 ms.

PROFILE breaks the cost down further (my own lab table, not production):

| Node / shape | rows processed | rows produced | execution time |
|---|---|---|---|
| `Scan` (both shapes) | 98,784 | 98,784 | 1,061 us |
| `Analytical` (TIMESERIES 5 min) | - | 103,680 | **23,446 us** |
| `GroupByPipe` (GROUP BY TIME_SLICE) | - | 98,784 | 10,929 us |
| `Analytical` (TIMESERIES 1 min) | - | 518,352 | **46,315 us** |

**Scanning is 4.5% of the work (1,061 / 23,446); the rest is slicing and interpolation.** The slice node also reserved **10,660,392 bytes (about 10.2 MB)** for 103,680 output rows. That only hurts under concurrency: 20 such queries at once is 200 MB-plus of pool usage.

And narrowing the time range barely shrinks it. Adding `WHERE ts >= '2026-01-10' AND ts < '2026-01-11'` before `TIMESERIES` cut output from 103,680 to **2,880 rows** (36x) and slice execution time from 23,446 us to 398 us, yet `Analytical`'s `memory reserved` only fell from 10,660,392 to **8,467,152 bytes (-21%)**, because the node sizes memory by partition count and batch, not by output rows.

```sql
=> -- one day, 12 partitions
   operator_name | rows_out | exec_us |    mem
 -----------------+----------+---------+-----------
   Scan          |    2,880 |     948 |   102,576
   Analytical    |    2,880 |     398 | 8,467,152
```

So size your resource pools as **concurrent queries x 8-10 MB**, not as a function of output rows.

There is one more plan-level surprise: **the optimizer does not know slicing multiplies rows.**

```sql
=> -- before statistics
   ANALYTICAL [Cost: 2K, Rows: 10K (NO STATISTICS)]
=> SELECT ANALYZE_STATISTICS('article_lab_ts.readings');
=> -- after statistics
   ANALYTICAL [Cost: 1K, Rows: 99K]     -- actual output 103,680
=> -- switch to 1-minute granularity
   ANALYTICAL [Cost: 1K, Rows: 99K]     -- actual output 518,352
```

`EXPLAIN`'s `Rows` is always the **input** row count, whatever the slice interval. `Rows: 99K` versus 103,680 is a 4.7% miss; versus 518,352 it is a 5.2x underestimate. **Do not estimate `TIMESERIES` output from `EXPLAIN`** - compute it: `partitions x (time span / interval)`.

---

## 6. Common Mistakes and Anti-Patterns

### 1. Treating `TIMESERIES` as a speed-up for time aggregation

It is slower (40.2 vs 15.3 ms) and emits more rows. **Its value is gap filling and alignment, not performance.** If you only need 5-minute buckets, `GROUP BY TIME_SLICE(ts,5,'MINUTE')` is enough.

### 2. Using `linear` across long gaps and believing the result

A 15-day outage became a 23.89 to 24.00 ramp, all 4,320 values computed. Practical rule: **if the gap exceeds two or three normal sampling intervals, do not interpolate** - use constant, or keep `NULL` so downstream knows. 25.4 has no parameter to cap the interpolation distance:

```sql
=> SELECT parameter_name, current_value FROM configuration_parameters
   WHERE parameter_name ILIKE '%timeseries%' OR parameter_name ILIKE '%interpolat%';
 parameter_name | current_value
----------------+---------------
(0 rows)
```

So the guard has to live in SQL: filter with the `real_rows` flag from section 4.4, or keep only real values with `WHERE temp IS NOT NULL`.

### 3. Filtering time after `TIMESERIES`

```sql
ERROR 4856:  Syntax error at or near "WHERE" at character 224
```

A `WHERE` after `OVER (...)` is a syntax error; wrapping in a subquery works but **all 103,680 rows are already computed**. Put the condition before `TIMESERIES`, on the raw columns.

### 4. Assuming slices start at your first reading

`'1 week'` places 2026-01-01 data into a slice starting 2025-12-27, whose value is `NULL`. A weekly report filtered on `slice_time = Monday` returns nothing useful.

### 5. Assuming an empty slice behaves like a NULL value

An empty slice carries the previous value forward; **a slice whose value is `NULL` does not** (section 4.5). If your cleaning step rewrites `NULL` to 0, the interpolation result changes completely.

### 6. Running `TIMESERIES` against a projection sorted on something other than time

The plan gains a `Sort` node. At 100 MB the cost does not show up, but this is one of the few wins that needs no SQL change:

```sql
=> EXPLAIN ... | grep -E 'Group Sort|StorageMergeStep|Sort:'
```

### 7. Slicing 5-minute data to 1 second or 1 minute

1-minute slices emit 518,352 rows (5.2x); at 1 second the same scaling gives roughly 31 million (12 x 2,592,000). **Match the granularity to the sampling rate** - anything finer than the source data is pure interpolation with zero information.

---

## 7. Risks and Recommendations

### Safe today (read-only, low risk)

1. **Inventory which queries use `TIMESERIES`** and check the granularity against the sampling rate:

```sql
=> SELECT user_name, LEFT(request, 120) AS req, request_duration_ms
   FROM v_monitor.query_requests
   WHERE request ILIKE '%TIMESERIES%' AND start_timestamp > now() - INTERVAL '7 days'
   ORDER BY request_duration_ms DESC LIMIT 20;
```

2. **Check for a redundant `Sort`** in the plans of hot time-series queries with `EXPLAIN`; a `Group Sort` / `Sort:` node means the projection's sort prefix disagrees with `TIMESERIES ... ORDER BY`.
3. **`PROFILE` the `Analytical` node in a test environment** to read `rows produced` and `memory reserved`, then size the pool for concurrency (here: 103,680 rows, ~10.2 MB).
4. **Flag the synthetic rows** (the `real_rows` pattern in 4.4) so reports can distinguish measurements from interpolation.

### Needs a change window or approval

1. **Re-sort the projection** so `(partition column, timestamp)` is the prefix, removing the `Sort`. This affects load and mergeout; do one table at a time and watch for a week.
2. **Move `TIMESERIES` out of the BI query path into a pre-computed table** (Vertica has no native materialized views; schedule `INSERT ... SELECT`). Every ad-hoc query currently recomputes the slicing.
3. **Drop to 5-minute granularity** and retire the 1-minute reports. The numbers in those reports will change (fewer filled values), so confirm with the consumers first.
4. **Switch long gaps to constant or `NULL`.** This is a semantic change to values; diff old and new output in a non-production environment before rolling it out.

---

## 8. References

- Vertica 25.4 docs: Time series analytics (the `TIMESERIES`, `TS_FIRST_VALUE`, `TS_LAST_VALUE` concepts plus gap filling and interpolation) - <https://docs.vertica.com/25.4.x/en/data-analysis/time-series-analytics/>
- Vertica 25.4 docs: TIMESERIES clause (syntax, `slice_time`, `PARTITION BY`, mandatory `ORDER BY`) - <https://docs.vertica.com/25.4.x/en/sql-reference/statements/select/timeseries-clause/>
- Vertica 25.4 docs: `TS_FIRST_VALUE` (value at the slice start, interpolation argument) - <https://docs.vertica.com/25.4.x/en/sql-reference/functions/aggregate-functions/ts-first-value/>
- Vertica 25.4 docs: `TIME_SLICE` (bucketing without gap filling, the comparison baseline here) - <https://docs.vertica.com/25.4.x/en/sql-reference/functions/time-slice/>
- OpenText/Vertica community: Time Series Gap Filling at Scale (practical discussion of gap filling at volume) - <https://community.opentext.com/data-analytics/b/data-analytics-blog/posts/time-series-gap-filling-at-scale-opentext-analytics-database-vertica-s-built-in-magic>
