---
layout: post_en
title: "Tuple Mover Behaviour in EON: When It Moves Data, Where It Moves It, and How It Differs from Enterprise"
date: 2026-11-22 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, tuple-mover, ros, moveout, mergeout, depot, 25.4]
description: "In EON the Tuple Mover has no WOS to flush, so its real moveout is uploading depot ROS containers to communal storage. Read-only measurements on a three-node eondb cluster (v25.4.0-0, K-Safe=0, four shards) show only eight mergeouts in ten days, against 2,394 depot uploads totalling 77.0 GB and 501 evictions totalling 65.0 GB. The mergeout planner and executor were the same node in all eight cases."
image: /assets/images/og/vertica-eon-tuple-mover-en.png
faq:
  - q: "In EON mode, where does the Tuple Mover's moveout actually send data?"
    a: "To communal storage, not from WOS to ROS. On this cluster v_monitor.depot_uploads holds 2,394 uploads across 385 plans and 77,005,174,001 bytes (77.0 GB), with source paths under /data/depot/eondb/v_eondb_node000N_depot/ and destinations under s3://verticadb/eondb/. Every row is COMPLETED. Because EON writes straight into depot ROS, the Enterprise WOS-to-ROS moveout does not exist here."
  - q: "How often does mergeout run in EON?"
    a: "MergeOutInterval=600 only sets how often the Tuple Mover checks for work. This cluster ran eight mergeouts in ten days, leaving 16 rows in v_monitor.tuple_mover_operations (eight Start and eight Complete), all on v_internal_tables.license_consumption_metrics_super, merging 32 to 41 ROS containers per run in 93 to 514 ms."
  - q: "Does lowering MoveOutInterval make EON uploads faster?"
    a: "No. get_config_parameter() still returns 300 for MoveOutInterval and 1800 for MoveOutMaxAgeTime, but neither appears in the configuration_parameters view, which returns zero rows on both this EON cluster and the Enterprise test database. Both parameters have been deprecated since version 10.0 with no effect. Upload behaviour is governed by UseDepotForWrites, MaxDepotSizePercent (80 here), and DepotOperationsForQuery (ALL here)."
  - q: "Are 1,395 ROS containers too many?"
    a: "Not by itself. Thirty of those containers hold 1,074,221,351 rows, 97.7% of all rows, while 1,166 hold fewer than 1,000 rows each and 108,533 rows in total. The extremes are mtk.partition_columns, with 1.008 billion rows in three containers, and ADA.LOWIDOC_V6_super, with 307,000 rows in 789 containers that all carry start_epoch = 23, the mark of a single load."
  - q: "Which node executes a mergeout in EON?"
    a: "The primary subscriber to the shard plans it and then picks an executor. On this cluster the executor_node_name matched the planning node in all eight mergeouts (8/8), split as two on node0001, three on node0002, and three on node0003."
---

<!--more-->

---

## 1. The Short Version

Bring an Enterprise mental model to EON's Tuple Mover and the first question is usually how often moveout runs. On EON that question points the wrong way.

Enterprise moveout turns WOS data into ROS. EON has no such layer: a load writes straight into ROS containers in the depot, so the Tuple Mover has no WOS to flush. What it actually moves is the next stage: **uploading depot ROS containers to communal storage**, and that work is recorded in `v_monitor.depot_uploads`, not in `v_monitor.tuple_mover_operations`.

Twelve days of measurements on this three-node cluster:

| Action | Monitoring source | Measured |
|--------|-------------------|----------|
| mergeout (combine small ROS in the depot) | `v_monitor.tuple_mover_operations` | 8 runs, 16 rows |
| moveout (depot to communal upload) | `v_monitor.depot_uploads` | 2,394 uploads, 385 plans, 77.0 GB |
| eviction (free depot space) | `v_monitor.depot_evictions` | 501 rows, 65.0 GB, all `DROP OBJECT` |

Put the three side by side and the point is obvious: **mergeout ran eight times, uploads ran 2,394 times.** In EON the Tuple Mover's workload lives in the upload path; mergeout exists mostly to keep those uploads from fragmenting into thousands of tiny objects.

The second conclusion concerns the anxiety about container counts. This cluster has 1,395 containers, but 30 of them hold 97.7% of the rows and another 1,166 hold fewer than 1,000 rows each. Container count is not a health metric.

