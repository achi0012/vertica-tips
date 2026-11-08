---
layout: post
title: "Vertica 分區表設計與 Partition Pruning 實戰"
date: 2026-11-08 05:00:00 +0800
categories: vertica partition data-management
tags: [vertica, partition, pruning, drop-partition, ahm, purge, ros]
description: "實測 Vertica 分區表的 PARTITION BY / GROUP BY 設計、partition pruning、v_monitor.partitions、DROP PARTITION 與 DELETE 的差異，以及 AHM 與 purge 對資料生命週期的影響。"
image: /assets/images/og/vertica-partition-design-pruning-zh.png
---

<!--
AI Summary: Vertica 25.4.0-0 單節點實測分區表。600 萬列、24 個月分區不增加儲存（91,065,421 vs 91,088,745 bytes）也不增加載入時間；單月查詢 PROFILE 只掃 250,000 列（未分區 6,000,000 列），但 6M 列規模下 wall clock 僅快約 9%；120 個每日分區讓全表聚合慢 49%；CALENDAR_HIERARCHY_DAY 把 120 個 partition key 壓成 4 個 ROS container，代價是單日查詢多讀 31 倍列數；DROP PARTITION 13.1ms 立刻釋放 3,795,575 bytes，DELETE 後則需 mergeout 1,604.8ms。
AI Keywords: Vertica, partition, partition pruning, DROP_PARTITION, CALENDAR_HIERARCHY_DAY, AHM, mergeout, ROS, 25.4
-->
<!--more-->

---

## 一、先講結論

分區（partitioning）是 Vertica 裡少數「設計對了就幾乎不用維護」的功能，但它也是最容易被誤解的：有人以為分區是索引，有人以為 `DELETE` 會釋放空間，也有人每個月手動跑一次 `DELETE` 然後納悶磁碟為什麼沒變小。

我在 Vertica 25.4.0-0 單節點上，用 600 萬列、24 個月的資料建了分區表、未分區表、每日分區表與分區群組表，量到六個結論：

1. **分區幾乎不增加成本**。同樣 600 萬列，24 個月的分區表佔 91,065,421 bytes，未分區表佔 91,088,745 bytes（差 23,324 bytes，−0.03%）；`COPY ... DIRECT` 載入 313 MB CSV 分別是 2,967.2 ms 與 2,974.6 ms。**分區不是多存一份資料。**
2. **Pruning 是真的，而且可以直接量到列數**。查 2026 年 1 月，分區表的 `Scan` 只處理 250,000 列；未分區表處理 6,000,000 列（24 倍）。查詢 initiator 記憶體也從 47,014 KB 降到 10,436 KB。
3. **但實際耗時不會等比例下降**。同一條重聚合查詢，分區表 24.9–37.9 ms、未分區表 28.1–38.5 ms，中位數 25.7 對 28.2 ms，只差約 9%。在 6M 列、全部快取的實驗環境裡，**聚合運算才是瓶頸，不是掃描**。
4. **分區粒度本身有成本**。120 個每日分區的表，全表聚合中位數 42.6 ms；同樣 60 萬列放在單一容器的未分區表是 28.6 ms，**慢了 49%**。
5. **分區群組是拿「讀取的列數」換「ROS 容器數」**。`CALENDAR_HIERARCHY_DAY` 把 120 個 partition key 壓成 4 個 ROS container，但單日查詢從讀 5,000 列變成讀 155,000 列（31 倍）。
6. **`DROP PARTITION` 才是刪歷史資料的正解**。丟掉 2025 年 1 月：`DROP_PARTITION` 花 13.1 ms、立刻釋放 3,795,575 bytes；同樣 25 萬列用 `DELETE` 花 35.0 ms、**當下釋放 0 bytes**，還要等 mergeout（1,604.8 ms）才真的把空間還回來。

