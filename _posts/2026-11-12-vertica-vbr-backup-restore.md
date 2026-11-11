---
layout: post
title: "Vertica vbr 備份還原實戰：Enterprise 模式完整備份演練、還原程序與 EON 差異"
date: 2026-11-12 05:00:00 +0800
categories: vertica backup vbr
tags: [vertica, vbr, backup, restore, enterprise-mode, eon-mode, communal-storage, disaster-recovery]
description: "在 v25.4.0-0 單節點 Enterprise 模式實測 vbr：全庫備份需要 77.19 GB，而 /tmp 只有 15.10 GiB 可用，vbr 直接以 free space check 拒絕；改用 objects 參數只備份兩張表（payload 4.57 MB），12.09 秒完成，備份目錄卻佔 7.94 MB——因為每個 archive 都自帶一份 3.37 MB 的 catalog snippet。第二次備份只新增 12,981 bytes 的物件檔，加上 3.38 MB 的 catalog，總成本 3.39 MB。還原只用 dry-run 驗證流程（本機沒有可安全還原的目標資料庫），未在 testdb 上執行。"
image: /assets/images/og/vertica-vbr-backup-restore-zh.png
faq:
  - q: "vbr 備份需要停機嗎？"
    a: "備份不用。實測在 testdb 正常運行、其他 session 可查詢的情況下，`vbr --task backup` 12.09 秒完成 4.57 MB 的物件備份。要停機的是還原：官方文件明訂 full restore 必須 database DOWN，object restore 必須 database UP，兩者不能顛倒。"
  - q: "物件備份和全庫備份差多少？"
    a: "同一台 testdb：全庫備份 vbr 估算需要 77,187,279,132 bytes（77.19 GB），物件備份（2 張表、302,000 + 20,000 筆）只有 4,568,464 bytes，相差 16,896 倍。全庫備份在這台機器根本跑不完，因為備份目錄所在的 /tmp 只有 16,211,030,016 bytes（15.10 GiB）可用。"
  - q: "為什麼 4.57 MB 的備份在磁碟上佔 7.94 MB？"
    a: "因為每個 archive 都帶一份 catalog snippet。實測單一 archive 的 `vbr_lab_snapshot.rst` 是 3,372,563 bytes，比要備份的資料還大；只備一張 121,840 bytes 的小表，備份目錄仍要 3,446,338 bytes，是資料的 28.3 倍。備份頻率越高，這個固定成本越明顯。"
  - q: "vbr 備份檔名的時間為什麼和本地時間差 8 小時？"
    a: "archive 名稱用的是 UTC。本機時間 2026-11-12 05:04（+08），`vbr --task listbackup` 顯示的 archive 是 `vbr_lab_snapshot_20261111_210405`，正好差 8 小時。要跟本機 log 對時間，先把名稱加 8 小時再比。"
  - q: "vbr 可以只備份一張表嗎？"
    a: "可以，在 config 的 `[Misc]` 段加 `objects = schema.table, schema.table`。實測只備 customers（20,000 筆、121,840 bytes）花 12.17 秒；指定不存在的表會得到 `ERROR 11781: Unknown or unsupported table name`。"
---
<!--
AI Summary: Vertica 25.4.0-0 單節點 Enterprise 模式實測 vbr：全庫備份估算 77,187,279,132 bytes（77.19 GB）超過 /tmp 可用 16,211,030,016 bytes 而被 free space check 拒絕；改以 objects 參數備份 2 張表（302,000 + 20,000 筆、payload 4,568,464 bytes）12.09 秒完成，備份目錄 7,943,135 bytes，其中每個 archive 固定 3,372,563 bytes 的 catalog snippet；第二次備份僅新增 12,981 bytes 物件檔＋3,379,095 bytes catalog，合計 3,392,199 bytes。listbackup 顯示 epoch 2330901/2330902、archive 名稱為 UTC。full-check 回報 0 missing／0 unreferenced。restore 僅以 --dry-run 驗證，未在 testdb 執行（本機無 vbr_demo）。EON 段以 eondb 實查：3 節點、4 shard、6 subscription、7 個 storage location、communal s3://verticadb/eondb，備份需 CloudStorage 段與 communal 認證，未實測。
AI Keywords: Vertica, vbr, backup, restore, object backup, catalog snippet, epoch, EON, communal storage, full-check, 25.4
-->
<!--more-->

