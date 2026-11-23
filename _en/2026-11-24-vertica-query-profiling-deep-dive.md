---
layout: post_en
title: "Vertica Query Profiling in Depth: From EXPLAIN to PROFILE, Node by Node"
date: 2026-11-24 05:00:00 +0800
categories: vertica performance
tags: [vertica, performance, query-plan, explain, profile, v-monitor, projection]
description: "EXPLAIN tells you the plan and its cost; PROFILE tells you how many rows each operator actually produced, how much CPU it burned, and whether the data came from disk or cache. On a single-node testdb running v25.4.0-0, four queries showed that cost ranking is not time ranking: the cheapest plan at 4K took 59.6 ms while a 12K plan finished in 38.6 ms. Profile data lives in query_profiles, query_plan_profiles, and execution_engine_profiles; the profile_paths and profile_projections tables that older documentation mentions do not exist in this version."
image: /assets/images/og/vertica-query-profiling-deep-dive-en.png
faq:
  - q: "When a query slows down, should I look at EXPLAIN or PROFILE first?"
    a: "Use EXPLAIN to confirm the plan shape, then PROFILE to decide which operator to fix. On the same machine, a 4K-cost ILIKE query took 59.6 ms while a 12K-cost join finished in 38.6 ms. Cost is a relative unit, not milliseconds, so optimizing in cost order makes you fix the wrong query first."
  - q: "Which tables hold PROFILE data?"
    a: "v_monitor.query_profiles has one row per statement and is always populated. v_monitor.query_plan_profiles and v_monitor.execution_engine_profiles only get rows when the session ran enable_profiling('query') and enable_profiling('ee'). Join them on transaction_id and statement_id; the EE table adds path_id and activity_id."
  - q: "Why does the sum of execution time exceed the query's actual duration?"
    a: "Because execution_engine_profiles stores one row per operator instance and those timers overlap. A 5M-row aggregation measured 164.5 ms wall clock but 554.4 ms when summing execution time across 15 activities, about 3.4x. Use query_profiles.query_duration_us for real elapsed time."
  - q: "What does clear_profiling('query') actually clear?"
    a: "It clears the global query profile, not just your session. In testing, v_monitor.query_profiles held 367,371 rows before the call and 2 rows after, those two being the queries I ran afterwards. Use disable_profiling('query') to stop profiling instead."
  - q: "Why does EXPLAIN still estimate 5M rows for an equality lookup that returns one row?"
    a: "That Rows value is the scan volume. The (ROW COUNT ONLY) annotation means the estimate used row counts without column statistics, so filter selectivity never entered the calculation. A Scan for where pos_transaction_number = 12345 estimated 4,999,999 rows and produced 1, yet it still processed all 5,000,000 rows."
---
<!--more-->

---

## 1. The short version

`EXPLAIN` answers "what does the planner intend to do". `PROFILE` answers "what actually happened". On a slow query those two answers regularly disagree, and by default you only see the first one.

Five findings from a measured run on a local database:

1. **Cost is not milliseconds, and cost order is not time order.** The cheapest query in the set (cost `4K`) took 59.6 ms, while a `12K` join query took 38.6 ms. Optimizing in cost order makes you start with the wrong statement.
2. **Profile data in 25.4 lives in three tables, not the two older docs mention.** `v_monitor.profile_paths` and `v_monitor.profile_projections` do not exist in this version; `\d` returns `Did not find any relation`. The real tables are `query_profiles`, `query_plan_profiles`, and `execution_engine_profiles`.
3. **`query_profiles` always records; the other two only exist for sessions that enabled profiling.** A marker query from a plain connection left 1 row in `query_profiles` and 0 rows in both `query_plan_profiles` and `execution_engine_profiles`.
4. **The node with the largest estimation gap is usually not a statistics failure; it is filter selectivity that never entered the estimate.** A `where pos_transaction_number = 12345` Scan estimated 4,999,999 rows and produced 1, but it still had to read all 5,000,000 rows. That is where the 20.9 ms of CPU went.
5. **`clear_profiling('query')` is a global, destructive operation.** It took `query_profiles` from 367,371 rows to 2 and deleted the very profile I was reading.

Every number below comes from an actual run on the local testdb. The whole exercise used `SELECT` and `EXPLAIN` only: no objects created, no settings changed.

## 2. Test environment