這篇寫給需要設計時序表、正在被「資料刪不掉、磁碟不會小」困擾，或想確認 pruning 到底有沒有生效的人。所有數字都來自本機實測，schema `article_lab_part` 在結束時已 `DROP SCHEMA ... CASCADE`。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 節點數 | 1（Community Edition，`v_monitor.system.node_count = 1`） |
| 資料庫 / 使用者 | `testdb` / `dbadmin` |
| 測試 schema | `article_lab_part`（實驗後刪除，已確認 0 個殘留物件） |
| 事實表 | 6,000,000 列 × 5 欄，24 個月 |
| 欄位 | `sale_id`、`event_ts`、`region`、`amount`、`payload` |
| 時間範圍 | 2024-11-01 至 2026-10-30 |
| 載入方式 | `COPY ... DIRECT`，單檔 313 MB |
| 量測日期 | 2026-11-08 |

三個會影響重現的細節：

- **這個連線的 `autocommit` 是 `off`**。`CREATE`、`COPY` 會自行提交，但 `INSERT` 不會。我插入一列 2026-12 的資料後，下一個 session 查 `COUNT(*)` 仍是 5,500,000，少的那一列就是沒 commit。所有測試都在單一 `vsql -f` session 內完成。
- **單節點只有 1 個 segment**，所以看不到節點間的資料傾斜；本文不談跨節點分佈。
- **25.4 的 AHM 不在 `v_monitor.epochs`**。本機 `SELECT * FROM v_monitor.epochs` 直接回 `ERROR 4568: Relation "v_monitor.epochs" does not exist`；`v_catalog.epochs` 只剩 `epoch_number`、`epoch_close_time` 兩欄。AHM 要查 `v_monitor.system`：

```sql
=> SELECT current_epoch, ahm_epoch, last_good_epoch FROM v_monitor.system;
 current_epoch | ahm_epoch | last_good_epoch
---------------+-----------+-----------------
       2329826 |   2329825 |         2329825
```

這個差異會讓照舊文件寫的監控查詢直接失敗，是本篇第一個實務陷阱。

---

## 三、核心原理

### 3.1 分區是「表格屬性」，不是索引

`PARTITION BY` 寫在 `CREATE TABLE`，套用到這張表的所有投影。Vertica 依 partition key 把資料切成不同的 ROS container：一個 partition key 至少一個 container，資料量大時會是多個。

| 你寫的 | Vertica 實際做的 | 對查詢的影響 |
|---|---|---|
| `PARTITION BY (year*100+month)` | 每個月一個 partition key，各自成 ROS container | 查單月時只掃該 container |
| `PARTITION BY event_ts::DATE` | 每天一個 key | 查單日很快，但 container 數量暴增 |
| `GROUP BY CALENDAR_HIERARCHY_DAY(...)` | 多個 key 合併成一個 partition group、共用 container | container 變少，但單日查詢要讀整個群組 |

重點：**分區不改變資料的儲存格式，也不建立額外索引**。它改變的是「資料被切成幾塊、每塊的邊界在哪」。所以儲存成本幾乎不變（實測 −0.03%），改變的是掃描時能跳過多少塊。

### 3.2 Pruning 的條件

優化器要知道「這個 predicate 對應哪些 partition key」。實測結果比文件寫的寬鬆：

| 查詢條件 | 實際掃描列數 | 是否 pruning |
|---|---|---|
| `event_ts >= '2026-01-01' AND event_ts < '2026-02-01'` | 250,000 | 是 |
| `EXTRACT(YEAR FROM event_ts)*100 + EXTRACT(MONTH FROM event_ts) = 202601` | 250,000 | 是 |
| `EXTRACT(MONTH FROM event_ts) = 1` | 500,000 | 是（只掃 2025-01、2026-01 兩個分區） |
| `TO_CHAR(event_ts,'YYYYMM') = '202601'` | 5,500,000 | 否（全表） |
| `(event_ts 在 1 月) OR region = 'ZZ'` | 250,000 | 是 |

也就是說：**只要 predicate 能推導出 partition key 的範圍就會 pruning，不一定要寫出跟 `PARTITION BY` 一模一樣的運算式**。但把欄位包進 `TO_CHAR` 這種「轉成字串再比較」的寫法，優化器推不出範圍，直接退化為全表掃描。

（表中 `TO_CHAR` 與 `OR` 兩列是後續刪除測試之後才量的，當時表上剩 5,500,000 列；其餘四列在 6,000,000 列時量測。）

### 3.3 DELETE 之後發生什麼事

這是分區主題最常被誤解的一段。`DELETE` 不會立刻刪掉資料：

