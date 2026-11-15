---
layout: post
title: "EON 的 ROS 與 storage container：1,428 個 container 與十億筆資料的實際分佈"
date: 2026-11-16 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, ros, storage-container, partitions, projection, tuple-mover]
description: "在本機 3 節點 eondb（v25.4.0-0、K-Safe=0、4 shard）實查 v_monitor.storage_containers：1,428 個 container 裝了 1,099,951,625 筆、磁碟 11.99 GB（payload 109.03 GB，壓縮 9.10 倍）。但 84% 的 container（1,199 個）只裝 0.01% 的資料，而 3 個 container 就裝了 91.62%。同一個叢集上，掃 10 億筆的 3 個大 container 花 2.87 秒，掃 92 萬筆的 789 個小 container 花 0.028 秒——每筆成本差 10.7 倍。"
image: /assets/images/og/vertica-eon-ros-storage-containers-zh.png
faq:
  - q: "1,428 個 storage container 代表叢集有 1,428 份資料嗎？"
    a: "不是。這個數字是「三個節點各自看到的 container 列數」合計：segment0001 有 470 個、segment0002 有 481 個、segment0003 有 462 個，replica shard 只有 15 個（5 個 unsegmented 投影 × 3 節點）。replica shard 的 300,129 筆在三個節點各算一次，扣掉重複後是 1,099,751,539 筆；真正存在 segment shard 裡的是 1,099,651,496 筆。"
  - q: "為什麼 84% 的 container 只裝 0.01% 的資料？"
    a: "因為容器大小極端不均。本機 1,199 個 container 不到 1,000 筆（合計 108,335 筆、18.94 MB），其中 789 個來自 `LOWIDOC_V6_super`：267 個 partition key 分布在 3 個 shard 上，每個 partition 一個 container，平均每個 container 只有 1,167.1 筆、最小 1 筆。這些是每日小批量載入留下的 ROS，`MergeOutInterval` 是預設 600 秒、`ActivePartitionCount` 是 1，mergeout 沒有把它們合併掉。"
  - q: "container 太多會影響查詢嗎？會到上限嗎？"
    a: "會影響，而且本機量得到。`ContainersPerProjectionLimit` 是預設 1,024；`LOWIDOC_V6_super` 有 789 個 container（單節點最多 265 個，是上限的 25.9%），還沒觸發 ROS pushback，但掃 920,871 筆要 28 ms，換算每筆 30.5 ns；掃 3 個大 container 的 1,007,772,484 筆只要 2,865 ms，每筆 2.84 ns——每筆成本差 10.7 倍。"
  - q: "十億筆的投影為什麼只有 3 個 container？"
    a: "`partition_columns_super` 是 segmented 投影，資料只存在 3 個 segment shard 上，每個 shard 一個 container：336,125,677／335,881,586／335,765,221 筆，各 2.95 GB。三個 container 的 payload 是 92.58 GB、磁碟 8.86 GB（10.45 倍壓縮），每筆在磁碟上 8.79 bytes。掃這三個 container 的聚合查詢實測 2.87 秒，約 1,031 MB/s／節點。"
---
<!--
AI Summary: 在 3 節點 eondb（Vertica v25.4.0-0、K-Safe=0、4 shard）以唯讀查詢拆解 v_monitor.storage_containers 的 1,428 列：219 個投影名稱、1,099,951,625 筆、磁碟 11.99 GB、payload 109.03 GB（9.10 倍壓縮）。container 尺寸極端不均——1,199 個（84%）不到 1,000 筆只佔 0.01% 資料列與 0.16% 位元組，3 個 container 佔 91.62% 資料列與 73.91% 位元組；LOWIDOC_V6_super 以 267 個 partition × 3 shard 產生 789 個 container、平均 1,167.1 筆。實測掃 10 億筆（3 個 container）2,865 ms、每筆 2.84 ns，掃 920,871 筆（789 個 container）28 ms、每筆 30.5 ns，差 10.7 倍。ContainersPerProjectionLimit 為預設 1,024。全程唯讀。
AI Keywords: Vertica, EON, ROS container, storage_containers, partitions, projection, ContainersPerProjectionLimit, depot, 25.4
-->
<!--more-->

---

## 一、先講結論

