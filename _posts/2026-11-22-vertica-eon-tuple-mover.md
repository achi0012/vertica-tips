---
layout: post
title: "EON 的 Tuple Mover 行為：什麼時候搬、搬到哪裡、跟 Enterprise 差在哪"
date: 2026-11-22 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, tuple-mover, ros, moveout, mergeout, depot, 25.4]
description: "EON 的 Tuple Mover 沒有 WOS 可以搬，它的 moveout 是把 depot 裡的 ROS 上傳到 communal storage。本機 3 節點 eondb（v25.4.0-0、K-Safe=0、4 shard）唯讀實測：10 天只發生 8 次 mergeout（16 列紀錄），但 depot_uploads 有 2,394 筆、77.0 GB，depot_evictions 501 筆、65.0 GB。mergeout 的規劃者是 shard 的 primary subscriber，實測 8 次的 planner 與 executor 都是同一台。"
image: /assets/images/og/vertica-eon-tuple-mover-zh.png
faq:
  - q: "EON 模式下 Tuple Mover 的 moveout 到底把資料搬到哪裡？"
    a: "搬到 communal storage，不是從 WOS 搬到 ROS。本機 depot_uploads 實測 2,394 筆、385 個 plan、77,005,174,001 bytes（77.0 GB），來源路徑是本機 /data/depot/eondb/v_eondb_node000N_depot/…，目的路徑是 s3://verticadb/eondb/…，狀態全部 COMPLETED。EON 的寫入直接進 depot 的 ROS，所以 Enterprise 那個「WOS→ROS」的 moveout 在 EON 不存在。"
  - q: "EON 的 mergeout 多久跑一次？"
    a: "MergeOutInterval=600 秒只是檢查間隔，不等於執行頻率。本機 10 天內只有 8 次 mergeout（v_monitor.tuple_mover_operations 共 16 列，Start 與 Complete 各 8），全部集中在 v_internal_tables.license_consumption_metrics_super，每次合併 32~41 個 ROS，耗時 93~514 ms。"
  - q: "把 MoveOutInterval 調小會讓 EON 上傳更快嗎？"
    a: "不會。MoveOutInterval（實查 300）與 MoveOutMaxAgeTime（實查 1800）用 get_config_parameter() 查得到值，但兩個都不在 configuration_parameters 檢視裡（查詢回 0 列），文件從 10.0 起就標記為已棄用、設定無效。要影響上傳行為該看 UseDepotForWrites、MaxDepotSizePercent（本機 80）與 DepotOperationsForQuery（本機 ALL）。"
  - q: "1,395 個 ROS container 是不是太多了？"
    a: "不一定。本機 1,395 個 container 裡，30 個就裝了 1,074,221,351 筆（97.7% 的列數），另外 1,166 個各裝不到 1,000 筆、合計只有 108,533 筆。最大反差是 mtk.partition_columns：10.08 億筆只用 3 個 container；ADA.LOWIDOC_V6_super 30.7 萬筆卻有 789 個，而且全部 start_epoch=23，是同一次載入留下的。"
  - q: "EON 的 mergeout 由哪個節點執行？"
    a: "由 shard 的 primary subscriber 規劃，再決定交給哪個節點執行。本機 4 個 shard、3 個節點，實測 8 次 mergeout 的 executor_node_name 與規劃節點完全相同（8/8），節點分佈是 node0001 兩次、node0002 三次、node0003 三次。"
---
<!--
AI Summary: 在本機 3 節點 EON 叢集（v25.4.0-0、K-Safe=0、4 shard）唯讀拆解 Tuple Mover：10 天僅 8 次 mergeout（16 列紀錄、每次 32~41 個 ROS、93~514 ms），全部作用在 license_consumption_metrics；真正的「moveout」是 depot_uploads 的 2,394 筆、385 plan、77.0 GB 上傳，另有 depot_evictions 501 筆、65.0 GB（全部 DROP OBJECT）。比較 Enterprise testdb 的 9,203 次 mergeout（平均 3.8 個 ROS），並記錄 MoveOutInterval/MoveOutMaxAgeTime 已棄用、不在 configuration_parameters、v_monitor.strata 兩張檢視皆 0 列。
AI Keywords: Vertica, EON, Tuple Mover, mergeout, moveout, depot_uploads, depot_evictions, ROS container, communal storage, 25.4
-->
<!--more-->