1. `DELETE` 在 ROS container 上寫入 **delete vector**，標記哪些列已刪除。
2. `SELECT` 會過濾掉它們（結果正確），但**磁碟用量不變**。
3. 要等 tuple mover 的 mergeout 合併 container，而且 **AHM 必須超過 delete 當時的 epoch**，過期的列才會被真正清掉。
4. `DROP PARTITION` 則是直接把整個 container 從目錄移除，**不需要 mergeout、不受 AHM 限制**。

這個差異在下面的實測裡有完整數字。

---

## 四、Step-by-step 實測

### 4.1 建立分區表並載入

```sql
=> CREATE TABLE article_lab_part.sales_p (
     sale_id  INT NOT NULL,
     event_ts TIMESTAMP NOT NULL,
     region   VARCHAR(8),
     amount   NUMERIC(12,2),
     payload  VARCHAR(64)
   )
   PARTITION BY (EXTRACT(YEAR FROM event_ts)::INT * 100 + EXTRACT(MONTH FROM event_ts)::INT);
```

如果不轉型，寫成 `PARTITION BY (EXTRACT(YEAR FROM event_ts) * 100 + EXTRACT(MONTH FROM event_ts))`，會多一個警告：

```
WARNING 6100:  Using PARTITION expression that returns a Numeric value
HINT:  This PARTITION expression may cause too many data partitions.
       Use of an expression that returns a more accurate value,
       such as a regular VARCHAR or INT, is encouraged
```

把整個運算式 `::INT` 轉型後，6100 就消失了（實測對照：未轉型會出現、轉型後不出現）。上面這個建表語句仍然會留一個 `WARNING 9249`，提醒分區鍵可能有 NULL：

```
WARNING 9249:  Using PARTITION expression that may result in NULL values
HINT:  DML statements which produce NULL values for partition expressions will roll back
```

所以 `event_ts` 宣告 `NOT NULL` 是必要的。載入後 partition key 是 `202411`、`202412`…`202610`，共 24 個，符合預期。

載入 600 萬列（313 MB）：

```
=> COPY article_lab_part.sales_p FROM '/tmp/opencode/sales_part.csv' DELIMITER '|' DIRECT;
 Rows Loaded
-------------
     6000000
Time: First fetch (1 row): 2967.224 ms.
```

同樣資料載入未分區表 `sales_flat` 是 2,974.6 ms；載入「月份打散」的 `sales_flat_shuf` 是 3,879.3 ms（+30.3%）。**分區本身不增加載入成本，資料有沒有照時間排序才會影響載入**（排序好的資料壓縮與寫入都比較有效率）。

儲存結果：

```sql
=> SELECT projection_name, SUM(used_bytes) AS used_bytes, SUM(total_row_count) AS rows,
          COUNT(*) AS ros
   FROM v_monitor.storage_containers
   WHERE schema_name='article_lab_part' GROUP BY 1 ORDER BY 1;
    projection_name    | used_bytes |  rows   | ros
-----------------------+------------+---------+-----
 sales_flat_shuf_super |   91088745 | 6000000 |   1
 sales_flat_super      |   91088745 | 6000000 |   1
 sales_p_super         |   91065421 | 6000000 |  24
```

24 個分區 = 24 個 ROS container，總 bytes 反而少 23,324。

### 4.2 Pruning 到底省了什麼：用 PROFILE 量列數

`EXPLAIN` 在 25.4 的輸出不會標「Partition pruning」，只會看到同樣的 `Filter:`。要確認 pruning 有沒有發生，最直接的是 `PROFILE`（這裡是自建實驗表，不是生產表）：

```sql
=> PROFILE SELECT COUNT(*), SUM(amount) FROM article_lab_part.sales_p
   WHERE event_ts >= '2026-01-01' AND event_ts < '2026-02-01';
```

從 `v_monitor.execution_engine_profiles` 讀 Scan 的 `rows processed`：

| 查詢 | Scan 列數 | initiator memory |
|---|---|---|
| 分區表，單月 | 250,000 | 10,436 KB |
| 未分區表，單月 | 6,000,000（6 個 Scan 加總） | 47,014 KB |
| 分區表，`TO_CHAR` 包住欄位 | 5,500,000 | 47,020 KB |
| 分區表，`(單月) OR region='ZZ'` | 250,000 | 10,468 KB |

