---
layout: post_en
title: "A Blind Spot in EON Data Distribution: 1,435 Containers, 221 Projections, and Only Six With Partition Records"
date: 2026-11-21 05:00:00 +0800
categories: vertica eon monitoring
tags: [vertica, eon, storage-containers, partitions, ros, audit, data-distribution, monitoring, 25.4]
description: "On a three-node eondb cluster (v25.4.0-0, K-Safe=0, four shards), v_monitor.storage_containers reports 1,435 containers across 221 projections and 11.99 GB, while v_monitor.partitions reports six projections, 819 rows, and 151 MB. The gap is not missing data: 819 equals the container count of exactly those six projections, and the other 215 sit on tables with no partition_expression. The join key is ros_id = storage_oid, not sal_storage_id."
image: /assets/images/og/vertica-eon-container-partition-audit-en.png
faq:
  - q: "Are partition records missing when v_monitor.partitions shows only six projections?"
    a: "No. The 819 partition rows equal the container counts of exactly those six projections, 789 + 11 + 9 + 4 + 3 + 3, with a difference of zero for each. The other 215 projections, 97.29% of the total, sit on tables whose partition_expression is NULL, so they have no partition key and cannot appear in partitions."
  - q: "Which columns join partitions to storage_containers?"
    a: "v_monitor.partitions.ros_id = v_monitor.storage_containers.storage_oid. All 819 partition rows matched in this run (819/819). Using sal_storage_id instead matches nothing (0/819), because sal_storage_id is a hexadecimal string while ros_id is a numeric OID."
  - q: "Which view should capacity estimates use?"
    a: "v_monitor.storage_containers. Of the 11.99 GB on this cluster, partitions covers only 151 MB, or 1.261%. Reporting from partitions alone understates capacity by 79 times and omits the largest object entirely, mtk.partition_columns_super, at three containers, 1.008 billion rows, and 8.86 GB."
  - q: "Why do those six projections have exactly as many partition rows as containers?"
    a: "Because partitions stores one row per partition key per ROS container. Each of those six partitioned tables writes one partition per container, so 789 containers produce 789 rows. Those 789 rows break down as 258 partitions present on all three nodes, six on two nodes, and three on one node."
  - q: "Why is active_partition_count NULL for partitioned tables?"
    a: "On this eondb cluster (v25.4.0-0), all 34 tables that declare a partition_expression return NULL for active_partition_count, so the column cannot be used to count partitions. Count them from v_monitor.partitions instead: count(distinct partition_key) is 267 for ADA.LOWIDOC_V6."
---

<!--more-->

---

## 1. The Short Version

Put the two monitoring views side by side and the first question is always why the numbers refuse to agree:

```sql
=> SELECT count(*) AS containers, count(DISTINCT projection_id) AS projs,
          sum(total_row_count) AS rows, sum(used_bytes) AS bytes
   FROM v_monitor.storage_containers;
 containers | projs |    rows     |    bytes
------------+-------+-------------+--------------
       1435 |   221 | 1099951796  | 11987583195

=> SELECT count(*) AS part_rows, count(DISTINCT projection_id) AS projs,
          sum(ros_row_count) AS rows, sum(ros_size_bytes) AS bytes
   FROM v_monitor.partitions;
 part_rows | projs |   rows  |   bytes
-----------+-------+---------+-----------
       819 |     6 |  932819 | 151116590
```

1,435 containers against 819 partition rows, 221 projections against six, 11.99 GB against 151 MB. It looks like partitions is missing 98.7% of the data.

**It is not missing anything. The two views measure different populations.**

1. **The 819 rows are not a sample of the six projections; they are all of their containers.** 789 + 11 + 9 + 4 + 3 + 3 = 819, and a projection-by-projection comparison against `storage_containers` returns a difference of zero every time.
2. **The remaining 215 projections (97.29%) live on tables with no `partition_expression`.** No partition key means no partition, so `partitions` has nothing to list.
3. **Only six of the 34 partitioned tables hold any data.** The other 29 are empty: zero rows, zero containers, zero partition rows.
4. **Join the two views on `ros_id = storage_oid`.** All 819 rows matched here. Joining on `sal_storage_id` matches nothing, because one column is a numeric OID and the other is a hexadecimal string.
5. **Capacity estimates must come from `storage_containers`.** Reading `partitions` alone returns 151 MB where the truth is 11.99 GB, a 79x understatement that also ignores the largest table on the cluster.

