---
layout: post
title: "十億筆資料在 EON 上的查詢解剖：shard pruning 與 depot 命中如何決定 EXPLAIN 與實際時間"
date: 2026-11-18 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, query-plan, explain, shard-pruning, depot, performance, profile]
description: "在本機 3 節點 eondb（v25.4.0-0、K-Safe=0、4 shard）對 10 億筆的 mtk.partition_columns 做唯讀解剖：count(*) 0.42 秒、加一個聚合欄位變 4.76 秒、兩個欄位 12.05 秒；同一個 filter 打在 sort key 前綴反而比非前綴慢；10 億 × 293 萬的 hash join 花 90.9 秒。EXPLAIN LOCAL 顯示三個節點都有 ScanStep——EON 沒有計畫層級的 shard pruning，只有「掃不到資料」的節點。"
image: /assets/images/og/vertica-eon-billion-row-query-anatomy-zh.png
faq:
  - q: "十億筆的 count(*) 只要 0.42 秒，真的掃過 10 億列嗎？"
    a: "不是。本機 3 個 container 各 3.36 億列，count(*) 實測 420 ms（第二次 437 ms），等於每秒 24 億列，遠高於任何欄位掃描速度；同一張表改成 sum(row_count) 就變 4,756 ms，慢 11 倍。差別在於 count(*) 只需要 container 的資料列數，不必把欄位值讀出來。"
  - q: "為什麼 EXPLAIN 的 Participating subscriptions 三個節點都列出來？"
    a: "因為那段是叢集當下的訂閱地圖：replica 在三個節點各一份，segment0001~0003 分別在 node0001~0003。它不是「這個查詢會用到的 shard」。要驗證這點用 EXPLAIN LOCAL：連只有一個 container 的小表 public.ujtest（3,826 筆，全部在 segment0003）都還是三個節點各有一份 ScanStep。"
  - q: "加了 sum() 之後同一張表從 0.42 秒變 4.76 秒，為什麼差這麼多？"
    a: "因為 count(*) 可以只讀 container 的資料列數，sum(row_count) 必須真的把 10 億個 int 值讀出來。實測 sum(row_count) 4,756 ms、count(*)+sum(row_count) 7,844 ms、count+sum+min/max 12,047 ms——每多一個要 materialize 的欄位大約多 5 秒，掃描成本由欄位數決定，不是由資料列數決定。"
  - q: "本機量得到 depot miss 或 communal 延遲嗎？"
    a: "量不到。每個節點的 depot 上限是 566,381,859,840 bytes，這張表每節點只佔 2.95 GB（0.52%），資料全部在 depot 裡；實測前後 v_monitor.depot_fetches 都是 30 筆陳年紀錄、fetch_time_ms 最大 3 ms，完全沒有新增。同一條查詢重跑的 4,162 ms → 1,437 ms 差異來自 page cache，不是 depot。"
  - q: "10 億筆和 293 萬筆的 hash join 為什麼要 90 秒？"
    a: "EXPLAIN 顯示 JOIN HASH Inner (BROADCAST)，把 293 萬筆的 partitions_super 廣播到三個節點，再接 GROUPBY HASH (GLOBAL RESEGMENT GROUPS) 把 841,034,568,612 個配對列重新分段。實測 90,875 ms，其中大部分是廣播與 resegment 的網路與排序成本，不是讀取。"
---
<!--
AI Summary: 在 3 節點 eondb（v25.4.0-0、K-Safe=0、4 shard）對 1,007,772,484 筆的 mtk.partition_columns 做唯讀查詢解剖：count(*) 420 ms、sum(row_count) 4,756 ms、count+sum+min/max 12,047 ms、filter column_name='epoch'（2,725,727 筆）5,809 ms、filter partition_key='-1'（4,421,986 筆）4,162 ms、LIMIT 5 19 ms、1B×293M hash join 90,875 ms。EXPLAIN LOCAL 證明三個節點都有 ScanStep，EON 沒有計畫層級 shard pruning；depot 每節點上限 566 GB、資料 2.95 GB（0.52%），depot_fetches 30 筆未增加，量不到 communal fetch。
AI Keywords: Vertica, EON, EXPLAIN LOCAL, shard, depot, storage_containers, hash join, broadcast, resegment, 25.4
-->
<!--more-->