---

## 一、先講結論

vbr 的備份不是「把資料抄一份」那麼簡單。它抄的是一個 **epoch 一致的快照**：catalog 的一小段（snippet）、對應的 ROS 物件檔、以及一份描述這次備份的 metadata。這三者的比例決定你的備份成本，而這個比例跟你的資料量幾乎無關——**跟你備份的次數有關**。

我在 Vertica 25.4.0-0、單節點 Enterprise 模式的 testdb 上，用 2 張表（302,000 + 20,000 筆）把 `init → backup → listbackup → full-check → restore --dry-run` 全部跑過，得到六個結論：

1. **全庫備份在這台機器不可能成功。** vbr 估算需要 `77,187,279,132` bytes（77.19 GB），備份目錄所在的 `/tmp` 只有 `16,211,030,016` bytes（15.10 GiB）可用，只有需求的 21.0%，vbr 在複製任何檔案前就以 free space check 中止。
2. **物件備份便宜到可以天天跑。** 2 張表、payload `4,568,464` bytes，`12.09` 秒完成；全庫與物件備份的體積差 **16,896 倍**。
3. **但每個 archive 都固定付 3.37 MB 的 catalog。** 單一 archive 的 `.rst` 是 `3,372,563` bytes；只備一張 121,840 bytes 的小表，備份目錄仍要 `3,446,338` bytes（28.3 倍）。**備份目錄的大小約等於「archive 數 × 3.37 MB ＋ 物件資料」**。
4. **第二次備份不是全量重抄。** 新增 2,000 筆後再備份，Objects 只多了 `12,981` bytes 的新物件檔，Snapshots 多了 `3,379,095` bytes，總增量 `3,392,199` bytes——與「新資料 ＋ 一份新 catalog」完全對得上。
5. **`full-check` 值得放進例行工作。** 實測回報 `0 unreferenced objects`、`0 missing objects`，比等到還原失敗才發現檔案不見便宜太多。
6. **還原我沒有在 testdb 上執行。** 這台機器只有 testdb 一個資料庫（`/opt/data` 下只有 testdb，`admintools.conf` 也只登記它），沒有可安全還原的 vbr_demo；用 `--dry-run` 驗證了流程與參數，實際 restore 步驟以官方文件為依據並在文中標明。這一點先講清楚，後面不再重複。

這篇寫給三種人：第一次要設 vbr 的人、備份空間突然不夠的人、以及被要求「EON 要不要另外備份」的人。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 模式 | Enterprise（`is_eon_mode = False`） |
| 節點 | 1（`v_testdb_node0001`，192.168.100.9，Community Edition） |
| 資料庫 | `testdb`，port 5433，catalog 在 `/opt/data/testdb/v_testdb_node0001_catalog` |
| 測試 schema | `article_lab_vbr`（實驗後 `DROP SCHEMA ... CASCADE`） |
| 表 1 | `customers`：20,000 筆，4 欄，`projection_storage` 實測 120,776 bytes |
| 表 2 | `orders`：302,000 筆，6 欄，`PARTITION BY date_trunc('month', order_ts)`，7 個分區，實測 4,443,309 bytes |
| 備份目錄 | `/tmp/opencode/vbr_lab/backup`（同一顆 `/dev/sda2`，可用 212 GB；`/tmp` 是 16 GB tmpfs） |
| vbr | `/opt/vertica/bin/vbr`（`vbr.py`，以 dbadmin 身分執行） |

`orders` 的 7 個分區在 `v_monitor.partitions` 的實際大小如下，等一下對照備份檔會用到：

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

## 三、vbr 在備份什麼：四個東西