| Item | Value |
|---|---|
| Version | Vertica Analytic Database v25.4.0-0 |
| Nodes | Single node `v_testdb_node0001`, Community Edition |
| Database | `testdb` |
| Fact table | `online_sales.online_sales_fact`, 5,000,000 rows |
| Dimensions | `public.customer_dimension` 50,000 rows, `public.product_dimension` 60,000 rows |
| Workload | Read-only, no schemas or tables created |
| Client | `vsql-testdb`; each `-c` is a separate session |

A single node has one advantage here: no inter-node traffic pollutes the profile, so `bytes read from disk` versus `bytes read from cache` is unambiguous. The cost is that nothing below says anything about cross-node data distribution.

## 3. What the three tables hold

Get the data model straight before writing any query against it.

| Table | One row represents | When it has data | Key columns |
|---|---|---|---|
| `v_monitor.query_profiles` | One statement: duration, row count, full SQL, session | Always, even with `GlobalQueryProfiling=0` | `transaction_id`, `statement_id` |
| `v_monitor.query_plan_profiles` | One line of the executed plan with `Cost`, `Rows`, `PATH ID` | Only for sessions with profiling enabled | `transaction_id`, `statement_id`, `path_id`, `path_line_index` |
| `v_monitor.execution_engine_profiles` | One counter of one operator instance | Only for sessions with profiling enabled | `transaction_id`, `statement_id`, `path_id`, `activity_id`, `counter_name` |

Two related tables, `session_profiles` (connection level) and `mergeout_profiles` (mergeout), are out of scope here.

**The two tables that do not exist.** Plenty of older articles and third-party analysis tools assume `v_monitor.profile_paths` and `v_monitor.profile_projections`. On 25.4.0-0 they fail outright:

```
=> \d v_monitor.profile_paths
Did not find any relation.
=> \d v_monitor.profile_projections
Did not find any relation.
```

The equivalent information is in `query_plan_profiles` (plan text plus running_time) and `execution_engine_profiles` (counters). Check the version before shipping a monitoring script, or your scheduler will mail you "relation does not exist" every morning.

**Retention.** The relevant parameters, read from the catalog:

```
=> select parameter_name, current_value from configuration_parameters
   where parameter_name in ('GlobalQueryProfiling','GlobalEEProfiling','SessionProfilingAgeOut','SaveDCEEProfileThresholdUS');
     parameter_name     | current_value
------------------------+---------------
 GlobalQueryProfiling   | 0
 GlobalEEProfiling      | 0
 SaveDCEEProfileThresholdUS | 60000000
 SessionProfilingAgeOut | 30
```

`SaveDCEEProfileThresholdUS` is 60,000,000 microseconds (60 seconds): any query slower than that is profiled even without session profiling. `SessionProfilingAgeOut` is 30 days. On this machine `query_profiles` held 367,371 rows, oldest `2026-11-03 11:23:05`, newest `2026-11-24 05:04:20`, roughly 21 days of history, consistent with the 30-day setting.

## 4. Turning profiling on: a session-scoped function, not a parameter

In 25.4 the switch is a function:

```
=> select enable_profiling('query');
    enable_profiling
-------------------------
 Query Profiling Enabled
(1 row)

=> select enable_profiling('ee');
   enable_profiling
----------------------
 EE Profiling Enabled
(1 row)
```

You need both. `'query'` writes `query_profiles` and `query_plan_profiles`; `'ee'` writes `execution_engine_profiles`. To stop, `disable_profiling('query')` returns `Query Profiling Disabled`.

**The old syntax does not work here.** The widely quoted `ProfileExecution` parameter is gone:

```
=> alter session set ProfileExecution = 1;
ROLLBACK 3638:  Invalid configuration parameter profileexecution; aborting configuration change
```

And do **not** use `clear_profiling('query')` to "turn profiling off". Its output looks harmless (`GLOBAL QUERY Profiling Cleared`), but it empties the global `query_profiles` table:

```
Before: select count(*) from v_monitor.query_profiles;  -> 367371
Run:    select clear_profiling('query');               -> GLOBAL QUERY Profiling Cleared
After:  select count(*) from v_monitor.query_profiles;  -> 2
```

The two surviving rows are the queries I ran after clearing. The command deletes the evidence you were analyzing, with no confirmation. Use `disable_profiling`.

**Verifying the scope.** Open a connection that never calls `enable_profiling`, run a query with a marker, then check all three tables:

```
=> /* marker_no_profiling_1124 */ select count(*) from online_sales.online_sales_fact;
 5000000

=> select count(*) from v_monitor.query_profiles where query like '%marker_no_profiling_1124%';
 1
=> select count(*) from v_monitor.query_plan_profiles
   where transaction_id=45035996285530091 and statement_id=1;
 0
=> select count(*) from v_monitor.execution_engine_profiles
   where transaction_id=45035996285530091 and statement_id=1;
 0
```

The conclusion is clear: **statement-level duration is always recorded, but plan lines and operator counters exist only for the session that enabled profiling**. That means profiling costs other sessions nothing, but it also means you cannot reconstruct the detail after the fact.

## 5. Step by step: four queries

All four ran in sequence inside one profiling-enabled session. Each was explained first for cost, then read back from the profile tables.

### Query 1: low-selectivity dimension filter

```sql
select c.customer_state, count(*) as n, round(sum(f.sales_dollar_amount),2) as rev
from online_sales.online_sales_fact f
join public.customer_dimension c on f.customer_key = c.customer_key
where c.customer_name ilike '%Smith%'
group by 1 order by n desc limit 5;
```

The `EXPLAIN` shape and estimates:

```
+-SELECT  LIMIT 5 [Cost: 12K, Rows: 5 (ROW COUNT ONLY)] (PATH ID: 0)
 | +---> SORT [TOPK] [Cost: 12K, Rows: 10K (ROW COUNT ONLY)] (PATH ID: 1)
 | | +---> GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 12K, Rows: 10K (ROW COUNT ONLY)] (PATH ID: 2)
 | | | +---> JOIN HASH [Cost: 10K, Rows: 5M (ROW COUNT ONLY)] (PATH ID: 3)
 | | | | +-- Outer -> STORAGE ACCESS for f [Cost: 4K, Rows: 5M (ROW COUNT ONLY)] (PATH ID: 4)
 | | | | +-- Inner -> STORAGE ACCESS for c [Cost: 748, Rows: 50K (ROW COUNT ONLY)] (PATH ID: 5)
```

Actual result (first five rows):

```
 customer_state |   n   |   rev
----------------+-------+---------
 CA             | 24182 | 6594827
 TX             | 12992 | 3525134
 MI             |  5948 | 1632859
 FL             |  4192 | 1139807
 IL             |  4032 | 1108211
```

`query_profiles` recorded **38,611 microseconds (38.6 ms)** and 5 returned rows.

### Query 2: full aggregation over 5M rows

```sql
select p.category_description, count(*) as n, round(sum(f.sales_dollar_amount),2) as rev
from online_sales.online_sales_fact f
join public.product_dimension p on f.product_key = p.product_key and f.product_version = p.product_version
group by 1 order by rev desc limit 5;
```

`Cost: 21K`, actual **164,471 microseconds (164.5 ms)**, 4 rows:

```
       category_description       |    n    |    rev
----------------------------------+---------+-----------
 Food                             | 2694455 | 735048814
 Misc                             |  770954 | 209945651
 Medical                          |  767397 | 209302089
 Non-food                         |  767194 | 209186779
```

### Query 3: full-table ILIKE

```sql
select count(*) as n from online_sales.online_sales_fact where transaction_type ilike '%purchase%';
```

`Cost: 4K`, actual **59,585 microseconds (59.6 ms)**, one row: `4750347`.

### Query 4: equality point lookup

```sql
select count(*) as n from online_sales.online_sales_fact where pos_transaction_number = 12345;
```

`Cost: 5K`, actual **7,944 microseconds (7.9 ms)**. That is the warm second run; the first run that day was 121,215 microseconds (121.2 ms), explained in section 9. Result: one row, value `1`.

Side by side:

| # | Query | EXPLAIN cost | PROFILE actual | Rows returned |
|---|---|---|---|---|
| 1 | Smith join plus group by | 12K | 38.6 ms | 5 |
| 2 | Category aggregation over 5M | 21K | 164.5 ms | 4 |
| 3 | Full-table ILIKE count | 4K | 59.6 ms | 1 |
| 4 | Equality point lookup | 5K | 7.9 ms | 1 |

## 6. Reading the profile node by node

### 6.1 Aggregate correctly

`execution_engine_profiles` stores one row per operator instance; one query produced 15 activities. To turn that into "what each operator actually did", use:

```sql
select statement_id, path_id, operator_name,
  max(case when counter_name='estimated rows produced' then counter_value end) as est_rows,
  sum(case when counter_name='rows produced' then counter_value end) as rows_out,
  sum(case when counter_name='rows processed' then counter_value end) as rows_in,
  sum(case when counter_name='execution time (us)' then counter_value end) as cpu_us,
  count(distinct activity_id) as instances
from v_monitor.execution_engine_profiles
where session_id = 'v_testdb_node0001-3431075:0xead44'
  and statement_id in (1,2,3,4)
  and counter_name in ('estimated rows produced','rows produced','rows processed','execution time (us)')
group by 1,2,3
order by 1, cpu_us desc;
```

Three counter names carry most of the meaning:

- `estimated rows produced`: the planner's row estimate for that operator's output. Every instance in the same path carries the same value, so take `max`.
- `rows produced`: rows actually handed upstream. A path can contain several operators (Scan → StorageUnion → ExprEval), so **summing across them double counts**.
- `rows processed` / `rows to process`: rows entering the operator. For a Scan this is the scan volume.

### 6.2 Query 1: estimated 5M, scanned 98,124

```
 statement_id | path_id | operator_name | est_rows | rows_out | rows_in | cpu_us | instances
--------------+---------+---------------+----------+----------+---------+--------+-----------
            1 |       4 | Scan          |  5000000 |    98124 | 5000000 |  76894 |         1
            1 |       5 | Scan          |    49999 |      983 |   50000 |   6960 |         1
            1 |       3 | Join          |  4999801 |    98124 |         |   6578 |         1
            1 |       2 | GroupByPipe   |    10000 |      160 |         |   2606 |         1
            1 |       3 | ExprEval      |  4999801 |    98124 |         |    717 |         1
            1 |       2 | GroupByHash   |    10000 |       32 |      32 |    469 |         2
```

Three conclusions you can read directly:

1. **The fact-table Scan estimated 5,000,000 and produced 98,124.** The difference is not a statistics error; it is a runtime filter. `rows filtered by SIPs expression = 4,901,876`, so 98.0% of rows were discarded at scan time by the SIP built from the hash join, leaving 98,124 to enter the join. That is why a 12K-cost query finishes in 38.6 ms.
2. **The dimension Scan estimated 49,999 and produced 983** (`customer_dimension` has 50,000 rows, of which 983 are Smiths). The estimate applied almost no selectivity: a 50.9x gap.
3. **What `(ROW COUNT ONLY)` means.** Every `Rows:` in the plan carries that annotation, meaning the estimate used row counts without column statistics. For a predicate like `ilike '%Smith%'`, the planner simply cannot see selectivity; only the profile after execution shows it.

The Join's `rows_out` of 98,124 matches the fact Scan, so 983 customers produced roughly 100 orders each: the join neither amplified nor reduced the row count.

### 6.3 Query 2: accurate estimates, slow in join and aggregation

```
 statement_id | path_id | operator_name | est_rows | rows_out | rows_in | cpu_us | instances
--------------+---------+---------------+----------+----------+---------+--------+-----------
            2 |       3 | Join          |  5000000 |  5000000 |         | 291042 |         1
            2 |       2 | GroupByPipe   |    10000 |       20 |         | 125773 |         1
            2 |       4 | Scan          |  5000000 |  5000000 | 5000000 |  90808 |         1
            2 |       3 | ExprEval      |  5000000 |  5000000 |         |  42874 |         1
            2 |       5 | Scan          |    60000 |    60000 |   60000 |   1074 |         1
```

Here the estimates are right: the fact Scan estimated 5,000,000 and read 5,000,000, the product dimension estimated 60,000 and read 60,000. The value of PROFILE on this query is not catching a mistake but allocating time: Join 291.0 ms, GroupByPipe 125.8 ms, Scan 90.8 ms. The cost sits in the hash join and the aggregation, not in the scan.

That is why "are the estimates accurate" should not be the only reason to open a profile. Accurate estimates can still be slow, and you need to know which operator owns the time.

### 6.4 Query 4: estimated 4,999,999, produced 1 row

```
 statement_id | path_id | operator_name | est_rows | rows_out | rows_in | cpu_us | instances
--------------+---------+---------------+----------+----------+---------+--------+-----------
            4 |       2 | Scan          |  4999999 |        1 | 5000000 |  20964 |         1
            4 |       2 | ExprEval      |  4999999 |        1 |         |    126 |         1
```

