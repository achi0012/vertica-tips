---
layout: post
title: "EON 的 shard 與 segmentation：為什麼資料分佈不是看 projection 的 segmentation"
date: 2026-11-14 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, shard, segmentation, hash-ring, node-subscription, data-distribution]
description: "在 3 節點 eondb（v25.4.0-0、K-Safe=0、4 shard）實測：資料落點是 hash(segmentation 欄位) mod 2^32 落在哪個 shard，不是節點數取模。13,245,243 筆的表用 hash(id) % 4294967296 分三段算出 4,413,479 / 4,419,435 / 4,412,329，與三個 ROS container 的 total_row_count 完全一致。projection 的 segment_range 在 EON 只寫「每個節點 33.3%」，看不到邊界；真正的邊界在 v_catalog.shards。"
image: /assets/images/og/vertica-eon-shards-segmentation-zh.png
faq:
  - q: "EON 的資料分佈是看 projection 的 segmentation，還是看 shard？"
    a: "兩者分工：segmentation 決定 hash 的**輸入欄位**，shard 決定 hash 值**去哪個節點**。本機實測把 `ADA.GCIS_Companyinfo_CmpB`（13,245,243 筆、`hash(id)`）用 `hash(id) % 4294967296` 分成三段，得到 4,413,479 / 4,419,435 / 4,412,329 筆，與三個 ROS container 的 `total_row_count` 一位不差；但這段邊界只存在於 `v_catalog.shards`，`v_catalog.projections.segment_range` 只寫「implicit range: 每個節點 33.3%」。"
  - q: "為什麼 shard 邊界要用 32 位，而不是 hash() 回傳的 64 位？"
    a: "`v_catalog.shards` 的 `lower_hash_bound`/`upper_hash_bound` 是 32 位整數，最大 4,294,967,295；`hash()` 回傳的是 64 位 BIGINT。實測直接拿 `hash(id)` 比邊界，13,245,243 筆全部落在 `segment0003`（因為 64 位值幾乎都大於 4.29e9）；改成 `hash(id) % 4294967296` 才對上三個容器的筆數。"
  - q: "K-Safe 要怎麼從 system table 看出來？"
    a: "數 `v_catalog.node_subscriptions`：本機 6 筆，`replica` 有 3 筆（1 primary + 2 secondary），`segment0001`～`segment0003` 各只有 1 筆。每個 segment 沒有 buddy 副本，所以 K-Safe = 1 − 1 = 0，連線時會收到 `WARNING 6046`。"
  - q: "沒有分段的表（is_segmented = false）在 EON 放哪裡？"
    a: "放在 replica shard，三個節點各一份。本機 26 個未分段 projection 在 `v_catalog.projections` 佔 78 列（每個 3 列，`node_name` 有值），在 `v_monitor.storage_containers` 是 15 個 container、300,129 筆、8,360,166 bytes，換算每節點 5 個 container、100,043 筆、2,786,722 bytes，三節點數字完全相同。"
---
<!--
AI Summary: 在 3 節點 eondb（Vertica v25.4.0-0、K-Safe=0、4 shard）以唯讀查詢驗證 EON 的資料落點機制：三段 segment 覆蓋 0–4,294,967,295 共 4,294,967,296 個 hash 值、缺口 0，13,245,243 筆的 ADA.GCIS_Companyinfo_CmpB 用 hash(id) % 4294967296 分桶得到 4,413,479 / 4,419,435 / 4,412,329，與三個 ROS container 的 total_row_count 完全相同，第二張表 1,093,169 筆亦吻合；v_catalog.projections 的 486 列實為 434 個 projection（408 個分段各 1 列、26 個未分段各 3 列），segment_range 只顯示每節點 33.3% 而非 hash 邊界；全叢集 1,381 個 container、1,099,951,556 筆，三段佔比 33.34% / 33.32% / 33.31%；EXPLAIN 顯示單一 segmentation key 查詢只在 node0003 執行，非 segmentation 欄位 filter 則三個節點全開。全程唯讀。
AI Keywords: Vertica, EON, shard, segmentation, hash ring, node subscription, ROS container, K-Safe, data distribution, 25.4
-->
<!--more-->

---

## 一、先講結論