![EON write path: loads land in depot ROS containers first, then the Tuple Mover uploads them to communal storage. Numbers are twelve days of measurements from this cluster.]({{ '/assets/images/eon-tuple-mover-flow.svg' | relative_url }}){: loading="lazy" }

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Database | `eondb` (EON), three nodes, K-Safe = 0 |
| Version | `Vertica Analytic Database v25.4.0-0` |
| Shards | 4 (`replica` plus `segment0001` through `segment0003`) |
| Communal storage | `s3://verticadb/eondb` (`sharing_type = COMMUNAL`) |
| Depot | `/data/depot/eondb/v_eondb_node0001_depot` and two peers |
| Data volume | 1,395 containers, 221 projections, 1,099,951,823 rows |
| TM resource pool | 3,145,728 KB (3 GiB) per node, 27,546,256 KB max, `PLANNEDCONCURRENCY` 7 |
| Control group | Enterprise `testdb` on the same version: 1,493 containers, 9,203 mergeouts |
| Run time | From 2026-11-22 05:00 (Asia/Taipei) |

**Read-only throughout.** Only `SELECT` statements were issued. No `CREATE`, `DROP`, `INSERT`, or `ALTER`; no calls to `mergeout()`, `moveout()`, or `DO_TM_TASK()`; no configuration parameter was changed. This article deliberately does not manufacture a moveout by touching the cluster. Every number comes from work the cluster did on its own or from records it had already kept.

One time-zone note: the cluster's `now()` returns UTC while the publication date is Taipei time, eight hours ahead. All `11-xx` timestamps below are UTC.

---

## 3. How It Works: EON Splits the Tuple Mover into Planning and Execution

The documentation describes EON mergeout as two phases. Planning is always done by the primary subscriber to the shard involved, and as part of planning that subscriber picks a node to execute the plan. It chooses the node that holds the most ROS containers for the mergeout in its depot.

| Aspect | Enterprise | EON |
|--------|-----------|-----|
| Who plans mergeout | Each node plans for the data it stores | The shard's primary subscriber plans centrally |
| Who executes | The planning node itself | May be delegated, choosing the node with the most ROS in depot |
| What moveout moves | WOS to ROS (historical, deprecated) | Depot ROS to communal storage |
| Consequence of no WOS | MoveOutInterval controls the flush | Loads write to the depot directly, `UseDepotForWrites=1` |
| Primary monitoring views | `tuple_mover_operations` | `tuple_mover_operations` plus `depot_uploads` and `depot_evictions` |
| TM memory | One pool per node | One pool per node; setting it to 0 disables mergeout for that subcluster |

This cluster is a single primary subcluster with three nodes, four shards, and K-Safe=0. `v_catalog.node_subscriptions` returns six rows: the `replica` shard has three subscriptions with only node0001 marked `is_primary = True`, and the three segment shards have one each, so every node carries exactly two subscriptions, all `ACTIVE`. At this size the rule "pick the node with the most ROS" almost always resolves to the planning node, which is why all eight mergeouts below show planner equal to executor.

### The three Tuple Mover actions and the view that records each

| Action | What it does | Enterprise target | EON target | Monitoring |
|--------|--------------|-------------------|-----------|------------|
| mergeout | Combine small ROS, purging along the way | Local ROS | ROS in the depot | `tuple_mover_operations` |
| moveout | Push buffered writes to durable storage | WOS to ROS (deprecated) | Depot ROS to communal | `depot_uploads` |
| reclaim / evict | Discard copies no longer needed | Local disk | Depot cache | `depot_evictions` |

This table is the most useful page in the article. **When someone asks whether EON moveout is running, the view to check is `depot_uploads`, not `tuple_mover_operations`.** Ask instead whether ROS are being combined and `tuple_mover_operations` is the right view. Two questions, two views; mixing them produces the false conclusion that the Tuple Mover is idle.

`v_catalog.storage_locations` confirms the layering:

```sql
=> SELECT location_path, location_usage, sharing_type
   FROM v_catalog.storage_locations ORDER BY 2, 1;
                location_path                | location_usage | sharing_type
---------------------------------------------+----------------+--------------
 s3://verticadb/eondb                        | DATA           | COMMUNAL
 /data/eondb/v_eondb_node0001_data           | DATA,TEMP      | NONE
 /data/depot/eondb/v_eondb_node0001_depot    | DEPOT          | NONE
 ... (two more nodes of each kind)
```

