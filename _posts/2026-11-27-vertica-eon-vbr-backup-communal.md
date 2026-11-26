---
layout: post
title: "EON 備份的真相：vbr 還是會複製資料，真正免拷貝的是 in-db restore point"
date: 2026-11-27 05:00:00 +0800
categories: vertica eon backup
tags: [vertica, eon, vbr, backup, communal-storage, restore-point, catalog]
description: "在 3 節點 eondb（v25.4.0-0、K-Safe=0）唯讀實查：1,410 個 container、221 個 projection、disk_size 合計 12.05 GB，但 vbr 快照、archive、restore point 三張表全是 0 列，這台機器也沒有 eondb 的 vbr 設定檔（listbackup 直接回 Must specify a config file.）。讀 25.4 內附的 vbr.py 可以確認：備份會逐物件複製，已存在的物件才跳過——「EON 備份只備 catalog」是錯的；真正不複製資料的是 in-db restore point。"
image: /assets/images/og/vertica-eon-vbr-backup-communal-zh.png
faq:
  - q: "EON 的 vbr 備份會複製資料，還是只備 catalog？"
    a: "會複製資料。25.4 內附的 vbr.py 在備份時先用 database_snapshot() 建快照，再逐一把 snapshot manifest 裡、備份位置上還沒有的物件複製過去；程式碼裡只有 EON 的 lib 物件會只複製一次，沒有跳過 ROS 物件的分支。本機 eondb 的 ROS 物件 disk_size 合計 12,047,811,278 bytes（12.05 GB），第一次全量備份就是要處理這個量級。"
  - q: "EON 備份沒有設定檔時，vbr 會回什麼？"
    a: "回一行 Must specify a config file. 然後印出說明。本機 eondb 就是這個狀態：/opt/vertica/config 底下沒有任何 eondb 相關設定，admintools.conf 也只有 testdb，所以 vbr --task listbackup 什麼都列不出來。"
  - q: "in-db restore point 和 vbr 備份差在哪裡？"
    a: "in-db restore point 只存 metadata，不另外複製資料，因此建立快、佔用低，但它存在資料庫自己的 catalog 裡，communal storage 掛掉時會一起消失。vbr 備份則把物件複製到另一個備份路徑，缺點是要空間與時間。本機 eondb 的 ARCHIVES 與 ARCHIVE_RESTORE_POINTS 都是 0 列，代表兩者都沒有做。"
  - q: "為什麼不能用 v_monitor.disk_storage 估算 catalog 大小？"
    a: "因為它回的是檔案系統層級的用量，不是路徑層級。本機 eondb 的 9 個路徑（3 個 CATALOG、3 個 DATA,TEMP、3 個 DEPOT）全部回報同樣的 694,374 MB used、205,865 MB free、22%，因為它們都在同一個 /data 檔案系統上。要量 catalog 只能到節點上對 catalog 目錄做 du。"
---

<!--
AI Summary: 在 3 節點 EON 叢集 eondb（Vertica v25.4.0-0、K-Safe=0、4 shard）以唯讀查詢實查備份現況：1,410 個 storage container、221 個有資料的 projection、disk_size 合計 12,047,811,278 bytes（12.05 GB），mtk schema 佔 77.5%；v_monitor.database_snapshots、ARCHIVES、ARCHIVE_RESTORE_POINTS 全部 0 列，vbr --task listbackup 回 Must specify a config file.，本機沒有 eondb 的 vbr 設定檔。並讀 25.4 內附 vbr.py（12,529 行）確認備份流程會逐物件複製、以 storage_id 去重、已存在物件記為 skipped，藉此修正「EON 備份只備 catalog」的常見說法，並區分 vbr 備份與 in-db restore point 兩種機制。
AI Keywords: Vertica, Eon Mode, vbr, backup, communal storage, restore point, catalog, K-Safe, ARCHIVE, 25.4
-->
<!--more-->

---

## 一、先講結論

「EON 的資料已經在 communal storage，所以 vbr 只備 catalog」這句話，我在三本不同的簡報裡看過。它錯了一半，而且錯的那一半會直接影響你的備份容量規劃與還原演練。

我在本機 3 節點的 eondb（Vertica v25.4.0-0、K-Safe=0）上，用唯讀查詢把備份相關的狀態全部查了一輪，再讀 25.4 內附的 `vbr.py`（12,529 行）確認流程。五個結論：