---

## 一、先講結論

如果你帶著 Enterprise 的直覺來看 EON 的 Tuple Mover，第一個會問的問題通常是「moveout 多久跑一次」。這個問題在 EON 上問錯了方向。

Enterprise 的 moveout 是把 **WOS 的資料寫成 ROS**；EON 沒有這一層，載入直接寫進 depot 的 ROS container，所以 TM 沒有 WOS 可搬。EON 真正在「搬」的是另一件事：**把 depot 裡的 ROS container 上傳到 communal storage**，而這件事記在 `v_monitor.depot_uploads`，不在 `v_monitor.tuple_mover_operations`。

本機 3 節點叢集 12 天的實測數字：

| 動作 | 監控來源 | 實測 |
|------|----------|------|
| mergeout（合併 depot 內小 ROS） | `v_monitor.tuple_mover_operations` | 8 次，共 16 列 |
| moveout（depot → communal 上傳） | `v_monitor.depot_uploads` | 2,394 筆、385 個 plan、77.0 GB |
| eviction（depot 清出空間） | `v_monitor.depot_evictions` | 501 筆、65.0 GB，全部 `DROP OBJECT` |

三個數字放在一起就看出重點了：**mergeout 的次數少到只有 8 次，而上傳有 2,394 次。** 在 EON，TM 的主要工作量在「把 depot 的檔案推上 communal」，mergeout 只是順帶把檔案變大、變少。

第二個結論是關於「container 太多」的焦慮。本機 1,395 個 container 看起來很嚇人，但拆開來看：30 個 container 裝了 97.7% 的列數，另外 1,166 個各裝不到 1,000 筆。container 數量本身不是健康指標。

![EON 寫入路徑：載入先寫 depot 的 ROS container，Tuple Mover 再上傳 communal storage；括號內為本機 12 天實測值]({{ '/assets/images/eon-tuple-mover-flow.svg' | relative_url }}){: loading="lazy" }

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| 資料庫 | `eondb`（EON），3 節點，K-Safe = 0 |
| 版本 | `Vertica Analytic Database v25.4.0-0` |
| shard | 4（`replica` + `segment0001`~`segment0003`） |
| communal storage | `s3://verticadb/eondb`（`sharing_type = COMMUNAL`） |
| depot | `/data/depot/eondb/v_eondb_node0001_depot` 等三個本機路徑 |
| 資料規模 | 1,395 個 container、221 個 projection、1,099,951,823 筆 |
| TM 資源池 | 每節點 `MEMORYSIZE` 3,145,728 KB（3 GiB）、上限 27,546,256 KB、`PLANNEDCONCURRENCY` 7 |
| 對照組 | Enterprise `testdb`（同版本），1,493 個 container、9,203 次 mergeout |
| 實測時間 | 2026-11-22 05:00 起（Asia/Taipei） |

**全程唯讀。** 只跑 `SELECT`；沒有 `CREATE`、`DROP`、`INSERT`、`ALTER`，也沒有呼叫任何 `mergeout()` / `moveout()` / `DO_TM_TASK()`，沒有改任何 config parameter。這一篇不會為了「製造」一次 moveout 而動叢集——所有數字都是它自己跑出來的，或本來就留在監控表裡的。

時間欄位要注意：叢集 `now()` 回的是 UTC，本文的 2026-11-22 是台北時間，兩者差 8 小時。上面所有 `11-xx` 的時間戳都是 UTC。

---

## 三、原理：EON 把 TM 拆成「規劃」與「執行」

官方文件對 EON 的 TM 有一句關鍵描述：mergeout 分成 **planning** 與 **execution** 兩段，planning 一律由該 shard 的 primary subscriber 負責，planning 時它會挑一個節點來執行，挑選條件是「depot 裡參與這次 mergeout 的 ROS container 最多的節點」。

