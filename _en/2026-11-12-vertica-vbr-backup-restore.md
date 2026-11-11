---
layout: post_en
title: "Vertica vbr Backup and Restore in Practice: A Full Enterprise Backup Walkthrough, the Restore Procedure, and How EON Differs"
date: 2026-11-12 05:00:00 +0800
categories: vertica backup vbr
tags: [vertica, vbr, backup, restore, enterprise-mode, eon-mode, communal-storage, disaster-recovery]
description: "Tested vbr on a single-node Enterprise-mode Vertica 25.4.0-0 database: a full backup needed 77.19 GB while /tmp had only 15.10 GiB free, so vbr refused at the free-space check. An object backup of two tables (302,000 and 20,000 rows, 4.57 MB payload) finished in 12.09 seconds but occupied 7.94 MB on disk, because every archive carries its own 3.37 MB catalog snippet. A second backup added only 12,981 bytes of object data plus 3.38 MB of catalog, 3.39 MB total. Restore was validated with --dry-run only: this host has no disposable target database, so nothing was restored over testdb."
image: /assets/images/og/vertica-vbr-backup-restore-en.png
faq:
  - q: "Do I need downtime to run a vbr backup?"
    a: "No. With testdb fully up and serving other sessions, `vbr --task backup` completed a 4.57 MB object backup in 12.09 seconds. Downtime is a restore requirement: the documentation states a full restore needs the database DOWN while an object restore needs it UP."
  - q: "How much smaller is an object backup than a full backup?"
    a: "On the same testdb, vbr estimated the full backup at 77,187,279,132 bytes (77.19 GB), while the object backup of two tables (302,000 plus 20,000 rows) was 4,568,464 bytes, a difference of 16,896 times. The full backup could not run here at all: the backup directory sits on /tmp with only 16,211,030,016 bytes (15.10 GiB) available."
  - q: "Why does a 4.57 MB backup occupy 7.94 MB on disk?"
    a: "Because each archive carries a catalog snippet. A single archive's `vbr_lab_snapshot.rst` measured 3,372,563 bytes, larger than the data being protected; backing up one 121,840-byte table still produced a 3,446,338-byte backup directory, 28.3 times the payload. The more often you back up, the more this fixed cost matters."
  - q: "Why is the timestamp in my vbr archive name eight hours off?"
    a: "Archive names use UTC. With the host at 2026-11-12 05:04 (+08), `vbr --task listbackup` reported `vbr_lab_snapshot_20261111_210405`, exactly eight hours earlier. Add the offset before correlating an archive with local logs."
  - q: "Can vbr back up a single table?"
    a: "Yes, add `objects = schema.table, schema.table` to the `[Misc]` section of the configuration file. Backing up only customers (20,000 rows, 121,840 bytes) took 12.17 seconds; naming a table that does not exist fails with `ERROR 11781: Unknown or unsupported table name`."
---

<!--more-->

---

## 1. The Short Version

A vbr backup is not "copy the data somewhere". It copies an **epoch-consistent snapshot**: a slice of the catalog (a snippet), the matching ROS object files, and metadata that describes the run. The ratio between those three determines your cost, and that ratio has almost nothing to do with how much data you have — it has everything to do with **how often you back up**.

On a single-node Enterprise-mode testdb running Vertica 25.4.0-0, I ran `init → backup → listbackup → full-check → restore --dry-run` against two tables (302,000 and 20,000 rows). Six findings:

1. **A full backup cannot succeed on this host.** vbr estimated `77,187,279,132` bytes (77.19 GB) against `16,211,030,016` bytes (15.10 GiB) available on the `/tmp` backup volume — 21.0% of what was needed — and aborted before copying a single file.
2. **Object backups are cheap enough to run daily.** Two tables, `4,568,464` bytes of payload, finished in `12.09` seconds. Full versus object differed by a factor of **16,896**.
3. **But every archive pays a fixed 3.37 MB catalog bill.** One archive's `.rst` measured `3,372,563` bytes; backing up a single 121,840-byte table still produced `3,446,338` bytes on disk (28.3x). **Backup directory size ≈ (archive count × 3.37 MB) + object data.**
4. **The second backup is not a full re-copy, but it is not free either.** After adding 2,000 rows, `Objects/` grew by only `12,981` bytes while `Snapshots/` grew by `3,379,095` bytes — `3,392,199` bytes total, matching "new data plus one more catalog copy".
5. **`full-check` belongs in your routine.** It reported `0 unreferenced objects` and `0 missing objects`, which is far cheaper than discovering a missing file during a restore.
6. **I did not run a restore against testdb.** This host has exactly one database (only testdb under `/opt/data`, and `admintools.conf` registers only testdb), so there is no disposable vbr_demo to restore into. I validated the flow and its parameters with `--dry-run`; the actual restore steps below are documented and labelled as such. That caveat applies to the whole article and I will not repeat it.

This is for three audiences: people setting up vbr for the first time, people whose backup volume just ran out of space, and people who have been asked "do we need separate backups for EON?"

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Mode | Enterprise (`is_eon_mode = False`) |
| Node | 1 (`v_testdb_node0001`, 192.168.100.9, Community Edition) |
| Database | `testdb`, port 5433, catalog at `/opt/data/testdb/v_testdb_node0001_catalog` |
| Test schema | `article_lab_vbr` (dropped with `DROP SCHEMA ... CASCADE` after the lab) |
| Table 1 | `customers`: 20,000 rows, 4 columns, 120,776 bytes in `projection_storage` |
| Table 2 | `orders`: 302,000 rows, 6 columns, `PARTITION BY date_trunc('month', order_ts)`, 7 partitions, 4,443,309 bytes |
| Backup directory | `/tmp/opencode/vbr_lab/backup` (same `/dev/sda2`, 212 GB free; `/tmp` itself is a 16 GB tmpfs) |
| vbr | `/opt/vertica/bin/vbr` (`vbr.py`, executed as dbadmin) |

The actual size of the seven `orders` partitions in `v_monitor.partitions` — useful later when matching backup files to data:

```
=> SELECT partition_key, ros_row_count, ros_size_bytes
   FROM v_monitor.partitions
   WHERE table_schema = 'article_lab_vbr' ORDER BY partition_key;

    partition_key    | ros_row_count | ros_size_bytes
---------------------+---------------+----------------
 2026-05-01 00:00:00 |         50544 |         745387
 2026-06-01 00:00:00 |         48913 |         726907
 2026-07-01 00:00:00 |         50544 |         742942
 2026-08-01 00:00:00 |         50543 |         742723
 2026-09-01 00:00:00 |         48913 |         727603
 2026-10-01 00:00:00 |         50543 |         745622
 2026-11-01 00:00:00 |          2000 |          12125
(7 rows)
```

---

## 3. What vbr Actually Backs Up: Four Things

| Component | What it looks like here | What sets its size |
|-----------|-------------------------|--------------------|
| Snapshot | Directory `Snapshots/<snapshot>_<UTC time>/<node>/` holding `.rst` (catalog snippet), `.txt` (SnapshotInfo), `.manifest`, `.info` | Almost fixed; depends on catalog complexity |
| Object files | `Objects/<hash dir>/<id>_0.gt`, one per ROS container | The actual data |
| Manifest | `backup_manifest`, roughly 745 bytes | Fixed |
| Metadata | `backupEpoch`, `ahmEpoch`, `catalogVersion`, `catalogRevision` inside the `.txt` | Fixed |

The contents of `vbr_lab_snapshot.txt` (excerpt) show the consistency boundary of a backup:

```
isObject:true
objectNames:
{
 :string
 _:article_lab_vbr.customers
 .
 :string
 _:article_lab_vbr.orders
 .
}
backupEpoch:2330902
ahmEpoch:2330901
isDurable:false
catalogVersion:v25.4.0-0
catalogRevision:ff72226db9584d058d44cac4c161a7ec5a0505b7
```

The two epochs differ by one: the snapshot's catalog version is `2330902` while AHM sits at `2330901`. AHM (Ancient History Mark) is the purge boundary: history below the AHM has already been purged and cannot appear in any backup, so a backup can only guarantee data above the AHM.