`v_monitor.storage_containers` 是一張很容易被誤讀的表。它看起來像「叢集有幾個檔案」，實際上它是「每個 shard 在每個節點上的 ROS container 清單」。在本機 3 節點的 eondb（v25.4.0-0、K-Safe=0、4 個 shard）上，它回 **1,428 列**，而這 1,428 列背後的資料量與尺寸差距，比大多數人想象的極端：

1. **總量：1,428 個 container、1,099,951,625 筆、磁碟 11.99 GB。** payload（未壓縮的欄位總量）是 109.03 GB，整體壓縮 9.10 倍。平均每個 container 770,274 筆——這個平均值沒有意義，因為中位數落在 1 筆那一側。
2. **84% 的 container 只裝 0.01% 的資料。** 1,199 個 container 不到 1,000 筆，合計 108,335 筆、18.94 MB；反過來，3 個 container 裝了 1,007,772,484 筆（91.62%）與 8.86 GB（73.91%）。
3. **container 數不是複本數。** 1,428 列裡，segment0001／0002／0003 分別是 470／481／462 個，replica shard 只有 15 個。replica shard 放的是 unsegmented 投影（5 個投影 × 3 節點），segmented 投影在 replica shard 上完全沒有 container。
4. **十億筆只需要 3 個 container。** `partition_columns_super` 每個 segment shard 一個 container，各 336,125,677／335,881,586／335,765,221 筆、2.95 GB，磁碟上每筆 8.79 bytes。
5. **容器太多，每筆成本量得到。** 掃 3 個大 container 的 10 億筆：2,865 ms、每筆 2.84 ns。掃 789 個小 container 的 920,871 筆：28 ms、每筆 30.5 ns。**每筆成本差 10.7 倍**，而兩張表的資料量差了 1,094 倍。
6. **三個節點的分佈很平均，container 數不平均。** 資料列數 366,827,870／366,626,825／366,496,930（差距 0.09%），磁碟 3.997／3.995／3.995 GB（差距 0.06%），但 container 數是 475／486／467（差距 3.91%）。**container 數的差距來自小容器，不是資料傾斜。**
7. **`v_monitor.partitions` 只有 6 個投影、819 列。** 因為這張 view 只列「有 partition key 的表」，不是 container 的完整目錄；用它的列數去對 container 總數，一定會對不起來。

這篇寫給三種人：看到 `storage_containers` 有 1,428 列、以為叢集有上千個檔案要處理的人；被「ROS pushback」警告嚇到、想算自己離上限多遠的人；以及要對老闆解釋「為什麼十億筆的報表比九十萬筆的還快」的人。**所有數字都來自 2026-11-16 這次執行的唯讀查詢，沒有對叢集做任何變更。**

---

## 二、實測環境

| 項目 | 值（本次實查） |
|------|----------------|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 資料庫 | `eondb`（EON 模式） |
| 節點 | 3（`v_eondb_node0001/0002/0003`，`10.89.10.11`／`.12`／`.13`，全部 `UP`） |
| Shard | 4（`replica` + `segment0001`～`segment0003`），K-Safe = 0 |
| Storage location | 7 個：3 個 `DATA,TEMP`（`/data/eondb/…_data`）、3 個 `DEPOT`（`/data/depot/eondb/…_depot`）、1 個 `COMMUNAL`（`s3://verticadb/eondb`，`has_catalog = true`） |
| 投影 | catalog 內 486 列／434 個 projection_id；其中 78 個 unsegmented、408 個 segmented |
| 有資料的投影 | 219 個名稱／221 個 projection_id（`users_super`、`short_url_super` 各在兩個 schema 下同名） |
| 存取方式 | `~/.hermes/scripts/sched/connect_eondb.py`（`vertica_python`，三個入口 1533/2533/3533），全部 `SELECT` |
| 實測時間 | 2026-11-16 05:05–05:40（Asia/Taipei） |

系列規劃時（11-11）記錄的 container 數是 1,424，本次是 1,428。**這張表是活的**：四天內多了 4 個 container，全部落在小容器那一側。任何拿 container 總數當 SLA 的報表，都要接受它每天會動。

---

## 三、核心原理：container 是「每個 shard 的儲存單位」

在 EON 裡，一筆資料的落點是三層決定下來的：

