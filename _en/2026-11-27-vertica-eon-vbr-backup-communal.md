---
layout: post_en
title: "EON Backups in Practice: vbr Still Copies Data, and the Copy-Free Option Is the In-DB Restore Point"
date: 2026-11-27 05:00:00 +0800
categories: vertica eon backup
tags: [vertica, eon, vbr, backup, communal-storage, restore-point, catalog]
description: "Read-only inspection of the three-node eondb cluster (v25.4.0-0, K-Safe=0): 1,410 containers, 221 projections, 12.05 GB of ROS data, yet zero rows in the vbr snapshot table, zero archives, and zero restore points. vbr --task listbackup answers Must specify a config file., because no EON backup configuration exists on this host. The bundled vbr.py shows the backup still copies objects one by one and only skips what the backup location already has, so 'EON backups only capture the catalog' is wrong. The mechanism that truly copies no data is the in-db restore point."
image: /assets/images/og/vertica-eon-vbr-backup-communal-en.png
faq:
  - q: "Does a vbr backup of an EON database copy data or only the catalog?"
    a: "It copies data. The vbr.py shipped with 25.4 first creates a snapshot with database_snapshot(), then copies every object in the snapshot manifest that the backup location does not already hold; the only EON-specific branch copies lib objects once. On this cluster the ROS objects total 12,047,811,278 bytes (12.05 GB), which is what a first full backup has to move."
  - q: "What does vbr return when an EON database has no configuration file?"
    a: "It prints Must specify a config file. followed by the usage text. That is exactly the state of eondb here: nothing EON-related under /opt/vertica/config and only testdb in admintools.conf, so vbr --task listbackup lists nothing at all."
  - q: "How does an in-db restore point differ from a vbr backup?"
    a: "An in-db restore point stores metadata only and copies no data, so it is fast and cheap, but it lives in the database catalog and disappears if communal storage is lost. A vbr backup copies objects to a separate backup location at the cost of space and time. On this cluster ARCHIVES and ARCHIVE_RESTORE_POINTS both return zero rows, so neither exists."
  - q: "Why can't v_monitor.disk_storage be used to size the catalog?"
    a: "Because it reports filesystem usage, not per-path usage. All nine paths on this cluster (three CATALOG, three DATA,TEMP, three DEPOT) report the same 694,374 MB used, 205,865 MB free and 22%, since they share one /data filesystem. Sizing the catalog requires du on the catalog directory on each node."
---

<!--more-->

---

## 1. The short version

"The data already lives in communal storage, so you only need to back up the catalog." I have seen that sentence in three different slide decks. It is half wrong, and the wrong half changes both your capacity planning and your restore drills.

On a local three-node eondb cluster (Vertica v25.4.0-0, K-Safe=0) I queried every backup-related state read-only, then read the bundled `vbr.py` (12,529 lines) to confirm the flow. Five findings:

1. **A vbr backup still copies data objects.** The flow is "call `database_snapshot()` to create a snapshot, then copy every object in the manifest that the backup path does not already have." There is no "skip ROS because this is EON" branch; the only EON special case copies `lib` objects once.
2. **The genuinely copy-free mechanism is the in-db restore point.** It is EON-only, created with `CREATE ARCHIVE` plus `SAVE RESTORE POINT TO ARCHIVE`, and stores metadata only. The price is that it lives in the database's own catalog, so it dies with communal storage.
3. **This cluster has neither.** `v_monitor.database_snapshots` returns zero rows, `ARCHIVES` returns zero rows, `ARCHIVE_RESTORE_POINTS` returns zero rows. That is 12.05 GB of data with no recovery point at all.
4. **This host has no vbr configuration for eondb.** `vbr --task listbackup` answers `Must specify a config file.`; there is nothing EON-related under `/opt/vertica/config`, and `admintools.conf` contains only testdb. EON backups must use `[CloudStorage]`, not the Enterprise `[Mapping]` section.
5. **"Restore point" means two different things here.** `vbr listbackup` calls each backup an archive (restore point) identifier stored in the backup location; an in-db restore point is an archive object inside the catalog. Same name, completely different lifetime, and mixing them up leads to bad decisions.

## 2. Environment and method