The rest of this article proves each point with output from this run, then leaves three queries you can schedule.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Database | `eondb` (EON), three nodes, K-Safe = 0 |
| Version | `Vertica Analytic Database v25.4.0-0` |
| Shards | 4 (`replica` plus `segment0001` through `segment0003`) |
| Nodes | `v_eondb_node0001`, `0002`, `0003` |
| Data | 1,435 containers, 1,099,951,796 rows, 11,987,583,195 bytes (11.17 GiB) |
| Projections | 486 rows in `v_catalog.projections` over 434 distinct ids; 221 hold containers |
| Partitioned tables | 34 (`partition_expression` non-empty), of which six hold data |
| Client | `vertica_python` 1.4.0, `ssl=False` |
| Measured | 2026-11-21 05:00 CST (Asia/Taipei) |

**Read-only throughout.** Only `SELECT` and `EXPLAIN` ran; there was no `CREATE`, `DROP`, `INSERT`, `ALTER`, and no configuration parameter was touched. The EON series has held that line from the first article.

One detail that trips up cross-timezone reporting: the article date is Taipei time, while the cluster's `now()` returned `2026-11-20 21:02:15+00`. That eight-hour offset explains a lot of "yesterday's numbers" arguments.

---

## 3. The Core Idea: Two Views, Two Populations

| | `v_monitor.storage_containers` | `v_monitor.partitions` |
|---|---|---|
| One row is | one ROS container on one node | one partition key in one ROS container |
| Population | every projection that holds data | only partitioned tables, and only partitions that have a container |
| Rows here | 1,435 | 819 |
| Projections | 221 | 6 |
| Bytes covered | 11,987,583,195 | 151,116,590 (1.261%) |
| Has `partition_key` | no | yes |
| Typical use | capacity, distribution, container counts | partition granularity, per-partition size, pre-checks for DROP PARTITION |

`storage_containers` is the ledger of where data physically sits. `partitions` is a slice of that ledger along the partition dimension. The first can describe a table with no partitions at all; the second has nothing to say about such a table. That is the first layer of the gap.

![How storage_containers and partitions cover different populations: 434 projections narrow to six with partition records]({{ '/assets/images/eon-partitions-scope-funnel.svg' | relative_url }}){: loading="lazy" }

Every number in the diagram came from this run: 434, then 221, then 35, then 6. The 35 matters most. It proves the six are not a random sample but the intersection of "partitioned" and "has data".

---

## 4. Step by Step

### 4.1 Start With Both Totals

```sql
=> SELECT count(*) AS rows_total, count(DISTINCT projection_id) AS ids,
          count(DISTINCT projection_name) AS names,
          count(DISTINCT schema_name) AS schemas, count(DISTINCT node_name) AS nodes
   FROM v_monitor.storage_containers;
 rows_total | ids | names | schemas | nodes
------------+-----+-------+---------+-------
       1435 | 221 |   219 |      19 |     3
```

221 ids but 219 names: `users_super` and `short_url_super` each exist under two schemas (measured `count(distinct schema_name) = 2` for both). So "219 or 221" is not corruption, it is two different counting units. **Any report should say which one it means.**

The node breakdown is equally instructive:

```sql
=> SELECT node_name, count(*) AS containers, sum(total_row_count) AS rows, sum(used_bytes) AS bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;
     node_name     | containers |   rows    |   bytes
-------------------+------------+-----------+------------
 v_eondb_node0001  |        468 | 366827918 | 3997359299
 v_eondb_node0002  |        494 | 366626885 | 3995251668
 v_eondb_node0003  |        473 | 366496993 | 3994972228
```

Containers differ by up to 5.6% (468 to 494), while rows differ by 0.090%. **Uneven container counts are normal.** A container exists per projection per node that holds data, so a projection present on two nodes contributes two containers. Rows are the balance metric; containers are not.

### 4.2 Find the Projections With Containers but No Partitions