| 面向 | Enterprise | EON |
|------|-----------|-----|
| 誰規劃 mergeout | 每個節點各自規劃自己存的資料 | shard 的 primary subscriber 統一規劃 |
| 誰執行 | 規劃的節點自己執行 | 可委派給其他節點（挑 depot 內 ROS 最多的） |
| moveout 的對象 | WOS → ROS（歷史行為，已棄用） | depot 的 ROS → communal storage |
| 沒有 WOS 的後果 | 需要 MoveOutInterval 控制 flush | 載入直寫 depot，`UseDepotForWrites=1` |
| 監控主表 | `tuple_mover_operations` | `tuple_mover_operations` + `depot_uploads` + `depot_evictions` |
| TM 記憶體 | 每節點一份 | 每節點一份，TM 池設 0 可讓該 subcluster 不接 mergeout |

本機是單一 primary subcluster、3 節點、4 個 shard（`replica` + `segment0001`~`segment0003`）、K-Safe=0。`v_catalog.node_subscriptions` 實查共 6 列：`replica` shard 有 3 個 subscription（只有 node0001 的 `is_primary = True`），3 個 segment shard 各一個，所以每個節點剛好 2 列，狀態全部 `ACTIVE`。在這種規模下，「挑 depot 內 ROS 最多的節點」幾乎一定挑回自己，因此下面看到的 8 次 mergeout 都是 planner = executor。

### TM 的三個動作，在 EON 各對應到哪張表

| TM 動作 | 做什麼 | Enterprise 的對象 | EON 的對象 | 監控 |
|---|---|---|---|---|
| mergeout | 合併小 ROS、順帶 purge | 本機 ROS | depot 內的 ROS | `tuple_mover_operations` |
| moveout | 把寫入緩衝的資料推到持久層 | WOS → ROS（已棄用） | depot ROS → communal | `depot_uploads` |
| reclaim / evict | 清掉不再需要的副本 | 本機磁碟 | depot 快取 | `depot_evictions` |

這張表是本文最實用的一頁：**當有人問「EON 的 moveout 有沒有在跑」，要去查的不是 `tuple_mover_operations`，而是 `depot_uploads`。** 反過來，問「ROS 有沒有在合併」才看 `tuple_mover_operations`。兩個問題、兩張表，混用就會得到「TM 都沒在動」的錯誤結論。

`v_catalog.storage_locations` 的實查結果確認了這個分層：

```sql
=> SELECT location_path, location_usage, sharing_type
   FROM v_catalog.storage_locations ORDER BY 2, 1;
                location_path                | location_usage | sharing_type
---------------------------------------------+----------------+--------------
 s3://verticadb/eondb                        | DATA           | COMMUNAL
 /data/eondb/v_eondb_node0001_data           | DATA,TEMP      | NONE
 /data/depot/eondb/v_eondb_node0001_depot    | DEPOT          | NONE
 ...（其餘兩個節點同型）
```

`COMMUNAL` 只有一個、`DEPOT` 有三個。資料的正本在 communal，depot 是可重建的快取與寫入緩衝——這句話決定了「TM 搬的到底是什麼」：**它搬的是快取裡的副本，不是唯一一份資料。** 所以上傳失敗可以重試，eviction 也不會掉資料（本機 501 次 eviction 全部是 `DROP OBJECT`，不是容量壓力）。

---

## 四、實測：10 天只 mergeout 8 次

`v_monitor.tuple_mover_operations` 在本機只有 16 列，剛好是 8 次操作的 Start 與 Complete：

```sql
=> SELECT operation_name, operation_status, count(*)
   FROM v_monitor.tuple_mover_operations GROUP BY 1,2 ORDER BY 1,2;
 operation_name | operation_status | count
----------------+------------------+-------
 Mergeout       | Complete         |     8
 Mergeout       | Start            |     8
```

沒有 `DVMergeout`、沒有 `Analyze Statistics`、沒有任何 `Moveout` 這個 operation_name。把 8 次 Start 與 Complete 對上 transaction_id，得到完整的執行紀錄：