250,000 正好是一個月（6,000,000 / 24），代表**只掃了一個 container**。未分區表雖然資料是按月順序載入的，但單一 `COPY` 只產生 1 個 container，container 層級的 min/max 修剪完全幫不上忙，只能掃 6,000,000 列。

順帶一個反直覺的結果：`OR` 沒有讓 pruning 失效。文件與社群常說「OR 會關閉 pruning」，但在這個形狀下優化器仍把分區條件推下去了（250,000 列）。**結論是不要憑通則假設，要量。**

### 4.3 為什麼時間只快 9%：把成本拆開看

同樣的單月重聚合查詢（`GROUP BY region` + `SUM(LENGTH(payload))`），各跑三次：

```
分區表：   37.890 ms / 25.658 ms / 24.882 ms   （中位數 25.7）
未分區表： 38.518 ms / 28.209 ms / 28.111 ms   （中位數 28.2）
```

掃描列數差 24 倍，時間只差 9%。原因很單純：這個環境的 91 MB 資料全部在記憶體/頁快取裡，掃描本身只花 0.9 ms（PROFILE 的 `Scan` execution time 885 µs），其餘時間是 `SUM(NUMERIC)` 與 `GROUP BY` 的 CPU 成本。**pruning 省的是 I/O 與掃描列數，不會讓聚合變快。**

在生產環境這個比例會完全不同：掃描的是磁碟、欄位更多、每個月資料量更大，掃描成本占比會拉高。但我不想用實驗室的 9% 去推算 production 的加速倍數，那會是編造。可以確定的是：**pruning 的上限就是「被跳過的那部分掃描成本」，不是整條查詢的加速倍數。**

### 4.4 分區粒度：120 個每日分區讓全表查詢慢 49%

建立每日分區表（120 天、每天 5,000 列、共 60 萬列）：

```sql
=> CREATE TABLE article_lab_part.sales_day (...) PARTITION BY (event_ts::DATE);
=> SELECT COUNT(DISTINCT partition_key) AS keys, COUNT(DISTINCT ros_id) AS ros
   FROM v_monitor.partitions p
   JOIN v_catalog.projections pr ON pr.projection_id = p.projection_id
   WHERE pr.anchor_table_name='sales_day';
 keys | ros
------+-----
  120 | 120
```

全表聚合（沒有 pruning 條件）：

```
120 個分區：41.762 / 42.552 / 44.354 ms（中位數 42.6）
單一容器：  38.605 / 26.202 / 28.598 ms（中位數 28.6）
```

**慢了 49%**。每個 container 都有固定的開啟、讀取與初始化成本，120 個就是 120 份；這與文件提醒「不要無限制地細分」的方向一致。

相反的，有 pruning 條件時細分是好事：查 `2025-03-15` 這一天，每日分區表只掃 5,000 列。

### 4.5 分區群組：用 31 倍的讀取量換 30 倍的容器數

資料累積幾年後，每天一個分區很快就會有上千個 container。Vertica 的解法是 partition grouping：

```sql
=> CREATE TABLE article_lab_part.sales_hier (...)
   PARTITION BY event_ts::DATE
   GROUP BY CALENDAR_HIERARCHY_DAY(event_ts::DATE, 2, 2);
```

載入同樣 120 天、60 萬列後：

```sql
=> SELECT COUNT(DISTINCT p.partition_key) AS partition_keys, COUNT(DISTINCT p.ros_id) AS ros
   FROM v_monitor.partitions p JOIN v_catalog.projections pr ON pr.projection_id=p.projection_id
   WHERE pr.anchor_table_name='sales_hier';
 partition_keys | ros
----------------+-----
            120 |   4
```

**120 個 partition key 只用了 4 個 ROS container**（每組涵蓋一個月）。代價在查詢：

| 查 2025-03-15 一天 | Scan 列數 |
|---|---|
| 每日分區（未分組） | 5,000 |
| 分區群組（月群組） | 155,000 |