---

## 一、先講結論

「10 億筆的查詢慢，是因為 EON 要去 communal storage 搬資料」——這句話在本機的 3 節點 eondb 上是錯的。這篇把一張 10 億筆的表攤開，用唯讀查詢逐段量測，結論如下（全部是 2026-11-18 這次執行的數字）：

1. **`count(*)` 0.42 秒，不是掃描。** 1,007,772,484 筆的 `count(*)` 實測 420 ms／437 ms，換算每秒 24 億列。改成 `sum(row_count)` 立刻變 4,756 ms，慢 11 倍。
2. **成本由「讀幾個欄位」決定，不是由資料列數決定。** `sum(row_count)` 4,756 ms、`count(*)+sum(row_count)` 7,844 ms、`count+sum+min/max(disk_space_bytes)` 12,047 ms。每多一個要 materialize 的欄位，大約多 5 秒。
3. **EXPLAIN 沒有計畫層級的 shard pruning。** `EXPLAIN LOCAL` 顯示 node0001／0002／0003 三個節點各有一份 `ScanStep`。連只有一個 container、3,826 筆、全部落在 segment0003 的 `public.ujtest`，三個節點的 local plan 也都還有 `ScanStep`。
4. **資料位置要查 `v_monitor.storage_containers`，不是查 EXPLAIN。** 這張表 3 個 container、每個 segment shard 一個，各 336,125,677／335,881,586／335,765,221 筆，磁碟 8.86 GB、payload 92.58 GB（10.45 倍壓縮），每列 8.79 bytes。
5. **filter 打在 sort key 前綴沒有比較快。** `column_name='epoch'`（命中 2,725,727 筆）5,809 ms／5,146 ms；打在非前綴的 `partition_key='-1'`（命中 4,421,986 筆）4,162 ms／1,437 ms。每個 segment 只有一個 container，容器層級沒有可跳過的區間。
6. **Cost 不能用來預估時間。** 722K 的計畫花 12.0 秒，2M 的計畫花 5.8 秒，1B 的計畫花 4.2 秒。
7. **depot 全命中，量不到 communal fetch。** 每個節點 depot 上限 566,381,859,840 bytes，這張表每節點只佔 2.95 GB（0.52%）；`v_monitor.depot_fetches` 在整段實測前後都是 30 筆、最長 3 ms。
8. **join 才是真正的成本。** 10 億 × 293 萬的 hash join，EXPLAIN 是 `Inner (BROADCAST)` 加 `GLOBAL RESEGMENT GROUPS`，實測 90,875 ms。

這篇寫給兩三種人：看 EXPLAIN 只看得懂 Cost 的人；以為 EON 查詢慢一定是 communal I/O 的人；以及要對老闆解釋「為什麼 10 億筆的 count 比 90 萬筆的表還快」的人。**全程唯讀：只跑 SELECT 與 EXPLAIN，沒有 CREATE、沒有 INSERT、沒有改任何參數。**

![同一份 EXPLAIN 送到三個節點，各自的 ScanStep 只讀自己 shard 上的 container；資料在哪一台由 storage_containers 決定]({{ '/assets/images/eon-query-anatomy-layers.svg' | relative_url }}){: loading="lazy" }

---

## 二、實測環境

| 項目 | 值（本次實查） |
|------|----------------|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 資料庫 | `eondb`（EON 模式） |
| 節點 | 3（`v_eondb_node0001`～`0003`，全部 `PERMANENT`／`is_primary = true`） |
| Shard | 4（`replica` + `segment0001`～`segment0003`），K-Safe = 0 |
| Hash 邊界 | `0–1,431,655,738`／`1,431,655,739–2,863,311,477`／`2,863,311,478–4,294,967,295` |
| 受測表 | `mtk.partition_columns`（1,007,772,484 筆） |
| 投影 | `mtk.partition_columns_super`（segmented，8 欄排序鍵） |
| Container | 3 個（每個 segment shard 一個） |
| Depot 上限 | 每節點 566,381,859,840 bytes（`v_catalog.storage_locations.max_size`） |
| 連線 | `connect_eondb.py`（`vertica_python`），入口 1533／2533／3533 |
| 實測時間 | 2026-11-18 05:05–06:05（Asia/Taipei） |