「EON 的資料怎麼分佈」這個問題，網路上最常見的答案是「照 projection 的 segmentation 分」。這句話不算錯，但它會讓你在算容量、查傾斜、判斷 shard pruning 的時候全部算錯，因為它漏掉了中間一層。我在本機 3 節點的 eondb（v25.4.0-0、K-Safe=0、4 個 shard）上用唯讀查詢把這件事從頭算了一遍，七個結論：

1. **落點是「hash 值落在哪個 shard」，不是「hash 值 mod 節點數」。** 三段 segment 連續覆蓋 `0`–`4,294,967,295`，合計 `4,294,967,296` = `2^32`，缺口 `0`；每段只由一個節點持有 primary subscription。
2. **落點可以一位不差地算回來。** `ADA.GCIS_Companyinfo_CmpB`（13,245,243 筆、segmentation 是 `hash(id)`）用 `hash(id) % 4294967296` 分三段，得到 `4,413,479` / `4,419,435` / `4,412,329`，與三個 ROS container 的 `total_row_count` **完全相同**。換一張 1,093,169 筆的表再算一次，`365,062` / `365,354` / `362,753` 也完全相同。
3. **segmentation 沒有消失，但它只管 hash 的輸入。** 486 列 `v_catalog.projections` 裡有 408 列 `is_segmented = true`，`segment_expression` 是完整的 `hash(...)`；可是 `segment_range` 在 EON 只寫 `implicit range: 每個節點[33.3%]`，看不到任何 hash 邊界。**邊界只存在於 `v_catalog.shards`。**
4. **未分段的表走 replica shard，三個節點各一份。** 26 個 `is_segmented = false` 的 projection 在 catalog 佔 78 列（每個 3 列，`node_name` 有值），實際資料是 15 個 container、每節點 5 個、各 100,043 筆、各 2,786,722 bytes。
5. **K-Safe 可以直接數出來。** `v_catalog.node_subscriptions` 共 6 筆：`replica` 3 筆（1 primary + 2 secondary）、每個 segment 各 1 筆。沒有 buddy，所以是 0-safe，連線時就丟 `WARNING 6046`。
6. **三段負載幾乎一樣重。** 366,727,810 / 366,526,751 / 366,396,866 筆，佔比 33.34% / 33.32% / 33.31%，最大與最小差 0.03 個百分點。
7. **查詢端真的會 prune。** 對 segmentation 欄位做等值查詢，`EXPLAIN` 的 `Execute on:` 只有 `v_eondb_node0003`，實測 5.5–7.3 ms；換成非 segmentation 欄位過濾，同一張表變成 `All Nodes`、58.0–58.5 ms。

這篇寫給三種人：以為「shard 數 = 節點數」而算錯 depot 容量的人；看著 `segment_range` 的 33.3% 想找出 hash 邊界卻找不到的人；以及要判斷「這條查詢會不會打三個節點」的人。**所有標「實查」的數字都來自 2026-11-14 這次執行；機制面的說明屬官方文件範疇，文末附連結。全程唯讀，沒有對叢集做任何變更。**

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 資料庫 | `eondb`（EON 模式） |
| 節點 | 3（`v_eondb_node0001/0002/0003`，`node_type = PERMANENT`，全部 `UP`） |
| Shard | 4（1 replica + 3 segment），`default_shard_count = 3` |
| Node subscription | 6 筆（`replica` 3、`segment*` 各 1） |
| K-Safe | 0（連線即 `WARNING 6046`） |
| Communal storage | `s3://verticadb/eondb` |
| 容器與筆數 | 1,381 個 ROS container、221 個 projection、1,099,951,556 筆、`disk_size` 合計 12,047,766,999 bytes |
| 對照組 | 本機 `testdb`（Enterprise 模式、單節點、同版本 v25.4.0-0） |
| 存取方式 | `eondb` 走 `vertica_python`（三個入口 1533/2533/3533 指向同一叢集）、`testdb` 走 `vsql-testdb`，全部 `SELECT` / `EXPLAIN` |

對照組只有單節點，所以**Enterprise 多節點的 hash 區間切法我沒有辦法在本機重現**；能實測的是「Enterprise 沒有 shard 這一層」與「單節點時整個 32 位 ring 都由同一個節點持有」，這兩點在第六節給數字。