同一條 SQL、同樣的 `WHERE event_ts >= '2025-03-15' AND event_ts < '2025-03-16'`，群組表要多讀 **31 倍**的列，因為最小可修剪單位是「群組」不是「一天」。

分組還改變了 `DROP` 的粒度。想丟掉群組內的某一天，會直接失敗：

```
=> SELECT DROP_PARTITIONS('article_lab_part.sales_hier', '2025-03-15', '2025-03-15');
ERROR 8430:  Some storage containers must be split before the specified partition range
             can be dropped
```

丟整個月群組則成功：

```
=> SELECT DROP_PARTITIONS('article_lab_part.sales_hier', '2025-03-01', '2025-03-31');
 Partition dropped
```

**分組把「可以丟得多細」跟「container 有多少」綁在一起**，設計時要一起決定。

### 4.6 DROP PARTITION vs DELETE vs mergeout

這是最有價值的一組數字。先 `DELETE` 2025 年 1 月的 25 萬列：

```
=> DELETE FROM article_lab_part.sales_p
   WHERE event_ts >= '2025-01-01' AND event_ts < '2025-02-01';
 OUTPUT
--------
 250000
Time: 35.033 ms
```

刪完馬上查 storage：

```sql
=> SELECT projection_name, SUM(used_bytes) AS used_bytes, SUM(total_row_count) AS rows,
          SUM(delete_vector_count) AS dvs, SUM(deleted_row_count) AS deleted
   FROM v_monitor.storage_containers
   WHERE schema_name='article_lab_part' AND projection_name='sales_p_super' GROUP BY 1;
 projection_name | used_bytes |  rows   | dvs | deleted
-----------------+------------+---------+-----+---------
 sales_p_super   |   91065421 | 6000000 |   1 |  250000
```

**bytes 完全沒變（91,065,421），還多了 1 個 delete vector。** `SELECT COUNT(*)` 已經是 5,750,000，查詢結果正確，但磁碟上那 3.8 MB 還在。

換成 `DROP_PARTITION`：

```
=> SELECT DROP_PARTITION('article_lab_part.sales_p', 202501);
WARNING 8461:  drop_partition has been deprecated and will be removed in a future version
HINT:  Use drop_partitions and a single value for both the min and max range value
 Partition dropped
Time: 13.128 ms

=> -- used_bytes: 91,065,421 → 87,269,846，差 3,795,575 bytes
```

**13.1 ms，空間立刻回來 3,795,575 bytes**，而且 delete vector 一併消失（`dvs = 0`、`deleted = 0`）。這個數字剛好等於 `v_monitor.partitions` 裡 202501 的 `ros_size_bytes`。

同樣 25 萬列在未分區表上 `DELETE` 是 35.5 ms（跟分區表的 35.0 ms 幾乎一樣），但空間一樣不會釋放。要真正拿回來只能靠 mergeout：

```
=> SELECT DO_TM_TASK('mergeout', 'article_lab_part.sales_flat');
Time: 1604.794 ms
=> -- used_bytes: 91,088,745 → 87,167,347，釋放 3,921,398 bytes，delete vector 歸零
```

整理成一張表：

| 操作 | 耗時 | 空間何時釋放 | 釋放量 |
|---|---|---|---|
| `DELETE` 25 萬列（分區表） | 35.0 ms | 不會，等 mergeout | 0 bytes |
| `DELETE` 25 萬列（未分區） | 35.5 ms | 不會，等 mergeout | 0 bytes |
| `DROP_PARTITION` 202501 | **13.1 ms** | **立即** | **3,795,575 bytes** |
| `DO_TM_TASK('mergeout')` 未分區表 | 1,604.8 ms | 完成後 | 3,921,398 bytes |

`DROP_PARTITION` 比 mergeout 快 **122 倍**，而且不用等 AHM。這就是為什麼「時序表按時間分區」幾乎是強制要求：**它不是為了查詢變快，是為了讓資料可以被便宜地丟掉。**

還有一個容易忽略的成本：`DELETE` 如果沒帶分區條件，一樣是全表掃描。我跑了一次 `DELETE ... WHERE region = 'ZZ'`（比對不到任何列），PROFILE 顯示 initiator memory **4,212,454 KB**；帶分區條件的 `DELETE` 只有 **10,436 KB**，相差 400 倍。刪除作業的記憶體用量是會被 resource pool 限制的，這種寫法在生產環境很可能直接撞到 pool 上限而失敗。