| 元件 | 實測長相 | 誰決定大小 |
|------|----------|------------|
| Snapshot | 目錄 `Snapshots/<snapshot>_<UTC 時間>/<node>/`，內含 `.rst`（catalog snippet）、`.txt`（SnapshotInfo）、`.manifest`、`.info` | 幾乎固定，取決於 catalog 複雜度 |
| Object files | `Objects/<hash 目錄>/<id>_0.gt`，每個 ROS container 一個檔 | 真正的資料量 |
| Manifest | `backup_manifest`，約 745 bytes | 固定 |
| Metadata | `.txt` 裡的 `backupEpoch`、`ahmEpoch`、`catalogVersion`、`catalogRevision` | 固定 |

`vbr_lab_snapshot.txt` 的實際內容（節錄）說明了備份的「一致性邊界」：

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

兩個 epoch 差 1：快照的 catalog 版本是 `2330902`，AHM 停在 `2330901`。AHM（Ancient History Mark）是「已可清除」的界線：epoch 低於 AHM 的歷史資料已經被 purge，不會出現在任何備份裡，備份只能保證 AHM 之後的資料。

**這也是備份不能取代保留策略的原因。** 如果 purge 規則是「只留 30 天」，那 31 天前的分區在備份執行之前就已經不在資料庫裡，vbr 不會、也不能把它變出來。備份解決的是「檔案毀損、誤刪、catalog 壞掉」，不是「時間到自動消失」——後者要靠延長保留期，或在 purge 之前先把資料落地到別的地方。

---

## 四、Step-by-step 實測

### 4.1 準備資料

兩張表用 `COPY ... DIRECT` 從 CSV 載入，`orders` 用月分區：

```
=> COPY article_lab_vbr.orders (order_id, customer_id, order_ts, amount, region, status)
   FROM '/tmp/opencode/vbr_lab/orders.csv' DELIMITER ',' DIRECT;
 Rows Loaded
-------------
      300000
```

分區表達式有一個真實警告，值得順手記下來：

```
WARNING 6100:  Using PARTITION expression that returns a Timestamp value
HINT:  This PARTITION expression may cause too many data partitions.
```

### 4.2 寫 vbr config

最小可用的 Enterprise 設定只有三段。`objects` 放在 `[Misc]`，備份落地路徑放在 `[Mapping]` 的「節點 = host:目錄」：

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

兩個實務細節：

- `dbPassword` 是**明文**。檔案權限必須 `600`、擁有者是跑 vbr 的帳號（這裡是 dbadmin），而且不要放進 git。
- 沒有 `[Mapping]` 就沒有備份目的地。`vbr --help` 不會告訴你這件事，錯誤訊息也只會說 manifest 找不到。

### 4.3 init

```
$ vbr --task init --config-file backup.conf
Initializing backup locations.
Backup locations initialized.
```

init 只建立目錄骨架與 manifest，**不備份任何資料**。換一個新的備份路徑就要重跑一次，否則連 `--dry-run` 都會失敗：

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

- 耗時 **12.09 秒**
- 資料庫全程是 UP，其他 session 照常查詢
- `<snapshotName>_<UTC 時間>` 就是 archive 名：`vbr_lab_snapshot_20261111_210405`（本機時間 05:04，UTC 21:04）

### 4.5 listbackup

```
$ vbr --task listbackup --config-file backup.conf
backup                             backup_type   epoch     objects
vbr_lab_snapshot_20261111_210709   object        2330902   article_lab_vbr.customers,article_lab_vbr.orders
vbr_lab_snapshot_20261111_210405   object        2330901   article_lab_vbr.customers,article_lab_vbr.orders
```

`backup_type` 是 `object` 而不是 `full`——因為 config 有 `objects`。加上 `--json` 可以拿到同一份資料的機器可讀版本，適合塞進監控（以下是第一次備份後的輸出，當時只有一個 archive）：

```
{"num_backups": 1, "backups": [{"backup": "vbr_lab_snapshot_20261111_210405",
 "backup_type": "object", "epoch": "2330901",
 "objects": "article_lab_vbr.customers,article_lab_vbr.orders", ...}]}
```