There is one `COMMUNAL` location and three `DEPOT` locations. The authoritative copy lives in communal storage; the depot is a rebuildable cache and write buffer. That single fact defines what the Tuple Mover moves: **a copy in a cache, never the only copy of anything.** An upload can therefore be retried, and an eviction cannot lose data. All 501 evictions here are `DROP OBJECT`, not capacity pressure.

---

## 4. Measured: Eight Mergeouts in Ten Days

`v_monitor.tuple_mover_operations` holds only 16 rows on this cluster, exactly eight operations with a Start and a Complete row each:

```sql
=> SELECT operation_name, operation_status, count(*)
   FROM v_monitor.tuple_mover_operations GROUP BY 1,2 ORDER BY 1,2;
 operation_name | operation_status | count
----------------+------------------+-------
 Mergeout       | Complete         |     8
 Mergeout       | Start            |     8
```

No `DVMergeout`, no `Analyze Statistics`, and no operation named `Moveout`. Joining the Start rows to their Complete rows on `transaction_id` gives the full record:

| Start (UTC) | Node | ROS before | Bytes | Duration | Executor |
|---|---|---|---|---|---|
| 11-12 18:09:09 | node0003 | 32 | 6,830 | 251 ms | node0003 |
| 11-12 18:09:09 | node0001 | 32 | 6,788 | 241 ms | node0001 |
| 11-13 16:29:09 | node0002 | 32 | 6,848 | 514 ms | node0002 |
| 11-17 08:40:35 | node0002 | 35 | 8,188 | 237 ms | node0002 |
| 11-17 23:40:35 | node0003 | 39 | 9,065 | 93 ms | node0003 |
| 11-18 15:00:35 | node0001 | 41 | 9,404 | 126 ms | node0001 |
| 11-21 03:20:36 | node0002 | 32 | 8,354 | 155 ms | node0002 |
| 11-21 13:00:36 | node0003 | 32 | 8,482 | 179 ms | node0003 |

Three observations:

1. **All eight hit the same table:** `v_internal_tables.license_consumption_metrics_super`. The licensing service keeps writing small batches into that internal table, so it always has small ROS to combine. No user-schema table was touched by mergeout during these ten days.
2. **Few mergeouts do not mean an idle Tuple Mover.** The same window saw 2,394 depot uploads. On EON the work is in uploading; mergeout exists so those uploads do not fragment.
3. **Planner equalled executor in all eight rows.** With one subcluster and one primary subscriber per shard, there was nowhere else to delegate. On clusters with multiple subclusters this column starts to diverge, and `executor_node_name` becomes the first piece of evidence when asking why a mergeout ran somewhere unexpected.

### Observation window: nothing moved for four minutes

The same aggregate was queried at 05:02 and 05:05 CST:

```sql
=> SELECT count(*) containers, count(DISTINCT projection_id) projs,
          sum(total_row_count) rows, max(end_epoch) max_ep
   FROM v_monitor.storage_containers;
 containers | projs |    rows     | max_ep
------------+-------+-------------+--------
       1395 |   221 | 1099951823  |    405
```

Both runs are identical: 1,395 containers, 221 projections, 1,099,951,823 rows, maximum epoch 405. `tuple_mover_operations` stayed at 16 rows and `depot_uploads` at 2,394. **That is the expected result, not a broken Tuple Mover.** With no new data arriving there is nothing to merge and nothing to upload. Verifying that the Tuple Mover is working requires writes, and this article stays read-only, so it cites historical records instead of arguing from four minutes of stasis.

---

## 5. What Moveout Really Is: Depot Uploads, Not a WOS Flush

`v_monitor.depot_uploads` is the per-file ledger of depot files moving to communal storage. This cluster has accumulated 2,394 rows:

```sql
=> SELECT count(*) n, count(DISTINCT plan_id) plans,
          min(submit_time) first_t, max(submit_time) last_t,
          sum(file_size_bytes) bytes,
          round(avg(datediff('ms', start_time, end_time))) avg_ms,
          max(datediff('ms', start_time, end_time)) max_ms
   FROM v_monitor.depot_uploads;
  n   | plans |          first_t           |           last_t           |   bytes    | avg_ms |  max_ms
------+-------+----------------------------+----------------------------+------------+--------+---------
 2394 |   385 | 2026-11-09 07:31:24.154+00 | 2026-11-21 18:40:36.047+00 | 77005174001 |  12989 | 3526658
```

A representative row:

```sql
=> SELECT node_name, file_size_bytes, status, source_file, destination_file
   FROM v_monitor.depot_uploads ORDER BY file_size_bytes DESC LIMIT 1;
 node_name      | file_size_bytes |  status   | source_file                                   | destination_file
----------------+-----------------+-----------+-----------------------------------------------+-------------------------------------------
 v_eondb_node0001 |     14876718208 | COMPLETED | /data/depot/eondb/..._depot/599/0200...dfbb_0.gt | s3://verticadb/eondb/f15/0200...dfbb_0.gt
```

The source is a local depot path and the destination is S3. **That is EON moveout**, and no `MoveOutInterval` takes part in it. Every one of the 2,394 rows is `COMPLETED`; there is no `FAILED` and nothing `IN PROGRESS`.

### The three nodes share the work evenly

```sql
=> SELECT node_name, count(*) n, sum(file_size_bytes) bytes, max(submit_time) last_t
   FROM v_monitor.depot_uploads GROUP BY 1 ORDER BY 1;
    node_name    |  n  |   bytes    |           last_t
-----------------+-----+------------+----------------------------
 v_eondb_node0001 | 800 | 25480360679 | 2026-11-21 18:40:36.047+00
 v_eondb_node0002 | 806 | 25824521839 | 2026-11-21 18:40:36.045+00
 v_eondb_node0003 | 788 | 25700291483 | 2026-11-21 18:40:36.045+00
```

800, 806, and 788 uploads for 25.48, 25.82, and 25.70 GB, a spread of 1.34% in bytes. That matches the shard layout, with one segment shard per node and the replica shard on all three.

### Twelve days is one burst plus daily housekeeping

```sql
=> SELECT submit_time::date d, count(*) n, sum(file_size_bytes) bytes
   FROM v_monitor.depot_uploads GROUP BY 1 ORDER BY 1;
     d     |  n   |    bytes
-----------+------+-------------
 2026-11-09 | 2108 | 77004788079
 2026-11-10 |   27 |       35341
 2026-11-11 |   25 |       32754
 ...
 2026-11-20 |   19 |       24922
 2026-11-21 |   22 |       33319
```

November 9 alone accounts for 2,108 uploads and 77.0 GB: cluster creation and the first load. Every day after that shows 19 to 30 uploads and roughly 30 KB, all produced by mergeout on that internal licensing table. **This is the heartbeat of an idle EON cluster: a couple of dozen tiny files a day.**

### Upload latency against file size

```sql
=> SELECT CASE WHEN file_size_bytes < 1048576 THEN '<1MB'
               WHEN file_size_bytes < 10485760 THEN '1-10MB' ELSE '>=10MB' END bucket,
          count(*) n, round(avg(datediff('ms', start_time, end_time)),1) avg_ms,
          round(avg(file_size_bytes)/1024.0/1024.0,1) avg_mb
   FROM v_monitor.depot_uploads GROUP BY 1 ORDER BY 1;
 bucket |  n   | avg_ms  | avg_mb
--------+------+---------+--------
 <1MB   | 1627 |   135.9 |    0.1
 1-10MB |  592 |  1954.2 |    2.9
 >=10MB |  175 | 169817.5|  409.3
```

Uploads under 1 MB average 0.14 seconds; those above 10 MB average 169.8 seconds for 409 MB. The three largest, about 14.8 GB each, took 3,525, 3,521, and 3,527 seconds, which works out to roughly **4.0 MiB/s per node**. That figure is unattractive, but it comes from the first load on November 9, when three nodes pushed 44.5 GB at once. It is not a steady-state throughput number; steady state is 30 KB a day.