---

## 三、核心原理：三層，缺一層就會算錯

| 層 | 回答什麼 | 實查來源 | 本機數字 |
|----|----------|----------|----------|
| segmentation | 一筆資料的 **hash 輸入**是哪些欄位 | `v_catalog.projections.segment_expression` | 408 個分段 projection，例：`hash(GCIS_Companyinfo_CmpB.id)` |
| shard | hash 值**切成幾段、邊界在哪** | `v_catalog.shards` | 4 筆（3 segment + 1 replica），邊界 0 / 1,431,655,739 / 2,866,311,478 |
| node subscription | 哪個**節點**持有哪個 shard | `v_catalog.node_subscriptions` | 6 筆 |
| ROS container | 檔案實際落在哪個節點的哪個 shard 上 | `v_monitor.storage_containers` | 1,381 筆，`shard_name` 有值 |

一句話版本：**segmentation 決定「這筆資料的 hash 是多少」，shard 決定「這個 hash 值歸誰」。** 這兩個問題分開之後，很多事情就通了：

- 加節點不用改 segmentation，但 shard 要重新分配（官方文件：rebalance shards）。
- `K-Safe` 是 shard 的屬性（幾個 subscription），不是 projection 的屬性。
- 要看資料落點，`v_catalog.projections` 幫不上忙，`v_catalog.shards` 才是指標。

![EON 的資料落點：三段 segment 覆蓋 2^32 的 hash 空間，每段落在一個節點；replica shard 由三個節點各持一份]({{ '/assets/images/eon-shard-hash-ring.svg' | relative_url }}){: loading="lazy" }

上圖的每個數字都來自本次實查：三段 hash 值數、三個節點的 container 數與筆數、replica shard 的三節點副本。

---

## 四、Step-by-step：把落點算回來

### 4.1 shard 邊界與缺口

```sql
=> SELECT shard_name, shard_type, lower_hash_bound, upper_hash_bound,
          upper_hash_bound - lower_hash_bound + 1 AS hash_values
   FROM v_catalog.shards ORDER BY shard_name;

  shard_name  | shard_type | lower_hash_bound | upper_hash_bound | hash_values
--------------+------------+------------------+------------------+-------------
 replica      | Replica    |                  |                  |
 segment0001  | Segment    | 0                | 1431655738       |  1431655739
 segment0002  | Segment    | 1431655739       | 2863311477       |  1431655739
 segment0003  | Segment    | 2863311478       | 4294967295       |  1431655818

=> SELECT shard_name, lo, hi, (hi - lo + 1) AS span,
          sum(hi - lo + 1) OVER () AS total_span,
          lo - lag(hi) OVER (ORDER BY lo) - 1 AS gap_before
   FROM (SELECT shard_name, lower_hash_bound lo, upper_hash_bound hi
         FROM v_catalog.shards WHERE shard_type = 'Segment') s
   ORDER BY lo;

  shard_name  |     lo     |     hi     |   span    | total_span | gap_before
--------------+------------+------------+-----------+------------+------------
 segment0001  |          0 | 1431655738 | 1431655739 | 4294967296 | 
 segment0002  | 1431655739 | 2863311477 | 1431655739 | 4294967296 |          0
 segment0003  | 2863311478 | 4294967295 | 1431655818 | 4294967296 |          0
```

`gap_before` 兩段都是 `0`，`total_span` 是 `4,294,967,296`，正好 `2^32`。**邊界值是含在區間內的**：`segment0001` 是 `0 ≤ h ≤ 1,431,655,738`，下一段從 `1,431,655,739` 開始；算長度要 `hi - lo + 1`，不是 `hi - lo`。

這張表和上一篇（EON 架構總覽）量到的邊界一模一樣，但**container 數與筆數今天又變了**（昨天 1,394 個 container、450/484/445；今天 1,381 個、456/461/449）——shard 邊界是設定，container 是會 mergeout 的檔案，兩者的變動頻率不是同一個量級。

### 4.2 哪個節點持有哪個 shard