`mtk` 這個 schema 裡放的是另一套系統的 partition metadata：每一列描述「某個投影的某個 partition 在某個節點上的 ROS」。`partition_columns` 有 13 個欄位，`partition_columns_super` 用 `hash(column_id, projection_id, row_count, deleted_row_count, disk_space_bytes, statistics_updated_timestamp, column_name, table_name)` 分段，排序鍵是 `column_name, column_id, table_name, projection_name, projection_id, node_name, partition_key, grouped_partition_key`——記住這個排序鍵，第四節會用到。

三個 container 的實查結果：

```sql
=> SELECT node_name, shard_name, total_row_count, used_bytes, payload_size,
          segment_lower_bound, segment_upper_bound
   FROM v_monitor.storage_containers
   WHERE projection_name = 'partition_columns_super' ORDER BY shard_name;

node_name        | shard_name  | total_row_count | used_bytes | payload_size | lower      | upper
-----------------+-------------+-----------------+------------+--------------+------------+------------
v_eondb_node0001 | segment0001 |     336,125,677 | 2954595943 |  30879805419 |          0 | 1431655738
v_eondb_node0002 | segment0002 |     335,881,586 | 2952943511 |  30857616937 | 1431655739 | 2863311477
v_eondb_node0003 | segment0003 |     335,765,221 | 2952191804 |  30846258727 | 2863311478 | 4294967295
```

三列加起來 1,007,772,484 筆、磁碟 8,859,730,258 bytes（8.86 GB）、payload 92,583,681,083 bytes（92.58 GB），壓縮 10.45 倍，每列 8.79 bytes。**這是後面所有時間數字的物理基礎：任何查詢最多只能平行到 3 台，每台讀 2.95 GB。**

---

## 三、核心原理：一份邏輯計畫、三份本地計畫

### 3.1 平常看的 EXPLAIN 只有邏輯計畫

```sql
=> EXPLAIN SELECT count(*) AS rows_, sum(row_count) AS ros_rows,
            min(disk_space_bytes) AS min_b, max(disk_space_bytes) AS max_b
   FROM mtk.partition_columns;

+-GROUPBY NOTHING [Cost: 722K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*), sum(partition_columns.row_count),
|              min(partition_columns.disk_space_bytes), max(partition_columns.disk_space_bytes)
|  Execute on: All Nodes
| +---> STORAGE ACCESS for partition_columns [Cost: 591K, Rows: 1B (ROW COUNT ONLY)] (PATH ID: 2)
| |      Projection: default_namespace.mtk.partition_columns_super
| |      Materialize: partition_columns.row_count, partition_columns.disk_space_bytes
| |      Execute on: All Nodes
```

這段沒有告訴你任何「哪台做多少」的資訊——`Execute on: All Nodes` 就是字面意思。真正有用的兩個地方在 GraphViz 版本的標頭：

```
All Nodes Vector:
  node[0]=v_eondb_node0001 (initiator) Up
  node[1]=v_eondb_node0002 (executor) Up
  node[2]=v_eondb_node0003 (executor) Up

Participating subscriptions:
  replica: [<0, 1, 2>]
  segment0001: [<0>]
  segment0002: [<1>]
  segment0003: [<2>]
```

### 3.2 「Participating subscriptions」不是這個查詢的 shard 清單

很多人（包括以前的筆者）把這段讀成「這個查詢會用到哪些 shard」。它不是——它是**叢集當下的訂閱地圖**：replica shard 在三個節點各有一份，三個 segment shard 各在一台。任何查詢印出來都長這樣，跟你查的是 10 億筆的大表還是 3,826 筆的小表無關。

要證明這件事，找一張只有一個 container 的表：

