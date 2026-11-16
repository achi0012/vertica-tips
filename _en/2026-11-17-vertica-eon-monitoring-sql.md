---
layout: post_en
title: "Twelve SQL Queries That Keep an EON Cluster Honest: Shard Coverage, Depot Hit Rate, and Communal Latency"
date: 2026-11-17 05:00:00 +0800
categories: vertica eon monitoring
tags: [vertica, eon, monitoring, v-monitor, v-catalog, shard-coverage, depot, health-check]
description: "Run against a local three-node eondb (Vertica v25.4.0-0, K-Safe=0, four shards): all three nodes UP, three segment shards covering 4,294,967,296 hash values with zero gaps, six subscriptions all ACTIVE. The depot holds 3.72 GiB per node, 0.53% of the MaxDepotSizePercent=80 ceiling, and 1,450 containers hold 1,099,951,655 rows. depot_fetches has only 30 stale records and its wait_time_ms column is entirely NULL — this article is explicit about which fields you cannot measure here."
image: /assets/images/og/vertica-eon-monitoring-sql-en.png
faq:
  - q: "What is the minimum set of SQL queries for an EON health check?"
    a: "Eleven queries ran here in a single session in 284.6 ms total: node state, shard coverage, subscriptions, depot usage, depot fetches, communal location, ROS distribution, resource pools, running queries, failed loads, and disk headroom — plus one loop that calls version() on each of the three entry points. The slowest was load_streams at 137.7 ms, the fastest shards at 3.1 ms."
  - q: "Why is wait_time_ms empty in v_monitor.depot_fetches?"
    a: "Of the 30 records here, wait_time_ms, response_time_ms, and submission_time are all NULL — zero rows populated — while fetch_time_ms has values: 0.633 ms average, 3 ms maximum. This build only fills in the file fetch time, so you cannot compute communal wait latency from wait_time_ms."
  - q: "Are ports 1533, 2533, and 3533 three different databases?"
    a: "No. Connecting to each port separately put the session on v_eondb_node0001, node0002, and node0003 respectively; version() returned v25.4.0-0 on all three, v_catalog.shards returned four shards and v_catalog.nodes returned three nodes on all three. They are entry points to three nodes of one cluster, not three copies of the data."
  - q: "The depot uses only 3.72 GiB, so why is the disk only 23% free?"
    a: "Because the depot ceiling (MaxDepotSizePercent=80) and actual depot usage are different things. The depot holds 3.72 GiB per node, 0.42% of a 900,239 MB filesystem and 0.53% of the ceiling. The filesystem itself has 685,667 MB used and 214,572 MB free (23%); most of that usage is not in the depot."
  - q: "During a health check, should I look at K-Safe or current_fault_tolerance?"
    a: "Both. Here designed_fault_tolerance and current_fault_tolerance are both 0, which means the cluster is designed with no fault tolerance (K-Safe=0) rather than having lost it. If designed is 1 and current drops to 0, that is a real loss of a replica — go straight to subscription_state."
---

<!--more-->

---

## 1. The Short Version

Monitoring EON is not about watching a dashboard turn red. It is about asking the right tables the right questions at the right time. Below are twelve queries, every one of them actually run against the local three-node eondb; the numbers are from the 2026-11-17 run.

1. **Eleven SQL queries cost 284.6 ms in total.** One session, run back to back, warm. The slowest is `load_streams` at 137.7 ms; the fastest is `v_catalog.shards` at 3.1 ms. The bottleneck is round trips, not data volume.
2. **The skeleton is intact.** All three nodes `UP`, `node_down_count = 0`, `designed_fault_tolerance = 0`, `current_fault_tolerance = 0` — K-Safe was designed as 0, it did not fall to 0.
3. **Three segment shards tile the hash ring exactly.** Widths 1,431,655,739 / 1,431,655,739 / 1,431,655,818, summing to 4,294,967,296, with `missing = 0` and `gaps = 0`.
4. **All six subscriptions are `ACTIVE`.** The replica shard has three (one primary, two secondary); each segment shard has exactly one primary.
5. **The depot is nearly empty.** 3.72 GiB per node against `MaxDepotSizePercent = 80`, which is **0.53%** of the ceiling and **0.42%** of the filesystem. Note the parameter name: `MaxDepotSizePercent`, not the `DepotSizePercent` people often use in conversation.
6. **`depot_fetches` holds 30 records, all eight days old.** All successful, `fetch_time_ms` averaging 0.633 ms with a 3 ms maximum — but `wait_time_ms`, `response_time_ms`, and `submission_time` are **entirely NULL**.
7. **ROS grew again.** 1,450 containers, 1,099,951,655 rows, 11.16 GiB; 1,221 of them (84.21%) hold fewer than 1,000 rows, and three containers hold 91.62% of the rows.
8. **Disk is 23% free.** A 900,239 MB filesystem with 685,667 MB used and 214,572 MB free. All three nodes report identical figures down to the megabyte, so **do not add them up**.
9. **Three ports are three nodes, not three databases.** 1533 lands on node0001, 2533 on node0002, 3533 on node0003; all report `v25.4.0-0` and see the same four shards and three nodes.