| 開始時間（UTC） | 節點 | 合併前 ROS | 合併位元組 | 耗時 | 執行節點 |
|---|---|---|---|---|---|
| 11-12 18:09:09 | node0003 | 32 | 6,830 | 251 ms | node0003 |
| 11-12 18:09:09 | node0001 | 32 | 6,788 | 241 ms | node0001 |
| 11-13 16:29:09 | node0002 | 32 | 6,848 | 514 ms | node0002 |
| 11-17 08:40:35 | node0002 | 35 | 8,188 | 237 ms | node0002 |
| 11-17 23:40:35 | node0003 | 39 | 9,065 | 93 ms | node0003 |
| 11-18 15:00:35 | node0001 | 41 | 9,404 | 126 ms | node0001 |
| 11-21 03:20:36 | node0002 | 32 | 8,354 | 155 ms | node0002 |
| 11-21 13:00:36 | node0003 | 32 | 8,482 | 179 ms | node0003 |

三個觀察：

1. **8 次全部發生在同一張表**：`v_internal_tables.license_consumption_metrics_super`。這張內部表由授權服務持續寫入小批資料，所以它一直有小 ROS 需要合併。使用者 schema 的表在這 10 天內**一次都沒有被 mergeout 碰過**。
2. **次數少不代表 TM 閒著**。同一段時間 depot 上傳了 2,394 次。EON 的 TM 工作量在「上傳」，mergeout 只是為了讓上傳的檔案不要碎成幾千個。
3. **planner = executor 8/8**。單一 subcluster、每個 shard 一個 subscription，執行節點沒有其他地方可挑。若你的叢集有多個 subcluster，這一欄才會開始出現分歧，那時 `executor_node_name` 就是排查「mergeout 為什麼跑在某台」的第一手證據。

### 觀察窗：4 分鐘內什麼都沒動

05:02 CST 與 05:05 CST 各查一次同一組彙總：

```sql
=> SELECT count(*) containers, count(DISTINCT projection_id) projs,
          sum(total_row_count) rows, max(end_epoch) max_ep
   FROM v_monitor.storage_containers;
 containers | projs |    rows     | max_ep
------------+-------+-------------+--------
       1395 |   221 | 1099951823  |    405
```

兩次完全相同：1,395 個 container、221 個 projection、1,099,951,823 筆、最大 epoch 405；`tuple_mover_operations` 維持 16 列、`depot_uploads` 維持 2,394 列。**這是預期結果，不是 TM 壞了**——叢集沒有新資料進來，TM 沒有東西可合併，也沒有東西可上傳。要驗證 TM 有在動，得先有寫入；本篇守唯讀，所以只能引用歷史紀錄，這一點在文章裡講清楚，不用「觀察 4 分鐘沒變化」去推論「TM 停止運作」。

---

## 五、moveout 的真身：depot 上傳，不是 WOS 搬運

`v_monitor.depot_uploads` 是「depot 檔案上傳到 communal」的逐筆帳本，本機累積 2,394 筆：

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

一筆的代表性內容（節錄）：

```sql
=> SELECT node_name, file_size_bytes, status, source_file, destination_file
   FROM v_monitor.depot_uploads ORDER BY file_size_bytes DESC LIMIT 1;
 node_name      | file_size_bytes |  status   | source_file                                   | destination_file
----------------+-----------------+-----------+-----------------------------------------------+-------------------------------------------
 v_eondb_node0001 |     14876718208 | COMPLETED | /data/depot/eondb/..._depot/599/0200...dfbb_0.gt | s3://verticadb/eondb/f15/0200...dfbb_0.gt
```

`source_file` 是本機 depot、`destination_file` 是 S3——**這就是 EON 的 moveout**，不需要任何 `MoveOutInterval` 參與。2,394 筆的狀態全部是 `COMPLETED`，沒有 `FAILED`、沒有 `IN PROGRESS`。

### 三個節點分得很平均

```sql
=> SELECT node_name, count(*) n, sum(file_size_bytes) bytes, max(submit_time) last_t
   FROM v_monitor.depot_uploads GROUP BY 1 ORDER BY 1;
    node_name    |  n  |   bytes    |           last_t
-----------------+-----+------------+----------------------------
 v_eondb_node0001 | 800 | 25480360679 | 2026-11-21 18:40:36.047+00
 v_eondb_node0002 | 806 | 25824521839 | 2026-11-21 18:40:36.045+00
 v_eondb_node0003 | 788 | 25700291483 | 2026-11-21 18:40:36.045+00
```

