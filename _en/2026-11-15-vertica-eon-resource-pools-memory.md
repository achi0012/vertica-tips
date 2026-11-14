---
layout: post_en
title: "Resource Pools and Memory Across Three EON Nodes: Reading 27 Rows of Pool Status"
date: 2026-11-15 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, resource-pool, memory, v-monitor, general-pool, concurrency]
description: "Measured on a local three-node eondb (v25.4.0-0, K-Safe=0, 4 shards): v_monitor.resource_pool_status returns 27 rows — nine built-in pools across three nodes. Every node's pool memory totals exactly 28,594,832 KB (27.27 GiB), general plus metadata always adds up to 24,400,528 KB, and the GENERAL pool's per-query budget is 3,844,520 KB (3.67 GiB). A billion-row aggregation peaked at 3,844,519 KB per node, one kilobyte under budget; running two of them concurrently peaked at 7,688,856 KB, exactly double, and stretched each query from 13.57 s to 31.90 s."
image: /assets/images/og/vertica-eon-resource-pools-memory-en.png
faq:
  - q: "Is memory in EON shared across the cluster or held per node?"
    a: "Per node, and the rows must not be summed. `v_monitor.resource_pool_status` returns 27 rows here; adding all their `memory_size_kb` gives 85,784,496 KB (81.81 GiB), while the real per-node budget is 28,594,832 KB (27.27 GiB). Three nodes times 27.27 GiB is exactly the 81.81 GiB you get by summing."
  - q: "How is the query budget calculated?"
    a: "Verified pool by pool against the documented formula: GENERAL uses `queueing_threshold ÷ planned_concurrency`, i.e. 24,281,180 × 95% ÷ 6 = 3,844,520 KB. `tm` uses `MEMORYSIZE ÷ concurrency` = 3,145,728 ÷ 7 = 449,389 KB; `sysquery` is 1,048,576 ÷ 6 = 174,762 KB; `dbd`, `refresh` and `recovery` have `MEMORYSIZE` 0 and therefore reuse GENERAL's 23,067,120 KB divided by their own concurrency (4, 4 and 8)."
  - q: "Why do the three nodes show different GENERAL sizes but the same total?"
    a: "Because `metadata` needs different amounts on each node and GENERAL is the \"everything that is left\" pool. The three GENERAL sizes are 24,281,180 / 24,294,544 / 24,295,238 KB against metadata at 119,348 / 105,984 / 105,290 KB; the two add up to 24,400,528 KB on every node. Over 13 minutes on one node, GENERAL dropped 9,722 KB while metadata gained exactly 9,722 KB."
  - q: "How much memory can a single query use in EON?"
    a: "By default, one query budget on each participating node. GENERAL's budget is 3,844,520 KB here. A query scanning 1,007,772,484 rows peaked at 3,844,519 / 3,846,635 / 3,846,745 KB across the three nodes; two concurrent copies peaked at 7,688,856 / 7,693,270 / 7,693,490 KB, exactly twice, and each took 31.90 s instead of 13.57 s."
---

<!--more-->

---

## 1. The Short Version

`v_monitor.resource_pool_status` returns 27 rows on this EON cluster. That is not an accident: nine built-in pools times three nodes. Most people open the view and then do one of two wrong things — sum the 27 rows to get "cluster memory", or read `memory_size_kb` as "the most this pool can use". I read all 27 rows on a local three-node eondb (v25.4.0-0, K-Safe=0, four shards) and verified them with a query that scans a billion rows. Seven findings:

1. **The 27 rows do not add up to anything useful.** All of them together give `85,784,496` KB (81.81 GiB), but each node's pool budget is only `28,594,832` KB (27.27 GiB) — exactly one third. Memory in EON is per node, not shared.
2. **All three nodes have identical totals and different composition.** The total is `28,594,832` KB everywhere, but `general` is `24,281,180` / `24,294,544` / `24,295,238` and `metadata` is `119,348` / `105,984` / `105,290`. Added together they come to `24,400,528` KB on every node.
3. **GENERAL is the "everything that is left" pool, so it moves.** Over 13 minutes on one node, GENERAL lost `9,722` KB while metadata gained exactly `9,722` KB. A changing GENERAL size is not a configuration change.
4. **The query budget is a starting allocation, not a ceiling.** GENERAL's `query_budget_kb` is `3,844,520` (3.67 GiB); the moment a query starts it acquires the whole budget, which is why `memory_inuse_kb` hugs that number instead of reflecting actual page touches.
5. **All eight pools that have a budget match the documented formula.** GENERAL: `24,281,180 × 95% ÷ 6 = 3,844,520`. `tm`: `3,145,728 ÷ 7 = 449,389`. `sysquery`: `1,048,576 ÷ 6 = 174,762`. `jvm`: `2,097,152 × 95% ÷ 6 = 332,049`. `dbd` and `refresh`: `23,067,120 ÷ 4 = 5,766,780`. `recovery`: `23,067,120 ÷ 8 = 2,883,390`. Only `metadata` and `blobdata` have no budget (NULL).
6. **Measured queries run right at the budget.** A `GROUP BY` over `1,007,772,484` rows finished in 13.57 s while GENERAL peaked at `3,844,519` / `3,846,635` / `3,846,745` KB — within 1 KB of each node's budget. Two concurrent copies peaked at `7,688,856` / `7,693,270` / `7,693,490` KB, exactly double, but each query went from 13.57 s to 31.90 s.
7. **An Enterprise database on a host with the same 30.71 GiB lands on the same total.** `testdb` (single node, same version, `32,969,703,424` bytes of RAM) also totals `28,594,832` KB across nine pools, but the split differs: GENERAL runs at concurrency 4 and its per-query budget is `6,076,547` KB (5.80 GiB), 1.58× the EON figure.

This is for three audiences: the DBA who summed 27 rows during a health check and concluded the cluster has 81 GiB of RAM; the one who sees a query that only needs a few hundred megabytes yet reports 3.8 GB in `memory_inuse_kb` and suspects a leak; and the one who has to answer "how is memory divided on each EON node?" with real numbers. **Every number labelled "measured" comes from the 2026-11-15 run; everything was read-only — nothing on the cluster was changed.**

---

## 2. Test Environment

| Item | Value |
|------|-----|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Database | `eondb` (EON mode) |
| Nodes | 3 (`v_eondb_node0001/0002/0003`, all `UP`) |
| Shards | 4 (1 replica + 3 segment), K-Safe = 0 |
| Communal storage | `s3://verticadb/eondb` |
| Host | `Intel(R) Core(TM) i5-9400 @ 2.90GHz`, 1 socket / 6 cores, `32,969,703,424` bytes of RAM (30.71 GiB) |
| Resource pools | 9, every one with `is_internal = true`; this cluster has no user-defined pools |
| Control | `testdb` on the same host (Enterprise mode, single node, same v25.4.0-0) |
| Access | `eondb` via `vertica_python` (ports 1533/2533/3533 all point at the same cluster), `testdb` via `vsql-testdb`; `SELECT` only |

The three EON hosts sit on different IPs (`10.89.10.11`, `.12`, `.13`), and all three report the same CPU and memory. That is the precondition for "all three nodes have the same pool total"; on a cluster with mixed hardware, redo the arithmetic.

---

## 3. The Mechanism: Two Layers of Accounting

Reading this view only requires two layers:

| Layer | Question it answers | Column | Value on node0001 |
|-------|--------------------|--------|-------------------|
| node → pool | How is this machine's memory split? | `memory_size_kb` | nine pools totalling `28,594,832` KB |
| pool → query | What does one query get? | `query_budget_kb` | GENERAL `3,844,520` KB |
| queue watermark | Where does waiting start? | `queueing_threshold_kb` | GENERAL `23,067,120` KB (= size × 95%) |
| hard limit | Where does borrowing stop? | `max_memory_size_kb` | GENERAL `24,281,180` KB; `tm` `27,546,256` KB |