```sql
=> SELECT node_name, shard_name, is_primary, subscription_state
   FROM v_catalog.node_subscriptions ORDER BY node_name, shard_name;

     node_name     |  shard_name  | is_primary | subscription_state
-------------------+--------------+------------+--------------------
 v_eondb_node0001  | replica      | True       | ACTIVE
 v_eondb_node0001  | segment0001  | True       | ACTIVE
 v_eondb_node0002  | replica      | False      | ACTIVE
 v_eondb_node0002  | segment0002  | True       | ACTIVE
 v_eondb_node0003  | replica      | False      | ACTIVE
 v_eondb_node0003  | segment0003  | True       | ACTIVE
```

6 筆的形狀本身就是答案：每個節點對自己的 segment 是 primary，對 `replica` 只有 node0001 是 primary、另兩個是 secondary。**K-Safe 就是「同一個 segment 有幾筆 subscription 減 1」**，這裡每個 segment 只有 1 筆，所以是 0。

```sql
=> SELECT shard_name, count(*) subs,
          sum(CASE WHEN is_primary THEN 1 ELSE 0 END) primaries
   FROM v_catalog.node_subscriptions GROUP BY 1 ORDER BY 1;

  shard_name  | subs | primaries
--------------+------+-----------
 replica      |    3 |         1
 segment0001  |    1 |         1
 segment0002  |    1 |         1
 segment0003  |    1 |         1
```

### 4.3 用真實資料把 hash 算回來（本篇的關鍵實驗）

找一張 segmentation 只有一個欄位、資料量夠大的表：`ADA.GCIS_Companyinfo_CmpB`，segmentation 是 `hash(id)`，13,245,243 筆。三個 container 的 `total_row_count` 分別是 4,413,479 / 4,419,435 / 4,412,329。

現在用 SQL 自己分桶，看能不能湊出同一組數字：

```sql
=> SELECT CASE WHEN (hash(id) % 4294967296) <= 1431655738 THEN 'segment0001'
               WHEN (hash(id) % 4294967296) <= 2863311477 THEN 'segment0002'
               ELSE 'segment0003' END AS shard_from_hash,
          count(*) AS rows_
   FROM ADA.GCIS_Companyinfo_CmpB GROUP BY 1 ORDER BY 1;

 shard_from_hash |  rows_
-----------------+---------
 segment0001     | 4413479
 segment0002     | 4419435
 segment0003     | 4412329
```

對照 `v_monitor.storage_containers`：

```sql
=> SELECT node_name, shard_name, total_row_count, deleted_row_count,
          segment_lower_bound, segment_upper_bound, disk_size
   FROM v_monitor.storage_containers
   WHERE projection_name = 'GCIS_Companyinfo_CmpB_super' ORDER BY shard_name;

     node_name     |        shard_name  | total_row_count | deleted_row_count | segment_lower_bound | segment_upper_bound | disk_size
-------------------+--------------------+-----------------+-------------------+---------------------+---------------------+-----------
 v_eondb_node0001  | segment0001        |         4413479 |                 0 |                   0 |          1431655738 | 114183018
 v_eondb_node0002  | segment0002        |         4419435 |                 0 |          1431655739 |          2863311477 | 114384873
 v_eondb_node0003  | segment0003        |         4412329 |                 0 |          2863311478 |          4294967295 | 113996312
```

**三組數字完全相同**，而且 container 的 `segment_lower_bound`/`segment_upper_bound` 與 shard 的邊界一字不差。換一張表再驗一次（`ADA.GCIS_Companyinfo_Person`，1,093,169 筆、`hash(id)`）：分桶得到 365,062 / 365,354 / 362,753，container 也是 365,062 / 365,354 / 362,753。

這就是「為什麼不能只看 projection 的 segmentation」的正確版本：**segmentation 給的是 hash 的輸入，落點是 hash 值對到哪個 shard 區間。** 同一份 segmentation、同樣的 hash 值，如果 shard 邊界改了（rebalance、改 shard 數），資料就會搬家——但 projection 的 DDL 一個字都不用改。

### 4.4 容器的邊界一定被 shard 邊界包住

如果 shard 真的是上層切分，那每個 container 的 hash 區間不該超出它所屬 shard 的區間。全叢集驗一次：