| Item | Value |
|---|---|
| Version | Vertica Analytic Database v25.4.0-0 (Community Edition) |
| Database | `eondb` (Eon Mode, three nodes) |
| Nodes | `v_eondb_node0001` to `0003`, all UP, all primary |
| Shards | 4 (3 segments plus 1 replica) |
| K-Safe | 0 (the server sends WARNING 6046 on connect) |
| Storage containers | 1,410, disk_size summing to 12,047,811,278 bytes |
| Projections with data | 221 |
| Method | SELECT only; no CREATE, no vbr backup or restore |

Confirm the version and nodes first; the same query also surfaces the K-Safe warning:

```sql
=> SELECT version();
 Vertica Analytic Database v25.4.0-0

=> SELECT node_name, node_state, is_primary FROM v_catalog.nodes ORDER BY node_name;
     node_name     | node_state | is_primary
-------------------+------------+------------
 v_eondb_node0001  | UP         | True
 v_eondb_node0002  | UP         | True
 v_eondb_node0003  | UP         | True
```

On connect, `vertica_python` prints `WARNING 6046: The database is currently 0-safe...`. That is not noise: K-Safe=0 means communal storage holds the only copy, so a node failure is survivable but a communal failure is not. Under that constraint an external backup is not a nice-to-have; it is the only safety net.

Everything here is read-only against eondb, because it is a shared test cluster: `CREATE ARCHIVE`, `SAVE RESTORE POINT` and `vbr --task backup` all write to the catalog or to a backup path and should not run without a maintenance window. Anything marked "not tested" comes from documentation or source reading, and I label it as such.

## 3. Correcting the sentence: what vbr actually backs up

Separate the two mechanisms first; most of the confusion comes from treating them as one.

| Mechanism | What it captures | Extra data copy | Where it lives | 25.4 syntax |
|---|---|---|---|---|
| vbr backup | catalog snapshot plus ROS objects (and libs) | **Yes**, full on the first run | External backup path (cloud object storage only for EON) | `vbr --task backup` |
| in-db restore point | metadata only | **No** | The database's own catalog / communal storage | `CREATE ARCHIVE`, `SAVE RESTORE POINT TO ARCHIVE` |

![EON backup boundary: communal storage holds the catalog and 12.05 GB of ROS objects, the three depots are caches that are not backed up, vbr copies the catalog snapshot and ROS objects to a separate cloud path, and an in-db restore point stores metadata only]({{ '/assets/images/eon-backup-boundary.svg' | relative_url }}){: loading="lazy" }

The point of the diagram is which arrow produces bytes: only the one heading right (vbr). The green box lives inside the same database boundary, so it is born and dies with communal storage.

### 3.1 What the code says

The bundled `/opt/vertica/bin/vbr.py` is 12,529 lines. Two functions define the backup path:

- `database_snapshot()` (starting at line 3,406) issues `select database_snapshot('<name>',false);` to create the snapshot inside the database.
- `process_manifests()` (around line 7,460) counts objects that exist in the node snapshot manifest but not in the backup manifest as `bytes_copying`; objects present on both sides only get their ref count updated and are counted as `bytes_skipping`.

In other words, **backup incrementality is per object**: the first run copies everything, later runs copy only new ROS objects. That is very visible on EON because ROS objects are immutable once written, so second and later backups are usually far smaller than the first. The first one is still a solid 12.05 GB.

The only EON-specific shortcut is for `lib` objects, copied once from the first node (lines 8,101 to 8,128), because all three nodes see the same library. There is no such skip for ROS objects.

### 3.2 The restore path agrees

Before restoring, vbr lists the objects **already present** in the target communal storage (`_get_communal_storage_sids()`, line 8,603) and then restores only what is missing (`get_objects_to_restore()`, line 8,654 onward). If the backup contained no data, that "only what is missing" logic would be pointless.

So the accurate statement is:

> A vbr backup of EON does copy ROS objects, sourced from the single copy in communal storage. It does not copy three times just because three nodes can see the same data, and after the first run it copies only new objects.

"Catalog only" is a property of the in-db restore point, projected onto vbr.

## 4. Test 1: seven storage locations, and only communal holds the catalog