In one sentence: **`memory_size_kb` is how much the pool was allocated, `query_budget_kb` is how much each of its queries gets, and `queueing_threshold_kb` is where new requests start queuing.** Three numbers, all called memory, answering three different questions.

![How one EON node's memory is divided: of 30.71 GiB on the host, 27.27 GiB goes into resource pools (general 23.16, tm 3.00, sysquery 1.00, metadata 0.11) and 3.44 GiB stays outside them; 95% of general's 23.16 GiB, 22.00 GiB, is the queueing limit and equals six query budgets of 3.67 GiB]({{ '/assets/images/eon-memory-pool-budget.svg' | relative_url }}){: loading="lazy" }

Every number in that diagram comes from this run: `28,594,832` KB, `24,400,528` KB, `3,844,520` KB, `23,067,120` KB, and the host's `32,969,703,424` bytes.

### 3.1 What the nine built-in pools do

| Pool | `memory_size_kb` here | Role (documentation) | What was measured |
|------|----------------------|----------------------|-------------------|
| `general` | 24,281,180 | every query not assigned to a pool | concurrency 6, budget 3.67 GiB |
| `tm` | 3,145,728 | Tuple Mover (mergeout / moveout) | concurrency 7, budget 449,389 KB |
| `sysquery` | 1,048,576 | system table and monitoring queries | concurrency 6, budget 174,762 KB; 48–56 MB resident when observed |
| `metadata` | 119,348 | catalog and metadata cache | concurrency 1, no budget |
| `jvm` | 0 | Java VM (UDFs, ML) | 2 GiB cap, budget 332,049 KB |
| `dbd` | 0 | background daemon work | borrows GENERAL, budget 5,766,780 KB |
| `refresh` | 0 | projection refresh | borrows GENERAL, budget 5,766,780 KB |
| `recovery` | 0 | node recovery | borrows GENERAL, budget 2,883,390 KB |
| `blobdata` | 0 | in-memory blobs for ML | no budget; spills when over |

The five pools with `memory_size_kb = 0` (jvm, dbd, refresh, recovery, blobdata) do not lack memory — they simply reserve none and borrow from GENERAL. That matters during a health check: **a zero is not a broken pool.**

---

## 4. Step by Step: Reading All 27 Rows

### 4.1 What the 27 rows look like

```sql
=> SELECT node_name, pool_name, memory_size_kb, memory_inuse_kb,
          planned_concurrency, query_budget_kb
   FROM v_monitor.resource_pool_status
   ORDER BY node_name, pool_name;

node_name        | pool_name | memory_size_kb | memory_inuse_kb | pc | query_budget_kb
-----------------+-----------+----------------+-----------------+----+----------------
v_eondb_node0001 | blobdata  | 0              | 0               |  2 |
v_eondb_node0001 | dbd       | 0              | 0               |  4 | 5,766,780
v_eondb_node0001 | general   | 24,281,180     | 0               |  6 | 3,844,520
v_eondb_node0001 | jvm       | 0              | 0               |  6 | 332,049
v_eondb_node0001 | metadata  | 119,348        | 0               |  1 |
v_eondb_node0001 | recovery  | 0              | 0               |  8 | 2,883,390
v_eondb_node0001 | refresh   | 0              | 0               |  4 | 5,766,780
v_eondb_node0001 | sysquery  | 1,048,576      | 56,063          |  6 | 174,762
v_eondb_node0001 | tm        | 3,145,728      | 0               |  7 | 449,389
v_eondb_node0002 | blobdata  | 0              | 0               |  2 |
v_eondb_node0002 | dbd       | 0              | 0               |  4 | 5,769,954
v_eondb_node0002 | general   | 24,294,544     | 0               |  6 | 3,846,636
v_eondb_node0002 | jvm       | 0              | 0               |  6 | 332,049
v_eondb_node0002 | metadata  | 105,984        | 0               |  1 |
v_eondb_node0002 | recovery  | 0              | 0               |  8 | 2,884,977
v_eondb_node0002 | refresh   | 0              | 0               |  4 | 5,769,954
v_eondb_node0002 | sysquery  | 1,048,576      | 48,036          |  6 | 174,762
v_eondb_node0002 | tm        | 3,145,728      | 0               |  7 | 449,389
v_eondb_node0003 | blobdata  | 0              | 0               |  2 |
v_eondb_node0003 | dbd       | 0              | 0               |  4 | 5,770,119
v_eondb_node0003 | general   | 24,295,238     | 0               |  6 | 3,846,746
v_eondb_node0003 | jvm       | 0              | 0               |  6 | 332,049
v_eondb_node0003 | metadata  | 105,290        | 0               |  1 |
v_eondb_node0003 | recovery  | 0              | 0               |  8 | 2,885,059
v_eondb_node0003 | refresh   | 0              | 0               |  4 | 5,770,119
v_eondb_node0003 | sysquery  | 1,048,576      | 48,036          |  6 | 174,762
v_eondb_node0003 | tm        | 3,145,728      | 0               |  7 | 449,389
(27 rows)
```