```sql
=> SELECT count(*) AS violations
   FROM v_monitor.storage_containers sc
   JOIN v_catalog.shards s ON s.shard_name = sc.shard_name
   WHERE sc.segment_lower_bound IS NOT NULL
     AND (sc.segment_lower_bound < s.lower_hash_bound
       OR sc.segment_upper_bound > s.upper_hash_bound);

 violations
------------
          0
(1 row)
```

`0`。1,381 個 container 裡有 1,366 個帶 hash 區間（其餘 15 個是 replica shard 上未分段 projection 的 container，邊界為 NULL），沒有一個越界。

### 4.5 全叢集的分佈

```sql
=> SELECT shard_name, count(*) containers, count(DISTINCT projection_id) projs,
          sum(total_row_count) rows_, sum(disk_size) bytes_,
          round(100.0 * sum(total_row_count) / sum(sum(total_row_count)) OVER (), 3) AS pct_rows
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

  shard_name  | containers | projs |   rows_    |   bytes_   | pct_rows
--------------+------------+-------+------------+------------+----------
 replica      |         15 |     5 |     300129 |    8360166 |    0.027
 segment0001  |        456 |   183 |  366727810 | 4014640911 |   33.340
 segment0002  |        461 |   188 |  366526751 | 4012538061 |   33.322
 segment0003  |        449 |   180 |  366396866 | 4012227861 |   33.310
```

三段的 container 數 456/461/449 不完全一樣（mergeout 進度不同），但筆數幾乎相同：33.34% / 33.32% / 33.31%。**這就是 shard 分佈正常的長相**：container 數會抖，筆數不會。如果你看到某一段筆數 40%、另一段 20%，那不是「container 還沒 merge」，是 hash 真的不均——先去看 segmentation 欄位是不是有嚴重的 skew。

`projs` 那一欄是「該 shard 上有 container 的投影數」：segment0001 183 個、segment0002 188 個、segment0003 180 個，replica 另有 5 個。三者相加是 551 個 (投影, shard) 組合，加上 replica 的 5 個共 556 個，對應 221 個「實際有 container」的投影——同一個投影本來就會在多段各有一份，所以這兩個數字不該相等；要對的是 `count(DISTINCT projection_id)`。

### 4.6 把 shard 換算成容量

```sql
=> SELECT shard_name, sum(disk_size) bytes_,
          round(100.0 * sum(disk_size) / sum(sum(disk_size)) OVER (), 3) AS pct_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

  shard_name  |   bytes_   | pct_bytes
--------------+------------+-----------
 replica      |    8360166 |     0.069
 segment0001  | 4014640911 |    33.323
 segment0002  | 4012538061 |    33.305
 segment0003  | 4012227861 |    33.303
```

三個 segment 各約 4.01 GB，replica 只有 8.36 MB（0.069%）。`disk_size` 是壓縮後、落在容器裡的位元組數，不是 raw data 大小，所以這組數字直接對應「節點上要佔多少空間」，而不是「載入多少資料」。

兩個實務結論。第一，**每個節點的 depot 至少要放得下自己那 4.01 GB 加上 replica 的 8.36 MB**；放不下就會被 evict，查詢得回 communal 抓。第二，**這台機器上「用節點數算」和「用 shard 數算」剛好一樣**（3 對 3），所以看不出差別；一旦 shard 數與節點數不同（例如 `default_shard_count` 設成 6 而只有 3 個節點），容量要用 shard 數算，因為切的是資料不是機器。

---

## 五、那 486 列 projection 到底是幾個投影？

```sql
=> SELECT is_segmented, count(*) rows_, count(DISTINCT projection_id) ids
   FROM v_catalog.projections GROUP BY 1;

 is_segmented | rows_ | ids
--------------+-------+-----
 False        |    78 |  26
 True         |   408 | 408

=> SELECT n, count(*) AS proj_ids
   FROM (SELECT projection_id, count(*) n FROM v_catalog.projections GROUP BY 1) x
   GROUP BY 1 ORDER BY 1;

 n | proj_ids
---+----------
 1 |      408
 3 |       26
```

看懂這兩張表，EON 的 catalog 就不會再誤導你：

- **分段的 projection：一個投影只有一列**，`node_name` 是空的——因為它不屬於任何單一節點，它的資料按 hash 散在三個 shard 上。
- **未分段的 projection：一個投影三列**，`node_name` 分別是三個節點——因為資料在 replica shard 上，三個節點各一份。