Counts and bytes are wildly out of proportion: uploads under 1 MB are **68.0% of the rows but 0.11% of the bytes**, while the 175 uploads above 10 MB are 7.3% of the rows and 97.5% of the bytes. For S3 this means the overwhelming majority of requests carry a few kilobytes. That is the argument for keeping mergeout: it makes files bigger and directly reduces object counts and PUT requests. The corollary is that a cluster producing tens of thousands of sub-megabyte uploads a day has a load-pattern problem, not a Tuple Mover tuning problem; look at batch sizes before touching parameters.

### Evictions: 501, all from drops

```sql
=> SELECT reason, count(*) n, sum(file_size_bytes) bytes
   FROM v_monitor.depot_evictions GROUP BY 1;
    reason    |  n  |   bytes
--------------+-----+------------
 DROP OBJECT  | 501 | 64962959066
```

501 evictions for 65.0 GB, with a single reason: `DROP OBJECT`. That is 84.4% of the uploaded volume being dropped again after upload, the signature of a test cluster that repeatedly creates, loads, and drops tables. Capacity pressure would show different reasons and would coincide with the `MaxDepotSizePercent=80` threshold.

---

## 6. Container Count Is Not a Health Metric: Three Counterexamples

```sql
=> SELECT CASE WHEN total_row_count <= 1000 THEN '<=1K'
               WHEN total_row_count <= 100000 THEN '1K-100K'
               WHEN total_row_count <= 1000000 THEN '100K-1M' ELSE '>1M' END b,
          count(*) containers, sum(total_row_count) rows
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 2 DESC;
    b     | containers |    rows
----------+------------+-------------
 <=1K     |       1166 |      108533
 1K-100K  |        146 |     3111610
 100K-1M  |         53 |    22510329
 >1M      |         30 |  1074221351
```

1,166 containers hold 108,533 rows, 0.01% of the total, while 30 containers hold 1.074 billion rows, 97.7%. Three concrete cases:

**Counterexample one: `mtk.partition_columns` has 1.008 billion rows in three containers.** One per node, 8,449 MB. That is what a large file looks like.

**Counterexample two: `ADA.LOWIDOC_V6_super` has 307,000 rows in 789 containers.**

```sql
=> SELECT node_name, count(*) containers, sum(total_row_count) rows,
          round(avg(total_row_count)) avg_rows, min(start_epoch) min_ep, max(end_epoch) max_ep
   FROM v_monitor.storage_containers WHERE projection_name='LOWIDOC_V6_super' GROUP BY 1;
    node_name    | containers |  rows   | avg_rows | min_ep | max_ep
-----------------+------------+---------+----------+--------+--------
 v_eondb_node0001 |        262 |  306794 |     1171 |     23 |     23
 v_eondb_node0002 |        265 |  307559 |     1161 |     23 |     23
 v_eondb_node0003 |        262 |  306518 |     1170 |     23 |     23
```

All 789 containers carry `start_epoch = 23` and average 1,167 rows. This is one load's worth of files, not the residue of 789 loads, and `v_monitor.partitions` holds exactly 789 rows for it, one per container. The Tuple Mover never touched it in ten days. The reasonable explanation is that mergeout targets small ROS accumulated across epochs, while containers produced by a single load and already split along the sort key are not merge candidates. That last sentence is inference from this observation plus the documentation, not an explicit doc statement, so operationally judge these tables by whether the count is still growing rather than by the absolute number.

**Counterexample three: `license_consumption_metrics_super` has 30 containers and 417 rows.** This is the one table the Tuple Mover actually maintains:

```sql
=> SELECT node_name, count(*) containers, sum(total_row_count) rows,
          min(start_epoch) a, max(end_epoch) b
   FROM v_monitor.storage_containers
   WHERE projection_name='license_consumption_metrics_super' GROUP BY 1;
    node_name    | containers | rows | a  |  b
-----------------+------------+------+----+-----
 v_eondb_node0001 |         22 |  123 | 10 | 405
 v_eondb_node0002 |          6 |  148 | 10 | 405
 v_eondb_node0003 |          2 |  146 | 10 | 405
```

Compare that with section 4: the mergeout at 11-21 13:00 on node0003 combined 32 ROS, and node0003 now holds two containers. Mergeout works; it simply maintains a 417-row internal table.

**The rule:** rank by `rows / containers` and inspect the `start_epoch` distribution instead of running `count(*)`. The cluster-wide average here is 788,496 rows per container, but thirty large containers pull that average up and it describes no typical projection at all.

