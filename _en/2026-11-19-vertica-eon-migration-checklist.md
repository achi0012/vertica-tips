---
layout: post_en
title: "Enterprise to EON Migration Checklist: Fourteen Settings to Re-examine Before Go-Live"
date: 2026-11-19 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, migration, enterprise-mode, configuration, checklist, operations]
description: "Two databases of the same version on one machine — testdb (Enterprise, one node) and eondb (EON, three nodes, v25.4.0-0, K-Safe=0, four shards) — put side by side to re-check fourteen pre-go-live settings. Three structural gaps measured: storage locations 1 to 7, shards 0 to 4, and a depot of 540,144 MB per node (60% of the filesystem). The licence is Community Edition, 1TB, three nodes, so node expansion hits the licence before it hits the architecture."
image: /assets/images/og/vertica-eon-migration-checklist-en.png
faq:
  - q: "Does moving from Enterprise to EON always require downtime?"
    a: "The documentation is explicit. MIGRATE_ENTERPRISE_TO_EON runs in the foreground and blocks all operations in the same session until it returns, and the subsequent revive creates a new EON database while the database is DOWN. I did not execute it locally because it rewrites testdb, but the documented conversion results say DATA and DATA,TEMP storage locations are not migrated and the new DATA location sits on the depot path, so the data is written again."
  - q: "Why is the depot on this cluster set to 60% instead of 80%?"
    a: "v_catalog.storage_locations reports a per-node depot ceiling of 566,381,859,840 bytes (540,144 MB) against a 900,239 MB filesystem (695,635 used plus 204,604 free) — exactly 60.0%. MaxDepotSizePercent=80 is the ceiling, not the current value. The documentation also states the default is 60% and the maximum 80%, because 20% must remain for the catalog, Data Collector tables, and temp files."
  - q: "Is a three-node cluster with K-Safe=0 highly available?"
    a: "No. This cluster has three nodes, four shards, and six subscriptions, but only four are primary: node0001 holds the primary subscription for both replica and segment0001, while node0002 and node0003 hold one each. With K-Safe=0 every segment shard has exactly one subscriber, so losing one node makes a third of the data unavailable. The replica shard stores metadata for unsegmented projections; it is not a data copy."
  - q: "What goes wrong if you treat the three ports as three databases?"
    a: "Ports 1533, 2533, and 3533 land on v_eondb_node0001, node0002, and node0003 respectively and all report v25.4.0-0 — one cluster. Treating them as separate databases concentrates every connection on one node, which defeats Elastic Throughput Scaling, and it corrupts the node mapping in monitoring and backup work."
  - q: "Can an EON vbr backup be restored into Enterprise mode?"
    a: "No. The documentation states you cannot back up an Enterprise Mode database and restore it in Eon Mode, or vice versa. EON backups must go to a supported cloud storage location with cloud_storage_backup_path and cloud_storage_backup_file_system_path set, and the restore target must share the database name, node names, at least as many primary nodes, the same catalog directory, and the same ports."
---

<!--more-->

---

## 1. The Short Version

"Enterprise to EON" is one function call in the documentation — `MIGRATE_ENTERPRISE_TO_EON('s3://bucket', '/depot/path', false)` — and fourteen re-examined assumptions in practice. This is not a translation of the docs, because this machine happens to run both modes at the same version:

| Aspect | `testdb` (Enterprise) | `eondb` (EON) |
|--------|-----------------------|----------------|
| Version | `Vertica Analytic Database v25.4.0-0` | same |
| Nodes | 1 | 3 |
| `v_catalog.shards` | **0 rows** | **4 rows** (replica plus segment0001–0003) |
| `v_catalog.storage_locations` | **1 row** | **7 rows** |
| Licence | Community Edition, 1TB, 3-node limit | same |
| Projections | 448 | 486 |
| Rows | 1,506,960,224 | 1,099,951,727 |
| On disk | 75.59 GB | 12.05 GB |
| Containers | 1,366 (258 projections) | 1,387 (221 projections) |
| Resource pools | 9 | 9 (27 rows = 9 × 3 nodes) |
| `MergeOutInterval` | 300 | 600 |

(The two databases hold different data sets, so row counts and disk sizes are not comparable — they only bound the volume a migration has to move.)

Three structural gaps came out of the measurements, and the other eleven items orbit them:

1. **Storage locations go from one to seven.** `testdb` has a single `DATA,TEMP` local path. `eondb` has one communal (`s3://verticadb/eondb`, `has_catalog = true`), three `DATA,TEMP` paths, and three `DEPOT` paths. The catalog lives in communal storage, which is why EON backup and disaster recovery are a different problem.
2. **Shards go from zero to four.** Enterprise decides placement through projection segmentation; `v_catalog.shards` on `testdb` is empty. On EON the four hash ranges add up to exactly 2³² (1,431,655,739 + 1,431,655,739 + 1,431,655,818 = 4,294,967,296), with no gap.
3. **The depot goes from nothing to 540,144 MB per node.** `max_size = 566,381,859,840 bytes` against a 900,239 MB filesystem is exactly **60.0%** — the documented default. `MaxDepotSizePercent = 80` is only the ceiling.

Of the fourteen items, **nine can be settled with read-only SQL today**, three require a configuration change or downtime, and two rest on documentation and rehearsal (the EON vbr backup path and cross-mode restore). `MIGRATE_ENTERPRISE_TO_EON` was **not executed locally**: it rewrites `testdb`, which breaks this series' rule of only creating objects in my own schema. Everything in section 5 is therefore documented, not measured.

---

## 2. The Test Environment

| Item | Value |
|------|-------|
| Enterprise | `testdb`, one node `v_testdb_node0001`, `PERMANENT` |
| EON | `eondb`, nodes `v_eondb_node0001`–`0003`, all `PERMANENT`, `is_primary = true` |
| Version | `v25.4.0-0` on both |
| EON shards | 4 (K-Safe = 0, `ElasticKSafety = 1`, `elastic_cluster.scaling_factor = 4`, `maximum_skew_percent = 15`) |
| EON depot | `566,381,859,840` bytes (540,144 MB) per node, `disk_percent = 60%` |
| EON filesystem | 695,635 MB used, 204,604 MB free (22%), 900,239 MB total (879 GiB) |
| EON communal | `s3://verticadb/eondb`, `sharing_type = COMMUNAL`, `has_catalog = true` |
| Licence | Community Edition, 1TB, `node_restriction = 3`, Perpetual, on both |
| Connection | `connect_eondb.py` (`vertica_python`) to 192.168.100.9 on ports 1533 / 2533 / 3533 |
| Run window | from 2026-11-19 05:00 (Asia/Taipei) |

**Everything is read-only**: `SELECT`, `SHOW`, and system-table queries only. No `CREATE`, `INSERT`, `ALTER`, no `REBALANCE_SHARDS`, and no `vbr` operation against `eondb`.

---

## 3. The Core Idea: Three Responsibilities Change Hands

EON is not "Enterprise with the data moved to S3". Three things change ownership:

| Responsibility | Enterprise | EON | Evidence from this run |
|----------------|------------|-----|------------------------|
| Data placement | projection `SEGMENTED BY` | namespace shards | `testdb` 0 shard rows; `eondb` 4 |
| Catalog location | local catalog directory per node | communal storage | the communal row has `has_catalog = true` |
| Read cache | local ROS (which is the data) | depot (cache) plus communal (source of truth) | `UseDepotForReads = 1`, 540,144 MB depot ceiling |

### 3.1 What seven storage locations look like

```sql
=> SELECT node_name, location_path, location_usage, sharing_type, has_catalog, max_size
   FROM v_catalog.storage_locations ORDER BY location_usage, node_name;
```

```
                 | s3://verticadb/eondb                        | DATA      | COMMUNAL | t |          0
v_eondb_node0001 | /data/eondb/v_eondb_node0001_data           | DATA,TEMP | NONE     | f |          0
v_eondb_node0002 | /data/eondb/v_eondb_node0002_data           | DATA,TEMP | NONE     | f |          0
v_eondb_node0003 | /data/eondb/v_eondb_node0003_data           | DATA,TEMP | NONE     | f |          0
v_eondb_node0001 | /data/depot/eondb/v_eondb_node0001_depot    | DEPOT     | NONE     | f | 566381859840
v_eondb_node0002 | /data/depot/eondb/v_eondb_node0002_depot    | DEPOT     | NONE     | f | 566381859840
v_eondb_node0003 | /data/depot/eondb/v_eondb_node0003_depot    | DEPOT     | NONE     | f | 566381859840
```

The same query on `testdb` returns one row:

```
v_testdb_node0001 | /opt/data/testdb/v_testdb_node0001_data | DATA,TEMP | NONE | f |
```

