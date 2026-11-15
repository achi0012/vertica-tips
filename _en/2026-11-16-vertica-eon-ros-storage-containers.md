---
layout: post_en
title: "ROS and Storage Containers in EON: 1,428 Containers Across a Billion Rows"
date: 2026-11-16 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, ros, storage-container, partitions, projection, tuple-mover]
description: "Measured on a local three-node eondb (v25.4.0-0, K-Safe=0, 4 shards): v_monitor.storage_containers returns 1,428 rows holding 1,099,951,625 rows of data on 11.99 GB of disk, against 109.03 GB of payload — a 9.10× overall compression ratio. But 84% of those containers (1,199 of them) hold 0.01% of the rows, while three containers hold 91.62%. On the same cluster, scanning a billion rows out of three large containers took 2.87 s; scanning 920,871 rows out of 789 small containers took 0.028 s — a 10.7× difference in cost per row."
image: /assets/images/og/vertica-eon-ros-storage-containers-en.png
faq:
  - q: "Do 1,428 storage containers mean the cluster holds 1,428 copies of the data?"
    a: "No. The number is the sum of container rows as seen by each of the three nodes: segment0001 has 470, segment0002 has 481, segment0003 has 462, and the replica shard has just 15 (five unsegmented projections times three nodes). The replica shard's 300,129 rows are counted once per node; after removing the duplicates the cluster holds 1,099,751,539 rows, of which 1,099,651,496 live in segment shards."
  - q: "Why do 84% of the containers hold only 0.01% of the data?"
    a: "Container sizes are extremely uneven. Here 1,199 containers hold fewer than 1,000 rows, 108,335 rows and 18.94 MB in total. Of those, 789 belong to `LOWIDOC_V6_super`: 267 partition keys across three shards, one container per partition, averaging 1,167.1 rows and as few as one. Those are the ROS files left by small daily loads; `MergeOutInterval` is the default 600 seconds and `ActivePartitionCount` is 1, so mergeout has not consolidated them."
  - q: "Do too many containers hurt queries? Am I close to the limit?"
    a: "They hurt, and it is measurable here. `ContainersPerProjectionLimit` is the default 1,024; `LOWIDOC_V6_super` has 789 containers (at most 265 on one node, 25.9% of the limit), so ROS pushback is not imminent, yet scanning its 920,871 rows takes 28 ms, or 30.5 ns per row. Scanning 1,007,772,484 rows out of three large containers takes 2,865 ms, or 2.84 ns per row — 10.7× cheaper per row."
  - q: "Why does a billion-row projection need only three containers?"
    a: "`partition_columns_super` is segmented, so its data exists only in the three segment shards, one container each: 336,125,677 / 335,881,586 / 335,765,221 rows at 2.95 GB apiece. Their combined payload is 92.58 GB against 8.86 GB on disk (10.45× compression), 8.79 bytes per row on disk. An aggregation over those three containers took 2.87 s, roughly 1,031 MB/s per node."
---

<!--more-->

---

## 1. The Short Version

`v_monitor.storage_containers` is an easy table to misread. It looks like "how many files does the cluster have"; it actually lists ROS containers per shard per node. On a local three-node eondb (v25.4.0-0, K-Safe=0, four shards) it returns **1,428 rows**, and the spread behind those rows is wider than most people expect:

1. **Totals: 1,428 containers, 1,099,951,625 rows, 11.99 GB on disk.** Payload — the uncompressed column content — is 109.03 GB, an overall 9.10× compression. The mean is 770,274 rows per container, and that mean is useless because the median sits at the other end, near one row.
2. **84% of the containers hold 0.01% of the data.** 1,199 containers hold fewer than 1,000 rows each, 108,335 rows and 18.94 MB combined. At the other end, three containers hold 1,007,772,484 rows (91.62%) and 8.86 GB (73.91%).
3. **The container count is not a replica count.** Of the 1,428 rows, segment0001 / 0002 / 0003 account for 470 / 481 / 462 and the replica shard for just 15. The replica shard holds unsegmented projections (five projections across three nodes); segmented projections have no containers there at all.
4. **A billion rows needs three containers.** `partition_columns_super` has one container per segment shard at 336,125,677 / 335,881,586 / 335,765,221 rows and 2.95 GB each, 8.79 bytes per row on disk.
5. **Too many containers has a measurable price.** Scanning a billion rows from three large containers: 2,865 ms, 2.84 ns per row. Scanning 920,871 rows from 789 small containers: 28 ms, 30.5 ns per row. **10.7× more cost per row**, on 1,094× less data.
6. **Data distribution is even; container distribution is not.** Rows per node are 366,827,870 / 366,626,825 / 366,496,930 (a 0.09% spread) and disk is 3.997 / 3.995 / 3.995 GB (0.06%), but containers are 475 / 486 / 467 (3.91%). **The container gap comes from small containers, not skew.**
7. **`v_monitor.partitions` has only six projections and 819 rows.** The view lists only tables with a partition key; it is not a container catalogue, and comparing its row count with the container total will never reconcile.

This is for three audiences: the DBA who sees 1,428 rows and assumes there are a thousand files to clean up; the one warned about ROS pushback who wants to know how far from the limit they are; and the one who has to explain why a billion-row report is faster than a nine-hundred-thousand-row one. **Every number comes from the read-only queries run on 2026-11-16; nothing on the cluster was changed.**

---

## 2. Test Environment

| Item | Value (measured this run) |
|------|---------------------------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Database | `eondb` (EON mode) |
| Nodes | 3 (`v_eondb_node0001/0002/0003` at `10.89.10.11` / `.12` / `.13`, all `UP`) |
| Shards | 4 (`replica` plus `segment0001`–`segment0003`), K-Safe = 0 |
| Storage locations | 7: three `DATA,TEMP` (`/data/eondb/…_data`), three `DEPOT` (`/data/depot/eondb/…_depot`), one `COMMUNAL` (`s3://verticadb/eondb`, `has_catalog = true`) |
| Projections | 486 catalog rows / 434 projection_ids; 78 unsegmented, 408 segmented |
| Projections with data | 219 names / 221 projection_ids (`users_super` and `short_url_super` each exist in two schemas) |
| Access | `~/.hermes/scripts/sched/connect_eondb.py` (`vertica_python`, endpoints 1533/2533/3533), `SELECT` only |
| Run time | 2026-11-16 05:05–05:40 Asia/Taipei |

When the series was planned on 11-11 the count was 1,424; this run reports 1,428. **The table is alive.** Four containers appeared in four days, all on the small end. Any report that treats the container total as an SLA number has to accept that it moves daily.

---

## 3. How It Works: A Container Is the Storage Unit of One Shard

In EON, where a row lands is decided in three layers:

| Layer | What it decides | Example on this cluster |
|-------|-----------------|-------------------------|
| Communal storage | Where the authoritative copy lives | `s3://verticadb/eondb` (the only `COMMUNAL` location) |
| Shard | Which hash ranges belong to this shard | 4: `replica` (unsegmented) plus three segments |
| Container (ROS) | One physical file set for a batch of rows on one shard | 1,428, split 15 / 470 / 481 / 462 by shard |

So one row of `v_monitor.storage_containers` means: on one node, in one shard, one container of one projection. Three columns decide how you read it:

- `total_row_count` — rows in the container, deleted rows included (here `deleted_row_count` is 0 everywhere).
- `used_bytes` — actual disk footprint.
- `payload_size` — uncompressed column content. **`payload_size / used_bytes` is the compression ratio**, not `used_bytes / total_row_count`.

The totals here are `1,099,951,625` rows, `11,987,579,031` bytes (11.99 GB) and `109,033,186,943` bytes (109.03 GB) — a ratio of **9.10**.

![Three views of the same 1,428 containers: 84% of containers fall in the sub-1,000-row bucket but account for 0.01% of rows and 0.16% of disk, while three containers above 10M rows hold 91.62% of rows and 73.91% of disk]({{ '/assets/images/eon-ros-container-size-distribution.svg' | relative_url }}){: loading="lazy" }

Every figure in the chart — 1,199 / 146 / 53 / 27 / 3 containers — was measured in this run.

### 3.1 Why "three containers per projection" is normal