---

## 7. Parameters: What Works and What Is Left Over

```sql
=> SELECT parameter_name, current_value, default_value FROM configuration_parameters
   WHERE parameter_name IN ('MergeOutInterval','MaxROSPerStratum','MaxMrgOutROSSizeMB',
         'PurgeMergeoutPercent','MergeoutBlockParameter','ActivePartitionCount',
         'MaxDepotSizePercent','UseDepotForWrites','DepotOperationsForQuery',
         'DelayForDeletes','CatalogSyncInterval') ORDER BY 1;
```

| Parameter | Value here | Default | Meaning in EON |
|-----------|-----------|---------|----------------|
| `MergeOutInterval` | 600 | 600 | How often, in seconds, the Tuple Mover checks for mergeout work |
| `MaxROSPerStratum` | 32 | 32 | A stratum triggers a merge once it exceeds 32 ROS |
| `MaxMrgOutROSSizeMB` | -1 | -1 | Size cap on a merged ROS; -1 means no limit |
| `PurgeMergeoutPercent` | 20 | 20 | Purge during mergeout once deleted rows exceed 20% |
| `MergeoutBlockParameter` | 100 | 100 | Block DDL and DML when the mergeout queue exceeds 100 |
| `ActivePartitionCount` | 1 | 1 | Partitioned tables keep one active partition |
| `MaxDepotSizePercent` | 80 | 80 | The depot may use up to 80% of disk |
| `UseDepotForWrites` | 1 | 1 | Write to depot then upload; 0 writes to communal directly |
| `DepotOperationsForQuery` | ALL | ALL | Queries may fetch into and evict from the depot |
| `DelayForDeletes` | 0 | 0 | Delete from communal immediately after the depot delete |
| `CatalogSyncInterval` | 5 minutes | 5 minutes | Metadata sync interval to communal storage |

The Enterprise `testdb` control group for comparison: `MergeOutInterval` lowered to 300 from the 600 default and `MergeoutBlockParameter` raised to 5000 from 100, with 9,203 mergeouts in 18 days averaging 3.8 ROS each. **Enterprise runs many small mergeouts; EON runs few large ones, averaging 34.4 ROS.** Same Tuple Mover, completely different rhythm. Nothing was tuned on the EON side; every value is the default.

### So what about MoveOutInterval?

```sql
=> SELECT get_config_parameter('MoveOutInterval') moi, get_config_parameter('MoveOutMaxAgeTime') mat;
 moi | mat
-----+------
 300 | 1800

=> SELECT count(*) FROM configuration_parameters WHERE parameter_name ILIKE 'MoveOut%';
 count
-------
     0
```

`get_config_parameter()` returns 300 and 1800, yet the `configuration_parameters` view contains neither row. The Enterprise `testdb` also returns zero rows for the same query. Both parameters belong to the WOS era and have been marked deprecated since version 10.0, with documentation stating that setting them has no effect. **Tuning them on EON does not speed up uploads; it only creates the impression that something was tuned.**

### Three steps to decide whether the Tuple Mover is healthy

1. **Confirm writes are arriving:** `SELECT count(*) FROM v_monitor.depot_uploads WHERE submit_time > now() - interval '1 day';` If that returns 0, check the load schedule before blaming the Tuple Mover. Normal here is 19 to 30 uploads a day.
2. **Check the last mergeout:** `SELECT max(operation_start_timestamp) FROM v_monitor.tuple_mover_operations;` The last one here was 11-21 13:00:36 UTC. One or two a day is normal on an idle cluster, because only the internal licensing table grows small ROS.
3. **Only then look at containers:** rank by `rows / ros`, not by `count(*)`. Aggregated per table, exactly one table here exceeds 32 ROS (`ADA.LOWIDOC_V6_super` with 789); the other 220 projections sit inside the `MaxROSPerStratum=32` design envelope.

The order matters. Starting from container counts turns single-load fragmentation into a false diagnosis of "mergeout is not running".

---

## 8. Common Mistakes and Anti-Patterns