**The communal row has an empty `node_name`**, because it is not on any node's disk. `has_catalog = true` means the catalog is in there too. That one column is the answer to "why can't EON backups just back up the nodes".

### 3.2 Shards and subscriptions

```sql
=> SELECT shard_name, shard_type, is_replicated, lower_hash_bound, upper_hash_bound
   FROM v_catalog.shards ORDER BY shard_name;
```

```
replica     | Replica | t |          |
segment0001 | Segment | f |        0 | 1431655738
segment0002 | Segment | f | 1431655739 | 2863311477
segment0003 | Segment | f | 2863311478 | 4294967295
```

```sql
=> SELECT node_name, shard_name, is_primary, subscription_state
   FROM v_catalog.node_subscriptions ORDER BY node_name, shard_name;
```

```
v_eondb_node0001 | replica     | t | ACTIVE
v_eondb_node0001 | segment0001 | t | ACTIVE
v_eondb_node0002 | replica     | f | ACTIVE
v_eondb_node0002 | segment0002 | t | ACTIVE
v_eondb_node0003 | replica     | f | ACTIVE
v_eondb_node0003 | segment0003 | t | ACTIVE
```

Six subscriptions, four primaries. **The replica shard exists on all three nodes but only node0001 is its primary subscriber.** It holds metadata for unsegmented projections, not a copy of the data.

---

## 4. The Fourteen Checks

Each item gives the Enterprise measurement, the EON measurement, what to confirm before go-live, and the most common misconception. Every number in the first two rows was measured in this run.

### The fourteen at a glance

| # | Check | Gap measured here | How to verify |
|---|-------|-------------------|---------------|
| 1 | Data placement / shards | 0 to 4 shards | `v_catalog.shards` |
| 2 | K-Safe | single node to K-Safe 0, 6 subscriptions | `node_subscriptions`, warning 6046 |
| 3 | Depot size | none to 540,144 MB per node (60%) | `storage_locations`, `disk_storage` |
| 4 | Communal storage | none to `s3://verticadb/eondb`, `has_catalog` | `storage_locations` |
| 5 | Tuple Mover | 8,391 mergeouts to 6, no moveout | `tuple_mover_operations` |
| 6 | Resource pools | 9 pools to 27 rows (one set per node) | `resource_pool_status` |
| 7 | Monitoring metrics | 1,366 to 1,387 containers | `storage_containers` |
| 8 | Load paths | local ROS to communal plus depot | `load_streams` |
| 9 | Connection endpoints | 1 to 3, each pinned to a node | connect to each port once |
| 10 | Licensing | CE 1TB, 3-node limit | `licenses`, `GET_COMPLIANCE_STATUS()` |
| 11 | Time sync | 19 microseconds between simultaneous events | `node_states` |
| 12 | Version upgrades | both 25.4.0-0; about 45 s to restart | `version()`, `node_states` |
| 13 | Backup | local directory to cloud storage plus two required parameters | docs plus `storage_locations` |
| 14 | Disaster recovery | restore to `revive_db` | docs plus rehearsal |

Details follow.

### 4.1 Data placement: shards and segmentation

| | Detail |
|---|---|
| Enterprise | 448 projections over 417 tables; `elastic_cluster.segment_layout` = `v_testdb_node0001[100.0%]`, local layout 4 × 25% |
| EON | 4 shards over 3 nodes = 1.33 per node; node0001 holds 2 primaries, node0002 and node0003 hold 1 each |
| Confirm before go-live | Shard count is fixed when the namespace is created. The documentation recommends no more than a 2:1 shard-to-node ratio (1.33 here is inside it); changing it later requires `RESHARD_DATABASE` |
| Misconception | "Migration keeps the original segmentation." The documented conversion result is "same number of nodes, and an equal number of segmented shards" — shards equal nodes, not the old projection segmentation |

### 4.2 K-Safe and fault tolerance

| | Detail |
|---|---|
| Enterprise | Single node, no replication; `v_catalog.shards` returns 0 rows |
| EON | K-Safe = 0: the server returns `WARNING 6046: The database is currently 0-safe ...` on connect. `ElasticKSafety = 1` only permits elastic subscriptions; it does not create copies |
| Confirm before go-live | K-Safe decides how many subscribers each shard has. Here each of the four segment shards has exactly one primary subscriber, so losing a node loses a third of the data |
| Misconception | "Three nodes means three copies." Three nodes are three compute units; replication is visible in subscription `is_primary` and the shard count |