```sql
=> SELECT p.anchor_table_name, p.projection_name, sc.node_name, sc.shard_name, sc.total_row_count
   FROM v_catalog.projections p
   JOIN (SELECT projection_id FROM v_monitor.storage_containers
         GROUP BY 1 HAVING count(*) = 1) x ON x.projection_id = p.projection_id
   JOIN v_monitor.storage_containers sc ON sc.projection_id = p.projection_id
   ORDER BY sc.total_row_count DESC LIMIT 3;

anchor_table_name | projection_name | node_name        | shard_name  | total_row_count
------------------+-----------------+------------------+-------------+-----------------
ujtest            | ujtest_super    | v_eondb_node0003 | segment0003 |            3826
watch_seen        | watch_seen_super| v_eondb_node0001 | segment0001 |               4
cx_t              | cx_t_super      | v_eondb_node0002 | segment0002 |               3
```

`public.ujtest` 只有 3,826 筆，全部在 node0003 的 segment0003。照「shard pruning」的直覺，查它應該只有一台動起來。實際用 `EXPLAIN LOCAL` 看：

```sql
=> EXPLAIN LOCAL SELECT count(*) FROM public.ujtest;
...
-----------------------------------------------
PLAN: v_eondb_node0001 (GraphViz Format)
...
9[label = "ScanStep: ujtest_super\nsales_quantity (not emitted)", shape = "box"];
...
-----------------------------------------------
PLAN: v_eondb_node0002 (GraphViz Format)
...
6[label = "ScanStep: ujtest_super\nsales_quantity (not emitted)", shape = "box"];
...
-----------------------------------------------
PLAN: v_eondb_node0003 (GraphViz Format)
...
6[label = "ScanStep: ujtest_super\nsales_quantity (not emitted)", shape = "box"];
```

**三個節點都有 `ScanStep`。** node0001 和 node0002 上根本沒有 `ujtest_super` 的 container，它們的掃描是空掃。所以在本機 25.4.0-0 上：

- 計畫層級沒有 shard pruning，一份邏輯計畫被發到所有節點（`Send`／`Recv`，net id 1000）。
- 真正的「剪枝」發生在儲存層：沒有 container 的節點沒有 I/O 可做，只回一個空的區域結果。
- 想知道資料在哪裡，唯一可靠的來源是 `v_monitor.storage_containers`。

這個差異在實務上有兩個直接後果。第一，**EXPLAIN 的平行度不可信**：計畫說 `Parallelism: 3`，但如果表只落在一個 shard，實際只有一台在做。第二，**小表的查詢不會因為 EON 而變快**：3,826 筆的 `count(*)` 實測 13 ms／9 ms，跟單機沒兩樣，因為它本來就只在一台。

### 3.3 三種 EXPLAIN，看三件不同的事

| 指令 | 多給你什麼 | 什麼時候用 |
|------|------------|------------|
| `EXPLAIN` | 邏輯計畫、Cost、Materialize 欄位 | 日常：看它選了哪個投影、要不要 resegment |
| `EXPLAIN VERBOSE` | 每個運算子的 `nodeSet`、資源估算（Scratch Memory MB、Worker Threads）、排序鍵 | 要判斷「這個運算在幾台跑」 |
| `EXPLAIN LOCAL` | 每個節點一份 local plan（initiator + 2 個 executor） | 要確認某台到底有沒有 `ScanStep` |

`EXPLAIN LOCAL` 在 EON 上是事後才補上的工具，但它比 `EXPLAIN` 誠實得多：它會直接列出 `PLAN: v_eondb_node0001`、`PLAN: v_eondb_node0002`、`PLAN: v_eondb_node0003` 三段。

---

## 四、Step by step：把 10 億筆拆成七種問法

以下每個查詢都跑兩次（同一 session、連續執行），單位 ms，`run1` 是第一次、`run2` 是緊接著的第二次。這個「兩次」很重要，第六節會解釋為什麼。

### 4.1 `count(*)`：0.42 秒

```sql
=> SELECT count(*) FROM mtk.partition_columns;
```

| 執行 | 時間 |
|------|------|
| run1 | 420 ms |
| run2 | 437 ms |

1,007,772,484 ÷ 0.42 秒 ≈ **每秒 24 億列**。這個速度不可能來自欄位掃描——三個 container 的資料列數本來就存在 ROS metadata 裡，`count(*)` 不需要把值讀出來。計畫也顯示兩件事：`Materialize:` 是空的（沒有任何欄位要 materialize），而且整份計畫的 `Cost` 是 **2**（`STORAGE ACCESS` 為 1，標註 `1 RLE`）。優化器自己承認這個查詢只讀 metadata 的列數。