**That is also why a backup cannot replace a retention policy.** If purge drops partitions after 30 days, a 31-day-old partition was already gone before the backup ran; vbr will not, and cannot, bring it back. Backups protect against corruption, accidental deletion, and a broken catalog — not against scheduled expiry. That needs a longer retention window, or landing the data somewhere else before the purge.

---

## 4. Step by Step

### 4.1 Load the data

Both tables were loaded with `COPY ... DIRECT` from CSV, and `orders` is partitioned by month:

```
=> COPY article_lab_vbr.orders (order_id, customer_id, order_ts, amount, region, status)
   FROM '/tmp/opencode/vbr_lab/orders.csv' DELIMITER ',' DIRECT;
 Rows Loaded
-------------
      300000
```

The partition expression produces a real warning worth remembering:

```
WARNING 6100:  Using PARTITION expression that returns a Timestamp value
HINT:  This PARTITION expression may cause too many data partitions.
```

### 4.2 Write the vbr configuration

A minimal Enterprise configuration needs three sections. `objects` goes in `[Misc]`; the backup destination goes in `[Mapping]` as `node = host:directory`:

```ini
[Database]
dbName = testdb
dbUser = dbadmin
dbPassword = ********
dbPort = 5433

[Misc]
snapshotName = vbr_lab_snapshot
tempDir = /tmp/opencode/vbr_lab/vbr_tmp
objects = article_lab_vbr.customers, article_lab_vbr.orders

[Mapping]
v_testdb_node0001 = 192.168.100.9:/tmp/opencode/vbr_lab/backup

[Transmission]
concurrency_backup = 1
concurrency_restore = 1
hardLinkLocal = False
```

Two practical notes:

- `dbPassword` is **plaintext**. The file must be mode `600`, owned by the account running vbr (dbadmin here), and must never reach git.
- No `[Mapping]`, no destination. `vbr --help` does not tell you this, and the error only says the manifest is missing.

### 4.3 init

```
$ vbr --task init --config-file backup.conf
Initializing backup locations.
Backup locations initialized.
```

init only builds the directory skeleton and manifest; it **backs up no data**. Every new backup path needs its own init, otherwise even `--dry-run` fails:

```
Error: Manifest not found at rsync://[192.168.100.9]:50000/tmp/opencode/vbr_lab/backup_full
-- is the backup location initialized? Hint: Execute '--task init' first.
```

### 4.4 backup

```
$ time vbr --task backup --config-file backup.conf
Starting backup of database testdb.
Participating nodes: v_testdb_node0001.
Objects: ['article_lab_vbr.customers', 'article_lab_vbr.orders']
Snapshotting database.
Snapshot complete.
Approximate bytes to copy: 4568464 of 4568464 total.
Copying backup metadata.
Finalizing backup.
[==================================================] 100%
Backup complete!
```

- Duration: **12.09 seconds**
- The database stayed UP and other sessions kept querying
- `<snapshotName>_<UTC time>` becomes the archive name: `vbr_lab_snapshot_20261111_210405` (05:04 local, 21:04 UTC)

### 4.5 listbackup

```
$ vbr --task listbackup --config-file backup.conf
backup                             backup_type   epoch     objects
vbr_lab_snapshot_20261111_210709   object        2330902   article_lab_vbr.customers,article_lab_vbr.orders
vbr_lab_snapshot_20261111_210405   object        2330901   article_lab_vbr.customers,article_lab_vbr.orders
```

`backup_type` is `object`, not `full`, because the config names `objects`. Add `--json` for a machine-readable version suitable for monitoring (this is the output after the first backup, when only one archive existed):

```
{"num_backups": 1, "backups": [{"backup": "vbr_lab_snapshot_20261111_210405",
 "backup_type": "object", "epoch": "2330901",
 "objects": "article_lab_vbr.customers,article_lab_vbr.orders", ...}]}
```

### 4.6 A second backup after adding 2,000 rows

I inserted 2,000 November rows (creating a seventh partition) and ran the same backup:

```
Approximate bytes to copy: 4581445 of 4581445 total.
Backup complete!          # 13.02 seconds
```

How the backup directory changed:

| Location | After first | After second | Delta |
|----------|-------------|--------------|-------|
| `Objects/` | 4,568,464 | 4,581,445 | +12,981 |
| `Snapshots/` | 3,374,049 | 6,753,144 | +3,379,095 |
| `backup_manifest` | 745 | 745 | 0 |
| **Total** | **7,943,135** | **11,335,334** | **+3,392,199** |

`12,981 + 3,379,095 = 3,392,076`, which is 123 bytes short of the actual delta (manifest and directory entries).

**The backup is not a full re-copy, but it is not a free incremental either.** The six unchanged partition files were not duplicated; yet every run must write another catalog snippet. For an hourly schedule that is `24 × 3.37 MB ≈ 81 MB` of catalog per day, before any data.

### 4.7 full-check

```
$ vbr --task full-check --config-file backup.conf
Checking backup consistency.
Snapshot name and restore point: vbr_lab_snapshot_20261111_210709, nodes:['v_testdb_node0001'].
Snapshots that have missing objects(hint: use 'vbr --task remove' to delete these snapshots):
Backup locations have 0 unreferenced objects
Backup locations have 0 missing objects
Backup consistency check complete.
```

---

## 5. Cost Analysis: Three Numbers That Set Your Strategy

### 5.1 Fixed cost: 12 seconds and 3.37 MB

Backing up a single 121,840-byte table:

```
Objects: ['article_lab_vbr.customers']
Approximate bytes to copy: 121840 of 121840 total.
Backup complete!          # 12.17 seconds
```

The backup directory was `3,446,338` bytes — **28.3 times** the payload. Elapsed time barely moves with data volume; the fixed cost dominates.

| Backup content | Payload | Duration | Backup directory |
|----------------|---------|----------|------------------|
| customers (20,000 rows) | 121,840 | 12.17 s | 3,446,338 |
| customers + orders (322,000 rows) | 4,568,464 | 12.09 s | 7,943,135 |
| customers + orders (second run) | 4,581,445 | 13.02 s | 11,335,334 |

**Conclusion: backup time tracks catalog size and archive count, not data volume.** High-frequency backups of small tables save no time and only inflate the directory.

### 5.2 Full backup: 77.19 GB against 15.10 GiB

That 77.19 GB figure is not abstract — it lines up with the largest projections in testdb:

```
=> SELECT anchor_table_schema, anchor_table_name, sum(used_bytes) AS used
   FROM v_monitor.projection_storage GROUP BY 1,2 ORDER BY used DESC LIMIT 5;

 anchor_table_schema |  anchor_table_name   |    used
---------------------+----------------------+------------
 lowi_rap            | judicial             | 44225562112
 mtk                 | vs_ros               | 13500868636
 mtk                 | partition_columns    |  7545556569
 lowi                | lowidoc              |  7221021426
 public              | vsmart_test          |   355229635
```

The top four alone are 72.5 GB. **Plan backups from this query, not from the database list**: one 44 GB table makes a full backup impossible on a 16 GB tmpfs.

Removing `objects` and asking vbr to estimate a full backup:

```
Error: Not enough available disk space on the backup volume(s).
Backup host: '192.168.100.9', mount point: '/tmp' does not have enough space.
77187279132 bytes needed, 16211030016 bytes available
```

- Needed: `77,187,279,132` bytes (77.19 GB / 71.89 GiB)
- Available: `16,211,030,016` bytes (16.21 GB / 15.10 GiB)
- That is 21.0%

The failure also proved something useful: the space check happens after the snapshot and before any file copy, and afterwards `listbackup` was empty with only the three init files (71 bytes) in the directory. **No half-written backup is left behind.**

### 5.3 Why objects

This testdb (a single projection, `lowi_rap.judicial`, is 44.2 GB) is not a candidate for daily full backups. A workable split:

- **Weekly full** backup onto a host or S3 location with at least 80 GB of free space.
- **Daily object** backups of the schemas that change, starting at 3.4 MB per run.
- Deciding whether a schema can be object-backed: an object backup carries the named objects plus their dependent objects, so cross-schema dependencies need checking first — otherwise a restored view can point at a table that was not in the backup.