Three shapes jump out:

- **`tm` and `sysquery` are identical on all three nodes** (3,145,728 / 1,048,576): they have an explicit `MEMORYSIZE`.
- **`general` and `metadata` differ per node**: neither has a fixed size — both are computed.
- **`dbd`, `refresh` and `recovery` budgets also differ per node** (5,766,780 / 5,769,954 / 5,770,119), because they borrow 95% of *their own node's* GENERAL, and GENERAL differs.

### 4.2 Never sum the 27 rows

```sql
=> SELECT count(*) AS rows_, sum(memory_size_kb) AS sum_kb
   FROM v_monitor.resource_pool_status;

rows_ |  sum_kb
------+-----------
   27 | 85,784,496
```

85,784,496 KB is 81.81 GiB. The machine has 30.71 GiB of RAM. 81.81 GiB is three nodes at 27.27 GiB each. **The only correct use of that number is "pool budgets across all three nodes", never "how much memory the cluster has".**

The right way is to group by node:

```sql
=> SELECT node_name, sum(memory_size_kb) AS pools_kb,
          sum(CASE WHEN pool_name IN ('general','metadata') THEN memory_size_kb ELSE 0 END) AS gen_md_kb
   FROM v_monitor.resource_pool_status GROUP BY 1 ORDER BY 1;

node_name        | pools_kb   | gen_md_kb
-----------------+------------+-----------
v_eondb_node0001 | 28,594,832 | 24,400,528
v_eondb_node0002 | 28,594,832 | 24,400,528
v_eondb_node0003 | 28,594,832 | 24,400,528
```

Two numbers worth remembering: **28,594,832 KB per node (27.27 GiB)**, which is 88.81% of the host's 30.71 GiB, and **24,400,528 KB (23.27 GiB) for GENERAL plus metadata**, identical on all three nodes.

### 4.3 GENERAL and metadata share one bucket

Three snapshots 12 seconds apart, three nodes:

```sql
=> SELECT node_name, 
          max(CASE WHEN pool_name='general' THEN memory_size_kb END) AS general_kb,
          max(CASE WHEN pool_name='metadata' THEN memory_size_kb END) AS metadata_kb,
          max(CASE WHEN pool_name='general' THEN memory_size_kb END)
        + max(CASE WHEN pool_name='metadata' THEN memory_size_kb END) AS sum_kb
   FROM v_monitor.resource_pool_status GROUP BY 1 ORDER BY 1;

node_name        | general_kb | metadata_kb | sum_kb
-----------------+------------+-------------+-----------
v_eondb_node0001 | 24,281,180 |     119,348 | 24,400,528
v_eondb_node0002 | 24,294,544 |     105,984 | 24,400,528
v_eondb_node0003 | 24,295,238 |     105,290 | 24,400,528
```