This is the biggest gap in the whole set: 4,999,999 estimated, 1 row produced, a ratio of roughly five million. Read it carefully:

- `rows_in = 5,000,000`: the Scan really processed five million rows. Nothing was pruned by min/max or valindex (`rows pruned by valindex = 0`).
- `rows_out = 1`: the predicate left one row.
- So this is not "a bad estimate caused a bad plan". The plan genuinely has to read all 5M rows to answer the question, and the 20.9 ms of CPU went into scanning and predicate evaluation, not into producing output.

There are only two ways to improve it: make `pos_transaction_number` a sort key or a segmentation key in a projection (DDL), or accept the cost. Reading this query through cost alone tells you "5K, cheap" and hides the full table scan.

### 6.5 Finding the worst gap in SQL

The reading above can be turned into a query you run daily:

```sql
select statement_id, path_id,
  max(case when counter_name='estimated rows produced' then counter_value end) as est_rows,
  sum(case when counter_name='rows produced' then counter_value end) as rows_out,
  round(max(case when counter_name='estimated rows produced' then counter_value end)::numeric
        / nullif(sum(case when counter_name='rows produced' then counter_value end),0),1) as est_over_act
from v_monitor.execution_engine_profiles
where session_id = current_session()
  and counter_name in ('estimated rows produced','rows produced')
group by 1,2
having sum(case when counter_name='rows produced' then counter_value end) > 0
order by est_over_act desc
limit 10;
```

Two caveats. This query profiles itself, because profiling is still on for the session, so `session_id = current_session()` includes it. And summing `rows_out` across operators inside one path double counts (section 10). Treat the output as a suspect list, then go back and read the operator names.

## 7. Three kinds of estimate-versus-actual gap

| Pattern | Measured example | Estimated | Actual | Cause |
|---|---|---|---|---|
| Runtime filter shrinks the result | Query 1 fact Scan | 5,000,000 | 98,124 | SIP removed 4,901,876 rows (98.0%) |
| Selectivity never entered the estimate | Query 4 Scan | 4,999,999 | 1 | Row counts only, no column statistics |
| Join amplification | Query 1 Join | 4,999,801 | 98,124 | 983 customers times about 100 orders |
| Accurate estimate | Query 2 fact Scan | 5,000,000 | 5,000,000 | Full scan, no predicate |

The first pattern is usually good news (execution is faster than planned). The second is misleading (looks cheap, scans everything). The third depends on whether your join keys are unique. Keep the four apart, or you will start rebuilding statistics every time a ratio looks large.

## 8. Cost order is not time order

Rank the same four queries both ways:

| Rank | By cost | By measured time |
|---|---|---|
| 1 | Query 3 (4K, 59.6 ms) | Query 4 (7.9 ms) |
| 2 | Query 4 (5K, 7.9 ms) | Query 1 (38.6 ms) |
| 3 | Query 1 (12K, 38.6 ms) | Query 3 (59.6 ms) |
| 4 | Query 2 (21K, 164.5 ms) | Query 2 (164.5 ms) |

Only last place agrees. The cheapest query by cost took 59.6 ms, 1.5x the 12K query. The reason is straightforward: cost is a planner-side relative unit weighting CPU, I/O, network, and memory. It cannot see cache residency, the runtime effect of SIPs, or how much parallelism your machine actually delivers.

Practical use:

- **Compare cost between different rewrites of the same query**, to see whether the plan shape changed.
- **Rank which query to tune by `query_duration_us`**, because that is what the user experiences.
- **Pick where to start inside a query by operator time in the profile.**

Get that order wrong and you will "optimize" the cheapest plan that was never slow.

## 9. Cold versus warm cache: 49.9 ms against 11.9 ms

To confirm whether a measurement reflects I/O or CPU, run the same query twice against another 5M-row table, `store.store_sales_fact`:

```
select count(*) from store.store_sales_fact where sales_dollar_amount > 0;
```

| Run | Wall clock | bytes read from disk | bytes read from cache | Scan CPU |
|---|---|---|---|---|
| First | 49.9 ms | 8,915,851 | 262,144 | 87.7 ms |
| Second | 11.9 ms | 0 | 40,452,776 | 29.7 ms |