1. **vbr 備份仍然會複製資料物件。** 備份流程是「先 `database_snapshot()` 建快照，再把 manifest 裡、備份路徑上還沒有的物件逐一複製過去」。程式碼裡沒有「EON 就跳過 ROS」這種分支，唯一的 EON 特例是 `lib` 物件只複製一次。
2. **真正免拷貝的是 in-db restore point。** 它是 EON 專屬的 `CREATE ARCHIVE` + `SAVE RESTORE POINT TO ARCHIVE`，只存 metadata；代價是它存活在資料庫自己的 catalog 裡，communal storage 沒了就一起沒了。
3. **本機 eondb 現在兩種都沒有。** `v_monitor.database_snapshots` 0 列、`ARCHIVES` 0 列、`ARCHIVE_RESTORE_POINTS` 0 列。這是一個有 12.05 GB 資料、卻沒有任何還原點的叢集。
4. **這台機器也沒有 eondb 的 vbr 設定。** `vbr --task listbackup` 直接回 `Must specify a config file.`；`/opt/vertica/config` 底下沒有 eondb 的東西，`admintools.conf` 只有 testdb。EON 的備份設定必須用 `[CloudStorage]`，不能沿用 Enterprise 的 `[Mapping]`。
5. **「restore point」在這個主題裡有兩個意思。** vbr 的 `listbackup` 把每次備份叫 restore point（存在備份路徑上的 archive timestamp）；in-db restore point 是 catalog 裡的 archive 物件。兩者同名、壽命與用途完全不同，混用會做出錯的決策。

## 二、實測環境與方法

| 項目 | 值 |
|---|---|
| 版本 | Vertica Analytic Database v25.4.0-0（Community Edition） |
| 資料庫 | `eondb`（EON 模式，3 節點） |
| 節點 | `v_eondb_node0001`～`0003`，全部 UP、都是 primary |
| shard | 4 個（3 個 segment + 1 個 replica） |
| K-Safe | 0（連線時伺服器主動送 WARNING 6046） |
| storage container | 1,410 個，disk_size 合計 12,047,811,278 bytes |
| 有資料的 projection | 221 個 |
| 測試方式 | 只跑 SELECT；沒有 CREATE、沒有 vbr 備份或還原 |

先確認版本與節點，這一步同時會把 K-Safe 的警告帶出來：

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

連線時 `vertica_python` 會印出 `WARNING 6046: The database is currently 0-safe...`。這不是雜訊：K-Safe=0 意味著資料在 communal storage 只有一份，節點故障不會救你，communal 故障更不會。這個前提下，「有沒有外部備份」不是維運加分項，是唯一的安全網。

本文對 eondb 全程唯讀。原因是它是一個共用的測試叢集：`CREATE ARCHIVE`、`SAVE RESTORE POINT`、`vbr --task backup` 都會寫入 catalog 或備份路徑，不該在沒有維護窗口的情況下做。所有標「未實測」的段落，都是官方文件或程式碼閱讀的結果，我會明確標示。

## 三、修正那句話：vbr 到底備了什麼

先把兩個機制分開，很多混淆都來自把它們當成同一件事。

| 機制 | 備份標的 | 資料是否另外複製 | 存在哪裡 | 25.4 語法 |
|---|---|---|---|---|
| vbr 備份 | catalog 快照 + ROS 物件（+ lib） | **是**，第一次全量 | 外部備份路徑（EON 只能是雲端物件儲存） | `vbr --task backup` |
| in-db restore point | 只存 metadata | **否** | 資料庫自己的 catalog／communal storage | `CREATE ARCHIVE`、`SAVE RESTORE POINT TO ARCHIVE` |

![EON 備份邊界：communal storage 保存 catalog 與 12.05 GB 的 ROS 物件，三個 depot 只是快取不備份；vbr 把 catalog 快照與 ROS 物件複製到另一個雲端路徑，in-db restore point 只存 metadata 且不複製資料]({{ '/assets/images/eon-backup-boundary.svg' | relative_url }}){: loading="lazy" }

這張圖的重點是「哪個箭頭會產生位元組」：只有往右的那一條（vbr）會。綠色那塊（in-db restore point）在同一個資料庫邊界內，所以它跟著 communal 一起生、一起死。

### 3.1 程式碼怎麼說

25.4 內附的 `/opt/vertica/bin/vbr.py` 有 12,529 行。備份路徑上最重要的兩個函式是：