所以 486 列 = 408 個分段投影 + 26 個未分段投影 × 3。`SELECT count(*) FROM v_catalog.projections` 得到的**不是投影數量**，是「投影 × 放置位置」的列數。

再看 `segment_range`：

```sql
=> SELECT DISTINCT segment_range FROM v_catalog.projections WHERE is_segmented LIMIT 1;

 implicit range: v_eondb_node0001[33.3%] v_eondb_node0002[33.3%] v_eondb_node0003[33.3%]
```

它告訴你「三個節點各分到 33.3%」，但**沒有告訴你邊界在哪**。要邊界，只能查 `v_catalog.shards`。這就是本文標題的意思：資料分佈不是看 projection 的 segmentation——projection 只給你 hash 的輸入，落點要看 shard。

### 5.1 四個常被混用的數字

```sql
=> SELECT (SELECT count(*) FROM v_catalog.projections) AS catalog_rows,
          (SELECT count(DISTINCT projection_id) FROM v_catalog.projections) AS catalog_projs,
          (SELECT count(*) FROM v_monitor.storage_containers) AS containers,
          (SELECT count(DISTINCT projection_id) FROM v_monitor.storage_containers) AS projs_with_data;

 catalog_rows | catalog_projs | containers | projs_with_data
--------------+---------------+------------+-----------------
          486 |           434 |       1381 |             221
```

四個數字回答四個問題：486 是「投影 × 放置位置」的列數；434 是 catalog 裡真正的投影數；1,381 是 ROS container 數；221 是「目前真的有檔案」的投影數。寫巡檢報表時把四個一起放，就不會再出現「為什麼投影數每天在變」——會變的通常是 221 那一欄，因為 mergeout 與新投影建立都會動它。

未分段投影的清單也值得每天看一次：

```sql
=> SELECT projection_name, count(*) containers, count(DISTINCT node_name) nodes,
          sum(total_row_count) rows_
   FROM v_monitor.storage_containers WHERE shard_name = 'replica'
   GROUP BY 1 ORDER BY 4 DESC;

         projection_name          | containers | nodes |  rows_
----------------------------------+------------+-------+--------
 customer_dimension_flex2_1_unseg |          3 |     3 | 150000
 xxx_unseg                        |          3 |     3 | 150000
 lowi_user_save_source_unseg      |          3 |     3 |    114
 scheduled_task_unseg             |          3 |     3 |      9
 lowi_user_save_channel_unseg     |          3 |     3 |      6
```

5 個投影、15 個 container、300,129 筆，換算每節點 100,043 筆。`containers = 3` 且 `nodes = 3` 就是「三個節點各一份」的證據；哪天看到某個投影的 `nodes = 2`，代表有節點的 replica subscription 掉了或還在同步，值得當天查。

---

## 六、Enterprise 對照：沒有 shard 這一層

同一台機器的 `testdb`（Enterprise 模式、單節點、同版本）：

```sql
=> SELECT count(*) AS shard_rows FROM v_catalog.shards;

 shard_rows
------------
          0

=> SELECT count(*) containers,
          sum(CASE WHEN segment_lower_bound = 0 AND segment_upper_bound = 4294967295 THEN 1 ELSE 0 END) full_ring
   FROM v_monitor.storage_containers;

 containers | full_ring
------------+-----------
       1494 |      1483
```

兩個訊號很明確：

1. **Enterprise 沒有 shard。** `v_catalog.shards` 是 0 列，`v_monitor.storage_containers.shard_name` 全部是空字串。
2. **單節點把整個 32 位 ring 吃下來。** 1,483 個 container 的區間都是 `0`–`4294967295`，另外 11 個是 NULL（未分段 projection）。

catalog 也對得上：testdb 的 `v_catalog.projections` 是 433 列、433 個 `projection_id`（一個投影一列，因為只有一個節點），其中 411 個分段。**同樣的 `count(*)`，在 Enterprise 是投影數，在 EON 不是**——這件事在寫巡檢 SQL 時很容易踩。