### 4.7 監控分區：三個實際踩到的坑

**(1) `v_monitor.partitions` 沒有 `table_name`。** 它只有 `table_schema`、`projection_name`、`partition_key`、`ros_id`、`ros_row_count` 等欄位。要按表名查詢必須 join：

```sql
=> SELECT pr.anchor_table_name, COUNT(DISTINCT p.partition_key) AS keys,
          COUNT(DISTINCT p.ros_id) AS ros
   FROM v_monitor.partitions p
   JOIN v_catalog.projections pr ON pr.projection_id = p.projection_id
   WHERE p.table_schema = 'article_lab_part'
   GROUP BY 1 ORDER BY 1;
```

**(2) `SUM(ros_row_count)` 在分區群組下會放大。** `v_monitor.partitions` 是「每個 partition key 一列」，但 `ros_row_count` 填的是**整個 container 的列數**。群組表的一個 container 裝了 30 個 key：

```sql
=> SELECT ros_id, COUNT(*) AS keys, SUM(ros_row_count) AS summed
   FROM v_monitor.partitions p JOIN v_catalog.projections pr ON pr.projection_id=p.projection_id
   WHERE pr.anchor_table_name='sales_hier' GROUP BY 1;
      ros_id       | keys |  summed
-------------------+------+----------
 45035996322353409 |   30 |  4500000

=> SELECT COUNT(*) FROM article_lab_part.sales_hier WHERE event_ts < '2025-02-01';
 155000
```

加總 4,500,000，實際只有 155,000，**放大 29 倍**。要算列數請用 `v_monitor.storage_containers`（一個 container 一列，`total_row_count` 可以直接加總）；要數 container 數量則用 `COUNT(DISTINCT ros_id)`。

**(3) 新分區要等資料進 ROS 才看得到。** 我對 2026-12 載入 2 列：

```sql
=> COPY article_lab_part.sales_p FROM '/tmp/opencode/newpart.csv' DELIMITER '|' DIRECT;
 Rows Loaded: 2
=> -- partition key 從 22 個變成 23 個，202612 立刻出現，ros_row_count = 2
```

`COPY ... DIRECT` 直接寫 ROS，所以馬上看到。但如果是 `INSERT`（走 WOS），要等 moveout 之後 `v_monitor.partitions` 才會有那個 key；我用 `INSERT` 插的那一列不但沒進 partitions，連 `COUNT(*)` 都查不到——因為 `autocommit` 是 `off`，沒有 `COMMIT`。

另外，分區層級的統計資訊在 `v_monitor.partition_columns`。對分區表跑 `ANALYZE_STATISTICS` 會回傳 0，但每個分區的 `statistics_type` 會變成 `FULL`：

```sql
=> SELECT ANALYZE_STATISTICS('article_lab_part.sales_p');
 ANALYZE_STATISTICS
--------------------
                  0
=> SELECT p.partition_key, p.column_name, p.statistics_type
   FROM v_monitor.partition_columns p
   JOIN v_catalog.projections pr ON pr.projection_id=p.projection_id
   WHERE pr.anchor_table_name='sales_p' AND p.column_name='amount'
   ORDER BY p.partition_key LIMIT 3;
 partition_key | column_name | statistics_type
---------------+-------------+-----------------
 202411        | amount      | FULL
 202412        | amount      | FULL
 202503        | amount      | FULL
```

---

## 五、效能與成本分析

把這次量到的成本整理成決策表：

| 設計選擇 | 好處（實測） | 成本（實測） |
|---|---|---|
| 按月分區 | 單月查詢掃 250,000 列（未分區 6,000,000） | 儲存 +0%（−0.03%）、載入 +0% |
| 按日分區（120 天） | 單日查詢掃 5,000 列 | 全表查詢慢 49%（42.6 vs 28.6 ms）、120 個 container |
| 分區群組（月群組） | 120 key → 4 container | 單日查詢多讀 31 倍（155,000 vs 5,000）、無法只丟一天（ERROR 8430） |
| `DROP PARTITION` 丟一個月 | 13.1 ms、立即釋放 3.8 MB | 只能整塊丟；要確認沒有跨分區的外鍵/查詢依賴 |
| `DELETE` + mergeout | 可以只刪部分列 | 35.0 ms 刪除 + 1,604.8 ms mergeout，期間空間不釋放 |