```sql
=> SELECT location_id, node_name, location_path, location_usage, sharing_type, has_catalog
   FROM v_catalog.storage_locations
   ORDER BY location_usage, node_name, location_id;

     location_id     |     node_name     |              location_path               | location_usage | sharing_type | has_catalog
---------------------+-------------------+------------------------------------------+----------------+--------------+-------------
 45035996273705022   |                   | s3://verticadb/eondb                     | DATA           | COMMUNAL     | True
 45035996273705020   | v_eondb_node0001  | /data/eondb/v_eondb_node0001_data        | DATA,TEMP      | NONE         | False
 45035996273705160   | v_eondb_node0002  | /data/eondb/v_eondb_node0002_data        | DATA,TEMP      | NONE         | False
 45035996273705164   | v_eondb_node0003  | /data/eondb/v_eondb_node0003_data        | DATA,TEMP      | NONE         | False
 45035996273705260   | v_eondb_node0001  | /data/depot/eondb/v_eondb_node0001_depot | DEPOT          | NONE         | False
 45035996273705262   | v_eondb_node0002  | /data/depot/eondb/v_eondb_node0002_depot | DEPOT          | NONE         | False
 45035996273705264   | v_eondb_node0003  | /data/depot/eondb/v_eondb_node0003_depot | DEPOT          | NONE         | False
(7 rows)
```

Three columns tell you how to read those seven rows:

- **`has_catalog = True` appears only on the communal row.** The authoritative catalog lives in `s3://verticadb/eondb`; the local `/data/eondb/*_catalog` directories are working copies. That is why losing communal storage means losing the database.
- **`sharing_type`: only communal is `COMMUNAL`, the other six are `NONE`.** The three depots are per-node caches, not shared storage.
- **`location_usage`: `DATA`, `DATA,TEMP` and `DEPOT`.** Communal is plain `DATA` (no temp files on object storage), while node-local paths carry both DATA and TEMP.

A backup plan only cares about two rows: communal (catalog plus data) and depot (which you do not back up, because rebuilding it costs time, not data). **The depot is not a backup target**, and counting it is the most common way to over-size a backup.

## 5. Test 2: what the 12.05 GB consists of

```sql
=> SELECT count(*) AS containers, count(DISTINCT projection_id) AS projections,
          sum(total_row_count) AS total_rows, sum(disk_size) AS disk_bytes
   FROM v_monitor.storage_containers;

 containers | projections | total_rows  | disk_bytes
------------+-------------+-------------+------------
       1410 |         221 | 1099951985  | 12047811278
(1 row)
```

`sum(disk_size)` is 12,047,811,278 bytes, or 11.22 GiB (12.05 GB). That is compressed ROS, excluding the catalog and any temp usage.

Do not quote the 1,099,951,985 row count directly: it sums the row counts of 221 projections, so a table with two projections is counted twice. Real row counts have to come from the table level, not the container level.

The distribution across nodes is nearly even:

```sql
=> SELECT node_name, count(*) AS containers, sum(total_row_count) AS rows,
          sum(disk_size) AS disk_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

     node_name     | containers | rows       | disk_bytes
-------------------+------------+------------+------------
 v_eondb_node0001  |        477 |  366827980 | 4017450272
 v_eondb_node0002  |        478 |  366626953 | 4015342760
 v_eondb_node0003  |        455 |  366497052 | 4015018246
(3 rows)
```

Each node holds about 3.74 GiB, with 477, 478 and 455 containers. With four shards and three segments that is the expected shape; if one node is clearly short, look at shards and subscriptions before blaming backups.

The schema mix, by contrast, is very uneven:

```sql
=> SELECT schema_name, count(*) AS containers, count(DISTINCT projection_id) AS projections,
          sum(disk_size) AS disk_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 4 DESC LIMIT 8;

 schema_name  | containers | projections | disk_bytes
--------------+------------+-------------+-----------
 mtk          |         18 |           6 | 9333435257
 lowi_rap     |        124 |          39 |  905738367
 ADA          |        824 |          15 |  769713229
 public       |         68 |          22 |  636266388
 online_sales |          9 |           3 |  172612084
 store        |          9 |           3 |  130423989
 lowi         |         64 |          25 |   98629937
 a2v          |         93 |          37 |     444672
(8 rows)
```

`mtk` alone holds 9,333,435,257 bytes, **77.5%** of the data, in only 18 containers and 6 projections. `ADA` has 824 containers but accounts for 6.4%. Backup duration therefore tracks bytes, not container count: the first backup is dominated by those 8.7 GiB in `mtk`.

### 5.1 77 GB of historical writes is not 77 GB of backup

Every file the depot ships to communal storage is recorded:

```sql
=> SELECT count(*) AS uploads, sum(file_size_bytes) AS bytes,
          count(DISTINCT node_name) AS nodes,
          min(submit_time) AS first_upload, max(submit_time) AS last_upload
   FROM v_monitor.depot_uploads;

 uploads | bytes        | nodes | first_upload                   | last_upload
---------+--------------+-------+--------------------------------+-------------------------------
    2505 | 77005327737  |     3 | 2026-11-09 07:31:24.154117+00  | 2026-11-26 21:00:36.042876+00
(1 row)
```

2,505 files totalling 77,005,327,737 bytes (71.7 GiB), all `COMPLETED`, written between November 9 and November 26. That is **6.4 times** the 12.05 GB that currently exists.

The gap is merges: once ROS objects are merged, the old ones are no longer referenced by any catalog entry and never appear in a snapshot manifest. **A backup handles the objects that are alive now, not everything ever written.** Sizing a backup from `depot_uploads` overestimates by a factor of six; `storage_containers` is the right source. Conversely, if you are sizing communal storage *growth* (bucket capacity planning), then 77 GB over 17 days is exactly the number you want.

## 6. Test 3: this cluster has no backup and no restore point

Three tables, three sets of zero rows. Start with the vbr snapshot record:

```sql
=> SELECT snapshot_name, node_name, epoch, acquisition_timestamp,
          is_durable_snapshot, total_size_bytes, storage_cost_bytes
   FROM v_monitor.database_snapshots;

 snapshot_name | node_name | epoch | acquisition_timestamp | is_durable_snapshot | total_size_bytes | storage_cost_bytes
---------------+-----------+-------+-----------------------+---------------------+------------------+-------------------
(0 rows)
```

Then the two in-db restore point objects:

```sql
=> SELECT * FROM ARCHIVES;
 name | limit_count | owner
------+-------------+------
(0 rows)

=> SELECT id, save_time, index, archive, state, vertica_version
   FROM ARCHIVE_RESTORE_POINTS;

 id | save_time | index | archive | state | vertica_version
----+-----------+-------+---------+-------+----------------
(0 rows)
```

The three zeros mean different things:

- **`v_monitor.database_snapshots` is empty**: the cluster has never been backed up by vbr (each backup records a snapshot, including `total_size_bytes` and `storage_cost_bytes`). That view is also a ready-made source of backup sizes, once at least one backup exists.
- **`ARCHIVES` is empty**: nobody created an archive, so `SAVE RESTORE POINT` was never possible.
- **`ARCHIVE_RESTORE_POINTS` is empty**: no in-db restore point exists.

A cluster with 12.05 GB of data and K-Safe=0, with zero backups and zero restore points. That is the table worth showing your manager.

## 7. Test 4: this host has no vbr configuration for eondb

```bash
$ /opt/vertica/bin/vbr --task listbackup
Must specify a config file.
vertica backup and restore script help
-h --help                                    print help message
-t --task { backup | restore | copycluster |
            replicate | listbackup | init |
            quick-check | full-check | quick-repair |
            collect-garbage | remove }       specify task
...
```

It is not saying "no backups found"; it is saying "no configuration file to read". Two more checks:

```bash
$ ls /opt/vertica/config | grep -i eon
$ grep -c "Database:eondb" /opt/vertica/config/admintools.conf
0
```

There is no EON-related file under `/opt/vertica/config`, and `admintools.conf` contains only `[Database:testdb]` (`is_eon_mode = False`). The eondb cluster on this host was not created through admintools, and vbr will not go looking for it: **an EON backup always needs an explicit config file**.

### 7.1 The EON configuration looks different

The sample shipped with 25.4 (`/opt/vertica/share/vbr/example_configs/backup_restore_cloud_storage.ini`) says so up front:

```ini
[CloudStorage]
; !!Mandatory!! Backup location on Cloud or HDFS (no default).
cloud_storage_backup_path = s3://backup_bucket/database_backup_path/

; !!Mandatory!! directory used to manage locking during a backup (no default).
cloud_storage_backup_file_system_path = []:/home/dbadmin/backup_locks_dir/

[Database]
dbName = current_database
```

Three practical points:

1. **`[CloudStorage]` and `[Mapping]` are mutually exclusive.** The documented error is `Config has conflicting sections (Mapping, CloudStorage), specify only one of them.` Copying an Enterprise config and changing the database name fails right there.
2. **A backup path serves exactly one database.** The documentation is explicit that one path cannot be shared between databases.
3. **On-prem S3 needs two sets of environment variables.** The backup side uses `VBR_BACKUP_STORAGE_ACCESS_KEY_ID`, `SECRET_ACCESS_KEY` and `ENDPOINT_URL`; EON also needs the communal side, `VBR_COMMUNAL_STORAGE_ACCESS_KEY_ID`, `SECRET_ACCESS_KEY` and `ENDPOINT_URL`. They are not in the config file, so a config that looks fine does not mean a task that runs.