| 層 | 決定什麼 | 本機實例 |
|----|----------|----------|
| communal storage | 資料的權威副本放在哪裡 | `s3://verticadb/eondb`（唯一 `COMMUNAL` location） |
| shard | 哪些 hash 區間歸這個 shard | 4 個：`replica`（unsegmented）＋3 個 segment |
| container（ROS） | 一個 shard 上一批資料的實體檔案 | 1,428 個，依 shard 分佈 15／470／481／462 |

所以 `v_monitor.storage_containers` 的一列 = 一個節點上、一個 shard 裡、某個投影的一個 container。三個關鍵欄位決定你怎麼讀它：

- `total_row_count`：這個 container 有幾筆（含已刪除，本機 `deleted_row_count` 全是 0）。
- `used_bytes`：磁碟上實際佔用。
- `payload_size`：欄位內容的未壓縮總量。**`payload_size / used_bytes` 才是壓縮比**，不是 `used_bytes / total_row_count`。

本機三者合計：`1,099,951,625` 筆、`11,987,579,031` bytes（11.99 GB）、`109,033,186,943` bytes（109.03 GB），比值 **9.10**。

![1,428 個 container 的三種佔比：container 數有 84% 落在不到 1,000 筆的級距，但這些 container 只佔 0.01% 的資料列與 0.16% 的磁碟；3 個 10M+ 的 container 佔 91.62% 的資料列與 73.91% 的磁碟]({{ '/assets/images/eon-ros-container-size-distribution.svg' | relative_url }}){: loading="lazy" }

圖上五個級距的 container 數、資料列數、磁碟位元組都來自本次實查（1,199／146／53／27／3 個）。

### 3.1 為什麼「每投影 3 個 container」是常態

本機 221 個有資料的 projection_id，container 數分佈是：

| 每個投影的 container 數 | 投影數 | 資料列合計 |
|---:|---:|---:|
| 1 | 36 | 3,872 |
| 2 | 24 | 455 |
| 3 | 156 | 1,099,017,084 |
| 4 | 1 | 4 |
| 9 | 1 | 9,100 |
| 11 | 1 | 20 |
| 63 | 1 | 219 |
| 789 | 1 | 920,871 |

156 個投影剛好 3 個 container，就是「一個 segment shard 一個」。這是 segmented 投影在 EON 的正常形狀：資料依 hash 分到三個 segment，每個 segment 各寫一個 ROS。**看到 3 就放心，看到 789 才要查。**

那 36 個只有 1 個 container 的投影是什麼？全部落在**單一 shard、單一節點**上，合計只有 3,872 筆：

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
 45035996273710574       | 1 |       1 |       1 |     3
 45035996273709380       | 1 |      1 |       1 |     2
```

小表在 EON 上不會平均分散——它的 hash 值只落在一個 segment 的區間裡，於是整個投影只有一個 container、只存在一個節點。**「EON 把資料打散到三個節點」這句話，對小表是錯的。**

---

## 四、Step-by-step：把 1,428 列讀完

### 4.1 總覽

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

`deleted_rows` 是 0：本機沒有任何 container 帶 delete vector（`deleted_row_count > 0` 的 container 數也是 0）。所以下面的「筆數」都是實際筆數，不需要扣刪除。

### 4.2 三個節點：資料很平、container 數不平

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

- 資料列數最大差 330,940 筆（**0.09%**），磁碟最大差 2,389,794 bytes（**0.06%**）——這是 hash 分割的正常抖動。
- container 數最大差 19 個（**3.91%**）。差距全部來自小容器（LOWIDOC 在三個節點是 262／265／262）。
- 所以「節點之間 container 數不一樣」不代表資料傾斜。**要判斷傾斜，看 `rows_` 或 `used_bytes`，不要看 `count(*)`。**

### 4.3 Shard：replica 只有 15 個 container

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

replica shard 的 15 個 container 全部屬於 5 個 unsegmented 投影，每個投影在每個節點各一個：

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

這張清單同時解釋了兩件事：

1. **segmented 投影在 replica shard 上沒有 container。** 所以「K-Safe=0、4 個 shard」不等於「每份資料有 4 份」。
2. **總筆數 1,099,951,625 裡有重複。** replica 的 300,129 筆是同一份資料在三個節點各算一次（`300,129 ÷ 3 = 100,043`）。扣掉重複後，叢集的邏輯筆數是 `1,099,751,539`；其中 segment shard 佔 `1,099,651,496`：

```sql
=> SELECT sum(total_row_count) AS segment_rows,
          (SELECT sum(total_row_count)/3 FROM v_monitor.storage_containers WHERE shard_name='replica') AS unseg_rows
   FROM v_monitor.storage_containers WHERE shard_name LIKE 'segment%';