The second run was fully cached and took 24% of the time. That also explains query 4: the first execution was 121.2 ms and the second was 7.9 ms, with the second profile showing `bytes read from disk = 0` and `bytes read from cache = 40,452,776`.

Two practical rules:

1. **Warm each query once before comparing two SQL statements**, or you are measuring page cache state rather than query efficiency.
2. **`bytes read from disk` is the fastest signal for I/O-bound versus CPU-bound.** A Scan with high CPU and zero disk reads is spending time on predicate evaluation; low CPU with heavy disk reads points at projection design or partition pruning.

## 10. Common mistakes and anti-patterns

### Mistake 1: using `clear_profiling` to stop profiling

```
=> select clear_profiling('query');
 GLOBAL QUERY Profiling Cleared
```

It clears the global `query_profiles` table, not your session's data. Measured: 367,371 rows became 2, with no confirmation and no backup. Use `disable_profiling('query')`.

### Mistake 2: summing `rows produced` across one path

Query 1's dimension scan path has two operators: `Scan` produced 983 rows and `StorageUnion` produced the same 983 rows because it only passes them up. Summing gives 1,966, exactly double. Likewise in query 3, both `Scan` and `ExprEval` report 4,750,347, which sums to 9,500,694.

Read the last operator in the path, or group by `operator_name`. Never add across operators.

### Mistake 3: treating summed `execution time` as query duration

Query 2 measured 164.5 ms wall clock, while the 15 activities summed to 554.4 ms of `execution time (us)`, 3.4x higher. Operators in a pipeline run concurrently, so their timers overlap; on a single node several segments also scan in parallel.

For real elapsed time use `query_profiles.query_duration_us`. Operator timers are for comparing share of work.

### Mistake 4: using `max(statement_id)` to find the query you just ran

Once profiling is on, **your diagnostic queries are profiled too**. In testing, `max(statement_id)` inside a profiling session pointed at the diagnostic query itself, not at the statement under investigation. Print `transaction_id` and `statement_id` first, or match on SQL text:

```sql
select transaction_id, statement_id, query_duration_us
from v_monitor.query_profiles
where session_id = current_session()
  and query like 'select c.customer_state%';
```

### Mistake 5: querying tables that do not exist

```
=> \d v_monitor.profile_paths
Did not find any relation.
```

Older documentation, community scripts, and APM integrations often assume those two tables. On 25.4 use `query_plan_profiles` and `execution_engine_profiles`, whose column names differ as well (`path_line`, `counter_name`, `counter_value`).

### Mistake 6: assuming `ProfileExecution` still works

```
=> alter session set ProfileExecution = 1;
ROLLBACK 3638:  Invalid configuration parameter profileexecution; aborting configuration change
```

On 25.4 the only entry point is `enable_profiling('query'|'ee')`.

## 11. Risk and recommendations

### Safe now (read-only, low risk)

- Enable `enable_profiling('query')` and `enable_profiling('ee')` in the session you are investigating; it affects only you.
- Rank slow statements by `query_profiles.query_duration_us` and take the top 20 before reading details.
- Use `bytes read from disk` versus `bytes read from cache` in `execution_engine_profiles` to classify I/O versus CPU.
- Compare `estimated rows produced` with `rows produced`, but confirm the operator name first so you do not double count.
- Save the queries as views or scripts, and keep `clear_profiling` out of all of them.

### Requires change, a window, or approval

- `GlobalQueryProfiling=1`: cluster-wide, and it increases DC table writes. This machine accumulated 367,371 rows in 21 days with profiling effectively off, so size the disk before enabling it everywhere.
- Lowering `SaveDCEEProfileThresholdUS` (default 60 seconds): more queries leave profiles automatically, with the same capacity concern.
- `clear_profiling`: destructive and unconfirmed. No automation should call it.
- Changing projection design (sort order, segmentation) for pruning: requires DDL and a maintenance window, and should follow observed query patterns rather than a single statement.

## 12. References

- Vertica 25.4.x documentation, Profiling Queries: <https://docs.vertica.com/25.4.x/en/user/profiling/>
- `ENABLE_PROFILING`: <https://docs.vertica.com/25.4.x/en/sql-reference/functions/enable_profiling/>
- `v_monitor.query_profiles`: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor/query-profiles/>
- `v_monitor.execution_engine_profiles`: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor/execution-engine-profiles/>