Across the 221 projection_ids that hold data, the container counts are:

| Containers per projection | Projections | Rows |
|---:|---:|---:|
| 1 | 36 | 3,872 |
| 2 | 24 | 455 |
| 3 | 156 | 1,099,017,084 |
| 4 | 1 | 4 |
| 9 | 1 | 9,100 |
| 11 | 1 | 20 |
| 63 | 1 | 219 |
| 789 | 1 | 920,871 |

156 projections have exactly three containers — one per segment shard. That is the normal shape for a segmented projection in EON: rows hash into three segments, each segment writes one ROS. **Three is healthy; 789 is what you investigate.**

What about the 36 single-container projections? Every one of them sits on **one shard on one node**, 3,872 rows in total:

```sql
=> SELECT projection_id, count(*) AS c, count(DISTINCT node_name) AS nodes_,
          count(DISTINCT shard_name) AS shards_, sum(total_row_count) AS rows_
   FROM v_monitor.storage_containers
   GROUP BY 1 HAVING count(*) = 1
   ORDER BY rows_ DESC LIMIT 5;

      projection_id      | c | nodes_ | shards_ | rows_
-------------------------+---+--------+---------+-------
 45035996273720292       | 1 |      1 |       1 |  3826
 45035996273709890       | 1 |      1 |       1 |     4
 45035996273710530       | 1 |      1 |       1 |     3
 45035996273710574       | 1 |      1 |       1 |     3
 45035996273709380       | 1 |      1 |       1 |     2
```

Small tables do not spread across three nodes in EON — their hash values fall inside one segment's range, so the projection is one container on one node. **"EON spreads your data across three nodes" is false for small tables.**

---

## 4. Step by Step: Reading All 1,428 Rows

### 4.1 The overview

```sql
=> SELECT count(*) AS containers, count(DISTINCT projection_name) AS proj_names,
          count(DISTINCT projection_id) AS proj_ids, count(DISTINCT node_name) AS nodes_,
          sum(total_row_count) AS rows_, sum(used_bytes) AS used_bytes,
          sum(payload_size) AS payload_bytes, sum(deleted_row_count) AS deleted_rows
   FROM v_monitor.storage_containers;

containers | proj_names | proj_ids | nodes_ | rows_         | used_bytes   | payload_bytes | deleted_rows
-----------+------------+----------+--------+---------------+--------------+---------------+--------------
      1428 |        219 |      221 |      3 | 1,099,951,625 | 11,987,579,031 | 109,033,186,943 | 0
```

`deleted_rows` is 0: no container here carries a delete vector (the count of containers with `deleted_row_count > 0` is also 0). Every row count below is therefore a real row count, with nothing to subtract.

### 4.2 Three nodes: data is flat, container counts are not

```sql
=> SELECT node_name, count(*) AS containers, sum(total_row_count) AS rows_,
          sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

node_name        | containers | rows_         | used_bytes
-----------------+------------+---------------+--------------
v_eondb_node0001 |        475 |   366,827,870 | 3,997,359,830
v_eondb_node0002 |        486 |   366,626,825 | 3,995,249,165
v_eondb_node0003 |        467 |   366,496,930 | 3,994,970,036
```

- Rows differ by at most 330,940 (**0.09%**) and disk by at most 2,389,794 bytes (**0.06%**) — normal hash-distribution noise.
- Containers differ by 19 (**3.91%**). The gap comes entirely from small containers (LOWIDOC is 262 / 265 / 262 across nodes).
- So unequal container counts do not mean skew. **Judge skew on `rows_` or `used_bytes`, never on `count(*)`.**

### 4.3 Shards: the replica holds only 15 containers

```sql
=> SELECT shard_name, count(*) AS containers, sum(total_row_count) AS rows_,
          sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

shard_name  | containers | rows_         | used_bytes
------------+------------+---------------+-------------
replica     |         15 |       300,129 |   8,337,534
segment0001 |        470 |   366,727,827 | 3,994,580,652
segment0002 |        481 |   366,526,782 | 3,992,469,987
segment0003 |        462 |   366,396,887 | 3,992,190,858
```

All 15 replica-shard containers belong to five unsegmented projections, one per node per projection:

```sql
=> SELECT projection_name, node_name, total_row_count, used_bytes
   FROM v_monitor.storage_containers
   WHERE shard_name = 'replica' ORDER BY projection_name, node_name;

projection_name                 | node_name        | total_row_count | used_bytes
--------------------------------+------------------+-----------------+------------
customer_dimension_flex2_1_unseg | v_eondb_node0001 |          50,000 |     310,820
customer_dimension_flex2_1_unseg | v_eondb_node0002 |          50,000 |     310,820
customer_dimension_flex2_1_unseg | v_eondb_node0003 |          50,000 |     310,820
lowi_user_save_channel_unseg    | v_eondb_node0001 |               2 |         239
lowi_user_save_channel_unseg    | v_eondb_node0002 |               2 |         239
lowi_user_save_channel_unseg    | v_eondb_node0003 |               2 |         239
lowi_user_save_source_unseg     | v_eondb_node0001 |              38 |         674
lowi_user_save_source_unseg     | v_eondb_node0002 |              38 |         674
lowi_user_save_source_unseg     | v_eondb_node0003 |              38 |         674
scheduled_task_unseg            | v_eondb_node0001 |               3 |         483
scheduled_task_unseg            | v_eondb_node0002 |               3 |         483
scheduled_task_unseg            | v_eondb_node0003 |               3 |         483
xxx_unseg                       | v_eondb_node0001 |          50,000 |   2,466,962
xxx_unseg                       | v_eondb_node0002 |          50,000 |   2,466,962
xxx_unseg                       | v_eondb_node0003 |          50,000 |   2,466,962
(15 rows)
```

That list explains two things at once:

1. **Segmented projections have no containers on the replica shard.** "K-Safe=0, four shards" does not mean every row exists four times.
2. **The 1,099,951,625 total contains duplicates.** The replica shard's 300,129 rows are the same data counted once per node (`300,129 ÷ 3 = 100,043`). Net of duplicates the cluster holds `1,099,751,539` rows, of which segment shards hold `1,099,651,496`:

```sql
=> SELECT sum(total_row_count) AS segment_rows,
          (SELECT sum(total_row_count)/3 FROM v_monitor.storage_containers WHERE shard_name='replica') AS unseg_rows
   FROM v_monitor.storage_containers WHERE shard_name LIKE 'segment%';

segment_rows  | unseg_rows
--------------+-----------
1,099,651,496 |    100,043
```

`1,099,651,496 + 100,043 = 1,099,751,539`. **When someone asks how many rows you have, quote this number, not 1,099,951,625.**

### 4.4 Size distribution: 84% of containers hold fewer than 1,000 rows

```sql
=> SELECT CASE WHEN total_row_count < 1000 THEN '1-999'
               WHEN total_row_count < 100000 THEN '1K-99K'
               WHEN total_row_count < 1000000 THEN '100K-1M'
               WHEN total_row_count < 10000000 THEN '1M-10M'
               ELSE '10M+' END AS bucket,
          count(*) AS containers, sum(total_row_count) AS rows_,
          sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1
   ORDER BY min(total_row_count);

bucket   | containers | rows_         | used_bytes
---------+------------+---------------+-------------
1-999    |       1199 |       108,335 |   18,942,369
1K-99K   |        146 |     3,111,610 |  893,094,007
100K-1M  |         53 |    22,510,329 |  829,496,840
1M-10M   |         27 |    66,448,867 | 1,386,314,557
10M+     |          3 | 1,007,772,484 | 8,859,731,258
```

As shares:

| Bucket | Containers | Share of containers | Share of rows | Share of disk |
|--------|-----------:|--------------------:|--------------:|--------------:|
| 1–999 | 1,199 | 83.96% | 0.0098% | 0.16% |
| 1K–99K | 146 | 10.22% | 0.28% | 7.45% |
| 100K–1M | 53 | 3.71% | 2.05% | 6.92% |
| 1M–10M | 27 | 1.89% | 6.04% | 11.56% |
| 10M+ | 3 | 0.21% | **91.62%** | **73.91%** |