判斷原則：

- **資料有明確時間軸、且會整批過期** → 一定要分區，粒度選「業務上最小可接受的丟棄單位」（通常是月）。
- **查詢一定要帶時間範圍** → 分區才有意義；如果大多是全表聚合，分區只會讓你多付 container 的固定成本（+49%）。
- **資料超過一年、每天一區** → 用 `CALENDAR_HIERARCHY_DAY`，但先確認「單日查詢」是否為主要負載；如果是，群組會讓它變慢 31 倍。
- **要省空間** → 不要期待 `DELETE`，用 `DROP PARTITION`。

---

## 六、常見錯誤與反模式

### 1. 以為 `ALTER TABLE ... DROP PARTITION` 可以用

```
=> ALTER TABLE article_lab_part.sales_p DROP PARTITION 202503;
ERROR 4856:  Syntax error at or near "202503" at character 53
```

Vertica 沒有這個語法，只能用 meta-function `DROP_PARTITION` / `DROP_PARTITIONS`。而且 25.4 會提示單數版已棄用（WARNING 8461），新程式請直接用 `DROP_PARTITIONS(table, min, max)`。

### 2. 對未分區表呼叫 `DROP_PARTITION`

```
=> SELECT DROP_PARTITION('article_lab_part.sales_flat', 0);
ERROR 4881:  Table "sales_flat" is not partitioned
```

不痛不癢，但如果你在自動化腳本裡「先建表、再丟分區」，欄位或分區子句漏了就會在這裡才爆。

### 3. 分區鍵出現 NULL

```sql
=> CREATE TABLE t_null (d TIMESTAMP, v INT)
   PARTITION BY (EXTRACT(YEAR FROM d)::INT * 100 + EXTRACT(MONTH FROM d)::INT);
WARNING 9249:  Using PARTITION expression that may result in NULL values
HINT:  DML statements which produce NULL values for partition expressions will roll back

=> INSERT INTO t_null VALUES (NULL, 1);
ERROR 4195:  NULL value detected in data partitioning expression
```

**整筆 DML 會 rollback**，不是只跳過那一列。分區鍵欄位請設 `NOT NULL`；若必須容許 NULL，可以用 `ZEROIFNULL()` 包住，實測 `PARTITION BY (ZEROIFNULL(a))` 可接受 NULL 並歸到 key 0。

### 4. 用函數把分區欄位包起來

```sql
=> SELECT COUNT(*), SUM(amount) FROM article_lab_part.sales_p
   WHERE TO_CHAR(event_ts, 'YYYYMM') = '202601';
-- Scan rows processed: 5,500,000（全表），memory 47,020 KB
```

同樣的語意寫成 `event_ts >= '2026-01-01' AND event_ts < '2026-02-01'` 只掃 250,000 列。`TO_CHAR`、`CAST` 成不同型別、字串串接都會讓優化器失去範圍資訊。

### 5. 分區鍵用非決定性函數

```
=> CREATE TABLE t_e (d DATE, v INT) PARTITION BY RANDOM();
ERROR 2552:  Cannot use meta function or non-deterministic function in PARTITION BY expression
```

`RANDOM()`、`NOW()` 這類函數不能用在分區表達式。`CALENDAR_HIERARCHY_DAY` 內部會參考 `NOW()`，因此文件的用法一律放在 `GROUP BY`；本次實測也只在 `GROUP BY` 驗證過，沒有測試它出現在 `PARTITION BY` 的行為。

### 6. 自訂 `GROUP BY` 表達式寫錯

```
=> ... PARTITION BY (year*100+month) GROUP BY (year)
ERROR 8363:  Partition GROUP BY expression can only reference columns by way of
             inclusion of the PARTITION BY expression
```

`GROUP BY` 必須從**分區表達式**推導，不能直接引用原始欄位。想自訂階層只能用 `CASE` 包住同一個分區表達式（官方文件有範例），或直接用 `CALENDAR_HIERARCHY_DAY`。