```sql
=> WITH c AS (SELECT projection_id, schema_name, projection_name,
                     count(*) AS containers, sum(total_row_count) AS rows, sum(used_bytes) AS bytes
              FROM v_monitor.storage_containers GROUP BY 1,2,3),
        p AS (SELECT projection_id, count(*) AS part_rows FROM v_monitor.partitions GROUP BY 1)
   SELECT count(*) AS projs, sum(c.bytes) AS bytes
   FROM c LEFT JOIN p USING (projection_id) WHERE p.projection_id IS NULL;
 projs |    bytes
-------+--------------
   215 | 11836466605
```

215 projections carrying 11,836,466,605 bytes have no partition rows. The top of that list is the biggest data on the machine:

```sql
=> WITH c AS (SELECT projection_id, schema_name, projection_name, count(*) AS containers,
                     sum(total_row_count) AS rows, sum(used_bytes) AS bytes
              FROM v_monitor.storage_containers GROUP BY 1,2,3),
        p AS (SELECT projection_id, count(*) AS pr FROM v_monitor.partitions GROUP BY 1)
   SELECT c.schema_name, c.projection_name, c.containers, c.rows, c.bytes
   FROM c LEFT JOIN p USING (projection_id) WHERE p.projection_id IS NULL
   ORDER BY c.bytes DESC LIMIT 6;
 schema_name |         projection_name          | containers |   rows    |   bytes
-------------+----------------------------------+------------+-----------+------------
 mtk         | partition_columns_super          |          3 | 1007772484| 8859731258
 public      | vsmart_test_super                |          3 |    294958 |  382685908
 ADA         | GCIS_Companyinfo_CmpB_super      |          3 |  13245243 |  341623643
 mtk         | projection_usage_super           |          3 |   6891320 |  251250854
 public      | judicial_202604_super            |          3 |    100427 |  233305915
 lowi_rap    | gcis_companyinfo_cmpb_super      |          3 |  17210299 |  178673066
```

The first row deserves attention: `mtk.partition_columns_super` has "partition" in its name and is not partitioned at all.

```sql
=> SELECT table_schema, table_name, partition_expression, partition_group_expression
   FROM v_catalog.tables WHERE table_name = 'partition_columns_super';
(0 rows)
```

A table name proves nothing. The only authority on whether a table is partitioned is `partition_expression` in the catalog.

### 4.3 So Why Six? Reconcile Against the Catalog

List every projection that belongs to a partitioned table, then left join both monitoring views:

```sql
=> WITH c AS (SELECT projection_id, count(*) AS containers, sum(total_row_count) AS rows
              FROM v_monitor.storage_containers GROUP BY 1),
        p AS (SELECT projection_id, count(*) AS part_rows FROM v_monitor.partitions GROUP BY 1)
   SELECT t.table_schema, t.table_name, pr.projection_name,
          coalesce(c.containers,0) AS containers, coalesce(p.part_rows,0) AS part_rows
   FROM v_catalog.projections pr
   JOIN v_catalog.tables t ON pr.anchor_table_id = t.table_id
   LEFT JOIN c ON c.projection_id = pr.projection_id
   LEFT JOIN p ON p.projection_id = pr.projection_id
   WHERE t.partition_expression IS NOT NULL AND t.partition_expression <> ''
   ORDER BY 4 DESC;
 table_schema |      table_name      |      projection_name      | containers | part_rows
--------------+----------------------+---------------------------+------------+-----------
 ADA          | LOWIDOC_V6           | LOWIDOC_V6_super          |        789 |       789
 lowi_rap     | news_article_i18n    | news_article_i18n_p       |         11 |        11
 public       | tcpd_carcheck        | tcpd_carcheck_super       |          9 |         9
 a2v_test     | wiznew               | wiznew_super              |          4 |         4
 ADA          | LOWI_HASHTAG         | LOWI_HASHTAG_super        |          3 |         3
 lowi         | lowi_hashtag         | lowi_hashtag_super        |          3 |         3
 a2v_test     | target_partitioned   | target_partitioned_super  |          0 |         0
 ami_dg       | data_abnorm_log      | data_abnorm_log_super     |          0 |         0
 ...          | (the other 27 tables all show 0 containers)              |          0 |         0
(35 rows)
```