All three snapshots returned `sum_kb = 24,400,528` with not a kilobyte of movement in either component. Stretch the window and it becomes obvious that the pair is alive: at the start of this run (05:05) node0001 had GENERAL at `24,290,902` and metadata at `109,626`; halfway through (05:23) those were `24,281,180` and `119,348`. **GENERAL lost 9,722 KB and metadata gained exactly 9,722 KB.**

That is what "everything that is left" means: metadata (catalog, system table cache) takes what it needs first, and GENERAL gets the remainder. Consequently:

- Different GENERAL sizes across nodes are normal, not configuration drift.
- A fatter catalog (more projections, heavy DDL) directly shrinks GENERAL and therefore every query budget.
- When comparing nodes, compare `general + metadata`, not GENERAL alone.

### 4.4 Verifying every query budget

The documentation is explicit (`Query budgeting`): GENERAL's budget is `queuing threshold ÷ PLANNEDCONCURRENCY`, with the queuing threshold at 95% of `MAXMEMORYSIZE`; a pool with `MEMORYSIZE = 0` and no `MAXMEMORYSIZE` uses **GENERAL's queuing threshold** divided by its own concurrency; a pool with an explicit `MEMORYSIZE` uses `MEMORYSIZE ÷ PLANNEDCONCURRENCY`. Running all nine pools through it on node0001:

| Pool | memory_size_kb | Concurrency | query_budget_kb | Arithmetic | Match? |
|------|---------------:|------------:|----------------:|------------|--------|
| general | 24,281,180 | 6 | 3,844,520 | 24,281,180 × 0.95 ÷ 6 = 3,844,520.3 | ✅ |
| tm | 3,145,728 | 7 | 449,389 | 3,145,728 ÷ 7 = 449,389.7 | ✅ |
| sysquery | 1,048,576 | 6 | 174,762 | 1,048,576 ÷ 6 = 174,762.7 | ✅ |
| jvm | 0 | 6 | 332,049 | 2,097,152 × 0.95 ÷ 6 = 332,049.1 | ✅ |
| dbd | 0 | 4 | 5,766,780 | 23,067,120 ÷ 4 = 5,766,780 | ✅ |
| refresh | 0 | 4 | 5,766,780 | 23,067,120 ÷ 4 = 5,766,780 | ✅ |
| recovery | 0 | 8 | 2,883,390 | 23,067,120 ÷ 8 = 2,883,390 | ✅ |
| metadata | 119,348 | 1 | NULL | no budget — catalog only | — |
| blobdata | 0 | 2 | NULL | no budget; spills when over | — |

All eight budgeted pools match within a kilobyte of truncation. `23,067,120` is GENERAL's queuing threshold (`24,281,180 × 0.95`), and it is also the base for dbd, refresh and recovery — they borrow from GENERAL's watermark, not from their own.

One check is worth memorising: **budget × concurrency equals the queuing threshold exactly.**

```sql
=> SELECT pool_name, query_budget_kb, planned_concurrency,
          query_budget_kb * planned_concurrency AS product_kb, queueing_threshold_kb
   FROM v_monitor.resource_pool_status
   WHERE node_name='v_eondb_node0001' AND pool_name='general';

pool_name | query_budget_kb | pc | product_kb | queueing_threshold_kb
----------+-----------------+----+------------+-----------------------
general   | 3,844,520       |  6 | 23,067,120 | 23,067,120
```

`3,844,520 × 6 = 23,067,120`, to the kilobyte. In other words, **six queries take GENERAL exactly to its queueing watermark and the seventh starts waiting.** That is also why GENERAL's `planned_concurrency` is 6.

### 4.5 Where the AUTO value of 6 comes from

`v_catalog.resource_pools` stores GENERAL's `plannedconcurrency` as `AUTO`, resolved at runtime to 6. The documented rule for AUTO is the lower of the core count and memory ÷ 2 GB. Here:

- Cores: `v_monitor.host_resources.processor_core_count` = 6 (all three nodes are i5-9400, 6 cores)
- Memory ÷ 2 GB: 23.16 GiB ÷ 2 GiB ≈ 11

`min(6, 11) = 6`, matching the observed `planned_concurrency = 6`. Checks like this pay off: if GENERAL's concurrency ever reads 11, you know the memory grew, not the core count.

### 4.6 Measured: one query consumes one budget

Take the billion-row `mtk.partition_columns` table (`SELECT count(*)` answers from statistics and is useless for memory testing, so use an aggregation that actually scans):

```sql
=> SELECT count(*) AS rows_, count(DISTINCT table_name) AS tables_, sum(row_count) AS ros_rows
   FROM mtk.partition_columns;

     rows_     | tables_ |     ros_rows
---------------+---------+------------------
 1,007,772,484 |   1,056 | 76,946,974,970,101
```

Sampling `resource_pool_status` once a second while it ran, first once and then twice concurrently:

| Scenario | Wall time | GENERAL peak (node1 / node2 / node3) | `running_query_count` |
|----------|----------:|--------------------------------------|----------------------|
| Single | 13.79 s (query itself 13.57 s) | 3,844,519 / 3,846,635 / 3,846,745 KB | 1 |
| Two concurrent | 32.22 s (31.90 s each) | 7,688,856 / 7,693,270 / 7,693,490 KB | 2 |

Two conclusions:

1. **The peak is the budget.** node1's budget at the time was `3,844,429` KB and the peak was `3,844,519` KB (90 KB apart, sampling noise); `v_monitor.query_requests.memory_acquired_mb` recorded `3,754.32` MB, which is `3,844,424` KB — 5 KB under budget. **A query does not grow into memory as needed; it takes a budget up front and spends from it.**
2. **Two concurrent queries take two budgets.** 7,688,856 KB ≈ 2 × 3,844,519 KB, and `running_query_count` reads 2. The pair still only uses 35% of GENERAL's 22.00 GiB watermark, so nothing queued — yet each query took 31.90 s instead of 13.57 s. That is CPU contention (6 cores, `execution_parallelism = AUTO`), not memory.

This also answers "why does my query that only needs a few hundred megabytes report 3.8 GB in `memory_inuse_kb`?" The column is memory *acquired* — the budget itself — not pages touched.

### 4.7 Monitoring queries consume memory too

At the same moment, `sysquery` held `56,063` / `48,036` / `48,036` KB. Every `v_monitor` and `v_catalog` query runs in the sysquery pool, including the health check you are running right now. So when the system "feels slow", look at sysquery's budget (174,762 KB here) before blaming the workload: if it is saturated, your monitoring queries queue behind each other and the database looks wedged.

---

## 5. Enterprise Comparison: Same Total, Different Split

`testdb` on the same host (Enterprise mode, single node, v25.4.0-0, same `32,969,703,424` bytes and 6 cores) also returns nine pools, and the total is again `28,594,832` KB:

```sql
=> SELECT pool_name, memory_size_kb, planned_concurrency, query_budget_kb
   FROM v_monitor.resource_pool_status ORDER BY pool_name;

pool_name | memory_size_kb | pc | query_budget_kb
----------+----------------+----+----------------
blobdata  |              0 |  2 |
dbd       |              0 |  4 |         6076547
general   |      25585464 |  4 |         6076547
jvm       |              0 |  2 |         1992294
metadata  |         149886 |  1 |
recovery  |              0 |  8 |         3038273
refresh   |              0 |  4 |         6076547
sysquery  |        1429741 |  6 |          238290
tm        |        1429741 |  7 |          204248
(9 rows)
```

The same formulas hold in Enterprise: `25,585,464 × 0.95 ÷ 4 = 6,076,547`, `1,429,741 ÷ 7 = 204,248`, `1,429,741 ÷ 6 = 238,290`.