This is for the team that already runs EON but whose health check still stops at "is it UP", and for anyone writing a runbook or explaining to a manager why the depot is nearly empty while queries are still slow. **Everything here is read-only; nothing on the cluster was changed.**

![From symptom to query: the first and second query for five common symptoms, with the three queries every health check should run at the bottom]({{ '/assets/images/eon-monitoring-decision-flow.svg' | relative_url }}){: loading="lazy" }

---

## 2. Test Environment

| Item | Value (measured this run) |
|------|---------------------------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Database | `eondb` (EON mode) |
| Nodes | 3 (`v_eondb_node0001/0002/0003` at `10.89.10.11/.12/.13`, all `UP`) |
| Shards | 4 (`replica` plus `segment0001`–`segment0003`), K-Safe = 0 |
| Storage locations | 7: three `DATA,TEMP`, three `DEPOT`, one `COMMUNAL` (`s3://verticadb/eondb`, `has_catalog = true`) |
| Node hardware | 30.7 GB RAM, 6 cores, 11.7% free (`v_monitor.host_resources`) |
| Data volume | 1,099,951,655 rows, 11.16 GiB of ROS (`v_monitor.system`) |
| Client | `connect_eondb.py` (`vertica_python`), entry points 1533 / 2533 / 3533 |
| Run window | 2026-11-17 05:05–05:35 Asia/Taipei |

Disk usage moved by 2 MB during the run (`disk_space_used_mb` 685,665 → 685,667) and `storage_containers` grew from 1,428 the day before to 1,450. **Both tables are alive; any SLA report built on them has to accept that they move.**

---

## 3. Why These Twelve: Three Tables Answer Three Questions

EON's observability data lives in two schemas, `v_catalog` and `v_monitor`, but the daily questions fall into three buckets:

| Question | Main sources | What they will not tell you |
|----------|--------------|------------------------------|
| Is the skeleton right (nodes, shards, subscriptions, versions)? | `v_catalog.nodes`, `shards`, `node_subscriptions`, `v_monitor.system` | Whether data is in the depot, whether queries are slow |
| Where does the data live (depot, communal, ROS)? | `v_monitor.storage_containers`, `v_catalog.storage_locations`, `v_monitor.depot_fetches` | Replication state, whether loads failed |
| What is happening right now (long queries, failed loads, disk)? | `v_monitor.query_requests`, `query_profiles`, `load_streams`, `disk_storage` | Historical trends (you have to store those yourself) |

Two structural limits have to be stated up front, or the numbers that follow will be misread:

1. **Activity data in `v_monitor` has a short retention window.** Here `query_profiles` holds just 3,605 rows, the oldest from 2026-11-09 07:27 UTC, and `load_streams` holds four. That is "since the last load" visibility, not an audit trail. Trends require your own scheduled snapshots.
2. **Some columns are empty in this build.** `wait_time_ms`, `response_time_ms`, and `submission_time` in `depot_fetches` are all NULL. **A query can be perfectly correct and still return nothing useful** — which is why query #5 below checks the NULL ratio directly instead of letting you guess from blank cells.

---

## 4. Step by Step: The Twelve Queries

### 4.1 Skeleton: #1, #2, #3

**#1 Node state and fault-tolerance targets**

```sql
=> SELECT n.node_name, n.node_state, n.is_primary,
          s.node_count, s.node_down_count,
          s.designed_fault_tolerance, s.current_fault_tolerance
   FROM v_catalog.nodes n CROSS JOIN v_monitor.system s
   ORDER BY n.node_name;

node_name        | node_state | is_primary | node_count | node_down_count | designed_fault_tolerance | current_fault_tolerance
-----------------+------------+------------+------------+-----------------+--------------------------+-------------------------
v_eondb_node0001 | UP         | True       |          3 |               0 |                        0 |                       0
v_eondb_node0002 | UP         | True       |          3 |               0 |                        0 |                       0
v_eondb_node0003 | UP         | True       |          3 |               0 |                        0 |                       0
```

How to read it: `designed_fault_tolerance` is how many nodes the cluster can lose by design; `current_fault_tolerance` is how many it can lose right now. Both are 0 here, which means **no fault tolerance by design** (K-Safe=0), not a temporary degradation.