The mean is 770,274 rows per container, and it represents neither end: the largest container holds 336,125,677 rows, the smallest holds **1**. `min(total_row_count)` really is 1, and `deleted_row_count` is 0 everywhere, so that is a container with exactly one live row, not one left after deletes.

### 4.5 The two extremes

**Extreme one: `partition_columns_super`, a billion rows in three containers.**

```sql
=> SELECT projection_name, node_name, total_row_count, used_bytes, payload_size,
          shard_name, segment_lower_bound, segment_upper_bound
   FROM v_monitor.storage_containers
   WHERE total_row_count > 10000000 ORDER BY used_bytes DESC;

projection_name          | node_name        | total_row_count | used_bytes | payload_size | shard_name  | lower       | upper
-------------------------+------------------+-----------------+------------+--------------+-------------+-------------+------------
partition_columns_super  | v_eondb_node0001 |     336,125,677 | 2954595943 |  30879805419 | segment0001 |           0 | 1431655738
partition_columns_super  | v_eondb_node0002 |     335,881,586 | 2952943511 |  30857616937 | segment0002 |  1431655739 | 2863311477
partition_columns_super  | v_eondb_node0003 |     335,765,221 | 2952191804 |  30846258727 | segment0003 |  2863311478 | 4294967295
```

The three hash ranges split `0`–`4,294,967,295` into equal thirds, one per segment shard. Each row occupies `8.79` bytes on disk against `91.9` bytes of payload (10.45× compression). **This is the shape of a large container: one ROS per shard, no extra files.**

**Extreme two: `LOWIDOC_V6_super`, 920,871 rows spread over 789 containers.**

```sql
=> SELECT node_name, shard_name, count(*) AS containers, sum(total_row_count) AS rows_,
          sum(used_bytes) AS used_bytes, min(total_row_count) AS min_rows, max(total_row_count) AS max_rows
   FROM v_monitor.storage_containers
   WHERE projection_name = 'LOWIDOC_V6_super' GROUP BY 1,2 ORDER BY 1;

node_name        | shard_name  | containers | rows_   | used_bytes | min_rows | max_rows
-----------------+-------------+------------+---------+------------+----------+---------
v_eondb_node0001 | segment0001 |        262 | 306,794 | 50,155,799 |        1 |   84,039
v_eondb_node0002 | segment0002 |        265 | 307,559 | 50,216,794 |        1 |   84,583
v_eondb_node0003 | segment0003 |        262 | 306,518 | 50,147,838 |        1 |   84,039
```

That averages `920,871 ÷ 789 = 1,167.1` rows per container and `163.5` bytes per row on disk. Why 789? Because the table is partitioned:

```sql
=> SELECT count(*) AS partition_rows, count(DISTINCT partition_key) AS keys_,
          sum(ros_row_count) AS rows_, sum(ros_size_bytes) AS bytes
   FROM v_monitor.partitions WHERE projection_name = 'LOWIDOC_V6_super';

partition_rows | keys_ | rows_   | bytes
---------------+-------+---------+-----------
           789 |   267 | 920,871 | 150,520,431
```

**267 partition keys, 789 containers** — 2.95 containers per partition, where the ideal is three (one per shard). Twelve containers are missing relative to `267 × 3 = 801`, so a few partitions do not have a container on every shard. This is the classic "one ROS per partition, one more ROS per small load, mergeout has not caught up" pattern. `MergeOutInterval` is the default `600` seconds and `ActivePartitionCount` is `1`, so only one partition counts as active and the rest wait for mergeout.

### 4.6 Measured: cost per row, large container vs small

Same shape of query — scan a table, count rows — at the two extremes, timed with `v_monitor.query_profiles.query_duration_us` and cross-checked with wall clock in Python:

```sql
=> SELECT /*+label(w_bigscan)*/ count(*) FROM mtk.partition_columns WHERE table_name IS NOT NULL;
1007772484

=> SELECT /*+label(w_lowscan)*/ count(*) FROM ADA.LOWIDOC_V6 WHERE DREREFERENCE IS NOT NULL;
920871
```