### 7. 以為 `DELETE` 之後磁碟會變小

實測 `DELETE` 25 萬列後 `used_bytes` 一個 byte 都沒少。如果監控只看 `df`，你會以為資料沒刪掉；如果只看 `SELECT COUNT(*)`，你會以為空間已經還了。**兩個都要看，並且知道 mergeout 與 AHM 的角色。**

### 8. 每日分區沒有上限

120 天就 120 個 container。這次只測到 120；實務上幾年下來就是數千個，全表查詢的固定成本會線性堆上去（本次 120 個 container 已經讓全表聚合慢 49%）。**分區數量要當成容量參數來管理，不是「越細越好」。**

---

## 七、風險與建議

### 現在就能做（唯讀／低風險）

1. **盤點分區現況**：

```sql
=> SELECT pr.anchor_table_name, COUNT(DISTINCT p.partition_key) AS keys,
          COUNT(DISTINCT p.ros_id) AS ros
   FROM v_monitor.partitions p
   JOIN v_catalog.projections pr ON pr.projection_id = p.projection_id
   GROUP BY 1 ORDER BY ros DESC LIMIT 20;
```

2. **找出「有 delete vector 但沒 purge」的表**，這是磁碟沒有回收的直接證據：

```sql
=> SELECT projection_name, SUM(used_bytes) AS bytes,
          SUM(delete_vector_count) AS dvs, SUM(deleted_row_count) AS deleted
   FROM v_monitor.storage_containers
   WHERE delete_vector_count > 0
   GROUP BY 1 ORDER BY deleted DESC LIMIT 20;
```

3. **確認 AHM 有沒有落後**（25.4 請查 `v_monitor.system`，不是 `v_monitor.epochs`）：

```sql
=> SELECT current_epoch, ahm_epoch, current_epoch - ahm_epoch AS lag FROM v_monitor.system;
```

4. **用 `EXPLAIN` 檢查高頻查詢有沒有帶到分區條件**；必要時用 `PROFILE` 在測試環境確認 Scan 的 `rows processed`。

### 需要變更（要窗口／要核准）

1. **為時序表加上分區**：`ALTER TABLE ... PARTITION BY ... REORGANIZE` 會重寫整張表的資料（本次沒有在實驗表上執行 `REORGANIZE`，這是依文件與操作語意標示的風險），時間與空間成本約等於一次全量重載。要先確認磁碟餘裕、離峰窗口與回滾方式（最壞情況是重建表並重新載入）。
2. **把每日分區改成月分區或加上 `CALENDAR_HIERARCHY_DAY`**：同樣是 `REORGANIZE`，而且會改變 `DROP` 的粒度；如果有下游腳本按日 `DROP_PARTITIONS`，會開始出現 ERROR 8430。
3. **用 `DROP_PARTITION` 取代 `DELETE` 的清理排程**：先確認沒有跨分區的查詢依賴、沒有 view 或 external 流程假設資料會被逐列刪除，並且先在非生產環境演練一次。
4. **調整 `ActivePartitionCount` 或淘汰策略**：屬於表格/叢集層級設定，建議一次只改一張表，觀察一週的 ROS 數量與 mergeout 行為再擴大。

---

## 八、參考資料

- Vertica 文件：Partition clause（`PARTITION BY` / `GROUP BY` 語法與限制）— <https://docs.vertica.com/26.1.x/en/sql-reference/statements/create-statements/create-table/partition-clause/>
- Vertica 文件：Partition grouping（群組鍵與 `CALENDAR_HIERARCHY_DAY`）— <https://docs.rocketsoftware.com/bundle/vertica_24.2/page/admin/partitioning-tables/defining-partitions/partition-grouping.html>
- Vertica 文件：Hierarchical partitioning（動態群組與 tuple mover 行為）— <https://docs.rocketsoftware.com/bundle/vertica_11.1/page/admin/partitioning-tables/hierarchical-partitioning.html>
- Vertica 文件：`DROP_PARTITIONS` — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/drop_partitions/>
- Vertica Forum：Partitioning strategy（群組分區的 `DROP_PARTITIONS` 與 forceSplit 實例）— <https://forum.vertica.com/discussion/243329/partitioning-strategey>