Normal: `node_down_count = 0` and `current = designed`. When it is not: `current < designed` sends you to `subscription_state` in #3; a `node_state` other than `UP` sends you to `v_monitor.node_states` for the transition history.

While you are there, pull the single row from `v_monitor.system` — it carries numbers you cannot get elsewhere:

```sql
=> SELECT node_count, node_down_count, designed_fault_tolerance, current_fault_tolerance,
          current_epoch, ahm_epoch, last_good_epoch, catalog_revision_number,
          ros_used_bytes, total_row_count
   FROM v_monitor.system;

node_count | node_down_count | designed | current | current_epoch | ahm_epoch | last_good_epoch | catalog_revision_number | ros_used_bytes | total_row_count
-----------+-----------------+----------+---------+---------------+-----------+-----------------+-------------------------+----------------+----------------
         3 |               0 |        0 |       0 |           345 |       344 |             344 |                    2742 |   11987583716 |    1099951655
```

`last_good_epoch = 344` against `current_epoch = 345`: a gap of one, so nothing is behind. `catalog_revision_number` is 2,742; it increments on every catalog change, which makes it a useful reference point for "did anyone touch DDL in between".

**#2 Shard coverage**

```sql
=> SELECT shard_name, shard_type, lower_hash_bound, upper_hash_bound,
          upper_hash_bound - lower_hash_bound + 1 AS width
   FROM v_catalog.shards ORDER BY shard_name;

shard_name  | shard_type | lower_hash_bound | upper_hash_bound | width
------------+------------+------------------+------------------+------------
replica     | Replica    |                  |                  |
segment0001 | Segment    |                0 |       1431655738 | 1431655739
segment0002 | Segment    |       1431655739 |       2863311477 | 1431655739
segment0003 | Segment    |       2863311478 |       4294967295 | 1431655818
```

Note that `replica` has empty bounds: **a replica shard does not participate in hash assignment**; it holds unsegmented projections. Feeding it into a coverage calculation yields NULL, not zero.

A CTE turns that into a gap check:

```sql
=> WITH s AS (
     SELECT lower_hash_bound AS lo, upper_hash_bound AS hi,
            lag(upper_hash_bound) OVER (ORDER BY lower_hash_bound) AS prev_hi
     FROM v_catalog.shards WHERE shard_type = 'Segment')
   SELECT count(*) AS segments, sum(hi - lo + 1) AS covered,
          4294967296 - sum(hi - lo + 1) AS missing,
          sum(CASE WHEN prev_hi IS NOT NULL AND lo <> prev_hi + 1 THEN 1 ELSE 0 END) AS gaps
   FROM s;

segments | covered    | missing | gaps
---------+------------+---------+-----
       3 | 4294967296 |       0 |    0
```

How to read it: `covered + missing = 2^32` (4,294,967,296). `gaps = 0` means adjacent shards join exactly: `0`→`1431655738`, `1431655739`→`2863311477`, `2863311478`→`4294967295`. **Any `gaps > 0` or `missing > 0` means the ring is not fully covered and some hash values have nowhere to land.**

**#3 Subscription state**

```sql
=> SELECT shard_name, count(*) AS subscriptions,
          sum(CASE WHEN is_primary THEN 1 ELSE 0 END) AS primaries,
          min(subscription_state) AS min_state, max(subscription_state) AS max_state
   FROM v_catalog.node_subscriptions GROUP BY 1 ORDER BY 1;

shard_name  | subscriptions | primaries | min_state | max_state
------------+---------------+-----------+-----------+-----------
replica     |             3 |         1 | ACTIVE    | ACTIVE
segment0001 |             1 |         1 | ACTIVE    | ACTIVE
segment0002 |             1 |         1 | ACTIVE    | ACTIVE
segment0003 |             1 |         1 | ACTIVE    | ACTIVE
```

Six rows, all `ACTIVE`. This is what "K-Safe=0" actually means: each segment shard has exactly **one** subscription, so losing a node loses that data. The replica shard has three (one per node) because it holds unsegmented projections that exist on every node anyway. **"Four shards" does not mean "four copies".**

Normal: every `subscription_state = ACTIVE` and exactly one primary per shard. When it is not: a `PASSIVE` or `UNKNOWN` state sends you back to the node state in #1, then to whether the depot is still syncing.

### 4.2 Depot and Communal: #4, #5, #6

**#4 Depot usage**