- `database_snapshot()`（第 3,406 行起）送出 `select database_snapshot('<name>',false);`，在資料庫裡建立這次備份的快照。
- `process_manifests()`（約第 7,460 行起）把「節點 snapshot manifest 有、備份 manifest 沒有」的物件算進 `bytes_copying`；兩邊都有的物件只更新 ref count，算進 `bytes_skipping`。

換句話說，**備份的增量是「物件層級」的**：第一次全部複製，之後只複製新增的 ROS 物件。這是在 EON 上很有感的原因——ROS 物件不可變，載入後就不會被改寫，所以第二次之後的備份量通常遠小於第一次。但第一次仍然是扎扎實實的 12.05 GB。

EON 專屬的處理只有一處：`lib` 物件由第一個節點複製一次就好（第 8,101～8,128 行），因為三個節點看到的是同一份 library。ROS 物件沒有這種跳過邏輯。

### 3.2 還原端也印證了同一件事

還原時 vbr 會先列出目標 communal storage 上**已經存在的物件**（`_get_communal_storage_sids()`，第 8,603 行），再只補缺的（`get_objects_to_restore()`，第 8,654 行起）。如果備份裡沒有資料，這段「只補缺的」就沒有意義了。

所以比較準確的說法是：

> EON 的 vbr 備份會複製 ROS 物件，來源是 communal storage 裡那一份；它不會因為三個節點都看得到同一份資料就複製三份，而且第二次以後只複製新物件。

「只備 catalog」是把 in-db restore point 的特性，套到 vbr 身上。

## 四、實測一：7 個 storage location，只有 communal 帶 catalog

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

三個欄位決定這 7 列怎麼讀：

- **`has_catalog = True` 只有 communal 那一列。** catalog 的權威版本在 `s3://verticadb/eondb`，節點上的 `/data/eondb/*_catalog` 是工作副本。這是「communal 掛掉＝資料庫沒了」的來源。
- **`sharing_type`：只有 communal 是 `COMMUNAL`，其餘 6 列都是 `NONE`。** 三個 depot 是各節點自己的快取，不是共用儲存。
- **`location_usage`：`DATA`、`DATA,TEMP`、`DEPOT` 三種。** communal 是單獨的 `DATA`（物件儲存上不該有 temp），節點本機同時掛 DATA 與 TEMP。

備份計畫只需要關心兩列：communal（catalog + 資料本體）與 depot（不用備份，重建成本是時間）。**depot 不是備份標的**，這一點在容量估算時最常被多算。

## 五、實測二：12.05 GB 的組成

```sql
=> SELECT count(*) AS containers, count(DISTINCT projection_id) AS projections,
          sum(total_row_count) AS total_rows, sum(disk_size) AS disk_bytes
   FROM v_monitor.storage_containers;

 containers | projections | total_rows  | disk_bytes
------------+-------------+-------------+------------
       1410 |         221 | 1099951985  | 12047811278
(1 row)
```

`sum(disk_size)` 是 12,047,811,278 bytes，換算 11.22 GiB（12.05 GB）。這是壓縮後的 ROS 大小，不含 catalog、不含暫存。

`total_rows` 那個 1,099,951,985 不要直接引用：它把 221 個 projection 的列數加在一起，同一張表若有兩個 projection，列數就被算兩次。要算「資料有幾列」得回到表層級，不是 container 層級。

分佈上，三個節點幾乎均勻：

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

每個節點約 3.74 GiB，容器數 477／478／455。以 4 個 shard、3 個 segment 的設計來說，這個均勻度是預期結果；如果某個節點明顯偏少，先去看 shard 與 subscription，不要先怪備份。

資料組成則很不平均：

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

`mtk` 一個 schema 就佔 9,333,435,257 bytes，**77.5%** 的資料量，卻只有 18 個 container、6 個 projection。`ADA` 有 824 個 container，卻只佔 6.4%。這代表備份時間不會跟 container 數量成正比，而是跟 bytes 成正比：真正決定第一次備份時間的是 `mtk` 那 8.7 GiB。

### 5.1 77 GB 的歷史寫入，不是 77 GB 的備份量

depot 把資料送上 communal 的每一筆都有帳：

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

2,505 個檔案、合計 77,005,327,737 bytes（71.7 GiB），全部 `COMPLETED`。這是 11 月 9 日到 11 月 26 日之間寫進 communal 的總量，是現存 12.05 GB 的 **6.4 倍**。