Three facts hold at once:

- **34 tables declare a `partition_expression`, and they own 35 projections.** Six of those projections hold containers, and for each one `part_rows` equals `containers` exactly: 789=789, 11=11, 9=9, 4=4, 3=3, 3=3.
- **29 projections have zero containers**, meaning empty tables. Their absence from `partitions` is not an omission; there is no data to describe.
- **6 + 29 = 35.** Nothing is unaccounted for. The 819 rows are exactly what those six projections contain.

Turn the check into a query that returns rows only when something is wrong, which is what belongs in a health check:

```sql
=> WITH c AS (SELECT projection_id, count(*) n FROM v_monitor.storage_containers GROUP BY 1),
        p AS (SELECT projection_id, count(*) n FROM v_monitor.partitions GROUP BY 1),
        part AS (SELECT pr.projection_id FROM v_catalog.projections pr
                 JOIN v_catalog.tables t ON pr.anchor_table_id = t.table_id
                 WHERE t.partition_expression IS NOT NULL AND t.partition_expression <> '')
   SELECT part.projection_id, coalesce(c.n,0) AS containers, coalesce(p.n,0) AS part_rows
   FROM part LEFT JOIN c USING (projection_id) LEFT JOIN p USING (projection_id)
   WHERE coalesce(c.n,0) <> coalesce(p.n,0);
(0 rows)
```

Zero rows, in 29 ms. This is worth running daily: it catches a real problem, where a partitioned table's container count and partition record count diverge, for example after a partition is detached or a container is lost.

### 4.4 The Join Key Is `ros_id` to `storage_oid`, Not `sal_storage_id`

The two views share no column name, so the instinct is to grab whatever looks like an ID and use `sal_storage_id`. Both variants, measured:

```sql
=> SELECT count(*) AS matched FROM v_monitor.partitions p
   JOIN v_monitor.storage_containers s ON p.ros_id = s.sal_storage_id AND p.node_name = s.node_name;
 matched
---------
       0

=> SELECT count(*) AS matched FROM v_monitor.partitions p
   JOIN v_monitor.storage_containers s ON p.ros_id = s.storage_oid AND p.node_name = s.node_name;
 matched
---------
     819
```

819 out of 819. The documentation for `storage_oid` warns that the same OID can appear on more than one node, so the defensive form also matches on `node_name`. Without that clause the same run still returns 819:

```sql
=> SELECT count(*) FROM v_monitor.partitions p JOIN v_monitor.storage_containers s ON p.ros_id = s.storage_oid;
 count
-------
   819
```

The difference is between "fine today" and "fine the day an OID repeats". If an OID ever shows up on two nodes, the join without `node_name` doubles and every `sum()` over it overstates. Adding `AND p.node_name = s.node_name` costs nothing; hard-code it.

### 4.5 Where 819 Comes From: Decomposing the 789

One projection, `ADA.LOWIDOC_V6_super`, accounts for 789 of the 819 rows. Worth pulling apart:

```sql
=> SELECT node_name, count(*) AS part_rows, count(DISTINCT partition_key) AS keys
   FROM v_monitor.partitions WHERE projection_id = 45035996273708334 GROUP BY 1 ORDER BY 1;
     node_name     | part_rows | keys
-------------------+-----------+------
 v_eondb_node0001  |       262 |  262
 v_eondb_node0002  |       265 |  265
 v_eondb_node0003  |       262 |  262

=> WITH k AS (SELECT partition_key, count(DISTINCT node_name) AS nodes, sum(ros_row_count) AS rows
              FROM v_monitor.partitions WHERE projection_id = 45035996273708334 GROUP BY 1)
   SELECT nodes, count(*) AS part_keys, sum(rows) AS rows FROM k GROUP BY 1 ORDER BY 1;
 nodes | part_keys |  rows
-------+-----------+--------
     1 |         3 |      3
     2 |         6 |     36
     3 |       258 | 920832
```

Of 267 partition keys, 258 have a container on all three nodes, six on two nodes, three on only one: 258×3 + 6×2 + 3×1 = **789**. Exact.