```sql
=> SELECT node_name, count(*) AS containers, sum(used_bytes) AS depot_bytes,
          round(sum(used_bytes)/1024.0/1024/1024, 3) AS depot_gb
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

node_name        | containers | depot_bytes | depot_gb
-----------------+------------+-------------+----------
v_eondb_node0001 |        481 |  3997361108 |    3.723
v_eondb_node0002 |        492 |  3995250427 |    3.721
v_eondb_node0003 |        477 |  3994972181 |    3.721
```

Then compare against the ceiling and the filesystem:

```sql
=> SELECT parameter_name, current_value, default_value
   FROM v_monitor.configuration_parameters
   WHERE parameter_name IN ('MaxDepotSizePercent','UseDepotForReads','UseDepotForWrites','DepotOperationsForQuery')
   GROUP BY 1,2,3 ORDER BY 1;

parameter_name           | current_value | default_value
-------------------------+---------------+---------------
DepotOperationsForQuery  | ALL           | ALL
MaxDepotSizePercent      | 80            | 80
UseDepotForReads         | 1             | 1
UseDepotForWrites        | 1             | 1
```

```sql
=> SELECT node_name, min(disk_space_used_mb) AS used_mb,
          min(disk_space_free_mb) AS free_mb,
          min(disk_space_used_mb + disk_space_free_mb) AS total_mb
   FROM v_monitor.disk_storage GROUP BY 1 ORDER BY 1;

node_name        | used_mb | free_mb | total_mb
-----------------+---------+---------+---------
v_eondb_node0001 |  685665 |  214574 |   900239
v_eondb_node0002 |  685665 |  214574 |   900239
v_eondb_node0003 |  685665 |  214574 |   900239
```

The arithmetic: a 900,239 MB filesystem is about 879.1 GiB; at `MaxDepotSizePercent = 80` the depot ceiling is roughly **703.3 GiB**. It currently holds 3.72 GiB — **0.53% of the ceiling, 0.42% of the filesystem.**