### 4.6 第二次備份：新增 2,000 筆之後

先插入 2,000 筆 11 月的資料（多出第 7 個分區），再跑一次同樣的 backup：

```
Approximate bytes to copy: 4581445 of 4581445 total.
Backup complete!          # 13.02 秒
```

備份目錄的變化：

| 位置 | 第一次 | 第二次 | 增量 |
|------|--------|--------|------|
| `Objects/` | 4,568,464 | 4,581,445 | +12,981 |
| `Snapshots/` | 3,374,049 | 6,753,144 | +3,379,095 |
| `backup_manifest` | 745 | 745 | 0 |
| **總計** | **7,943,135** | **11,335,334** | **+3,392,199** |

`12,981 + 3,379,095 = 3,392,076`，與實際增量 `3,392,199` 只差 123 bytes（manifest 與目錄項）。

**備份不是全量重抄，但也不是免費的增量備份。** 未變動的 6 個舊分區物件檔沒有再複製一份；但只要跑一次 backup，就一定要再寫一份 catalog snippet。對「每小時備份一次」的排程，一天 24 份 catalog 就是 `24 × 3.37 MB ≈ 81 MB`，這還沒算資料。

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

## 五、成本分析：三個數字決定你的備份策略

### 5.1 固定成本：12 秒與 3.37 MB

只備一張 121,840 bytes 的表：

```
Objects: ['article_lab_vbr.customers']
Approximate bytes to copy: 121840 of 121840 total.
Backup complete!          # 12.17 秒
```

備份目錄 `3,446,338` bytes，是資料量的 **28.3 倍**。時間幾乎不隨資料量變動，固定成本占絕大多數。

| 備份內容 | payload | 耗時 | 備份目錄 |
|----------|---------|------|----------|
| customers（20,000 筆） | 121,840 | 12.17 s | 3,446,338 |
| customers + orders（322,000 筆） | 4,568,464 | 12.09 s | 7,943,135 |
| customers + orders（第二次） | 4,581,445 | 13.02 s | 11,335,334 |

**結論：備份時間與資料量幾乎無關，與 catalog 大小和 archive 數有關。** 小表高頻備份不但沒省時間，還會把目錄撐大。

### 5.2 全庫備份：77.19 GB 對 15.10 GiB

77.19 GB 這個數字不是憑空來的，它對應的是 testdb 上幾個大投影的實際用量：

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

前四名加起來就 72.5 GB。**備份規劃要從這張表開始，不是從資料庫清單開始**：只要有一張 44 GB 的表，全庫備份就不可能落在 16 GB 的 tmpfs 上。

把 `objects` 拿掉、`--dry-run` 試算全庫：

```
Error: Not enough available disk space on the backup volume(s).
Backup host: '192.168.100.9', mount point: '/tmp' does not have enough space.
77187279132 bytes needed, 16211030016 bytes available
```

- 需求：`77,187,279,132` bytes（77.19 GB / 71.89 GiB）
- 可用：`16,211,030,016` bytes（16.21 GB / 15.10 GiB）
- 只有 21.0%

這次失敗也順便驗證了一件事：快照完成後、複製檔案前就做空間檢查，失敗後 `listbackup` 是空的，備份目錄只剩 init 建的 3 個檔、71 bytes——**不會留下半套備份**。

### 5.3 為什麼要用 objects

這台 testdb 的資料量（`lowi_rap.judicial` 一個投影就 44.2 GB）不適合天天全備。實務切法：

- **每週全庫**一次，落在有 80 GB 以上空間的備份主機或 S3。
- **每日物件**備份變動大的 schema，成本 3.4 MB/天起跳。
- 判斷「這個 schema 能不能物件備份」：物件備份只帶被點名的物件與其依賴物件，跨 schema 的依賴要先確認，否則還原後 view 可能指向不存在的表。

---

## 六、還原：dry-run 能驗到什麼、不能驗到什麼

### 6.1 三種 dry-run 的實際輸出