---

## 6. Restore: What a Dry Run Proves and What It Does Not

### 6.1 Three dry runs, same answer

```
$ vbr --task restore --config-file backup.conf --dry-run
Starting object restore of database testdb.
Restoring from restore point: vbr_lab_snapshot_20261111_210709
Loading snapshot catalog from backup.
Restoring objects: article_lab_vbr.customers,article_lab_vbr.orders
Approximate bytes to copy: 0.
Dry run finished. Run vbr without --dry-run to complete the task.
```

Single table:

```
$ vbr --task restore --config-file backup.conf --restore-objects article_lab_vbr.orders --dry-run
Objects to restore: article_lab_vbr.orders
Preprocessing snapshot...
Restoring objects: article_lab_vbr.orders
Approximate bytes to copy: 0.
```

**All three variants (latest archive, a specific archive, a single object) reported `Approximate bytes to copy: 0`** — including when I pointed at the archive taken *before* the 2,000-row insert. So an object restore dry run proves that the catalog is readable, the objects resolve, and the nodes match; it **cannot** be used to estimate restore time or bandwidth. That is a measured observation, not something the documentation states.

### 6.2 The `--archive` trap

`listbackup` prints `vbr_lab_snapshot_20261111_210405`. Pasting that into `--archive` fails:

```
$ vbr --task restore --config-file backup.conf --archive vbr_lab_snapshot_20261111_210405 --dry-run
Restoring from restore point: vbr_lab_snapshot_vbr_lab_snapshot_20261111_210405
Error: Failed to get snapshot info file from backup location, please run backup validation task
```

vbr prepends `snapshotName_` itself. Pass the timestamp only:

```
$ vbr --task restore --config-file backup.conf --archive 20261111_210405 --dry-run
Restoring from restore point: vbr_lab_snapshot_20261111_210405
```

### 6.3 The real restore procedure (not executed here)

This is the version I would hand to an on-call colleague:

**Object restore (database must be UP)**

1. `vbr --task full-check -c backup.conf` and confirm `0 missing objects`.
2. `vbr --task restore -c backup.conf --restore-objects article_lab_vbr.orders` (add `--dry-run` first if unsure).
3. vbr takes a table O-lock and then the global catalog lock; DML on the same table blocks and background tuple-mover work is cancelled.
4. Compare fingerprints afterwards — do not stop at `count(*)`.

**Full restore (database must be DOWN)**

1. `admintools -t stop_db -d testdb`.
2. `vbr --task restore -c backup.conf` (or `--archive <timestamp>`).
3. `admintools -t start_db -d testdb`.
4. The target database must share the name, node names, catalog directory, and port; an empty database is fine, but it cannot be a different database.

**Why I did not run it:** this host has one database, no vbr_demo, and no second disposable environment. A full restore would overwrite testdb's catalog and data; an object restore would rewrite the schema holding the very tables this article is measured on. Both cross the line of "touch only your own lab schema". So: **backups were measured; the restore procedure is documented plus dry-run validated, and was not executed against testdb.**

### 6.4 Fingerprints for post-restore comparison

```sql
SELECT 'customers' AS tbl, count(*) AS rows,
       sum((hash(customer_id, customer_name, region, signup_date) % 1000000007)::numeric(38,0)) AS fingerprint
FROM article_lab_vbr.customers
UNION ALL
SELECT 'orders', count(*),
       sum((hash(order_id, customer_id, order_ts, amount, region, status) % 1000000007)::numeric(38,0))
FROM article_lab_vbr.orders;
```

Measured values:

```
    tbl    |  rows  |   fingerprint
-----------+--------+-----------------
 customers |  20000 |   9981599126718
 orders    | 302000 | 151035121926262
```

**Do not `sum(hash(...))` directly.** My first attempt returned:

```
ERROR 4845:  Sum() overflowed
HINT:  Try sum_float() instead
```