### 4.3 Depot size and `DiskSizePercent`

| | Detail |
|---|---|
| Enterprise | No DEPOT storage location; the depot is an EON concept |
| EON | Per node `max_size = 566,381,859,840` bytes = 540,144 MB = **60.0%** of the 900,239 MB filesystem; `disk_percent = 60%`, `MaxDepotSizePercent = 80` |
| Confirm before go-live | Size the depot from the hot data set, not the whole database. This cluster's projections occupy 12.05 GB against a 540 GB depot, so every read hits — which is also why a depot miss is unmeasurable here |
| Misconception | Reading `MaxDepotSizePercent = 80` as the current size. It is the ceiling; the current value is `storage_locations.max_size` and `disk_percent`, changed with `ALTER_LOCATION_SIZE` |

### 4.4 Communal storage and credentials

| | Detail |
|------|--------|
| Enterprise | None; data lives only under `/opt/data/testdb/...` |
| EON | `s3://verticadb/eondb`, `sharing_type = COMMUNAL`, `has_catalog = true`, `max_size = 0` (bounded by the object store) |
| Confirm before go-live | Object-store bandwidth and latency, bucket lifecycle rules, and whether the backup bucket is writable with the same credentials. Both `S3BucketConfig` and `S3BucketCredentials` are empty strings here, so credentials come from the environment or an instance profile, not from database parameters |
| Misconception | "Communal is just another DATA path." `has_catalog = true` says the catalog lives there, so losing communal loses the database |

### 4.5 Tuple Mover behaviour

| | Detail |
|---|---|
| Enterprise | `MergeOutInterval = 300`; `v_monitor.tuple_mover_operations` holds 8,391 Mergeout Start rows |
| EON | `MergeOutInterval = 600`; 6 Mergeout operations (all Complete, none executing), all on `v_internal_tables.license_consumption_metrics`, and **no moveout rows at all** |
| Confirm before go-live | The EON tuple mover only mergeouts shards for which the node is the primary subscriber. Data reaches communal storage through epochs (`EpochMapInterval = 180` seconds), not through moveout |
| Misconception | Thinking EON does not mergeout, or that a moveout ships ROS to communal storage. The only operation name observed here is Mergeout |

### 4.6 Resource pools and memory

| | Detail |
|---|---|
| Enterprise | 9 pools: `blobdata`, `dbd`, `general`, `jvm`, `metadata`, `recovery`, `refresh`, `sysquery`, `tm` |
| EON | The same 9, but `resource_pool_status` returns **27 rows** (9 × 3 nodes). `general` measured 24,278,522 / 24,288,148 / 24,286,161 KB (about 23.15 GiB per node) with `maxmemorysize` of `Special: 95%`; `tm` 3 GB, `sysquery` 1 GB, `metadata` 122,006 / 112,380 / 114,367 KB |
| Confirm before go-live | Memory is per node, not pooled across the cluster. The three `general` pools total 69.48 GiB, but no single query sees that budget |
| Misconception | Treating `maxmemorysize` as the query budget. Query budget is pool memory divided by planned concurrency, and `plannedconcurrency` is `AUTO` when unset |

### 4.7 Monitoring metrics

| | Detail |
|---|---|
| Enterprise | 1,366 containers over 258 projections; `projection_storage` 448 rows, 1,506,960,224 rows, 75.59 GB |
| EON | 1,387 containers over 221 projections, split 456 / 475 / 456 per node (19 containers, 4.2%, between the largest and smallest); `projection_storage` 1,302 rows, 1,099,951,727 rows, 12.05 GB |
| Confirm before go-live | Change the health-check tables: `storage_containers` for container and shard placement, `storage_locations` for the depot, `node_states` for restarts, `depot_fetches` for communal reads |
| Misconception | Using `v_monitor.partitions` to estimate capacity. It returns 819 rows covering 6 projections, while 221 projections have containers — most have no partition records at all |

### 4.8 Load paths

| | Detail |
|---|---|
| Enterprise | COPY writes local ROS; mergeout happens afterwards |
| EON | COPY writes communal storage and the depot at the same time. `v_monitor.load_streams` shows 4 loads; the largest moved 1,007,772,484 rows in 6,629,722 ms with 0 rejected — about **152,008 rows per second** |
| Confirm before go-live | That figure is one load stream against local S3. Recompute from the ETL host's bandwidth to communal storage, and watch `rejected_row_count` and `parse_complete_percent` |
| Misconception | Assuming EON loads are always slower. The slow pattern is many small commits, not EON itself; at this volume the bottleneck was commit and catalog work |