This section is documentation work; no backup was run here (no backup bucket, no maintenance window). **Not tested.**

## 8. Two different things both called restore points

| Aspect | vbr restore point | in-db restore point |
|---|---|---|
| Physical form | An archive (timestamp) in the backup location | An archive object in the catalog |
| Created by | `vbr --task backup` | `CREATE ARCHIVE` plus `SAVE RESTORE POINT TO ARCHIVE` |
| Inspected by | `vbr --task listbackup` | `SELECT * FROM ARCHIVES;` / `ARCHIVE_RESTORE_POINTS` |
| Copies data | Yes | No |
| If communal is lost | Survives (it is elsewhere) | Lost with it |
| Retention | `restorePointLimit` (default 1: one current plus one historical) | `CREATE ARCHIVE ... LIMIT n` |

The in-db syntax (documentation, **not tested**):

```sql
=> CREATE ARCHIVE vertica_restore LIMIT 15;
=> SAVE RESTORE POINT TO ARCHIVE vertica_restore;
```

It requires superuser or archive ownership. Its value is short-horizon, low-cost recovery: an accidental DROP, or a checkpoint before an upgrade. It is not off-site backup and cannot absorb a communal storage loss.

## 9. Sizing capacity and time

### 9.1 What can be calculated

- **The data volume of a first full backup is 12.05 GB** (`sum(disk_size)`), plus a catalog snapshot. The former is measured; the latter I could not measure (next section).
- **Later backups copy only new objects**, so the second backup is usually much smaller than the first. The exact size depends on how many ROS objects were created or merged in between, and that number only becomes visible once real backups exist (`database_snapshots.total_size_bytes`).
- **Retaining N restore points is not N times 12.05 GB.** Manifests track shared objects by ref count, so an object stays stored once as long as any retained restore point references it. Capacity is the union across retained points, not a multiplication.

### 9.2 What could not be measured, and why

I tried twice to size the catalog directory, and both attempts failed:

```bash
$ du -sh /opt/data/testdb/v_testdb_node0001_catalog
du: cannot read directory '/opt/data/testdb/v_testdb_node0001_catalog': Permission denied

$ du -sh /proc/2641687/root/data/eondb/v_eondb_node0001_catalog
du: cannot access '/proc/2641687/root/data/eondb/v_eondb_node0001_catalog': Permission denied
```

An ordinary account cannot read dbadmin's catalog directory, and the cluster runs in a different mount namespace. So this article gives no catalog size. The correct method is to run `du -sh <catalog dir>` as dbadmin on each node and take the maximum, since EON keeps one working copy per node.

### 9.3 Do not size anything with disk_storage

```sql
=> SELECT node_name, storage_usage, disk_space_used_mb, disk_space_free_mb,
          disk_space_free_percent
   FROM v_monitor.disk_storage ORDER BY storage_usage, node_name;
-- all 9 rows identical, 3 shown

     node_name     | storage_usage | disk_space_used_mb | disk_space_free_mb | disk_space_free_percent
-------------------+---------------+--------------------+--------------------+-------------------------
 v_eondb_node0001  | CATALOG       |             694374 |             205865 | 22%
 v_eondb_node0001  | DATA,TEMP     |             694374 |             205865 | 22%
 v_eondb_node0001  | DEPOT         |             694374 |             205865 | 22%
```

Three usages, three nodes, nine paths, all reporting the same numbers. The reason is simple: they all sit on the same `/data` filesystem, and `disk_storage` reports filesystem usage, not per-path usage. Treating 694,374 MB as the catalog size overestimates it by two orders of magnitude.

### 9.4 Time

I did not run a backup, so I will not invent a "how many minutes for 12 GB" number. The documented way to get one is `vbr --task backup --dry-run`, which evaluates the size and impact without copying anything. That requires a config file and a backup bucket; **not tested** here.

## 10. Restore flow (not tested, from documentation)

Restoring EON is not "copy the files back"; the prerequisites are stricter than in Enterprise:

1. **The target must share the database name, node names, catalog directory location and port numbers, with at least as many nodes as the source primary subclusters.** Miss one and vbr refuses.
2. **A full restore runs in this order: a reachable cluster first, then `vbr --task restore`, then start the database.** Restore overwrites the catalog directory; interrupting it leaves the database unusable until the task is rerun to completion.
3. **Restore only fills in objects missing from communal storage.** That is why restoring into the original communal location is usually much faster: most objects are already there.
4. **Object restore additionally requires matching shard counts, shard boundaries and node subscriptions for the namespace.** After adding nodes or rebalancing, an old object backup may no longer be restorable table by table; `--target-namespace` selects the destination namespace, and vbr creates it when it does not exist.
5. **Reviving from an in-db restore point is a separate path.** It returns the database to the state at save time without any backup files, provided communal storage still exists.

All of the above is documented flow; nothing was executed here (shared cluster, no backup files). **Not tested.**

## 11. Common mistakes and anti-patterns

1. **Treating communal storage as a backup.** "The data is already in S3, so we do not need backups" is the most expensive sentence in this topic. Communal storage is the live data store: a bad DROP, a corrupted catalog or a deleted bucket shows up right there. A backup must live in a **different** location.
2. **Treating an in-db restore point as off-site backup.** It copies no data, so it is the first thing to disappear when communal storage fails. The documentation is explicit: restore points live in the database and are lost if communal storage is compromised.
3. **Sizing catalog or depot with `v_monitor.disk_storage`.** Nine identical rows here (694,374 MB) because it is filesystem level. This mistake blows up capacity planning immediately.
4. **Expecting a `restorepoint` subcommand in vbr.** `vbr --help` on 25.4 lists backup, restore, copycluster, replicate, listbackup, init, quick-check, full-check, quick-repair, collect-garbage and remove only. Restore points are SQL (`SAVE RESTORE POINT TO ARCHIVE`), not a vbr task.
5. **Writing `[Mapping]` into an EON config.** You get `Config has conflicting sections (Mapping, CloudStorage)`. EON uses `[CloudStorage]` only.
6. **Running K-Safe=0 without an external backup.** That is this cluster exactly: zero vbr snapshots, zero archives, zero restore points. A node failure is recoverable; a communal failure is not.

## 12. Risks and recommendations

### Do now (read-only, low risk)

```sql
-- 1. Has this cluster ever been backed up?
SELECT count(*) AS snapshots, max(acquisition_timestamp) AS latest
FROM v_monitor.database_snapshots;

-- 2. Any in-db restore points?
SELECT count(*) AS archives FROM ARCHIVES;
SELECT count(*) AS restore_points FROM ARCHIVE_RESTORE_POINTS;

-- 3. How much data would a backup handle?
SELECT count(*) AS containers, sum(disk_size) AS disk_bytes
FROM v_monitor.storage_containers;

-- 4. Where is the authoritative catalog?
SELECT location_path, has_catalog FROM v_catalog.storage_locations
WHERE sharing_type = 'COMMUNAL';
```

Plus one non-SQL check: as dbadmin, confirm that `vbr --task listbackup -c <eondb config>` produces output. If it answers `Must specify a config file.` as it does here, there is not even a configuration yet.

### Requires change (window and approval)

1. **Create an archive and a restore point**: `CREATE ARCHIVE` plus `SAVE RESTORE POINT TO ARCHIVE`. Cheap, and it covers the "fat finger" class of incident first.
2. **Set up `[CloudStorage]`**: the backup bucket must be separate from communal storage, and credentials belong in environment variables, not the config file.
3. **Run a `--dry-run`**: get a real size and duration estimate before choosing a schedule and `restorePointLimit`.
4. **Do one full restore in an isolated cluster**: the EON prerequisites (same name, node names, catalog path, ports) are usually discovered to be unmet during the drill.

The order I recommend: in-db restore points for operator error, vbr external backups for communal failure, then a restore drill. Missing any one of the three means you performed backup activities, not that you have backups.

## 13. References

- [Backing up and restoring the database (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/)
- [Eon Mode database requirements (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/eon-db-requirements/)
- [Configuring cloud storage backups (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/setting-up-backup-locations/configuring-cloud-storage-backups)
- [\[CloudStorage\] configuration reference (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/vbr-config-file-reference/cloudstorage/)
- [Revive an Eon DB, including in-db restore points (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/eon/revive-eon-db/)
- [vbr reference (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/vbr-reference/)