`hash()` returns a 64-bit integer and 300,000 of them overflow any integer sum. Take a modulus and cast to `numeric(38,0)`; `sum_float()` runs, but floating-point accumulation is a poor fingerprint.

### 6.5 Side effects of an object restore (documented)

The catalog inside an object backup is not the full catalog but a **snippet**: the named objects, their dependents, and principal objects. Three rules worth knowing before you run one:

- Objects in the backup are updated in the catalog; **dependent objects not in the backup are dropped**, not left in place.
- The restore takes a table O-lock, then the global catalog lock (GCLX); DML on those tables blocks and tuple-mover background work is cancelled.
- The snippet is loaded into memory; if the catalog exceeds 5% of the restoring node's available memory, the object restore can fail and you should restore table by table.

Those three rules explain why object restores are fast but easier to get wrong: they touch part of the catalog, not the whole database.

---

## 7. EON Mode: Same vbr, Different Cargo

### 7.1 What this host's eondb actually looks like

```
=> SELECT node_name, node_state FROM v_catalog.nodes;
v_eondb_node0001 | UP
v_eondb_node0002 | UP
v_eondb_node0003 | UP

=> SELECT shard_name, shard_type, lower_hash_bound, upper_hash_bound FROM v_catalog.shards;
replica    | Replica |          |
segment0001| Segment |        0 | 1431655738
segment0002| Segment |1431655739 | 2863311477
segment0003| Segment |2863311478 | 4294967295

=> SELECT location_label, location_path, location_usage, sharing_type FROM v_catalog.storage_locations;
             | s3://verticadb/eondb                        | DATA        | COMMUNAL
             | /data/eondb/v_eondb_node0001_data            | DATA,TEMP   | NONE
             | /data/eondb/v_eondb_node0002_data            | DATA,TEMP   | NONE
             | /data/eondb/v_eondb_node0003_data            | DATA,TEMP   | NONE
auto-data-depot | /data/depot/eondb/v_eondb_node0001_depot | DEPOT       | NONE
auto-data-depot | /data/depot/eondb/v_eondb_node0002_depot | DEPOT       | NONE
auto-data-depot | /data/depot/eondb/v_eondb_node0003_depot | DEPOT       | NONE
```

Three nodes, four shards (one replica plus three segments whose bounds cover 0–4294967295 with no gaps), six node subscriptions, seven storage locations, K-Safe=0. The data itself lives in `s3://verticadb/eondb`.

### 7.2 Enterprise versus EON

| Aspect | Enterprise (measured here) | EON (read-only queries here + vendor docs) |
|--------|----------------------------|--------------------------------------------|
| Data source | Local ROS on each node (`/opt/data/testdb/..._data`) | Communal storage (`s3://verticadb/eondb`) plus per-node depot cache |
| Backup destination | `host:directory` in `[Mapping]`, local or NFS | **Cloud object storage only**: `cloud_storage_backup_path` and `cloud_storage_backup_file_system_path` in `[CloudStorage]`; one path per database |
| Credentials | dbadmin user and password | `VBR_BACKUP_STORAGE_*` **plus** `VBR_COMMUNAL_STORAGE_ACCESS_KEY_ID`, `..._SECRET_ACCESS_KEY`, `..._ENDPOINT_URL` (on-prem S3/GS means two endpoints) |
| Restore prerequisites | Same database name, node names, catalog directory, port | Same, plus object restore requires identical shard count, shard boundaries, and node subscriptions — after adding/removing nodes or a rebalance, older object backups may not be restorable per table |
| Restore behaviour | Copies from the backup directory back to node-local storage | Copies the source database's communal storage to the target; the original is untouched |
| Copy-free option | None | In-db restore points (no extra copies, but lost if communal storage is compromised) |
| Downtime | Full restore needs DOWN; object restore needs UP | Same |

Three common misunderstandings:

1. **"EON data is already in S3, so we do not need backups" is wrong.** Communal storage is the live data store, not a backup: a bad DROP, a mistaken delete, or catalog corruption hits that bucket directly. EON still needs backups written to a **separate** cloud path.
2. **"The depot must be backed up" is also wrong.** The depot is a local cache of communal data; rebuilding it costs time, not data. Backup capacity planning does not need to reserve space for it.
3. **"EON backups can go to a local directory" does not work.** `[Mapping]` does not apply; EON requires `[CloudStorage]`.