### 4.9 Connection endpoints and clients

| | Detail |
|---|---|
| Enterprise | `testdb` is a single node with a single endpoint, so there is no endpoint-to-node mapping to get wrong |
| EON | 1533 goes to `v_eondb_node0001`, 2533 to `node0002`, 3533 to `node0003`, all reporting `v25.4.0-0`; connect times were 277 ms (cold), 22 ms, and 25 ms. Node `node_address` values are 10.89.10.11–13 while the entry point is 192.168.100.9 |
| Confirm before go-live | Clients need a routable entry point and should round-robin across the three. Elastic Throughput Scaling assumes connections are spread; everything on one node removes the elasticity |
| Misconception | "Three ports means three databases or three copies of the data." Each port landed on a different node of the same cluster with the same catalog |

### 4.10 Licensing

| | Detail |
|---|---|
| Enterprise | Community Edition, 1TB, `node_restriction = 3`, `is_size_limit_enforced = true`, Perpetual |
| EON | The same licence; `GET_COMPLIANCE_STATUS()` reports raw 0.10TB of 1TB (10%) and node count 3 against a licence node limit of 3 |
| Confirm before go-live | **Node expansion hits the licence first**: Community Edition caps at three nodes and this cluster is already at the cap. Capacity is measured on raw data, not compressed disk (12.05 GB on disk corresponds to 0.10TB raw here) |
| Misconception | Assuming the licence counts disk usage, so good compression buys headroom. The audit uses raw data size and `license_audits` runs nightly at 23:59 |

### 4.11 Time synchronisation

| | Detail |
|---|---|
| Enterprise | `SHOW TimeZone` returns `UCT`; timestamps carry `+00:00` |
| EON | Also `UCT`. `v_monitor.node_states` records all three nodes shutting down on 2026-11-13 at 23:00:02, with timestamps `.922700`, `.922702`, and `.922719` — a **19-microsecond** spread |
| Confirm before go-live | EON epochs, catalog versions, and backup points all depend on node clocks. Verify all nodes share a chrony/NTP source and stay within milliseconds before migrating |
| Misconception | "A few seconds of skew does not matter." Nineteen microseconds is this machine's reality, not a guarantee; drift breaks the alignment between epochs and backup/restore points |

### 4.12 Version upgrades

| | Detail |
|---|---|
| Enterprise | `v25.4.0-0`; `MIGRATE_ENTERPRISE_TO_EON` requires source version 10.0 or later |
| EON | `v25.4.0-0`; `node_states` shows a restart on 2026-11-13 from SHUTDOWN at 23:00:02 to UP at 23:00:47, about 45 seconds for all three nodes |
| Confirm before go-live | Upgrade Enterprise to the target version before migrating. Cross-mode restore is not supported (Enterprise backups cannot be restored into EON and vice versa), and a restore can only target at most one minor version higher |
| Misconception | "Migrate first, upgrade later — it is faster." Cross-mode restore is explicitly forbidden, so that plan bundles the upgrade and the architecture change into one window |

### 4.13 Backup

| | Detail |
|---|---|
| Enterprise | `vbr` to a local directory or remote hosts (article 8 walked through `init`, `backup`, `listbackup`, and a restore into `vbr_demo`) |
| EON | Must back up to supported cloud storage with `cloud_storage_backup_path` and `cloud_storage_backup_file_system_path` set; HDFS communal storage does not support `copycluster` |
| Confirm before go-live | Restore requires the same database name, the same node names, at least as many primary nodes, the same catalog directory, and the same ports. Object-level restore also requires matching shard counts and boundaries, so take a fresh full backup after any `REBALANCE_SHARDS` |
| Misconception | "The data is in communal storage, so no backup is needed." If communal is deleted, credentials expire, or a lifecycle rule removes it, data and catalog disappear together — and in-db restore points live there too |

> This article **never ran `vbr` against `eondb`**. The EON backup section is documentation plus the measured `storage_locations` and `shards` output.

### 4.14 Disaster recovery