我沒有多節點的 Enterprise 環境，所以 Enterprise 多節點時「segmentation 的 hash 區間怎麼分給各節點」我不給實測數字，只說機制：Enterprise 由 projection 的 segmentation 直接對應到節點，加節點要重新分段；EON 由 shard 對應到節點，加節點是搬 shard。這個差異的官方說明在文末連結。

從 Enterprise 搬到 EON，巡檢 SQL 最常壞的三個地方：`SELECT count(*) FROM v_catalog.projections`（EON 會把未分段投影的節點副本一起算進去）、`WHERE shard_name IS NULL`（Enterprise 是空字串不是 NULL，這個條件抓不到任何東西）、以及任何假設「一個投影對一個節點」的 join。改法都一樣：投影數用 `count(DISTINCT projection_id)`、節點歸屬看 `v_catalog.node_subscriptions`、資料落點看 `v_monitor.storage_containers.shard_name`。

---

## 七、查詢端：shard pruning 長什麼樣

對 segmentation 欄位做等值查詢：

```sql
=> EXPLAIN SELECT count(*) FROM ADA.GCIS_Companyinfo_CmpB
   WHERE id = '3cc5cd9e-253e-11eb-9cb0-005056b1498c';

+-GROUPBY NOTHING [Cost: 19K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*)
|  Execute on: v_eondb_node0003
| +---> STORAGE ACCESS for GCIS_Companyinfo_CmpB [Cost: 19K, Rows: 13M ...]
| |      Projection: default_namespace.ADA.GCIS_Companyinfo_CmpB_super
| |      Filter: (GCIS_Companyinfo_CmpB.id = '3cc5cd9e-...')
| |      Execute on: v_eondb_node0003
```

`Execute on:` 只有 `v_eondb_node0003`——一筆資料的 hash 只會落在一個 shard，所以只有一個節點需要動。換成非 segmentation 欄位：

```sql
=> EXPLAIN SELECT count(*) FROM ADA.GCIS_Companyinfo_CmpB
   WHERE business_seq_no = '0021';

+-GROUPBY NOTHING [Cost: 6K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*)
|  Execute on: All Nodes
| +---> STORAGE ACCESS for GCIS_Companyinfo_CmpB [Cost: 6K, Rows: 13M ...]
| |      Filter: (GCIS_Companyinfo_CmpB.business_seq_no = '0021')
| |      Execute on: All Nodes
```

同一張表，`All Nodes`。實測時間（同一連線、暖 depot、各跑 3 次取最小）：

| 查詢 | 掃描節點 | 回傳筆數 | 最小耗時 |
|------|----------|----------|----------|
| `WHERE id = '3cc5cd9e-…'`（segmentation 欄位） | 1（node0003） | 1 | 5.5 ms |
| `WHERE id = 'ffffffff-…'`（不存在的 key） | 1 | 0 | 4.8 ms |
| `WHERE business_seq_no = '0021'`（非 segmentation） | 3（All Nodes） | 174,498 | 58.0 ms |
| `SELECT count(*)`（全表） | 3 | 13,245,243 | 15.3 ms |

兩點提醒。第一，**這不是等價比較**：兩條查詢回傳的筆數差了五個數量級，58 ms 主要是掃 13M 筆的成本，不只是節點數。第二，**`count(*)` 不能拿來當效能指標**：13M 筆的 `count(*)` 只要 15.3 ms，因為它是從 ROS 統計值回答的（計畫裡標 `ROW COUNT ONLY`），沒有真的掃資料。

### 7.1 `Participating subscriptions` 不是 prune 後的結果

計畫尾端的 GraphViz 區塊會列出這段：

```text
All Nodes Vector:
  node[0]=v_eondb_node0001 (initiator) Up
  node[1]=v_eondb_node0002 (executor) Up
  node[2]=v_eondb_node0003 (remote initiator) Up

Participating subscriptions:
  replica: [<0, 1, 2>]
  segment0001: [<0>]
  segment0002: [<1>]
  segment0003: [<2>]
```

它在「單一 id 查詢」與「全表 `count(*)`」兩個計畫裡**一字不差**——因為它列的是「這個叢集有哪些 subscription 可用」，不是「這條查詢實際用了哪些」。判斷 prune 要看 Access Path 的 `Execute on:`：單一 id 是 `v_eondb_node0003`，全表是 `All Nodes`。把這兩段搞混，很容易得出「EON 根本沒有 shard pruning」的錯誤結論，然後去改 segmentation——但問題其實在你讀錯了計畫。