### 4.2 加一個聚合欄位：4.76 秒

```sql
=> SELECT sum(row_count) FROM mtk.partition_columns;
```

| 執行 | 時間 |
|------|------|
| run1 | 4,756 ms |
| run2 | 4,769 ms |

同一個 session、同一張表，只因為要讀 `row_count` 這個 int 欄位，就從 0.42 秒變 4.76 秒，**慢 11 倍**。這就是「掃描」的真實成本：10 億列 ÷ 4.76 秒 ≈ 每秒 2.1 億列（三台合計，每台約 7,000 萬列）。

### 4.3 再加一個欄位：12.05 秒

```sql
=> SELECT count(*), sum(row_count), min(disk_space_bytes), max(disk_space_bytes)
   FROM mtk.partition_columns;
```

| 執行 | 時間 |
|------|------|
| run1 | 12,047 ms |
| run2 | 12,174 ms |

把兩個欄位都 materialize，時間幾乎是單欄的 2.5 倍。中間還有一個對照：

| 查詢 | run1 | run2 |
|------|------|------|
| `count(*)` | 420 ms | 437 ms |
| `sum(row_count)` | 4,756 ms | 4,769 ms |
| `count(*) + sum(row_count)` | 7,844 ms | 9,060 ms |
| `count + sum + min/max(disk_space_bytes)` | 12,047 ms | 12,174 ms |

**每多一個要讀的欄位，大約多 5 秒。** 反過來說，如果你的報表只需要列數，就不該順手加一個 `sum()`——那個 `sum()` 要 4.3 秒。

### 4.4 filter 打在 sort key 前綴：5.8 秒，沒有變快

排序鍵的第一欄是 `column_name`，所以 `column_name = 'epoch'` 理論上是最理想的前綴過濾：

```sql
=> SELECT count(*), sum(row_count) FROM mtk.partition_columns WHERE column_name = 'epoch';
-- 2,725,727 筆
```

| 執行 | 時間 |
|------|------|
| run1 | 5,809 ms |
| run2 | 5,146 ms |

命中率只有 0.27%（272 萬／10 億），卻還是花了 5 秒。原因是**每個 segment shard 只有一個 container**：排序只保證 container 內部的順序，沒有第二個 container 可以整顆跳過，所以還是得把 `column_name` 這一欄從頭讀到尾，再做 filter。

### 4.5 filter 打在非前綴欄位：4.16 秒 → 1.44 秒

```sql
=> SELECT count(*), sum(row_count) FROM mtk.partition_columns WHERE partition_key = '-1';
-- 4,421,986 筆
```

| 執行 | 時間 |
|------|------|
| run1 | 4,162 ms |
| run2 | 1,437 ms |

命中 442 萬筆（0.44%），第一次 4.16 秒，第二次 1.44 秒——**第二次快了 2.9 倍，但查的是同一件事**。這不是「第二次計畫變好」，是這一個欄位的資料已經在 page cache 裡。第六節會把這個現象跟 depot 一起講清楚。

### 4.6 `LIMIT 5`：19 ms

```sql
=> SELECT column_name, row_count, disk_space_bytes FROM mtk.partition_columns LIMIT 5;
```

| 執行 | 時間 |
|------|------|
| run1 | 19 ms |
| run2 | 15 ms |

10 億筆的表，`LIMIT 5` 只花 19 ms，因為它讀到第五列就停。**但這是「沒有 ORDER BY／GROUP BY」的特例**：一旦加上排序或聚合，就回到全掃的 5～12 秒。把 `LIMIT` 當成「加速大表查詢」的手段，在聚合報表上完全無效。

### 4.7 join：90.9 秒

```sql
=> SELECT p.table_schema, count(*) AS n
   FROM mtk.partition_columns pc
   JOIN mtk.partitions p ON pc.projection_id = p.projection_id
   GROUP BY 1 ORDER BY n DESC LIMIT 5;
```

計畫：