```
$ vbr --task restore --config-file backup.conf --dry-run
Starting object restore of database testdb.
Restoring from restore point: vbr_lab_snapshot_20261111_210709
Loading snapshot catalog from backup.
Restoring objects: article_lab_vbr.customers,article_lab_vbr.orders
Approximate bytes to copy: 0.
Dry run finished. Run vbr without --dry-run to complete the task.
```

單表還原：

```
$ vbr --task restore --config-file backup.conf --restore-objects article_lab_vbr.orders --dry-run
Objects to restore: article_lab_vbr.orders
Preprocessing snapshot...
Restoring objects: article_lab_vbr.orders
Approximate bytes to copy: 0.
```

**三個變體（最新 archive、指定 archive、單一物件）的 `Approximate bytes to copy` 都是 0。** 即使我拿的是「新增 2,000 筆之前」的 archive，也一樣是 0。所以物件還原的 dry-run 只能驗證「catalog 讀得到、物件點得到、節點對得上」，**不能拿來估算還原時間或頻寬**。這是實測結論，不是文件寫的。

### 6.2 `--archive` 的陷阱

`listbackup` 印出來的名字是 `vbr_lab_snapshot_20261111_210405`，直接貼進 `--archive` 會失敗：

```
$ vbr --task restore --config-file backup.conf --archive vbr_lab_snapshot_20261111_210405 --dry-run
Restoring from restore point: vbr_lab_snapshot_vbr_lab_snapshot_20261111_210405
Error: Failed to get snapshot info file from backup location, please run backup validation task
```

vbr 會自己補上 `snapshotName_` 前綴。正確用法是只給時間戳：

```
$ vbr --task restore --config-file backup.conf --archive 20261111_210405 --dry-run
Restoring from restore point: vbr_lab_snapshot_20261111_210405
```

### 6.3 真正的還原步驟（未在本機執行）

文件與實測條件的組合如下，這是我會交給值班同事的版本：

**物件還原（DB 必須 UP）**

1. `vbr --task full-check -c backup.conf`，確認 `0 missing objects`。
2. `vbr --task restore -c backup.conf --restore-objects article_lab_vbr.orders`（不確定就先加 `--dry-run`）。
3. 還原期間 vbr 會先取得 table O-lock，再取 global catalog lock；同表的 DML 會被擋住，TM 的背景工作會被取消。
4. 還原後用指紋查詢比對，不要只看 `count(*)`。

**全庫還原（DB 必須 DOWN）**

1. `admintools -t stop_db -d testdb`。
2. `vbr --task restore -c backup.conf`（或 `--archive <時間戳>`）。
3. `admintools -t start_db -d testdb`。
4. 目標資料庫必須同名、同節點名、同 catalog 目錄、同 port；空庫也可以，但不能是別的資料庫。

**為什麼我沒跑**：這台機器只有 testdb 一個資料庫，沒有 vbr_demo，也沒有第二個可丟棄的環境。全庫還原會覆蓋 testdb 的 catalog 與資料，物件還原則會改寫我剛好正在寫文章的那兩張表所屬的 schema——兩者都超出「只在自己的 lab schema 動手」的界線。所以：**備份已實測；還原步驟為文件整理＋dry-run 驗證，未在 testdb 上執行。**

### 6.4 還原後的指紋比對（可直接用）

```sql
SELECT 'customers' AS tbl, count(*) AS rows,
       sum((hash(customer_id, customer_name, region, signup_date) % 1000000007)::numeric(38,0)) AS fingerprint
FROM article_lab_vbr.customers
UNION ALL
SELECT 'orders', count(*),
       sum((hash(order_id, customer_id, order_ts, amount, region, status) % 1000000007)::numeric(38,0))
FROM article_lab_vbr.orders;
```

本次實測值：

```
    tbl    |  rows  |   fingerprint
-----------+--------+-----------------
 customers |  20000 |   9981599126718
 orders    | 302000 | 151035121926262
```

**不要直接 `sum(hash(...))`。** 第一次寫的時候得到：

```
ERROR 4845:  Sum() overflowed
HINT:  Try sum_float() instead
```