| | Detail |
|---|---|
| Enterprise | Restore to a cluster with the same node count and node names; `vbr restore` requires the database to be DOWN |
| EON | Two routes: `vbr restore` to another cluster meeting the same-name conditions, or `revive_db` from communal storage. `CLEAN_COMMUNAL_STORAGE(true)` removes files left behind by a failed restore |
| Confirm before go-live | Rehearse `revive_db` against a temporary cluster, never over production, and confirm the communal storage version and encryption keys are retrievable |
| Misconception | "Communal storage is disaster recovery." Communal is the source of truth, not a backup; backups belong in a different bucket with different credentials |

---

## 5. How `MIGRATE_ENTERPRISE_TO_EON` Actually Runs (documented, not measured)

The question DBAs ask first is "how long is the outage". The documented flow has five stages: prerequisites, compliance verification, execution, result inspection, and activation. Each step below is marked as documented or measured.

**Step 0: prerequisites (read-only, runs on `testdb`)**

```sql
-- version 10.0+, node state, elastic cluster
=> SELECT version();
Vertica Analytic Database v25.4.0-0

=> SELECT is_enabled, scaling_factor FROM v_catalog.elastic_cluster;
 t | 4
```

`testdb` reports `t` with `scaling_factor = 4`, satisfying the documented requirement that the source be an elastic cluster (any database created since 9.2.1 is, by default).

**Step 1: the four projection types EON rejects**

| Reason | Documented resolution |
|--------|-----------------------|
| Inconsistent with cluster segmentation (for example, after adding nodes) | `REBALANCE_CLUSTER()` or `REBALANCE_TABLE()` |
| Created with the `NODES` option, no elastic segmentation | Drop and recreate with `ALL NODES` |
| Uses the `GROUPED` clause | Drop and recreate without `GROUPED` |
| Data in unbundled storage containers (pre-7.2 databases) | `COMPACT_STORAGE()` |

`testdb` has 448 projections, 1,506,960,224 rows, and 75.59 GB. The catalog can tell me how many projections exist, but **a single-node database cannot verify the elastic segmentation difference** — every projection is on one node — so a dry run that writes `migrate_enterprise_to_eon_error.log` is mandatory on a real cluster.

**Step 2: dry run**

```sql
=> SELECT migrate_enterprise_to_eon('s3://dbbucket', '/vertica/depot', true);
```

The final `true` means verify compliance only. Problems are written to `migrate_enterprise_to_eon_error.log` in the database directory. **This step changes nothing and should always come first.**

**Step 3: execution**

```sql
=> SELECT migrate_enterprise_to_eon('s3://dbbucket', '/vertica/depot', false);
```

The documentation states the function runs in the foreground and blocks all operations in the same session until it returns. An interruption — client disconnect, network outage — fails the migration, and a rerun compares what is already in communal storage and copies only the unprocessed remainder. So **it resumes, but writes cannot continue during the copy**. Progress is visible in `v_monitor.database_migration_status` via `phase`, `status`, `bytes_to_transfer`, and `bytes_transferred`.

**Step 4: conversion results (the easiest place to be wrong)**

| Object | After migration |
|--------|-----------------|
| Node count / segmented shards | Same as the source (one-node `testdb` gives one shard) |
| Multiple buddy projections | One per group is migrated |
| `USER` and `TEMP` storage locations | Migrated |
| `DATA` and `DATA,TEMP` storage locations | **Not migrated**; the new defaults live on the depot path |
| Fault groups and storage policies | **Not migrated** |
| External procedures | **Not migrated** |
| Network settings (load balance groups, routing rules, subnets) | **Not migrated** |
| Depot size | **Defaults to 80% after revive**, not the source's 60% |

That last row is the point of this article. This EON database runs at 60% (540,144 MB), but a database migrated from Enterprise comes back at 80% by default. Adjust it with `ALTER_LOCATION_SIZE` after go-live, or the catalog and temp files are left with only 20% of the filesystem.

**Step 5: activation**

1. `admintools -t revive_db --communal-storage-location=... -d <db> -s <nodes>` (or the Management Console).
2. Check `controlmode` in `admintools.conf`. S3/AWS uses unicast and needs `point-to-point`; a cluster that used `broadcast` needs `admintools -t re_ip -d <db> -T`.
3. Start the database.
4. Run `CLEAN_COMMUNAL_STORAGE(true)` to clear migration leftovers.
5. Revisit depot size and shard count, resharding with `RESHARD_DATABASE` if needed.

---

## 6. Common Mistakes