The difference is the split:

| Item | eondb (EON) | testdb (Enterprise) |
|------|-------------|---------------------|
| Pool total | 28,594,832 KB | 28,594,832 KB |
| GENERAL | 24,281,180–24,295,238 KB, concurrency 6 | 25,585,464 KB, concurrency 4 |
| GENERAL per-query budget | 3,844,520 KB (3.67 GiB) | 6,076,547 KB (5.80 GiB) |
| tm | 3,145,728 KB (3 GiB) | 1,429,741 KB (5%) |
| sysquery | 1,048,576 KB (1 GiB) | 1,429,741 KB (5%) |
| jvm cap | 2 GiB | 4 GiB |

Two honest caveats. First, **this is not an EON-versus-Enterprise mode difference** — it is a configuration difference. `testdb`'s GENERAL concurrency is explicitly 4 (`v_catalog.resource_pools.plannedconcurrency = 4`, where the default is `AUTO`), and its `tm`/`sysquery` use 5% instead of 3G/1G. Second, **the totals match because the hosts have the same memory**; the total is set by the machine, the split by configuration. Copying Enterprise pool settings into EON will not break anything, but the per-query budget moves with them — compute both numbers before a migration rather than explaining a slowdown afterwards.

---

## 6. History and Retention: Two Tables Decide How Far Back You Can See

`v_monitor.memory_usage` gives node-level utilisation, one row per node per minute:

```sql
=> SELECT date_trunc('day', start_time) AS d, node_name,
          round(avg(average_memory_usage_percent),2) AS avg_pct,
          max(average_memory_usage_percent) AS max_pct
   FROM v_monitor.memory_usage GROUP BY 1,2 ORDER BY 1,2;

          d          |     node_name     | avg_pct | max_pct
---------------------+-------------------+---------+---------
 2026-11-09 00:00:00 | v_eondb_node0001  |   41.11 |   67.87
 2026-11-10 00:00:00 | v_eondb_node0001  |   45.01 |   47.43
 2026-11-11 00:00:00 | v_eondb_node0001  |   44.42 |   46.26
 2026-11-12 00:00:00 | v_eondb_node0001  |   44.44 |   91.91
 2026-11-13 00:00:00 | v_eondb_node0001  |   44.71 |   50.43
 2026-11-14 00:00:00 | v_eondb_node0001  |   30.76 |   51.54
```

(node0002 and node0003 return identical values, omitted here.) The five-day average is 42.02%; the peak of 91.91% occurred at `2026-11-12 15:49`, with all three nodes crossing it in the same minute — the signature of one large query hitting all three nodes, not of one node leaking. The 11-14 average of 30.76% reflects a stretch with no workload.

Retention differs, and that decides what you can investigate:

- `v_monitor.memory_usage`: `8,014` rows per node here, starting `2026-11-09 07:28` — about five days.
- `v_internal.dc_resource_pool_status`: `87,498` rows covering roughly one hour (`2026-11-14 20:01`–`21:01`). Use it for "which pool spiked in the last ten minutes"; use `memory_usage` for "was there memory pressure last week".

`v_monitor.resource_pool_move` is empty here — no query was ever moved between pools (no `CASCADE TO`, no `RUNTIMECAP` firing).

---

## 7. Common Mistakes and Anti-Patterns

**1. Summing `memory_size_kb` across the 27 rows.** That yields 85,784,496 KB (81.81 GiB), 2.66× the 30.71 GiB of physical memory. Somebody will reconcile that against the monitoring system and spend the afternoon hunting 50 GiB that never existed. Group by `node_name`.

**2. Expecting all three nodes to show identical pool sizes.** Only the totals match (28,594,832 KB). GENERAL differs by 14,058 KB across nodes and metadata differs by the same 14,058 KB in the opposite direction. Comparing GENERAL alone looks like drift and is not.