800／806／788 筆、25.48／25.82／25.70 GB，bytes 最大與最小差 1.34%。這和 shard 分佈一致（3 個 segment shard 各落一個節點、replica shard 三台都有），沒有單一節點被寫爆。

### 12 天其實是「一天爆量 + 每天微調」

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

11-09 一天 2,108 筆、77.0 GB，是叢集建立與首次載入；之後每天 19~30 筆、30 KB 上下，全部來自那張授權內部表的 mergeout 結果。**這才是 EON 叢集閒置時 TM 的真實心跳：每天二、三十個小檔案。**

### 上傳延遲與檔案大小的關係

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

換算下來：小於 1 MB 的上傳平均 0.14 秒；10 MB 以上平均 169.8 秒、平均 409 MB。最大三筆各約 14.8 GB，耗時 3,525／3,521／3,527 秒，換算單節點吞吐約 **4.0 MiB/s**。這個數字不好看，但它是 11-09 首次載入時、三個節點同時上傳 44.5 GB 的量測結果，不能當成常態吞吐；常態是每天 30 KB 的碎檔案。

值得一提的是「筆數」與「位元組」完全不成比例：小於 1 MB 的上傳佔了 **68.0% 的筆數、卻只有 0.11% 的位元組**；10 MB 以上的 175 筆佔 7.3% 的筆數、97.5% 的位元組。對 S3 而言，這代表絕大多數請求都在傳幾 KB 的物件——這也是為什麼 mergeout 值得留著：它把碎檔變大，直接減少了物件數與 PUT 次數。反過來說，如果你的 EON 叢集每天有上萬筆 `<1MB` 的上傳，先去看是不是每次載入都只寫了幾百筆資料（小批次載入），而不是去調 TM 參數。

### eviction：501 次，全部是 DROP

```sql
=> SELECT reason, count(*) n, sum(file_size_bytes) bytes
   FROM v_monitor.depot_evictions GROUP BY 1;
    reason    |  n  |   bytes
--------------+-----+------------
 DROP OBJECT  | 501 | 64962959066
```

501 次、65.0 GB，`reason` 只有一種：`DROP OBJECT`。等於上傳量的 84.4% 在上傳後又被 drop 掉（測試叢集反覆建表、載入、刪表），不是 depot 容量不足造成的被動淘汰。如果是容量壓力，`reason` 會是別的值，而且會跟 `MaxDepotSizePercent=80` 的門檻同時出現。

---

## 六、container 數不是健康指標：三個反例

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

1,166 個 container 只裝了 10.8 萬筆（0.01%），30 個 container 裝了 10.74 億筆（97.7%）。三個具體反例：

**反例一：`mtk.partition_columns` 10.08 億筆，只有 3 個 container。** 每個節點一個，8,449 MB。這是「大檔」的樣子。

**反例二：`ADA.LOWIDOC_V6_super` 30.7 萬筆，789 個 container。**

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

789 個 container **全部 `start_epoch = 23`**，平均 1,167 筆。這是同一次載入產生的一批檔案，不是 789 次載入累積出來的垃圾；`v_monitor.partitions` 也剛好 789 列，一列對一個 container。TM 在 10 天內沒有動它——合理解釋是 mergeout 的目標是「跨 epoch 累積出來的小 ROS」，而同一次載入、已按排序鍵切好的 container 不在合併範圍內。這是本次觀察加上文件推論，不是文件明文，實務上判斷時請以「是否還在長」為準，而不是「現在有幾個」。

**反例三：`license_consumption_metrics_super` 30 個 container、417 筆資料。** 這是 TM 唯一真正在顧的表：

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

對照第四節的紀錄：11-21 13:00 那次 mergeout 在 node0003 上合併了 32 個 ROS，現在 node0003 只剩 2 個 container。mergeout 是真的有效，只是它管的是一張 417 筆的內部表。

**判斷規則**：看 `rows / containers` 與 `start_epoch` 的分佈，不要只看 `count(*)`。本機整體平均是 788,496 筆/container，但這個平均被 30 個大 container 拉高，完全不能代表中位數。

---

## 七、參數：哪些有效、哪些是遺留