**The practical point:** `count(DISTINCT partition_key)` is 267 and `count(*)` is 789. The first answers "how many partitions does this table have", the second "how many containers did those partitions produce". Both are correct; they answer different questions. The documentation is explicit that the view stores one row per partition key per ROS container.

The three partitions that landed on a single node hold one row each; the six on two nodes hold 36 rows between them. **A partition is not replicated to every node.** Rows route to a segment by hash, which is also why you cannot derive partition row counts as "partitions times nodes" in EON.

### 4.6 Consistency: No Orphan Rows

```sql
=> SELECT count(*) AS orphans FROM v_monitor.partitions p
   WHERE NOT EXISTS (SELECT 1 FROM v_monitor.storage_containers s WHERE s.storage_oid = p.ros_id);
 orphans
---------
       0

=> SELECT sum(CASE WHEN partition_key IS NULL THEN 1 ELSE 0 END) AS null_keys, count(*) AS total
   FROM v_monitor.partitions;
 null_keys | total
-----------+-------
         0 |   819
```

All 819 partition rows resolve to a container, and no partition key is NULL. The six projections measured here are complete, not a partially populated view.

---

## 5. Performance and Plan Shape

Timings on this cluster, initiated on one node and executed on all three:

| Query | First run | Median of 5 |
|-------|-----------|-------------|
| Single-view scan (1,435 or 819 rows) | 10–24 ms | — |
| Difference set, 215 projections (4.2) | 27 ms | **26.3 ms** (27.2 / 23.8 / 24.4 / 26.6 / 26.3) |
| Partitioned-table reconciliation (4.3) | 45 ms | — |
| Consistency check (4.6) | 29 ms | — |
| Per-schema capacity rollup (health query in section 7) | 44 ms | — |

A full scan this fast is explained by the plan:

```sql
=> EXPLAIN WITH c AS (...), p AS (...)
   SELECT c.schema_name, ..., coalesce(p.pr,0) FROM c LEFT JOIN p USING (projection_id)
   ORDER BY c.bytes DESC LIMIT 10;
+-SELECT  LIMIT 10 [Cost: 3K, Rows: 10 (NO STATISTICS)] (PATH ID: 0)
|  Execute on: Query Initiator
| +---> SORT [TOPK] [Cost: 3K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
| |      Order: c.bytes DESC
| |      Execute on: All Nodes
| | +---> JOIN MERGEJOIN(inputs presorted) [LeftOuter] [Cost: 3K, Rows: 10K (NO STATISTICS)] (PATH ID: 2)
| | |      Join Cond: (c.projection_id = p.projection_id)
| | |      Runtime Filter: (SIP1(TopK): c.bytes)
| | | +-- Outer -> GROUPBY HASH (GLOBAL RESEGMENT GROUPS) [Cost: 3K, ...] (PATH ID: 4)
| | | | +---> STORAGE ACCESS for storage_containers [Cost: 1K, ...] (PATH ID: 5)
| | | | |      Projection: default_namespace.v_monitor.storage_containers_p
| | | +-- Inner -> ... STORAGE ACCESS for p (PATH ID: 9)
| | | | |      Projection: default_namespace.v_internal.vs_partitions_p
```

Three things to notice:

1. **Each view has a physical projection** (`storage_containers_p`, `vs_partitions_p`), so the query scans 1,435 and 819 rows of system data rather than expanding the catalog.
2. **The `SIP1(TopK)` runtime filter** prunes the outer side before the join, which is most of why this finishes in 26 ms.
3. **`GLOBAL RESEGMENT GROUPS`** means each node aggregates locally and resegments to the initiator. That is normal EON behaviour, not a misconfiguration.

One more line from the partitions side of the plan is worth keeping in mind:

```
The following JGNodes/Tables have been pruned due to uniqueness guarantee:
Subquery for Node 1 (default_namespace.v_internal.vs_partitions):
| +-Node 1 d (default_namespace.v_monitor.delete_vectors)
```

`delete_vectors` was pruned by uniqueness optimization. In this run every one of the 819 partition rows and 1,435 containers reports `deleted_row_count = 0`, so that pruning is harmless here. On a cluster with heavy DELETE traffic the plan shape changes, and `ros_size_bytes` can drift away from `used_bytes`.