segment_rows  | unseg_rows
--------------+-----------
1,099,651,496 |    100,043
```

`1,099,651,496 + 100,043 = 1,099,751,539`。**報表要對外講「我們有多少筆」時，用這個數字，不要用 1,099,951,625。**

### 4.4 尺寸分佈：84% 的 container 不到 1,000 筆

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

換成佔比：

| 級距 | container 數 | 佔 container | 佔資料列 | 佔磁碟 |
|------|-------------:|-------------:|---------:|-------:|
| 1–999 | 1,199 | 83.96% | 0.0098% | 0.16% |
| 1K–99K | 146 | 10.22% | 0.28% | 7.45% |
| 100K–1M | 53 | 3.71% | 2.05% | 6.92% |
| 1M–10M | 27 | 1.89% | 6.04% | 11.56% |
| 10M+ | 3 | 0.21% | **91.62%** | **73.91%** |

平均每個 container 770,274 筆，但這個數字兩邊都不代表：最大的 container 有 336,125,677 筆，最小的有 **1 筆**。`min(total_row_count)` 就是 1，而 `deleted_row_count` 全為 0，所以那是真的只有一筆資料的 container，不是刪到剩一筆。

### 4.5 兩個極端

**極端一：`partition_columns_super`，10 億筆只用 3 個 container。**

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

三個 container 的 hash 區間剛好把 `0`–`4,294,967,295` 切成三等份，對應三個 segment shard。每筆在磁碟上 `8.79` bytes、payload `91.9` bytes（10.45 倍壓縮）。**這就是「大 container」的極限形態：一個 shard 一個 ROS，沒有多餘的檔案。**

**極端二：`LOWIDOC_V6_super`，92 萬筆用掉 789 個 container。**

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

平均每個 container `920,871 ÷ 789 = 1,167.1` 筆、磁碟上每筆 `163.5` bytes。為什麼是 789？因為這張表是分區表：

```sql
=> SELECT count(*) AS partition_rows, count(DISTINCT partition_key) AS keys_,
          sum(ros_row_count) AS rows_, sum(ros_size_bytes) AS bytes
   FROM v_monitor.partitions WHERE projection_name = 'LOWIDOC_V6_super';

partition_rows | keys_ | rows_   | bytes
---------------+-------+---------+-----------
           789 |   267 | 920,871 | 150,520,431
```

**267 個 partition key、789 個 container**——平均每個 partition 2.95 個 container（理想是 3 個，也就是三個 shard 各一個；有 12 個 partition 少了一個 shard 的 container，`267 × 3 = 801`，實查 789）。這是「每個 partition 一個 ROS，每次小批量載入各自成一個 ROS，mergeout 還沒合併」的典型形狀。`MergeOutInterval` 是預設 `600` 秒、`ActivePartitionCount` 是 `1`，代表只有一個 partition 被視為 active，其他 partition 的小 ROS 要等 mergeout 排程處理。

### 4.6 量測：大 container 與小 container 的每筆成本

同樣是「掃一張表、數筆數」，兩個極端放在一起量（`label` 加上 `v_monitor.query_profiles.query_duration_us` 取值，wall clock 另以 Python 計時，兩者一致）：

```sql
=> SELECT /*+label(w_bigscan)*/ count(*) FROM mtk.partition_columns WHERE table_name IS NOT NULL;
1007772484