```
+-SELECT  LIMIT 5 [Cost: 36M, Rows: 5 (ROW COUNT ONLY)]
| +---> SORT [TOPK] [Cost: 36M]
| | +---> GROUPBY HASH (GLOBAL RESEGMENT GROUPS) (LOCAL RESEGMENT GROUPS) [Cost: 36M]
| | | +---> JOIN HASH [Cost: 879K, Rows: 1B (ROW COUNT ONLY)] Inner (BROADCAST)
| | | |      Join Cond: (pc.projection_id = p.projection_id)
| | | | +-- Outer -> STORAGE ACCESS for pc [Cost: 295K, Rows: 1B]
| | | | +-- Inner -> STORAGE ACCESS for p [Cost: 7K, Rows: 3M (PUSHED GROUPING)]
```

| 執行 | 時間 | 結果 |
|------|------|------|
| run1 | 90,875 ms | `eda`／841,034,568,612 |

兩個細節值得記下來：

1. **`Inner (BROADCAST)`**：`mtk.partitions` 只有 2,929,345 筆（3 個 container，各約 97.6 萬筆），優化器選擇把它廣播到三個節點，而不是把 10 億筆重新分段。
2. **`GLOBAL RESEGMENT GROUPS`**：join 完的 8,410 億個配對列要依 `table_schema` 重新分段才能做 `GROUP BY`，這一步是跨節點的網路傳輸。

90.9 秒裡，讀取只佔前面幾秒，剩下都是廣播、join、resegment、排序。**在 EON 上，join 的網路成本比掃描成本更難預測。**

---

## 五、把 EXPLAIN 讀對：Cost 不是時間

把本次幾個計畫的 Cost 與實測時間排在一起：

| 查詢 | EXPLAIN Cost | 實測 run1 | 每秒處理列數 |
|------|--------------|-----------|--------------|
| `count(*)` | 2 | 420 ms | 2.4 B |
| `sum(row_count)` | 361K | 4,756 ms | 212 M |
| `count+sum+min/max` | 722K | 12,047 ms | 84 M |
| `filter column_name='epoch'` | 2M | 5,809 ms | 173 M（掃描） |
| `filter partition_key='-1'` | 1B | 4,162 ms | 242 M（掃描） |
| `join` | 36M（頂層）／879K（join） | 90,875 ms | 11 M（以 10 億列計） |

![同一張表的七種問法：count(*) 0.42 秒到 join 90.88 秒，對數刻度下相差 216 倍]({{ '/assets/images/eon-query-time-bars.svg' | relative_url }}){: loading="lazy" }

兩件事很清楚：

- **Cost 的排序跟時間的排序不一致。** 722K 的計畫花 12 秒，1B 的計畫花 4.2 秒。Cost 是優化器用來在候選計畫之間比大小的相對值，沒有單位，也不線性。
- **Cost 的估算基準是資料列數，不是欄位數。** `count+sum+min/max` 與 `count(*)` 都是 722K，但實際差了 28 倍。

真正能預測時間的是三個問題：**要讀幾個欄位？**（4.3 節）**資料在幾台？**（3.2 節）**要不要跨節點搬？**（4.7 節）。

---

## 六、depot：本機全部命中，所以量不到 communal

EON 的查詢時間還有一個變數：資料在 depot（本地 SSD）還是要從 communal storage 抓。本機的實況是：

| 項目 | 值 |
|------|-----|
| Depot 上限／節點 | 566,381,859,840 bytes（約 527.5 GiB） |
| 這張表／節點 | 2,954,595,943 bytes（2.95 GB） |
| 佔比 | **0.52%** |
| `v_monitor.depot_fetches` 筆數（實測前） | 30 |
| `v_monitor.depot_fetches` 筆數（實測後） | 30 |
| `fetch_time_ms` 最大值 | 3 ms |

**整段實測跑了 30 次以上的 10 億列掃描，`depot_fetches` 一筆都沒增加。** 這代表沒有任何一次讀取需要從 communal storage 抓檔案——資料本來就都在 depot 裡。所以：

- 本機**量不到** depot miss，也量不到 communal 延遲。任何「EON 冷讀比較慢」的說法，在這台機器上都無法用數據支撐。
- 4.5 節那個 4,162 ms → 1,437 ms 的差異，來源是作業系統的 page cache（或 depot 的記憶體快取），不是 depot 換進換出。
- 如果你的環境 depot 小於熱資料量，情況會完全不同——但那需要一個 depot 裝不下的資料集才驗得出來，本機沒有。