---

## 八、常見錯誤與反模式

**1. 用 `hash()` 直接比 shard 邊界。** 我第一次就是這樣寫：`CASE WHEN hash(id) <= 1431655738 …`。結果 13,245,243 筆**全部**落在 `segment0003`，因為 `hash()` 回傳 64 位 BIGINT（該表實測最小值就是 293,223,774,726），永遠大於 4,294,967,295 這個上限。正確寫法是 `hash(id) % 4294967296`。

**2. 以為 `count(*)` 在 `v_catalog.projections` 是投影數。** EON 上 486 列 = 434 個投影（408 + 26×3）。要算「有幾個投影」，用 `count(DISTINCT projection_id)`；要算「資料放在幾個地方」，才用 `count(*)`。

**3. 以為 shard 數 = 節點數。** 本機 3 個節點、4 個 shard（`default_shard_count = 3` 只算 segment）。用「節點數 × 每節點容量」估 depot，再用 shard 數去對，兩個數字對不上就會懷疑人生。

**4. 拿 `v_monitor.storage_containers` 的 projection 數跟 catalog 對數。** 前者是「有檔案的投影」，本次 221 個；後者 434 個。中間的差是還沒產生資料的投影（`DELAYED CREATION`）與 mergeout 中的 container。兩個數字都對，只是問題不同。

**5. 看到 `segment_range` 的 33.3% 就以為邊界在裡面。** EON 的 `segment_range` 只寫節點百分比，沒有 hash 邊界。要邊界一律查 `v_catalog.shards`。

**6. 用 `node_name` 判斷資料在哪。** 分段 projection 的 `node_name` 是空的；未分段的有值但是三個節點都有。判斷落點要看 `v_monitor.storage_containers.shard_name`。

---

## 九、風險與建議

**現在就能做（唯讀、低風險）**

- 把「shard 邊界 + 每段筆數」做成每日巡檢：`v_catalog.shards` 三段 `span` 加總應等於 `4,294,967,296`，`gap_before` 應全為 0。
- 用 `v_monitor.storage_containers` 按 `shard_name` 統計筆數佔比，偏離 33% 超過 2 個百分點就去看 segmentation 的 skew。
- 檢查 `v_catalog.node_subscriptions`：每個 segment 的 subscription 數就是 K-Safe + 1，少於預期就是副本掉了。
- 對慢查詢看 `EXPLAIN` 的 `Execute on:`：寫 `All Nodes` 但你預期只打一個節點，就是 predicate 沒落在 segmentation 欄位上。

**需要變更（要窗口、要核准）**

- 改 shard 數（`ALTER DATABASE ... SHARD COUNT`）會觸發 rebalance，資料會在節點間搬動，不是唯讀操作。
- 提高 K-Safe 會新增 subscription，需要足夠的節點與 depot 空間；本叢集 3 節點、K-Safe=0，改成 1 需要重新規劃。
- 改 projection 的 segmentation 要重建投影，期間資料要重寫。

---

## 十、參考資料

- Vertica 25.4 文件：Shards and subscriptions — https://docs.vertica.com/25.4.x/en/architecture/eon-mode-architecture/shards-and-subscriptions/
- Vertica 25.4 文件：`v_catalog.shards` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-tables/shards/
- Vertica 25.4 文件：`v_catalog.node_subscriptions` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-tables/node-subscriptions/
- Vertica 25.4 文件：Segmentation clause（`CREATE PROJECTION`） — https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-projection/segmentation-clause/
- Vertica 25.4 文件：Projection segmentation — https://docs.vertica.com/25.4.x/en/architecture/projection-design/segmentation/
- Vertica 25.4 文件：`HASH` 函數 — https://docs.vertica.com/25.4.x/en/sql-reference/functions/hash/

> 註：`docs.vertica.com` 目前會 302 導向 `docs.rocketsoftware.com/bundle/vertica_25.4/`，路徑對應相同；本文連結於 2026-11-14 檢查皆回應 200。