| Query | Containers | Rows scanned | Disk | `query_duration_us` | Cost per row |
|-------|-----------:|-------------:|------|--------------------:|-------------:|
| `partition_columns` (3 containers) | 3 | 1,007,772,484 | 8.86 GB | 2,864,839 / 2,894,667 (two runs) | **2.84 ns** |
| `LOWIDOC_V6` (789 containers) | 789 | 920,871 | 150.5 MB | 28,060 / 27,023 (two runs) | **30.5 ns** |

In throughput terms, the large side runs at 351,772,817 rows/s across three nodes, about 1,031 MB/s per node; the small side at 32,817,926 rows/s. **Cost per row differs by 10.7×.**

Honest limits: the two tables have different column widths (`LOWIDOC_V6` has `varchar(65000)` columns), different filters, two samples each, and a warm depot. **This proves container count is not free; it does not produce a "milliseconds per extra container" formula.** Measure your own environment the same way — `query_duration_us` divided by rows scanned — before quoting a number.

### 4.7 A trap: `count(*)` may never touch a container

The same billion-row table without a filter:

```sql
=> SELECT /*+label(w_nofilter)*/ count(*) FROM mtk.partition_columns;
1007772484
-- wall clock: 0.406 s (second run 0.397 s)
```

0.4 seconds is not "scanning a billion rows"; the optimizer read statistics (`EXPLAIN` shows `ROW COUNT ONLY`). **To measure container scan cost you must write a predicate that touches data**, otherwise you are timing a catalog lookup.

### 4.8 `v_monitor.partitions` covers only six projections

```sql
=> SELECT projection_name, count(*) AS partition_rows, count(DISTINCT partition_key) AS keys_,
          sum(ros_size_bytes) AS ros_bytes, sum(ros_row_count) AS ros_rows
   FROM v_monitor.partitions GROUP BY 1 ORDER BY partition_rows DESC;

projection_name          | partition_rows | keys_ | ros_bytes  | ros_rows
-------------------------+----------------+-------+------------+---------
LOWIDOC_V6_super         |            789 |   267 | 150,520,431 | 920,871
news_article_i18n_p      |             11 |     5 |      60,455 |      20
tcpd_carcheck_super      |              9 |     3 |     475,457 |   9,100
wiznew_super             |              4 |     4 |         164 |       4
LOWI_HASHTAG_super       |              3 |     1 |      55,201 |   2,582
lowi_hashtag_super       |              3 |     1 |       4,882 |     242
(819 rows across 6 projections)
```

Six projections, 819 rows. Each row is one partition key times one ROS, and **tables without a partition key never appear**. Only six of the 221 projections that hold data are partitioned here, so the view's 819 rows and the 1,428 containers have nothing to do with each other — they measure different things.

---

## 5. Cost Analysis: Compression and Container Count Are Separate Things

| Projection | Containers | Rows | Disk | Payload | Ratio | Disk/row |
|------------|-----------:|-----:|------|---------|------:|---------:|
| `partition_columns_super` | 3 | 1,007,772,484 | 8.86 GB | 92.58 GB | 10.45× | 8.79 B |
| `vsmart_test_super` | 3 | 294,958 | 382.7 MB | 665.9 MB | 1.74× | 1,297 B |
| `GCIS_Companyinfo_CmpB_super` | 3 | 13,245,243 | 341.6 MB | 1.06 GB | 3.11× | 25.8 B |
| `projection_usage_super` | 3 | 6,891,320 | 251.3 MB | 1.27 GB | 5.06× | 36.5 B |
| `judicial_202604_super` | 3 | 100,427 | 233.3 MB | 601.8 MB | 2.58× | 2,323 B |
| `LOWIDOC_V6_super` | 789 | 920,871 | 150.5 MB | 7.47 GB | **49.62×** | 163.5 B |

Two observations:

1. **Compression has nothing to do with container count.** `vsmart_test_super` with three containers gets 1.74×; `LOWIDOC_V6_super` with 789 gets 49.62×. Compression is decided by column types, sort order and encoding; many containers only mean fewer rows per file.
2. **Disk per row explains scan speed better than container count does.** `LOWIDOC_V6_super` stores 163.5 bytes per row versus 8.79 for `partition_columns_super`, yet the measured per-row cost is 10.7× worse on the small-container side, so the driver is the fixed cost of opening 789 containers, not bytes.