這也是為什麼上一篇文章（`vertica-eon-ros-storage-containers`）只談 container 數量與掃描成本，不談冷熱：在這台機器上，冷熱對照是造不出來的。

---

## 七、常見錯誤與反模式

**錯誤 1：把 `Participating subscriptions` 當成「這個查詢用到的 shard」。**
它列出的是叢集訂閱地圖，三個節點永遠都在。判斷資料位置要查 `v_monitor.storage_containers`；判斷哪台有 `ScanStep` 要跑 `EXPLAIN LOCAL`。

**錯誤 2：用 Cost 排優先順序。**
本次實測 722K → 12.0 秒、2M → 5.8 秒、1B → 4.2 秒。Cost 只能比較「同一個查詢的不同計畫」，不能跨查詢比較時間，更不能當 SLA。

**錯誤 3：以為 sort key 前綴一定加速。**
`column_name='epoch'` 是排序鍵第一欄，命中率 0.27%，還是花了 5.8 秒；反而非前綴的 `partition_key='-1'` 第二次只花 1.4 秒。單一 container 的 layout 下，沒有容器層級的區間可以跳過，sort order 只省下 container 內部的比較，省不了 I/O。

**錯誤 4：`count(*)` 很便宜，所以順手加幾個聚合。**
`count(*)` 0.42 秒，加一個 `sum()` 變 4.76 秒，加兩個欄位變 12.05 秒。差的不是 10%，是 28 倍。「順手加的聚合」是這類報表最常見的效能稅。

**錯誤 5：把 `LIMIT` 當萬用加速器。**
`LIMIT 5` 在沒有排序／聚合時 19 ms，因為它提前停止；一旦有 `GROUP BY` 或 `ORDER BY`，就是全掃 5～12 秒。`LIMIT` 不改變掃描量。

**錯誤 6：以為 EON 上小表也會被平均分散。**
`public.ujtest` 3,826 筆全部在 node0003 的一個 container 裡。小表的 hash 值只落在一個 segment 區間，三個節點的 `ScanStep` 有兩個是空掃。**「EON 會把資料打散到三台」對小表是錯的。**

---

## 八、風險與建議

**現在就能做（唯讀、低風險）**

1. 用 `v_monitor.storage_containers` 建一張「投影 → shard → 節點 → 列數」的對照表，每次要調效能前先查資料在哪幾台。
2. 對慢查詢跑 `EXPLAIN LOCAL`，確認 `ScanStep` 出現幾台、有幾台是空掃。
3. 把報表拆成「只要列數」與「要聚合值」兩層：`count(*)` 走 metadata，聚合走另一條路，不要混在同一支 SQL。
4. 量測時一律跑兩次並同時記錄兩次：第一次是冷／暖邊界，第二次才是穩定值。本機 4,162 → 1,437 ms 就是例子。

**需要變更（要窗口、要核准）**

1. 若 depot 真的裝不下熱資料，調 `MaxDepotSizePercent` 或加 depot 磁碟——這會動到叢集設定，本機不測。
2. 大表 join 若常態超過 60 秒，考慮在 ETL 階段先做 pre-join 或改用 segmentation 對齊的投影；這需要建表與重新分段，屬於變更。
3. K-Safe 目前是 0。任何「查詢變慢／資料重分布」的討論都建立在「沒有複本」的前提上；要提高 K-Safe 需要重建 shard 與 subscription，不能在這裡順手做。

---

## 九、參考資料

- Vertica 25.4 EXPLAIN 指令：<https://docs.vertica.com/25.4.x/en/sql-reference/commands/explain/>
- `v_monitor.storage_containers`：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-storage-containers/>
- `v_catalog.storage_locations`：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-storage-locations/>
- EON 架構（communal storage、depot、shard）：<https://docs.vertica.com/25.4.x/en/admin/architecture/eon/>

---

*本文所有數字來自 2026-11-18 在本機 `eondb`（Vertica v25.4.0-0，3 節點，K-Safe=0）的唯讀查詢，沒有對叢集做任何變更。*