差距來自 merge：ROS 物件被合併後，舊物件不再被任何 catalog 參照，也就不會出現在 snapshot manifest 裡。**備份處理的是「現在還活著的物件」，不是「歷史上寫過多少」**。用 `depot_uploads` 的累計量去估備份容量，會高估 6 倍；用 `storage_containers` 才是對的。反過來說，如果你要估的是 communal 的**成長速度**（例如規劃 bucket 容量），那 77 GB／17 天才是你要看的數字。

## 六、實測三：這個叢集沒有任何備份或 restore point

三張表，三種 0 列。先看 vbr 的快照紀錄：

```sql
=> SELECT snapshot_name, node_name, epoch, acquisition_timestamp,
          is_durable_snapshot, total_size_bytes, storage_cost_bytes
   FROM v_monitor.database_snapshots;

 snapshot_name | node_name | epoch | acquisition_timestamp | is_durable_snapshot | total_size_bytes | storage_cost_bytes
---------------+-----------+-------+-----------------------+---------------------+------------------+-------------------
(0 rows)
```

再看 in-db restore point 的兩個物件：

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

這三個 0 列的意義不同：

- **`v_monitor.database_snapshots` 0 列**：這個叢集從來沒有被 vbr 備份過（vbr 每次備份都會留下 snapshot 紀錄，並在 `total_size_bytes`、`storage_cost_bytes` 記下大小）。順帶一提，這也是備份大小的現成來源——前提是至少備份過一次。
- **`ARCHIVES` 0 列**：沒有人建過 archive。沒有 archive，就不能 `SAVE RESTORE POINT`。
- **`ARCHIVE_RESTORE_POINTS` 0 列**：沒有任何 in-db restore point。

一個有 12.05 GB 資料、K-Safe=0 的叢集，備份與還原點都是零。這是文章裡最值得貼給主管看的一張表。

## 七、實測四：vbr 在這台機器沒有 eondb 的設定

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

它不是「找不到備份」，是「沒有設定檔可讀」。再確認兩件事：

```bash
$ ls /opt/vertica/config | grep -i eon
$ grep -c "Database:eondb" /opt/vertica/config/admintools.conf
0
```

`/opt/vertica/config` 底下沒有任何 eondb 相關檔案，`admintools.conf` 只有 `[Database:testdb]`（`is_eon_mode = False`）。這台機器的 eondb 不是用 admintools 建的，vbr 也不會自己去找它——**EON 的 vbr 一定要有明確的 config 檔**。

### 7.1 EON 的設定長相不一樣

25.4 內附的範例檔（`/opt/vertica/share/vbr/example_configs/backup_restore_cloud_storage.ini`）開頭就寫明：

```ini
[CloudStorage]
; !!Mandatory!! Backup location on Cloud or HDFS (no default).
cloud_storage_backup_path = s3://backup_bucket/database_backup_path/

; !!Mandatory!! directory used to manage locking during a backup (no default).
cloud_storage_backup_file_system_path = []:/home/dbadmin/backup_locks_dir/

[Database]
dbName = current_database
```

三個實務重點：

1. **`[CloudStorage]` 與 `[Mapping]` 互斥。** 官方文件給的錯誤訊息是 `Config has conflicting sections (Mapping, CloudStorage), specify only one of them.`。把 Enterprise 的設定直接搬過來改 dbName，會在這裡失敗。
2. **備份路徑一個資料庫專用。** 文件明講一個 backup path 只能給一個資料庫用，不能共用。
3. **on-prem 的 S3 需要兩組環境變數。** 備份位置用 `VBR_BACKUP_STORAGE_ACCESS_KEY_ID`／`SECRET_ACCESS_KEY`／`ENDPOINT_URL`；EON 還要 communal 的 `VBR_COMMUNAL_STORAGE_ACCESS_KEY_ID`／`SECRET_ACCESS_KEY`／`ENDPOINT_URL`。這些不放進 config 檔，所以「設定檔看起來沒問題」不代表跑得起來。

這一段是文件整理，本機沒有實際備份（沒有備份 bucket，也沒有維護窗口），**未實測**。

## 八、兩個都叫 restore point 的東西