=> SELECT /*+label(w_lowscan)*/ count(*) FROM ADA.LOWIDOC_V6 WHERE DREREFERENCE IS NOT NULL;
920871
```

| 查詢 | container 數 | 掃到的筆數 | 磁碟 | `query_duration_us` | 每筆成本 |
|------|-------------:|-----------:|------|--------------------:|---------:|
| `partition_columns`（3 個 container） | 3 | 1,007,772,484 | 8.86 GB | 2,864,839 / 2,894,667（兩次） | **2.84 ns** |
| `LOWIDOC_V6`（789 個 container） | 789 | 920,871 | 150.5 MB | 28,060 / 27,023（兩次） | **30.5 ns** |

換算吞吐：大 container 那一側是 351,772,817 筆/秒（三個節點合計），每節點約 1,031 MB/s；小 container 那一側是 32,817,926 筆/秒。**每筆成本差 10.7 倍。**

誠實說明限制：這兩張表的欄位寬度不同（`LOWIDOC_V6` 有 `varchar(65000)` 欄位）、filter 不同、樣本只有各兩次，而且 depot 是熱的。**這個量測能證明「container 數不是免費的」，不能推成「每多一個 container 就多幾毫秒」的公式。** 要算自己環境的成本，用同樣的方法量：`query_duration_us` 除以掃到的筆數，再對 container 數做迴歸。

### 4.7 一個常見陷阱：`count(*)` 可能根本不掃 container

同一張 10 億筆的表，不帶 filter 時：

```sql
=> SELECT /*+label(w_nofilter)*/ count(*) FROM mtk.partition_columns;
1007772484
-- wall clock: 0.406 s（第二次 0.397 s）
```

0.4 秒不是「掃了 10 億筆」，是優化器直接讀統計值（`EXPLAIN` 會顯示 `ROW COUNT ONLY`）。**要量 container 的掃描成本，一定要寫一個會碰到資料的 predicate**，否則你量到的是 catalog 查詢的速度。

### 4.8 `v_monitor.partitions` 只有 6 個投影

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
(819 rows total across 6 projections)
```

6 個投影、819 列。原因是 `v_monitor.partitions` 的每一列是「一個 partition key × 一個 ROS」，**沒有 partition key 的表不會出現在這裡**。本機 221 個有資料的投影裡只有 6 個是分區表，所以這張 view 的列數（819）跟 container 總數（1,428）沒有可比性——它們量的不是同一件事。

---

## 五、成本分析：壓縮比與 container 數是兩件事

| 投影 | container | 筆數 | 磁碟 | payload | 壓縮比 | 磁碟/筆 |
|------|----------:|-----:|------|---------|-------:|--------:|
| `partition_columns_super` | 3 | 1,007,772,484 | 8.86 GB | 92.58 GB | 10.45× | 8.79 B |
| `vsmart_test_super` | 3 | 294,958 | 382.7 MB | 665.9 MB | 1.74× | 1,297 B |
| `GCIS_Companyinfo_CmpB_super` | 3 | 13,245,243 | 341.6 MB | 1.06 GB | 3.11× | 25.8 B |
| `projection_usage_super` | 3 | 6,891,320 | 251.3 MB | 1.27 GB | 5.06× | 36.5 B |
| `judicial_202604_super` | 3 | 100,427 | 233.3 MB | 601.8 MB | 2.58× | 2,323 B |
| `LOWIDOC_V6_super` | 789 | 920,871 | 150.5 MB | 7.47 GB | **49.62×** | 163.5 B |

兩個觀察：

1. **壓縮比與 container 數無關。** 3 個 container 的 `vsmart_test_super` 只有 1.74 倍，789 個 container 的 `LOWIDOC_V6_super` 是 49.62 倍。壓縮比由欄位型別、排序與編碼決定；container 多只是讓「每個檔案內的樣本變少」，並沒有讓壓縮變好或變壞。
2. **磁碟/筆的差距比 container 數更能解釋掃描速度。** `LOWIDOC_V6_super` 每筆 163.5 bytes 的磁碟資料，`partition_columns_super` 每筆 8.79 bytes；但實測的每筆成本反而是小 container 那一側慢 10.7 倍，所以主因不是 bytes，是「每個 container 的固定成本 × 789」。

我沒有逐欄驗證 `LOWIDOC_V6` 的編碼（49.62 倍主要來自大量空值與重複字串，但我沒有拆開量），所以這一段只陳述 `payload_size / used_bytes` 的比值，不推論原因。

---

## 六、常見錯誤與反模式

**1. 把 1,428 當成「叢集有 1,428 份資料」。** 它是三個節點上每個 shard 的 container 列數合計。同一份 unsegmented 資料在三個節點各有一列（replica shard 300,129 筆），所以邏輯筆數是 1,099,751,539，不是 1,099,951,625。