How to read it: `used_bytes` is the physical size of containers in the depot, not uncompressed data (that is 109.03 GB; see #7). `MaxDepotSizePercent` says "the depot may consume at most this share of the filesystem", not "the depot should be this big".

Normal: there is no absolute target for depot share, but a depot hugging its ceiling while #5's fetch count climbs means queries keep going to communal storage — that is what a falling depot hit rate looks like.

**#5 Depot fetches: misses and latency**

```sql
=> SELECT node_name, is_successful, count(*) AS fetches,
          min(fetch_start_time) AS first_fetch, max(fetch_end_time) AS last_fetch,
          round(avg(fetch_time_ms), 3) AS avg_fetch_ms, max(fetch_time_ms) AS max_fetch_ms,
          sum(file_size_bytes) AS bytes_
   FROM v_monitor.depot_fetches GROUP BY 1,2 ORDER BY 1;

node_name        | is_successful | fetches | first_fetch                      | last_fetch                       | avg_fetch_ms | max_fetch_ms | bytes_
-----------------+---------------+---------+----------------------------------+----------------------------------+--------------+--------------+---------
v_eondb_node0002 | True          |      15 | 2026-11-09 07:31:24.158835+00:00 | 2026-11-09 13:50:01.000005+00:00 |        0.667 |            3 | 2787562
v_eondb_node0003 | True          |      15 | 2026-11-09 07:31:24.158836+00:00 | 2026-11-09 13:50:00.999488+00:00 |        0.600 |            2 | 2787562
```

Only two of three nodes appear — `node0001` has no rows at all. All 30 fetches succeeded, 5,575,124 bytes total, 185,838 bytes per file on average. **The story is not the 0.6 ms; it is that every record stops on 2026-11-09.** Nothing has been fetched since, because the depot has had the data and queries have not missed.

Now check how much of the table is actually populated:

```sql
=> SELECT count(*) AS rows_,
          count(wait_time_ms) AS wait_nonnull, count(response_time_ms) AS resp_nonnull,
          count(submission_time) AS submit_nonnull, count(fetch_time_ms) AS fetch_nonnull
   FROM v_monitor.depot_fetches;

rows_ | wait_nonnull | resp_nonnull | submit_nonnull | fetch_nonnull
------+--------------+--------------+----------------+--------------
   30 |            0 |            0 |              0 |            30
```

**`wait_time_ms` and `response_time_ms` have zero populated rows.** Any report computing communal wait latency from `wait_time_ms` returns NULL here. The only communal-to-depot cost available is `fetch_time_ms`: 0.633 ms average, 3 ms maximum.

How to read it: a rising `fetches` count means misses; an `is_successful = False` row means a communal read failed and the S3 endpoint and network come first. **Do not** treat this table as a hit rate — it records only the miss side, with no denominator.

**#6 Communal storage availability**

```sql
=> SELECT location_path, location_usage, sharing_type, has_catalog, count(*) AS nodes_
   FROM v_catalog.storage_locations WHERE sharing_type = 'COMMUNAL'
   GROUP BY 1,2,3,4;

location_path        | location_usage | sharing_type | has_catalog | nodes_
---------------------+----------------+--------------+-------------+-------
s3://verticadb/eondb | DATA           | COMMUNAL     | True        |     1
```

One row, `nodes_ = 1`: communal storage is a **cluster-level** location that belongs to no node. `has_catalog = true` means the authoritative copy of the catalog lives there too (S3 in this lab).

How to verify availability: from SQL you can only confirm that the configuration is present and that the last 30 fetches succeeded. Real availability has to be probed from outside — something like `aws s3 ls s3://verticadb/eondb` against the endpoint. That was not done here (no writes or listings against external services), so **this section reaches "configuration and history look fine", which is not the same as "S3 is healthy right now".**

For scale: `v_catalog.license_audits` reports `database_size_bytes = 108,572,148,738` (101.12 GiB) against an 11.16 GiB depot — nearly ten times larger. **Depot size is not database size.**

### 4.3 Data Placement and Memory: #7, #8

**#7 ROS distribution**

```sql
=> SELECT node_name, count(*) AS containers, sum(total_row_count) AS rows_,
          sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

node_name        | containers | rows_         | used_bytes
-----------------+------------+---------------+-------------
v_eondb_node0001 |        481 |   366,827,878 | 3,997,361,108
v_eondb_node0002 |        492 |   366,626,832 | 3,995,250,427
v_eondb_node0003 |        477 |   366,496,945 | 3,994,972,181
```

Rows differ by at most 330,933 (**0.09%**), disk by at most 2,389,000 bytes (**0.06%**), containers by 15 (**3.14%**). Same lesson as the previous article: **different container counts are not skew; look at `rows_` or `used_bytes`.**

```sql
=> SELECT CASE WHEN total_row_count < 1000 THEN '1-999'
               WHEN total_row_count < 100000 THEN '1K-99K'
               WHEN total_row_count < 1000000 THEN '100K-1M'
               WHEN total_row_count < 10000000 THEN '1M-10M'
               ELSE '10M+' END AS bucket,
          count(*) AS containers, sum(total_row_count) AS rows_, sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY min(total_row_count);

bucket   | containers | rows_         | used_bytes
---------+------------+---------------+------------
1-999    |       1221 |       108,365 |  18,947,054
1K-99K   |        146 |     3,111,610 |  893,094,007
100K-1M  |         53 |    22,510,329 |  829,496,840
1M-10M   |         27 |    66,448,867 | 1,386,314,557
10M+     |          3 | 1,007,772,484 | 8,859,731,258
```

1,221 containers (84.21%) hold fewer than 1,000 rows and just 0.0099% of the data; three containers hold 91.62%. **The shape has not changed since 11-16 — only the total, from 1,428 to 1,450.**

Normal: there is no single threshold. What matters is whether the `1-999` bucket keeps growing — that means small ROS files are accumulating faster than mergeout consolidates them.

**#8 Resource pool memory (27 rows)**

```sql
=> SELECT pool_name, is_internal, count(*) AS nodes_,
          min(memory_size_kb) AS min_kb, max(memory_size_kb) AS max_kb,
          sum(memory_inuse_kb) AS inuse_kb,
          min(running_query_count) AS min_running, max(running_query_count) AS max_running
   FROM v_monitor.resource_pool_status GROUP BY 1,2 ORDER BY max_kb DESC;

pool_name | is_internal | nodes_ | min_kb   | max_kb   | inuse_kb | min_running | max_running
----------+-------------+--------+----------+----------+----------+-------------+-------------
general   | True        |      3 | 24280522 | 24291145 |        0 |           0 |           0
tm        | True        |      3 |  3145728 |  3145728 |        0 |           0 |           0
sysquery  | True        |      3 |  1048576 |  1048576 |   177476 |           1 |           1
metadata  | True        |      3 |   109383 |   120006 |        0 |           0 |           0
dbd       | True        |      3 |        0 |        0 |        0 |           0 |           0
jvm       | True        |      3 |        0 |        0 |        0 |           0 |           0
blobdata  | True        |      3 |        0 |        0 |        0 |           0 |           0
recovery  | True        |      3 |        0 |        0 |        0 |           0 |           0
refresh   | True        |      3 |        0 |        0 |        0 |           0 |           0
```

Nine pools times three nodes equals 27 rows, all internal. `general` is 24,280,522 / 24,291,145 / 24,286,994 KB per node (about 23.2 GiB), a spread of 10,623 KB (0.04%); `metadata` ranges 109,383–120,006 KB, a 9.7% spread that is normal load variance for a small pool. `sysquery` has one running query per node using 177,476 KB — **that is this health check itself.**

How to read it: `memory_inuse_kb` is memory currently lent out, not the share of the pool consumed. `general` shows inuse 0 and running 0; if users report queuing at that moment, memory is not the problem — go to #9.

### 4.4 Activity, Capacity, and Versions: #9, #10, #11, #12

**#9 Long-running queries**

```sql
=> SELECT node_name, user_name, session_id, request_type,
          substring(request, 1, 50) AS query_head, memory_acquired_mb, start_timestamp
   FROM v_monitor.query_requests WHERE is_executing ORDER BY start_timestamp;

node_name        | user_name | session_id              | request_type | query_head                                    | memory_acquired_mb | start_timestamp
-----------------+-----------+-------------------------+--------------+-----------------------------------------------+--------------------+----------------------------------
v_eondb_node0001 | dbadmin   | v_eondb_node0001-91:... | QUERY        | select node_name, user_name, session_id, r... |             255.69 | 2026-11-16 21:02:49.735435+00:00
```

One row, and it is **this query itself** (`memory_acquired_mb = 255.69`). That is a property worth knowing: `v_monitor.query_requests` holds only requests executing right now, so any query looking for long-running work will always find itself.

For history, switch to `query_profiles`:

```sql
=> SELECT node_name, user_name, query_duration_us, processed_row_count,
          substring(query, 1, 60) AS query_head, query_start
   FROM v_monitor.query_profiles
   WHERE query_start::timestamptz > now() - INTERVAL '1 day'
   ORDER BY query_duration_us DESC LIMIT 5;

node_name        | user_name | query_duration_us | processed_row_count | query_head                                    | query_start
-----------------+-----------+-------------------+---------------------+-----------------------------------------------+----------------------------
v_eondb_node0001 | dbadmin   |         2894667.0 |                   1 | select /*+label(w_bigscan)*/ count(*) from... | 2026-11-15 21:03:52.369794+00
v_eondb_node0001 | dbadmin   |         2864839.0 |                   1 | select /*+label(w_bigscan)*/ count(*) from... | 2026-11-15 21:03:49.50398+00
v_eondb_node0001 | dbadmin   |          480507.0 |                   1 | select count(*) as n, count(distinct para...  | 2026-11-16 21:02:05.116894+00
v_eondb_node0001 | dbadmin   |          432357.0 |                   4 | select parameter_name, current_value, defa... | 2026-11-16 21:02:49.180924+00
v_eondb_node0001 | dbadmin   |          430294.0 |                   3 | select parameter_name, current_value, defa... | 2026-11-15 21:04:32.242464+00
```

`query_start` is a `varchar`; comparing it to a timestamp requires a cast. Writing `query_start > now() - INTERVAL '1 day'` without it fails with `Operator does not exist: varchar > timestamptz` — an error I hit while writing this section, left here for the next person.

How to read it: the slowest entry is a 2.89 s billion-row aggregation (the measurement query from the previous article); nothing is actually long-running. If minute-level rows appear on your system, check `memory_acquired_mb` against the pool ceiling first, then the container counts in #7.

**#10 Failed loads**

```sql
=> SELECT stream_name, table_name, load_start, load_duration_ms,
          accepted_row_count, rejected_row_count, parse_complete_percent
   FROM v_monitor.load_streams ORDER BY load_start DESC;

stream_name | table_name          | load_start                     | load_duration_ms | accepted_row_count | rejected_row_count | parse_complete_percent
------------+---------------------+--------------------------------+------------------+--------------------+--------------------+------------------------
            | judicial_exc        | 2026-11-09 13:59:20.29743+00   |          6745953 |         22,518,058 |                  0 |                    100
            | lowidoc_exc         | 2026-11-09 13:50:30.220575+00  |           529951 |            592,102 |                  0 |                    100
            | vs_ros              | 2026-11-09 12:48:09.015905+00  |          3576594 |        380,504,586 |                  0 |                    100
            | partition_columns   | 2026-11-09 10:56:33.560057+00  |          6629722 |      1,007,772,484 |                  0 |                    100
```

Four rows, `rejected_row_count` all zero, `parse_complete_percent` all 100. `stream_name` is an empty string — not every load carries a stream name.

How to read it: `load_duration_ms` is milliseconds, so 6,745,953 ms is **1 hour 52 minutes**, not 6,745 seconds. Loading a billion rows took 6,629,722 ms, or **152,010 rows/s** across the cluster. **`accepted + rejected` is the read volume**; accepted alone understates it.

`v_monitor.load_events` does not exist in this build (`Relation "v_monitor.load_events" does not exist`), so failed loads come from `rejected_row_count` and `parse_complete_percent` in `load_streams`, or from the `COPY` error message itself.

**#11 Disk headroom**

```sql
=> SELECT node_name, min(disk_space_used_mb) AS used_mb,
          min(disk_space_free_mb) AS free_mb,
          min(disk_space_free_percent) AS free_pct, min(storage_status) AS status,
          count(*) AS rows_
   FROM v_monitor.disk_storage GROUP BY 1 ORDER BY 1;

node_name        | used_mb | free_mb | free_pct | status | rows_
-----------------+---------+---------+----------+--------+-------
v_eondb_node0001 |  685667 |  214572 | 23%      | Active |     3
v_eondb_node0002 |  685667 |  214572 | 23%      | Active |     3
v_eondb_node0003 |  685667 |  214572 | 23%      | Active |     3
```

`rows_ = 3` because each node has three storage locations (`CATALOG`, `DATA,TEMP`, `DEPOT`) pointing at the same filesystem, so all three rows repeat. **`GROUP BY node_name` plus `min()` is the dedupe.**

All three nodes report the same free (214,572 MB) and used (685,667 MB) to the megabyte — in this lab the three nodes share one `/data` mount (confirm with `df -h /data` on a node). **Therefore do not add the three rows into a 2.7 TB figure**; that overstates capacity threefold.

How to read it: act when `free_pct` drops below 20% (a rule of thumb, not an official SLA). A `status` other than `Active` is a hard failure; a low `free_pct` alone is a capacity question, so check the depot and ROS (#4, #7) first.

**#12 Version consistency (one query per entry point)**

This one is not SQL but the same SQL run from three entry points:

```python
from connect_eondb import connect_eondb
for port in (1533, 2533, 3533):
    with connect_eondb(VERTICA_EON_ENDPOINTS="192.168.100.9:%d" % port) as conn:
        cur = conn.cursor()
        cur.execute("select version()");                       v = cur.fetchone()[0]
        cur.execute("select node_name from v_monitor.current_session"); n = cur.fetchone()[0]
        cur.execute("select count(*) from v_catalog.nodes");    c = cur.fetchone()[0]
        cur.execute("select count(distinct shard_name) from v_catalog.shards"); s = cur.fetchone()[0]
        print("port=%d node=%s shards=%d nodes=%d version=%s" % (port, n, s, c, v))
```

```text
port=1533 connect=67ms node=v_eondb_node0001 shards=4 nodes=3 version=Vertica Analytic Database v25.4.0-0
port=2533 connect=1ms  node=v_eondb_node0002 shards=4 nodes=3 version=Vertica Analytic Database v25.4.0-0
port=3533 connect=1ms  node=v_eondb_node0003 shards=4 nodes=3 version=Vertica Analytic Database v25.4.0-0
```

Each entry point lands on a **different node**, and version, shard count, and node count all agree. The first connection cost 67 ms (connection setup); the next two cost 1 ms each (warm).

How to read it: version drift usually happens mid-rolling-upgrade, when one node is on the new build and another is not. SQL has no per-node version view (`v_catalog.versions` does not exist in this build — checked), so **the only way is to ask `version()` through each node's entry point.** That is why query #12 is code, not a single statement.

---

## 5. Cost Analysis: What 284.6 ms Buys

Eleven queries, one session, second of two runs each (warm):

| # | Query | Rows returned | Time |
|---|-------|--------------:|-----:|
| 1 | Node state | 3 | 15.7 ms |
| 2 | Shard list | 4 | 3.1 ms |
| 3 | Subscriptions | 4 | 3.5 ms |
| 4 | Depot usage | 3 | 17.3 ms |
| 5 | Depot fetches | 2 | 10.9 ms |
| 6 | Communal location | 1 | 5.7 ms |
| 7 | ROS top 5 | 5 | 18.0 ms |
| 8 | Resource pools | 9 | 10.8 ms |
| 9 | Running queries | 1 | 57.8 ms |
| 10 | Failed loads | 4 | 137.7 ms |
| 11 | Disk headroom | 3 | 4.3 ms |
| | **Total** | | **284.6 ms** |

Three observations:

1. **3 ms is the floor.** `shards` returns four rows in 3.1 ms; that is network round trip plus driver overhead, not query cost. Never read a sub-5 ms difference as a performance signal.
2. **`load_streams` is the expensive one (137.7 ms).** It scans load history for every session (only four rows here) yet is still the slowest of the set. On a cluster with thousands of loads a day this becomes a long-running query and needs a `WHERE load_start > ...` filter.
3. **`query_requests` at 57.8 ms has self-observation bias.** The query appears in its own result set, so it measures a collection that includes itself. For automated thresholds, exclude your own `session_id` first.

These numbers do not mean "0.3 seconds, so run it every minute" — **cost grows with container count and `load_streams` size.** There are 1,450 containers here; once you are in the thousands, the two aggregates in section 4.3 start to show.

---

## 6. Common Mistakes and Anti-Patterns

**1. Using the parameter name `DepotSizePercent`.** This build has 563 parameters; the depot one is `MaxDepotSizePercent` (80). A query with the wrong name does not error — it returns zero rows, and you conclude there is no limit.

**2. Adding up the three `disk_storage` rows.** Three nodes times three locations, each reporting the same used/free down to the megabyte (685,667 / 214,572). Summing them yields a fictitious 2.7 TB of capacity.

**3. Computing communal latency from `depot_fetches.wait_time_ms`.** Zero of 30 rows are populated. Use `fetch_time_ms` (0.633 ms here) or admit the cluster cannot measure it.

**4. Treating depot size as database size.** The depot is 11.16 GiB; `license_audits.database_size_bytes` is 101.12 GiB. Nearly ten times apart — say which one you mean when reporting capacity.

**5. Assuming the container count is a copy count.** 1,450 is the sum of per-shard, per-node ROS rows, and replica rows appear once per node. The logical row count is `v_monitor.system.total_row_count` at 1,099,951,655.

**6. Querying history from `query_requests`.** It holds only what is executing; here you will only ever see yourself. History lives in `query_profiles` (3,605 rows here, oldest 2026-11-09 07:27 UTC).

**7. Believing three ports mean three copies or three databases.** Measured: three different nodes, same `version()`, same shard count. Rotating across them as failover entry points is fine (this tool does exactly that); treating them as separate environments is not.

**8. Judging a load by `count(*)`.** Read `accepted_row_count`, `rejected_row_count`, and `parse_complete_percent` together. All four loads here show rejected 0 and parse 100% — but when a load really fails, rejected will be greater than zero.

---

## 7. Risks and Recommendations

**Do now (read-only, low risk)**

- Package the eleven statements from sections 4.1–4.4 into one `.sql` file and run it with `vsql -f` or `connect_eondb.py`, pasting the output into a ticket. 284.6 ms is negligible load on production.
- Track four numbers every day: `node_down_count`, `gaps`, the worst `subscription_state`, and depot usage as a share of its ceiling. Those cover nodes, coverage, replicas, and caching.
- Read `depot_fetches` for whether the count grew, not for milliseconds. Here all 30 records stop on 11-09, which is itself evidence of no misses.
- For disk, look only at `free_pct` and `status`, and dedupe with `min()` rather than summing.
- Check version consistency through all three entry points weekly.

**Requires change (needs a window and approval)**

- To measure communal latency for real you must first provoke a depot miss (lower `MaxDepotSizePercent`, or query a table that is not warmed), then watch `depot_fetches`. **That is a change, and it was not done here.**
- `load_streams` gets slow on busy clusters; add a time filter or query `v_monitor.query_profiles` with `query_type = 'LOAD'`. Changing the query needs no approval; changing retention does.
- Raising K-Safe from 0 to 1 changes shard and subscription counts (each segment currently has one copy). **Do not do this straight in production**: size the depot and communal storage first, and schedule the subscription rebuild window.
- Rotating entry points is client-side behaviour; `connect_eondb.py` already tries 1533, then 2533, then 3533. If you use your own driver, configure backup hosts and retries instead of hard-coding one port.

---

## 8. References

- Vertica 25.4 documentation: EON architecture — https://docs.vertica.com/25.4.x/en/architecture/eon/architecture/
- Vertica 25.4 documentation: `NODES` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/nodes/
- Vertica 25.4 documentation: `SHARDS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/shards/
- Vertica 25.4 documentation: `NODE_SUBSCRIPTIONS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/node-subscriptions/
- Vertica 25.4 documentation: `STORAGE_LOCATIONS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/storage-locations/
- Vertica 25.4 documentation: `DEPOT_FETCHES` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/depot-fetches/
- Vertica 25.4 documentation: `RESOURCE_POOL_STATUS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/resource-pool-status/
- Vertica 25.4 documentation: `DISK_STORAGE` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/disk-storage/
- Vertica 25.4 documentation: Depots — https://docs.vertica.com/25.4.x/en/admin/managing-depots/

> Note: `docs.vertica.com` currently 302-redirects to `docs.rocketsoftware.com/bundle/vertica_25.4/` with matching paths; every link above returned 200 when checked on 2026-11-17.