`hash()` 回傳 64-bit 整數，30 萬筆加起來一定爆。取模再轉 `numeric(38,0)` 才穩定；`sum_float()` 可以跑，但浮點累加不適合當指紋。

### 6.5 物件還原的副作用（文件依據）

物件備份裡的 catalog 不是完整 catalog，而是一段 **snippet**：被點名的物件、它們的依賴物件、以及 principal 物件。還原時的規則值得先知道：

- 備份裡的物件會被更新到 catalog；**備份裡沒有的依賴物件會被 drop**，不會留在原地等你。
- 還原過程先取 table 的 O-lock，再取 global catalog lock（GCLX）；這段時間同表的 DML 會被擋住，TM 的背景工作會被取消。
- 物件還原需要把 snippet 載入記憶體；如果 catalog 大於節點可用記憶體的 5%，物件還原可能失敗，這時要改成逐表還原。

這三點解釋了為什麼「物件還原」比「全庫還原」快，但也更容易在依賴關係上出錯：它動的是 catalog 的一部分，不是整個資料庫。

---

## 七、EON 模式：同一支 vbr，搬的東西不一樣

### 7.1 本機 eondb 的實查值

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

3 個節點、4 個 shard（1 replica + 3 segment，segment 邊界完整覆蓋 0–4294967295）、6 筆 node subscription、7 個 storage location、K-Safe=0。資料本體在 `s3://verticadb/eondb`。

### 7.2 Enterprise 與 EON 的備份差異

| 面向 | Enterprise（本篇實測） | EON（本機唯讀查詢 ＋ 官方文件） |
|------|------------------------|----------------------------------|
| 資料來源 | 各節點本機 ROS（`/opt/data/testdb/..._data`） | communal storage（`s3://verticadb/eondb`）＋ 各節點 depot 快取 |
| 備份目的地 | `[Mapping]` 的 `host:目錄`，可本機可 NFS | **只能是雲端物件儲存**：`[CloudStorage]` 的 `cloud_storage_backup_path`、`cloud_storage_backup_file_system_path`；一個路徑只能給一個資料庫用 |
| 認證 | dbadmin 帳密 | 除 `VBR_BACKUP_STORAGE_*` 外，還要 `VBR_COMMUNAL_STORAGE_ACCESS_KEY_ID`／`SECRET_ACCESS_KEY`／`ENDPOINT_URL`（on-prem S3/GS 會有兩組 endpoint） |
| 還原前置 | 同名 DB、同節點名、同 catalog 目錄、同 port | 同上，另外 object restore 要求 shard 數、shard 邊界、node subscription 都相同——加減節點或 rebalance 之後，舊的物件備份可能不能單表還原 |
| 還原行為 | 從備份目錄複製回節點本機 | 會把來源資料庫的 communal storage 複製到目標（原本的不受影響） |
| 免拷貝備份 | 無 | 有 in-db restore point：不另外複製資料，但 communal 掛掉時一起消失 |
| 停機要求 | full restore 要 DOWN；object restore 要 UP | 相同 |

三個容易誤會的地方：

1. **「EON 的資料已經在 S3，所以不用備份」是錯的。** communal storage 是執行中的資料存放區，不是備份；誤刪、誤 DROP、catalog 損毀都會直接反映在那個 bucket 上。EON 仍然要備份到**另一個**雲端路徑。
2. **「depot 要備份」也是錯的。** depot 是 communal 資料的本機快取，三個節點的 depot 重建成本是時間，不是資料。備份計畫不需要為 depot 留空間。
3. **「EON 備份可以寫本機目錄」不行。** `[Mapping]` 那套在 EON 不適用，必須用 `[CloudStorage]`。

**本機沒有對 eondb 執行任何 vbr 操作**（備份 bucket 與 communal 認證都沒設定，且共用叢集不可寫），所以上面 EON 欄位是「唯讀查詢 ＋ 官方文件」，不是實測輸出。

---

## 八、常見錯誤與反模式

**1. 對不存在的物件備份，錯誤訊息藏在內層 SQL**