```sql
=> SELECT parameter_name, current_value, default_value FROM configuration_parameters
   WHERE parameter_name IN ('MergeOutInterval','MaxROSPerStratum','MaxMrgOutROSSizeMB',
         'PurgeMergeoutPercent','MergeoutBlockParameter','ActivePartitionCount',
         'MaxDepotSizePercent','UseDepotForWrites','DepotOperationsForQuery',
         'DelayForDeletes','CatalogSyncInterval') ORDER BY 1;
```

| 參數 | 本機值 | 預設 | 在 EON 的意義 |
|------|--------|------|---------------|
| `MergeOutInterval` | 600 | 600 | TM 每 600 秒檢查一次有沒有 mergeout 要做 |
| `MaxROSPerStratum` | 32 | 32 | 單一 stratum 超過 32 個 ROS 才觸發合併 |
| `MaxMrgOutROSSizeMB` | -1 | -1 | 合併後 ROS 大小上限，-1 表不限制 |
| `PurgeMergeoutPercent` | 20 | 20 | 刪除比例超過 20% 才順帶 purge |
| `MergeoutBlockParameter` | 100 | 100 | mergeout 佇列超過 100 就擋 DDL/DML |
| `ActivePartitionCount` | 1 | 1 | 分區表只有 1 個 active partition |
| `MaxDepotSizePercent` | 80 | 80 | depot 最多佔磁碟 80% |
| `UseDepotForWrites` | 1 | 1 | 載入先寫 depot 再上傳（0 = 直接寫 communal） |
| `DepotOperationsForQuery` | ALL | ALL | 查詢缺資料時可 fetch、可 evict |
| `DelayForDeletes` | 0 | 0 | depot 刪除後 communal 立即刪 |
| `CatalogSyncInterval` | 5 minutes | 5 minutes | metadata 同步 communal 的間隔 |

Enterprise `testdb` 的對照：`MergeOutInterval` 被調成 300（預設 600）、`MergeoutBlockParameter` 調成 5000（預設 100），而且 18 天內跑了 9,203 次 mergeout、平均每次只合併 3.8 個 ROS。**Enterprise 是「次數多、每次小」，EON 是「次數少、每次大（平均 34.4 個 ROS）」**——同樣的 TM，在兩種模式下節奏完全不同。EON 這次沒有任何人為調參，全部是預設值。

### 那 MoveOutInterval 呢？

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

`get_config_parameter()` 查得到 300 與 1800，但 `configuration_parameters` 檢視裡一列都沒有。Enterprise 的 `testdb` 同樣是 0 列。這是因為兩個參數屬於 WOS 時代、從 10.0 起已標記棄用（「setting these parameters has no effect」）。**在 EON 上調它不會讓上傳變快，只會讓你以為自己調了什麼。**

### 三步判斷「TM 是不是正常」

1. **先確認有沒有寫入**：`SELECT count(*) FROM v_monitor.depot_uploads WHERE submit_time > now() - interval '1 day';` 回 0 就先查載入排程，不要怪 TM。本機的常態是每天 19~30 筆。
2. **再看 mergeout 最近一次**：`SELECT max(operation_start_timestamp) FROM v_monitor.tuple_mover_operations;` 本機最後一次是 11-21 13:00:36（UTC）。閒置叢集一天只有一兩次是正常的，因為只有授權內部表在長小 ROS。
3. **最後才看 container**：用 `rows / ros` 排序，而不是 `count(*)`。本機以表聚合後只有 1 張表的 ROS 數超過 32（`ADA.LOWIDOC_V6_super`，789 個），其餘 220 個 projection 都在 `MaxROSPerStratum=32` 的設計範圍內。

順序很重要：反過來先看 container 數，會把「同一次載入留下的碎檔」誤判成「TM 沒有在合併」。

---

## 八、常見錯誤與反模式