1. **Assuming EON does not run mergeout.** Eight runs in ten days, each combining 32 to 41 ROS, all on `license_consumption_metrics`. It looks idle only because you are querying the wrong view, or because no table has accumulated small ROS across epochs.
2. **Treating `MoveOutInterval` and `MoveOutMaxAgeTime` as upload switches.** Neither appears in `configuration_parameters`, and `set_config_parameter` does not complain, so the change looks applied and reads back a value while doing nothing. Watch `depot_uploads` instead.
3. **Using total container count as a health signal.** Of 1,395 containers, 1,166 hold fewer than 1,000 rows while 30 hold 97.7% of the data. A total is dominated by single-load fragments.
4. **Expecting mergeout to clean up fragments from one load.** All 789 containers of `LOWIDOC_V6_super` carry epoch 23 and the Tuple Mover never touched them in ten days. Reducing them requires a reload or a projection rebuild, not patience.
5. **Assuming `v_monitor.strata` shows layering.** Both `v_monitor.strata` and `v_monitor.strata_structures` return zero rows here, and `ROS_container_count` does not exist in the latter. Use `v_monitor.projection_storage.ros_count` per projection instead.
6. **Setting the TM pool to 0 on a primary subcluster.** The documentation forbids it: primary subclusters must be able to execute mergeout. Do it and nobody merges, with no alert, only a slowly growing ROS count.

---

## 9. Risks and Recommendations

### Safe now, read-only

1. **Track three numbers daily:** the upload count and byte delta from `depot_uploads`, the reason mix from `depot_evictions`, and the most recent timestamp in `tuple_mover_operations`. Only when all three stall is something wrong; container count alone says nothing.
2. **Find fragmentation by rows per ROS:**

   ```sql
   SELECT anchor_table_schema||'.'||anchor_table_name AS tbl,
          sum(ros_count) AS ros, sum(row_count) AS rows,
          round(sum(row_count)::numeric / nullif(sum(ros_count),0)) AS rows_per_ros
   FROM v_monitor.projection_storage
   GROUP BY 1 HAVING sum(ros_count) > 32
   ORDER BY rows_per_ros ASC LIMIT 20;
   ```

   Exactly one projection here qualifies, `ADA.LOWIDOC_V6_super`, at about 1,167 rows per ROS.
3. **Check `executor_node_name`.** With multiple subclusters it immediately shows whether mergeout is piling up on one node.

### Requiring change windows and approvals

1. **Enlarge the TM pool.** It is 3 GiB here with a 26.3 GiB ceiling (3,145,728 of 27,546,256 KB). Only worth doing when mergeout queues trip `MergeoutBlockParameter`.
2. **Set `UseDepotForWrites` to 0.** Loads would go straight to communal storage, bypassing the depot. Fine for small batches, poor for latency-sensitive loads; measure communal write latency first.
3. **Adjust `DelayForDeletes`.** At 0, communal files disappear as soon as the depot copy is deleted. Raising it buys short-term recovery options at ongoing communal storage cost.
4. **Rebuild `LOWIDOC_V6_super` to collapse 789 containers.** This needs DDL, an offline window, and a decision about retention across its 267 partitions.

---

## 10. References

- Vertica 25.4.x documentation, Tuple mover (mergeout planning and execution in EON): <https://docs.vertica.com/25.4.x/en/admin/managing-db/tuple-mover/>
- Vertica 25.4.x documentation, Managing the tuple mover (TM resource pool, active partitions): <https://docs.vertica.com/25.4.x/en/admin/managing-db/tuple-mover/managing-tuple-mover/>
- Vertica 25.4.x documentation, Eon Mode parameters (`UseDepotForWrites`, `DepotOperationsForQuery`, `DelayForDeletes`): <https://docs.vertica.com/25.4.x/en/sql-reference/config-parameters/eon-parameters>
- Vertica 25.4.x documentation, `DEPOT_UPLOADS` system table: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/depot-uploads/>
- Vertica 25.4.x documentation, `TUPLE_MOVER_OPERATIONS` system table: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/tuple-mover-operations/>
- Vertica documentation, Deprecated and Removed Functionality (`MoveOutInterval`, `MoveOutMaxAgeTime`, `MoveOutSizePct` have no effect): <https://vertica.com/docs/10.0.x/HTML/Content/Authoring/NewFeatures/_VersionIndependent/DeprecatedandRetiredFunctionality.htm>