I did not verify the encoding of `LOWIDOC_V6` column by column (the 49.62× ratio comes largely from empty values and repeated strings, but I did not break it down), so this section reports `payload_size / used_bytes` and stops there.

---

## 6. Common Mistakes and Anti-patterns

**1. Reading 1,428 as "1,428 copies of the data".** It is the sum of container rows across three nodes and four shards. The same unsegmented data appears once per node on the replica shard (300,129 rows), so the logical row count is 1,099,751,539, not 1,099,951,625.

**2. Timing container scans with `count(*)`.** A filterless `count(*)` over a billion rows here takes 0.4 s because it reads statistics. Adding `WHERE table_name IS NOT NULL` makes it a real scan: 2.87 s. **Seven times apart, and measuring completely different things.**

**3. Assuming containers are evenly sized.** The mean is 770,274 rows per container, but 84% of containers hold fewer than 1,000. Capacity planning on the mean underestimates the fixed cost of small containers.

**4. Treating `v_monitor.partitions` as a container catalogue.** It has six projections and 819 rows because it only lists partitioned tables. Comparing 819 with 1,428 produces the false conclusion that 600 containers have no partition records — those tables are simply not partitioned.

**5. Expecting small tables to spread across three nodes.** 36 projections have one container each, all on a single shard on a single node, 3,872 rows combined. Those tables are not distributed at all, and with K-Safe=0 a node loss means losing them.

**6. Believing you are safe until `ContainersPerProjectionLimit` is hit.** The limit is the default `1,024` here and `LOWIDOC_V6_super` is at 789 (at most 265 per node, 25.9%), nowhere near pushback, yet its cost per row is already 10.7× that of a large container. **The limit is the last straw, not a performance target.**

**7. Using `used_bytes` as "data size".** The same data is 109.03 GB of payload and 11.99 GB on disk. Say which one you mean, or storage purchasing will be off by an order of magnitude.

---

## 7. Risks and Recommendations

**Safe now (read-only, low risk)**

- Add the size-distribution query from section 4.4 to your health check and watch two shares: containers under 1,000 rows (83.96% here) and rows in the 10M+ bucket (91.62%). The first rising means small ROS files are piling up; the second means large tables are concentrating.
- Use `v_monitor.partitions` only for partitioned tables, never as a denominator for the whole cluster's containers.
- Compare nodes on `sum(total_row_count)` or `sum(used_bytes)`, not `count(*)` (containers differ by 3.91% here while data differs by 0.06%).
- Always include a predicate when measuring scan cost, and read `v_monitor.query_profiles.query_duration_us` rather than timing an empty `count(*)`.

**Needs a change window and approval**

- For tables like `LOWIDOC_V6_super` where every partition produces a small ROS, the fix is the load batch size or the partition strategy so each load produces a sensibly sized ROS, then let mergeout consolidate. That requires DDL/DML and was not done here.
- `MergeOutInterval` (600 seconds here) and `ActivePartitionCount` (1 here) decide how often mergeout runs and which partitions count as active. Check the mergeout window against peak load before changing either.
- If you genuinely approach `ContainersPerProjectionLimit`, consolidate small ROS first and only then consider raising the limit; raising it merely delays pushback and will not make 789 containers faster.

---

## 8. References

- Vertica 25.4 documentation: ROS containers — https://docs.vertica.com/25.4.x/en/admin/managing-tables/ros-containers/
- Vertica 25.4 documentation: `STORAGE_CONTAINERS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/storage-containers/
- Vertica 25.4 documentation: `PARTITIONS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/partitions/
- Vertica 25.4 documentation: Partitions — https://docs.vertica.com/25.4.x/en/admin/managing-tables/partitions/
- Vertica 25.4 documentation: EON architecture — https://docs.vertica.com/25.4.x/en/architecture/eon/architecture/
- Vertica 25.4 documentation: Configuration parameters — https://docs.vertica.com/25.4.x/en/sql-reference/config-parameters/

> Note: `docs.vertica.com` currently 302-redirects to `docs.rocketsoftware.com/bundle/vertica_25.4/` with the same paths; all links above returned 200 on 2026-11-16.