1. **以為 EON 不跑 mergeout。** 本機 10 天 8 次、每次 32~41 個 ROS，全部在 `license_consumption_metrics`。看不到是因為你查錯表——TM 只會碰有小 ROS 累積的表，不會沒事去合併一次載入的大檔。
2. **把 `MoveOutInterval`、`MoveOutMaxAgeTime` 當成上傳開關。** 兩個參數不在 `configuration_parameters`，`set_config_parameter` 也不會報錯，於是「調了、看起來有值、其實無效」。要觀察上傳請直接查 `depot_uploads`。
3. **用 container 總數當健康指標。** 1,395 個 container 裡有 1,166 個裝不到 1,000 筆；反過來 30 個裝了 97.7% 的資料。看總數只會被同一次載入留下的碎檔誤導。
4. **以為同一次載入的碎檔會被 mergeout 收掉。** `LOWIDOC_V6_super` 789 個 container 全部 epoch 23，10 天內 TM 一次都沒動。要減少它們只能重新載入或重建 projection，不是等 TM。
5. **以為 `v_monitor.strata` 可以看分層。** 本機 `v_monitor.strata` 與 `v_monitor.strata_structures` 都是 0 列（`ROS_container_count` 這欄也不存在於後者）。要看每個 projection 的 ROS 數，用 `v_monitor.projection_storage.ros_count`。
6. **在 primary subcluster 上把 TM 池設成 0。** 文件明確禁止：primary subcluster 必須能執行 mergeout，設 0 會讓 mergeout 沒有人做，而你不會收到警報，只會看到 ROS 越疊越多。

---

## 九、風險與建議

### 現在就能做（唯讀、低風險）

1. **每天看三個數字**：`depot_uploads` 的筆數與 bytes 增量、`depot_evictions` 的 reason 分佈、`tuple_mover_operations` 的最近一次時間。三個都停住才叫異常，單看 container 數沒有意義。
2. **用 `rows / containers` 排序找碎檔**：

   ```sql
   SELECT anchor_table_schema||'.'||anchor_table_name AS tbl,
          sum(ros_count) AS ros, sum(row_count) AS rows,
          round(sum(row_count)::numeric / nullif(sum(ros_count),0)) AS rows_per_ros
   FROM v_monitor.projection_storage
   GROUP BY 1 HAVING sum(ros_count) > 32
   ORDER BY rows_per_ros ASC LIMIT 20;
   ```

   本機符合 `ros > 32` 的只有 1 個 projection（`ADA.LOWIDOC_V6_super`），`rows_per_ros` 約 1,167。
3. **確認 `executor_node_name`**：多 subcluster 環境下，mergeout 是否集中在某台，一眼可看出。

### 需要變更（要窗口、要核准）

1. **加大 TM 池**：本機 3 GiB、上限 26.3 GiB（3,145,728 / 27,546,256 KB）。只有在 mergeout 排隊（`MergeoutBlockParameter` 被觸發）時才值得調。
2. **把 `UseDepotForWrites` 設 0**：載入直接寫 communal、繞過 depot，適合小批次但不適合延遲敏感的載入；要先量 communal 的寫入延遲。
3. **調整 `DelayForDeletes`**：本機 0，代表 depot 刪完 communal 立刻刪。要保留短期回溯能力就得調大，代價是 communal 成本。
4. **重建 `LOWIDOC_V6_super` 以合併 789 個 container**：會動 DDL 與離線視窗，且要先確認 267 個分區的保留策略。

---

## 十、參考資料

- Vertica 25.4.x 文件，Tuple mover（EON 的 mergeout planning / execution）：<https://docs.vertica.com/25.4.x/en/admin/managing-db/tuple-mover/>
- Vertica 25.4.x 文件，Managing the tuple mover（TM 資源池、active partition）：<https://docs.vertica.com/25.4.x/en/admin/managing-db/tuple-mover/managing-tuple-mover/>
- Vertica 25.4.x 文件，Eon Mode parameters（`UseDepotForWrites`、`DepotOperationsForQuery`、`DelayForDeletes`）：<https://docs.vertica.com/25.4.x/en/sql-reference/config-parameters/eon-parameters>
- Vertica 25.4.x 文件，`DEPOT_UPLOADS` 系統表：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/depot-uploads/>
- Vertica 25.4.x 文件，`TUPLE_MOVER_OPERATIONS` 系統表：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/tuple-mover-operations/>
- Vertica 文件，Deprecated and Removed Functionality（`MoveOutInterval`、`MoveOutMaxAgeTime`、`MoveOutSizePct` 設定無效）：<https://vertica.com/docs/10.0.x/HTML/Content/Authoring/NewFeatures/_VersionIndependent/DeprecatedandRetiredFunctionality.htm>