**2. 用 `count(*)` 量 container 的掃描成本。** 本機 10 億筆不帶 filter 的 `count(*)` 是 0.4 秒，因為它讀統計值。加了 `WHERE table_name IS NOT NULL` 之後才是真的掃 container，2.87 秒。**差了 7 倍，量到的東西完全不同。**

**3. 以為 container 數平均。** 平均 770,274 筆/container，但 84% 的 container 不到 1,000 筆。用平均值做容量規劃，會低估小容器造成的固定成本。

**4. 以為 `v_monitor.partitions` 是 container 的目錄。** 它只有 6 個投影、819 列，因為只列有 partition key 的表。拿 819 去對 1,428，會得到「有 600 個 container 沒有 partition 紀錄」的錯誤結論——那些表根本沒有分區。

**5. 以為小表會平均分散到三個節點。** 36 個投影只有 1 個 container，全部落在單一 shard／單一節點，合計 3,872 筆。這種表在 EON 上沒有「分散」可言，節點故障時它就是不可用（K-Safe=0 時更是直接遺失）。

**6. 以為沒到 `ContainersPerProjectionLimit` 就沒事。** 本機上限是預設 `1,024`，`LOWIDOC_V6_super` 789 個（單節點最多 265 個，是上限的 25.9%），距離 pushback 還很遠，但每筆掃描成本已經比大 container 高 10.7 倍。**上限是最後一根稻草，不是效能指標。**

**7. 把 `used_bytes` 當成資料大小。** 同一批資料的 payload 是 109.03 GB、磁碟 11.99 GB。對外報「資料量」時要說清楚是哪一個，否則採購 storage 時會差一個數量級。

---

## 七、風險與建議

**現在就能做（唯讀、低風險）**

- 巡檢加一條「尺寸分佈」查詢（4.4 節那一段），固定看兩個比例：`< 1,000 筆` 的 container 佔比（本機 83.96%）、`10M+` 的資料列佔比（本機 91.62%）。前者上升代表小 ROS 在累積，後者上升代表大表在集中。
- 用 `v_monitor.partitions` 對「分區表」單獨看，不要拿它對全叢集的 container 總數。
- 比較節點時看 `sum(total_row_count)` 或 `sum(used_bytes)`，不要看 `count(*)`（本機 container 數差 3.91%，資料只差 0.06%）。
- 量掃描成本時一定帶 predicate，並用 `v_monitor.query_profiles.query_duration_us` 取值，不要用 `count(*)` 的空查詢。

**需要變更（要窗口、要核准）**

- `LOWIDOC_V6_super` 這類「每 partition 一個小 ROS」的表，正解是調整載入批次或 partition 策略，讓每次載入的量足以形成合理大小的 ROS，再讓 mergeout 合併。這需要 DDL／DML，本文沒有做。
- `MergeOutInterval`（本機 600 秒）與 `ActivePartitionCount`（本機 1）會決定 mergeout 多久處理一次、以及哪些 partition 被視為 active。調整前要先確認 mergeout 的視窗與負載高峰不重疊。
- 若真的要逼近 `ContainersPerProjectionLimit`，處理順序是「先合併小 ROS，再考慮調高上限」；調高上限只是把 pushback 往後延，不會讓 789 個 container 變快。

---

## 八、參考資料

- Vertica 25.4 文件：ROS containers — https://docs.vertica.com/25.4.x/en/admin/managing-tables/ros-containers/
- Vertica 25.4 文件：`STORAGE_CONTAINERS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/storage-containers/
- Vertica 25.4 文件：`PARTITIONS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/partitions/
- Vertica 25.4 文件：Partitions — https://docs.vertica.com/25.4.x/en/admin/managing-tables/partitions/
- Vertica 25.4 文件：EON architecture — https://docs.vertica.com/25.4.x/en/architecture/eon/architecture/
- Vertica 25.4 文件：Configuration parameters — https://docs.vertica.com/25.4.x/en/sql-reference/config-parameters/

> 註：`docs.vertica.com` 目前 302 導向 `docs.rocketsoftware.com/bundle/vertica_25.4/`，路徑對應相同；本文連結於 2026-11-16 檢查皆回應 200。