**No vbr operation was run against eondb** (no backup bucket or communal credentials are configured here, and the cluster is shared), so the EON column above is read-only queries plus vendor documentation, not measured output.

---

## 8. Common Errors and Anti-Patterns

**1. Backing up an object that does not exist buries the real error**

```
Objects: ['article_lab_vbr.no_such_table']
Error: SQL command "select object_snapshot('vbr_bad_snapshot','article_lab_vbr.no_such_table','','',false,false,'');" failed:
ERROR 11781:  Unknown or unsupported table name no_such_table for object article_lab_vbr.no_such_table
Backup FAILED.
```

A typo in `objects` passes init and only explodes at backup time. **Run `--dry-run` before you put it on a schedule.**

**2. Planning backup space from data size alone**

77.19 GB of data needs more than 80 GB of space, and that is before each archive's catalog. Use `du -sb` for real footprint; do not infer backup size from `projection_storage.used_bytes`.

**3. Pasting the archive name into `--archive`**

See 6.2. The error blames backup validation when you simply duplicated the `snapshotName_` prefix.

**4. Fingerprinting with `sum(hash(...))`**

`ERROR 4845: Sum() overflowed`. Take a modulus or accumulate into `numeric`.

**5. Committing the config file**

`dbPassword` is plaintext. Delete the config when the lab is done, keep it mode `600`, owned by dbadmin.

**6. Assuming archive timestamps are local time**

`vbr_lab_snapshot_20261111_210405` is `2026-11-12 05:04` local. Any monitoring that compares "did we back up today" by filename will misjudge every run between 00:00 and 08:00.

---

## 9. Risks and Recommendations

### Six pre-flight checks

1. The directory in `[Mapping]` exists, is writable, and has more space than "data + archive count × 3.4 MB".
2. The config is mode `600`, owned by the vbr account, and is not in git.
3. `vbr --task init` has been run for every new backup path.
4. `vbr --task backup --dry-run` shows the expected `Approximate bytes to copy` and passes the space check.
5. After each backup, `vbr --task full-check` reports `0 missing objects`.
6. The `listbackup --json` epoch feeds monitoring, with an alert when it falls behind.

On measurement: every vbr run was executed as dbadmin, timings came from `date +%s.%N` before and after rather than vbr's own output, the database stayed UP, and no other workload was running.

### Safe now (read-only or low risk)

1. Feed `vbr --task listbackup --json` into monitoring and alert when the newest archive's epoch lags the database's current epoch.
2. Run `vbr --task full-check` daily; `missing objects > 0` is a P1.
3. Practise an object backup on a lab schema (3.4 MB, 12 seconds) until `objects`, `[Mapping]`, and `full-check` are routine.
4. Report "backup directory size ÷ data size" weekly to catch catalog growth from too many archives or too-frequent runs.

### Needs change control (approval required)

1. Full backups need a real destination first: 77.19 GB will not fit in 15.10 GiB, so a backup host or S3 location is mandatory.
2. Set an explicit frequency and retention policy: `restorePointLimit` defaults to 1, and old archives are removed with `vbr --task remove --archive <timestamp>`. Write the retention window into the runbook; do not assume archives disappear on their own.
3. EON needs its own backup bucket plus `[CloudStorage]` and communal credentials — not something to "try quickly" on a shared cluster.
4. A restore drill needs a second environment (or a disposable vbr_demo). Without one, any claim that "restore drills are done" is empty.

---

## 10. References

- Vertica 25.4 documentation: Eon Mode database requirements — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/eon-db-requirements
- Vertica 25.4 documentation: Restoring a database from a full backup — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/restoring-backups/restoring-db-from-full-backup
- Vertica 25.4 documentation: vbr reference — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/vbr-reference
- Vertica 25.4 documentation: Configuring cloud storage backups — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/setting-up-backup-locations/configuring-cloud-storage-backups