**3. Treating `memory_size_kb = 0` pools as unused.** jvm, dbd, refresh, recovery and blobdata all read 0, yet their budgets are 332,049 / 5,766,780 / 5,766,780 / 2,883,390 KB, borrowed from GENERAL. Dropping zeros from a health report hides recovery, which can legitimately take 2.8 GB.

**4. Summing `v_monitor.resource_acquisitions.memory_inuse_kb`.** The table holds `2,974` rows accumulated since `2026-11-09`; adding them gives `3,316,095,420` KB (3.09 TB). It is a history of acquisitions, not a live figure. Filter `WHERE is_executing` for the present (three rows, 524,286 KB, at the time of measurement).

**5. Reading `memory_inuse_kb` as "memory actually used".** A billion-row scan peaked at 3,844,519 KB in GENERAL, but that is the acquired budget, not touched pages. Conversely, a value pinned to the budget does not mean the query is wasteful.

**6. Using `memory_usage.average_memory_usage_percent` as pool utilisation.** It is *node* memory utilisation, OS cache included. The 42% average sits on top of a 28,594,832 KB pool allocation; the two are not interchangeable.

**7. Believing you can raise the query budget with `MAXMEMORYSIZE` alone.** The documentation warns that lowering `MAXMEMORYSIZE` lowers the budget with it; to keep the budget, change `MAXMEMORYSIZE` and `PLANNEDCONCURRENCY` together. Here GENERAL's budget is `size × 95% ÷ 6`, so either change moves it.

---

## 8. Risks and Recommendations

**Safe today (read-only, low risk)**

- Track three numbers in every health check: the per-node pool total (28,594,832 KB here), `general + metadata` (24,400,528 KB), and whether GENERAL's `query_budget_kb × planned_concurrency` still equals `queueing_threshold_kb`. Any change deserves a look.
- Always filter `resource_acquisitions` with `WHERE is_executing`; use `memory_usage` for history and `dc_resource_pool_status` for the last hour.
- Include sysquery in monitoring. It normally sits at 48–56 MB; approaching its 174,762 KB budget means your monitoring queries are competing with themselves.
- Compare nodes using `general + metadata`, never GENERAL alone.

**Requires change control**

- Adjusting GENERAL's `PLANNEDCONCURRENCY`: dropping 6 to 4 here would raise the per-query budget from 3.67 GiB to roughly 5.5 GiB at the cost of a lower concurrency ceiling (the fifth query starts queuing). `testdb` already carries that change and its numbers are a reference.
- Creating user-defined pools with `CASCADE TO`: all nine pools here are built-in, so every workload currently shares GENERAL.
- Changing `MEMORYSIZE`: it moves GENERAL's remainder and every pool that borrows from it, so the whole allocation has to be recomputed rather than one number adjusted.

---

## 9. References

- Vertica 25.4 documentation: Query budgeting — https://docs.vertica.com/25.4.x/en/admin/managing-db/managing-workloads/resource-pool-architecture/query-budgeting/
- Vertica 25.4 documentation: Built-in pools — https://docs.vertica.com/25.4.x/en/admin/managing-db/managing-workloads/resource-pool-architecture/built-pools/
- Vertica 25.4 documentation: `RESOURCE_POOL_STATUS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/resource-pool-status/
- Vertica 25.4 documentation: Monitoring resource pools — https://docs.vertica.com/25.4.x/en/admin/monitoring/monitoring-resource-pools/
- Vertica 25.4 documentation: `ALTER RESOURCE POOL` — https://docs.vertica.com/25.4.x/en/sql-reference/statements/alter-resource-pool/
- Vertica knowledge base: Best Practices for Managing Resource Pools — https://vertica.com/kb/BestPracticesforManagingResourcePools/Content/BestPractices/BestPracticesforManagingResourcePools.htm

> Note: `docs.vertica.com` currently 302-redirects to `docs.rocketsoftware.com/bundle/vertica_25.4/` with identical paths; all links above returned 200 when checked on 2026-11-15.