| 面向 | vbr 的 restore point | in-db restore point |
|---|---|---|
| 實體 | 備份路徑上的一組 archive（timestamp） | catalog 裡的 archive 物件 |
| 建立方式 | `vbr --task backup` | `CREATE ARCHIVE` + `SAVE RESTORE POINT TO ARCHIVE` |
| 查詢方式 | `vbr --task listbackup` | `SELECT * FROM ARCHIVES;` / `ARCHIVE_RESTORE_POINTS` |
| 是否複製資料 | 是 | 否 |
| communal 損毀時 | 可用（在另一個路徑） | 一起消失 |
| 保留策略 | `restorePointLimit`（預設 1：1 份現行 + 1 份歷史） | `CREATE ARCHIVE ... LIMIT n` |

in-db restore point 的語法（官方文件，**未實測**）：

```sql
=> CREATE ARCHIVE vertica_restore LIMIT 15;
=> SAVE RESTORE POINT TO ARCHIVE vertica_restore;
```

需要 superuser 或 archive owner。它的價值是「誤 DROP 一張表、升級前留一個點」這種短時間、低成本的回復；它不是異地備份，不能拿來應付 communal storage 遺失。

## 九、容量與時間怎麼估

### 9.1 可以算的

- **第一次全量備份的資料量就是 12.05 GB**（`sum(disk_size)`），加上 catalog 快照。前者是實測值，後者我量不到（下一節說明）。
- **之後的備份只複製新物件**，所以第二份備份通常遠小於第一份。實際大小取決於兩次備份之間新增／合併了多少 ROS 物件，這個數字只能等真的有備份紀錄（`database_snapshots.total_size_bytes`）才看得到。
- **保留 N 份不等於 N × 12.05 GB**。manifest 用 ref count 管理共用物件，同一份 ROS 只要還被任何一份保留中的 restore point 引用就不會重複存。容量是「所有保留點的聯集」，不是乘法。

### 9.2 量不到的，以及為什麼

我想量 catalog 目錄大小，兩種做法都失敗：

```bash
$ du -sh /opt/data/testdb/v_testdb_node0001_catalog
du: cannot read directory '/opt/data/testdb/v_testdb_node0001_catalog': Permission denied

$ du -sh /proc/2641687/root/data/eondb/v_eondb_node0001_catalog
du: cannot access '/proc/2641687/root/data/eondb/v_eondb_node0001_catalog': Permission denied
```

一般帳號讀不到 dbadmin 的 catalog 目錄，叢集又跑在別的 mount namespace 裡。所以本文不給 catalog 大小的數字。正確做法是以 dbadmin 身分在每個節點上跑 `du -sh <catalog 目錄>`，取最大值乘節點數（EON 是每個節點各一份工作副本）。

### 9.3 不要用 disk_storage 估

```sql
=> SELECT node_name, storage_usage, disk_space_used_mb, disk_space_free_mb,
          disk_space_free_percent
   FROM v_monitor.disk_storage ORDER BY storage_usage, node_name;
-- 9 列皆同，節錄 3 列

     node_name     | storage_usage | disk_space_used_mb | disk_space_free_mb | disk_space_free_percent
-------------------+---------------+--------------------+--------------------+-------------------------
 v_eondb_node0001  | CATALOG       |             694374 |             205865 | 22%
 v_eondb_node0001  | DATA,TEMP     |             694374 |             205865 | 22%
 v_eondb_node0001  | DEPOT         |             694374 |             205865 | 22%
```

三個用途、三個節點、九個路徑，全部回報同一個數字。原因很簡單：它們都在同一個 `/data` 檔案系統上，`disk_storage` 回的是檔案系統用量，不是路徑用量。拿 694,374 MB 當 catalog 大小，會高估兩個數量級。

### 9.4 時間

我沒有跑備份，所以不給「12 GB 備份要幾分鐘」這種數字。文件提供的正確做法是 `vbr --task backup --dry-run`，它會評估這次備份的大小與影響，但不實際複製。這一步需要在有設定檔與備份 bucket 的環境做，**未實測**。

## 十、還原流程（未實測，依官方文件）

EON 的還原不是「把檔案倒回去」那麼單純，前置條件比 Enterprise 多：

1. **目標資料庫同名、節點名相同、catalog 目錄相同、port 相同，節點數不少於來源的 primary subcluster。** 少一項，vbr 就不讓你還原。
2. **full restore 的順序是：先有可連線的叢集，再 `vbr --task restore`，最後啟動資料庫。** 還原會覆寫 catalog 目錄；中斷 restore 會讓資料庫停在不可用狀態，必須重跑到完成。
3. **還原只補 communal storage 上缺的物件。** 這也是為什麼「還原到原本的 communal」通常比「還原到新的 communal」快：大部分物件已經在了。
4. **object restore 另外要求 namespace 的 shard 數、shard 邊界、node subscription 一致。** 加減節點或 rebalance 之後，舊的物件備份可能不能單表還原；`--target-namespace` 可以指定還原到哪個 namespace，不存在時 vbr 會建一個。
5. **in-db restore point 的 revive 是另一條路。** 它把資料庫回復到存點時的狀態，不需要備份檔，但前提是 communal storage 還在。