```
Objects: ['article_lab_vbr.no_such_table']
Error: SQL command "select object_snapshot('vbr_bad_snapshot','article_lab_vbr.no_such_table','','',false,false,'');" failed:
ERROR 11781:  Unknown or unsupported table name no_such_table for object article_lab_vbr.no_such_table
Backup FAILED.
```

`objects` 拼錯不會在 init 被擋，要等 backup 才炸。**上線前先跑 `--dry-run`。**

**2. 備份目錄空間沒算 catalog**

`77.19 GB` 的資料需要 80 GB 以上空間，這是「資料大小」；實際還要加上每個 archive 的 catalog。用 `du -sb` 看真實佔用，不要用 `projection_storage` 的 `used_bytes` 推估備份大小。

**3. 把 archive 名直接丟給 `--archive`**

見 6.2。錯誤訊息說「please run backup validation task」，實際上你只是多貼了 `snapshotName_`。

**4. 指紋查詢用 `sum(hash(...))`**

`ERROR 4845: Sum() overflowed`。取模或改用 `numeric` 累加。

**5. 把 config 檔 commit 進 git**

`dbPassword` 是明文。這次的 config 跑完就刪，權限 `600`，擁有者是 dbadmin。

**6. 假設備份檔名時間是本地時間**

`vbr_lab_snapshot_20261111_210405` 對應本地 `2026-11-12 05:04`。排程監控若用檔名比對「今天有沒有備份」，在 00:00–08:00 之間會全部誤判。

---

## 九、風險與建議

### 上線前的六項檢查

1. `[Mapping]` 指到的目錄存在、可寫、空間大於「資料量 ＋ archive 數 × 3.4 MB」。
2. config 權限 `600`、擁有者是跑 vbr 的帳號，且不在 git 裡。
3. `vbr --task init` 對每個新備份路徑都跑過一次。
4. `vbr --task backup --dry-run` 先看 `Approximate bytes to copy` 與空間檢查結果。
5. 備份後 `vbr --task full-check` 必須是 `0 missing objects`。
6. 把 `listbackup --json` 的 epoch 接到監控，落後就告警。

量測方式說明：每次 vbr 都以 dbadmin 身分執行，耗時用 shell 的 `date +%s.%N` 取前後差，不是 vbr 自己印的時間；資料庫全程 UP，量測期間沒有其他負載。

### 現在就能做（唯讀／低風險）

1. `vbr --task listbackup --json` 接進監控，比對「最近一份 archive 的 epoch」與 `v_monitor` 的當前 epoch，落後就告警。
2. 每天跑 `vbr --task full-check`；`missing objects > 0` 就是 P1。
3. 用本機 lab schema 練一次物件備份（3.4 MB、12 秒），把 `objects`、`[Mapping]`、`full-check` 的流程走順。
4. 把「備份目錄實際用量 ÷ 資料量」做成週報，抓到 catalog 佔比異常成長（archive 太多、備份頻率過高）。

### 需要變更（要窗口／要核准）

1. 全庫備份要先解決空間：77.19 GB 的需求在這台只有 15.10 GiB，必須換到備份主機或 S3。
2. 備份頻率與保留策略：`restorePointLimit` 預設 1；舊 archive 用 `vbr --task remove --archive <時間戳>` 清，保留幾天要寫進 runbook，不要假設它會自動消失。
3. EON 要另外建備份 bucket、設 `[CloudStorage]` 與 communal 認證；這件事不能在共用叢集上「試一下」。
4. 還原演練需要第二個環境（或可丟棄的 vbr_demo）。沒有這個環境，任何「還原演練完成」的說法都是空的。

---

## 十、參考資料

- Vertica 25.4 文件：Eon Mode database requirements — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/eon-db-requirements
- Vertica 25.4 文件：Restoring a database from a full backup — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/restoring-backups/restoring-db-from-full-backup
- Vertica 25.4 文件：vbr reference — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/vbr-reference
- Vertica 25.4 文件：Configuring cloud storage backups — https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/setting-up-backup-locations/configuring-cloud-storage-backups