---

## 6. Common Mistakes

### Mistake 1: Estimating capacity from `partitions`

```
partitions only:   151,116,590 bytes (151 MB)
actual:         11,987,583,195 bytes (11.99 GB)
understated by: 79.3x
```

The understatement is not even the worst part. `mtk.partition_columns_super` alone is 8.86 GB, 73.91% of all bytes, and it contributes zero partition rows. Any capacity report reading only `partitions` will declare this cluster nearly empty.

### Mistake 2: Joining on `sal_storage_id`

```
p.ros_id = s.sal_storage_id  -> 0 rows
p.ros_id = s.storage_oid     -> 819 rows (correct)
```

`sal_storage_id` looks like `02009b2cd7db9cfaad6bf701051f89e600a00000000049af`, a hexadecimal string. `ros_id` looks like `45035996273755056`, a number. Different types, different namespaces. The failure mode is quiet: zero rows, which gets misread as "this table has no partitions".

### Mistake 3: Counting partitions with `active_partition_count`

```sql
=> SELECT count(*) AS tables_with_part_expr,
          sum(CASE WHEN active_partition_count IS NULL THEN 1 ELSE 0 END) AS null_cnt
   FROM v_catalog.tables WHERE partition_expression IS NOT NULL AND partition_expression <> '';
 tables_with_part_expr | null_cnt
-----------------------+----------
                    34 |       34
```

All 34 partitioned tables return NULL. The column exists in the catalog but carries no value on this EON cluster. Count from `v_monitor.partitions` instead: `count(DISTINCT partition_key) = 267` for `ADA.LOWIDOC_V6`.

### Mistake 4: Treating `start_epoch` as a timestamp

```sql
=> SELECT min(start_epoch) AS min_e, max(start_epoch) AS max_e, count(DISTINCT start_epoch) AS distinct_e
   FROM v_monitor.storage_containers;
 min_e | max_e | distinct_e
-------+-------+------------
    10 |   394 |        252

=> SELECT to_timestamp(min(start_epoch)), to_timestamp(max(start_epoch)) FROM v_monitor.storage_containers;
    to_timestamp     |    to_timestamp
---------------------+---------------------
 1970-01-01 00:00:10 | 1970-01-01 00:06:34
```

The values run from 10 to 394, and converting them yields January 1970. This is Vertica's logical epoch counter, not Unix seconds. Filtering with `WHERE start_epoch > now() - interval '1 day'` returns zero rows, exactly as it did in this run, and invites the conclusion that nothing new has arrived.

### Mistake 5: Writing container totals into an SLA

The ROS article from 2026-11-16 measured 1,428 containers on this machine. Today it is 1,435: seven more in five days, with 171 additional rows and 4,164 additional bytes. Containers are the tuple mover's output; a mergeout changes the count. **Trends are useful, absolute values are not.**

---

## 7. Risks and Recommendations

### Safe now (read-only, low risk)

1. **Build capacity reports on `storage_containers`.** Treat `partitions` as supplementary, for the partition dimension only.
2. **Schedule the 4.3 reconciliation daily.** It returns zero rows when healthy and names the projection when not.
3. **Join on `ros_id = storage_oid`, with `node_name`.** Never `sal_storage_id`.
4. **State the counting unit in every report.** 219 names, 221 ids, and 1,435 containers are three different numbers; mixing them guarantees reconciliation arguments.

Three queries ready to schedule:

```sql
-- 1. Real capacity per schema, with partition coverage
WITH c AS (SELECT projection_id, schema_name, count(*) AS containers,
                  sum(total_row_count) AS row_cnt, sum(used_bytes) AS used_bytes
           FROM v_monitor.storage_containers GROUP BY 1,2),
     p AS (SELECT projection_id, count(*) AS part_rows FROM v_monitor.partitions GROUP BY 1)
SELECT c.schema_name,
       sum(c.containers)             AS containers,
       sum(c.row_cnt)                AS row_cnt,
       sum(c.used_bytes)             AS used_bytes,
       sum(coalesce(p.part_rows, 0)) AS part_rows
FROM c LEFT JOIN p ON p.projection_id = c.projection_id
GROUP BY 1 ORDER BY 4 DESC;

-- 2. Partitioned-table reconciliation (zero rows is healthy)
WITH c AS (SELECT projection_id, count(*) n FROM v_monitor.storage_containers GROUP BY 1),
     p AS (SELECT projection_id, count(*) n FROM v_monitor.partitions GROUP BY 1),
     part AS (SELECT pr.projection_id FROM v_catalog.projections pr
              JOIN v_catalog.tables t ON pr.anchor_table_id = t.table_id
              WHERE t.partition_expression IS NOT NULL AND t.partition_expression <> '')
SELECT part.projection_id, coalesce(c.n,0) AS containers, coalesce(p.n,0) AS part_rows
FROM part LEFT JOIN c USING (projection_id) LEFT JOIN p USING (projection_id)
WHERE coalesce(c.n,0) <> coalesce(p.n,0);

-- 3. Large projections with no partition coverage at all
WITH c AS (SELECT projection_id, schema_name, projection_name, count(*) AS containers,
                  sum(total_row_count) AS rows, sum(used_bytes) AS bytes
           FROM v_monitor.storage_containers GROUP BY 1,2,3)
SELECT c.schema_name, c.projection_name, c.containers, c.rows, c.bytes
FROM c LEFT JOIN (SELECT DISTINCT projection_id FROM v_monitor.partitions) p USING (projection_id)
WHERE p.projection_id IS NULL ORDER BY c.bytes DESC LIMIT 20;
```

Output of query 1 (top nine):

```sql
 schema_name  | containers |   row_cnt  | used_bytes | part_rows
--------------+------------+------------+------------+-----------
 mtk          |         18 | 1021448137 | 9285153505 |         0
 lowi_rap     |        124 |   48073315 |  903421199 |        11
 ADA          |        824 |   18434488 |  762136677 |       792
 public       |         68 |    1331253 |  635920412 |         9
 online_sales |          9 |    5001200 |  172013956 |         0
 store        |          9 |    5300250 |  129906261 |         0
 lowi         |         64 |     336230 |   98373713 |         3
 a2v          |         93 |       3606 |     304464 |         0
 a2v_test     |         84 |      13295 |      98365 |         4
```

Note how `part_rows` has to be aggregated per projection before rolling up. `ADA` reports 792, which is 789 + 3. Joining container rows straight to partition rows and summing inflates it to 789×789 + 3×3 = 622,530: a number that looks plausible and is completely wrong. The author tripped over exactly this while drafting and re-ran the query.

`ADA` holds 824 containers, 57.42% of the cluster's container count, for 18,434,488 rows and 762 MB. `mtk` holds 18 containers and 9.29 GB. **Container count has almost nothing to do with capacity**, which is mistake 5 seen from another angle.

### Requires change (needs a window and approval)

1. **Repartition the largest non-partitioned tables.** DDL, projection rebuilds, and both downtime and disk headroom have to be planned.
2. **Clean up the 29 empty partitioned projections.** `DROP PROJECTION` or dropping the table outright, after confirming nothing depends on them.
3. **Add partition groups to the seven partitioned tables that have none:** `ADA.LOWIDOC_V6`, `ADA.LOWI_HASHTAG`, `a2v_test.target_partitioned`, `a2v_test.wiznew`, `lowi.lowi_hashtag`, `lowi_rap.news_article_i18n`, `v_internal_tables.v_vbr_ref_count`. That is a design choice, not a defect, but with 267 partitions on `LOWIDOC_V6` already, container growth becomes the binding constraint. Confirm retention policy with the data owner first.

---

## 8. References

- Vertica 25.4.x documentation, `PARTITIONS` system table ("one row per partition key, per ROS container"): <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/partitions/>
- Vertica 25.4.x documentation, `STORAGE_CONTAINERS` system table (`STORAGE_OID`, `SAL_STORAGE_ID` definitions): <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/storage-containers/>
- Vertica 25.4.x documentation, `TABLES` system table (`PARTITION_EXPRESSION`, `ACTIVE_PARTITION_COUNT`): <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/tables/>
- Vertica 25.4.x documentation, `PROJECTIONS` system table: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/projections/>