以上全部是文件流程，本機沒有執行任何 restore（shared cluster、沒有備份檔），**未實測**。

## 十一、常見錯誤與反模式

1. **把 communal storage 當備份。** 「資料已經在 S3，所以不用備份」是這個主題最貴的一句話。communal 是執行中的資料存放區：誤 DROP、catalog 損毀、bucket 被刪，都會直接反映在那個路徑上。備份必須在**另一個**路徑。
2. **把 in-db restore point 當異地備份。** 它不複製資料，所以 communal 出事時它第一個消失。文件寫得很清楚：restore point 存在資料庫內，communal 被破壞就沒了。
3. **用 `v_monitor.disk_storage` 估 catalog 或 depot 大小。** 本機 9 列同值（694,374 MB），因為是檔案系統層級。這個錯誤會讓容量規劃直接爆掉。
4. **以為 vbr 有 `restorepoint` 子命令。** 25.4 的 `vbr --help` 只列 backup／restore／copycluster／replicate／listbackup／init／quick-check／full-check／quick-repair／collect-garbage／remove。restore point 是 SQL（`SAVE RESTORE POINT TO ARCHIVE`），不是 vbr task。
5. **在 EON 設定檔裡寫 `[Mapping]`。** 會得到 `Config has conflicting sections (Mapping, CloudStorage)`。EON 只能用 `[CloudStorage]`。
6. **K-Safe=0 又沒有外部備份。** 本機就是這個狀態：0 個 vbr 快照、0 個 archive、0 個 restore point。節點掛掉還能重啟，communal 掛掉就真的沒了。

## 十二、風險與建議

### 現在就能做（唯讀、低風險）

```sql
-- 1. 這個叢集有沒有備份過
SELECT count(*) AS snapshots, max(acquisition_timestamp) AS latest
FROM v_monitor.database_snapshots;

-- 2. 有沒有 in-db restore point
SELECT count(*) AS archives FROM ARCHIVES;
SELECT count(*) AS restore_points FROM ARCHIVE_RESTORE_POINTS;

-- 3. 備份要處理多少資料
SELECT count(*) AS containers, sum(disk_size) AS disk_bytes
FROM v_monitor.storage_containers;

-- 4. catalog 的權威版本在哪
SELECT location_path, has_catalog FROM v_catalog.storage_locations
WHERE sharing_type = 'COMMUNAL';
```

再加一項不是 SQL 的檢查：在 dbadmin 的環境裡確認 `vbr --task listbackup -c <eondb 的 config>` 有沒有輸出。像本機這樣回 `Must specify a config file.`，就代表連設定都還沒有。

### 需要變更（要窗口、要核准）

1. **建 archive 與 restore point**：`CREATE ARCHIVE` + `SAVE RESTORE POINT TO ARCHIVE`，成本低，先擋「誤操作」這一類。
2. **設好 `[CloudStorage]` 備份設定**：備份 bucket 必須與 communal 分開，憑證用環境變數，不要寫進 config。
3. **做一次 `--dry-run`**：拿到真正的大小與時間估計，再決定排程頻率與 `restorePointLimit`。
4. **在隔離叢集做一次 full restore 演練**：EON 的前置條件（同名、同節點名、同 catalog 路徑、同 port）很容易在演練時才發現不成立。

備份策略的順序我建議是：先有 in-db restore point 擋誤操作，再有 vbr 外部備份擋 communal 故障，最後才是還原演練。三者缺一，都只能算「有做動作」，不算「有備份」。

## 十三、參考資料

- [Backing up and restoring the database (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/)
- [Eon Mode database requirements (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/eon-db-requirements/)
- [Configuring cloud storage backups (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/setting-up-backup-locations/configuring-cloud-storage-backups)
- [\[CloudStorage\] configuration reference (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/vbr-config-file-reference/cloudstorage/)
- [Revive an Eon DB — in-db restore points (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/eon/revive-eon-db/)
- [vbr reference (Vertica 25.4.x)](https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/vbr-reference/)