**Mistake 1: assuming shard count can be fixed later.**
Shard count is fixed when the namespace is created. `RESHARD_DATABASE` rewrites shard boundaries, and until storage containers realign, queries must filter out data outside the new bounds. Four shards over three nodes (1.33) is a comfortable ratio here; if you plan to add nodes right after migration, pick the shard count before you migrate.

**Mistake 2: treating a three-node K-Safe=0 cluster as highly available.**
Six subscriptions, four primaries, and segment0002 has exactly one subscriber. Lose node0002 and a third of the segments have no primary. K-Safe of 1 or more needs enough shards and subscriptions, and that is a database-creation decision, not an `ALTER`.

**Mistake 3: reading `MaxDepotSizePercent = 80` as the depot's current size.**
The current value is `storage_locations.max_size`: 566,381,859,840 bytes here. Eighty percent is the cap; the setting is 60%. To see remaining room, read `v_monitor.disk_storage.disk_space_free_mb` (204,604 MB, 22% here), not the parameter.

**Mistake 4: expecting DATA/TEMP locations, fault groups, and external procedures to come along.**
The documentation is explicit that they do not. Discovering a missing fault group after go-live is a common reason to roll back.

**Mistake 5: expecting the depot to stay at 60% after migration.**
Revive defaults it to 80%. On this 900,239 MB filesystem that is 720,191 MB, leaving 180,048 MB for catalog and temp — exactly the 20% floor the documentation requires.

**Mistake 6: assuming communal storage makes backups unnecessary.**
Communal is the source of truth. Backups need another bucket and both `cloud_storage_backup_path` and `cloud_storage_backup_file_system_path`. In-db restore points live in communal storage too, so they vanish with it.

---

## 7. Risks and Recommendations

**Do now (read-only, low risk)**

1. Run the nine read-only queries from the fourteen checks against the source database and save the output of `storage_locations`, `shards`, `resource_pool_status`, `disk_storage`, and `license_audits` as a pre-migration baseline.
2. Confirm `elastic_cluster.is_enabled = t` (`testdb` is `t`).
3. Measure clock skew: compare timestamps of simultaneous events in `v_monitor.node_states` (19 microseconds here) or compare NTP offsets directly.
4. Estimate the transfer volume with `v_monitor.projection_storage` (1,506,960,224 rows, 75.59 GB on `testdb`) and divide by the object store's measured throughput to get a floor for migration time.
5. Run `migrate_enterprise_to_eon(..., true)` and read `migrate_enterprise_to_eon_error.log`.

**Needs change control (window and approval)**

1. The actual `MIGRATE_ENTERPRISE_TO_EON` run: it blocks writes in the foreground and requires reviving a new EON database.
2. Depot sizing (`ALTER_LOCATION_SIZE`): 60% now, 80% after revive unless someone changes it back.
3. Raising K-Safe or adding nodes: the Community Edition licence caps at three nodes and this cluster is at the cap, so licensing comes before architecture.
4. Creating the EON backup path and credentials: a separate bucket and a separate access key, not the communal ones "borrowed" for the purpose.

**Not recommended**

Migrating without a full backup, without a `revive_db` rehearsal, and without a dry-run log. Every EON number here came from read-only queries; the migration and recovery sections are documented, not executed on this machine.

---

## 8. References

- Migrating an enterprise database to Eon Mode (25.4): <https://docs.vertica.com/25.4.x/en/eon/migrating-an-enterprise-db-to-eon/>
- Eon Mode database requirements (backup and restore): <https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/eon-db-requirements/>
- Namespaces and shards: <https://docs.vertica.com/25.4.x/en/architecture/eon-concepts/shards-and-subscriptions/>
- Resizing depot caching capacity: <https://docs.vertica.com/25.4.x/en/eon/depot-management/resizing-depot-caching-capacity/>
- CREATE LOCATION (the 60% / 80% depot rules): <https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-statements/create-location/>
- REBALANCE_SHARDS: <https://docs.vertica.com/25.4.x/en/sql-reference/functions/management-functions/eon-functions/rebalance-shards/>
- Tuple mover: <https://docs.vertica.com/25.4.x/en/admin/managing-db/tuple-mover/>

---

*All numbers come from read-only queries on 2026-11-19 against `testdb` (Enterprise, one node) and `eondb` (EON, three nodes, K-Safe=0, four shards), both Vertica v25.4.0-0. No cluster was modified, and neither `MIGRATE_ENTERPRISE_TO_EON` nor `vbr` was executed.*
