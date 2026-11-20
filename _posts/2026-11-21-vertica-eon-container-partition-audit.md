---
layout: post
title: "EON 資料分佈的盲點：1,435 個 container、221 個 projection，卻只有 6 個有 partition 紀錄"
date: 2026-11-21 05:00:00 +0800
categories: vertica eon monitoring
tags: [vertica, eon, storage-containers, partitions, ros, audit, data-distribution, monitoring, 25.4]
description: "本機 eondb（3 節點、K-Safe=0、4 shard、v25.4.0-0）的 v_monitor.storage_containers 有 1,435 個 container、221 個 projection、11.99 GB，但 v_monitor.partitions 只有 6 個 projection、819 列、151 MB。實測兩者的落差不是漏資料：819 列剛好等於那 6 個投影的 container 數，其餘 215 個投影全部位於沒有 partition_expression 的表上。join 鍵是 ros_id = storage_oid，不是 sal_storage_id。"
image: /assets/images/og/vertica-eon-container-partition-audit-zh.png
faq:
  - q: "v_monitor.partitions 只有 6 個投影，是資料漏掉了嗎？"
    a: "不是。實測 819 列 partition 紀錄，剛好等於那 6 個投影在 storage_containers 裡的 789＋11＋9＋4＋3＋3 個 container，一列對一個 container、差值 0。其餘 215 個投影（97.29%）位於 partition_expression 為 NULL 的表上，本來就不會有 partition key，所以不會出現在 partitions。"
  - q: "partitions 和 storage_containers 要用哪個欄位 join？"
    a: "用 v_monitor.partitions.ros_id = v_monitor.storage_containers.storage_oid。實測 819 列全部對得上（819/819）；改用 sal_storage_id 則一列都對不上（0/819），因為 sal_storage_id 是十六進位字串，ros_id 是數值 OID。"
  - q: "估算 EON 容量時該看哪一張表？"
    a: "看 v_monitor.storage_containers。本機 11.99 GB 的資料裡，partitions 只涵蓋 151 MB、1.261%；只查 partitions 會把容量低估 79 倍，而且會漏掉最大的 mtk.partition_columns_super（3 個 container、10.08 億筆、8.86 GB）。"
  - q: "為什麼那 6 個投影的 partition 列數剛好等於 container 數？"
    a: "因為 partitions 的定義是一列代表「一個 partition key × 一個 ROS container」。這 6 張分區表每個 container 只裝一個 partition，所以 789 個 container 就是 789 列；而 789 又可以拆成 258 個 partition 分佈在 3 個節點、6 個在 2 個節點、3 個只在 1 個節點。"
  - q: "分區表的 active_partition_count 為什麼是 NULL？"
    a: "本機 eondb（v25.4.0-0）實測 34 張有 partition_expression 的表，active_partition_count 全部是 NULL，不能用來數分區。要數分區請用 count(distinct partition_key) FROM v_monitor.partitions，例如 ADA.LOWIDOC_V6 是 267 個。"
---
<!--
AI Summary: 在本機 3 節點 EON 叢集（v25.4.0-0、K-Safe=0、4 shard）唯讀實測 v_monitor.storage_containers 與 v_monitor.partitions 的落差：1,435 個 container／221 個投影／11.99 GB 對上 819 列／6 個投影／151 MB（1.261%）。落差可完全對帳——819 列等於那 6 個投影的 container 數（差值 0），其餘 215 個投影位於非分區表；join 鍵是 ros_id = storage_oid（819/819 命中），不是 sal_storage_id（0/819）。另記錄 active_partition_count 全 NULL、start_epoch 是邏輯 epoch 不是時間戳、container 總數五天內由 1,428 增至 1,435。
AI Keywords: Vertica, EON, v_monitor.partitions, v_monitor.storage_containers, storage_oid, ros_id, partition audit, capacity estimate, 25.4
-->
<!--more-->

---

## 一、先講結論

把兩張監控表並排放在一起，第一個冒出來的問題一定是「數字為什麼對不起來」：

```sql
=> SELECT count(*) AS containers, count(DISTINCT projection_id) AS projs,
          sum(total_row_count) AS rows, sum(used_bytes) AS bytes
   FROM v_monitor.storage_containers;
 containers | projs |    rows     |    bytes
------------+-------+-------------+--------------
       1435 |   221 | 1099951796  | 11987583195

=> SELECT count(*) AS part_rows, count(DISTINCT projection_id) AS projs,
          sum(ros_row_count) AS rows, sum(ros_size_bytes) AS bytes
   FROM v_monitor.partitions;
 part_rows | projs |   rows  |   bytes
-----------+-------+---------+-----------
       819 |     6 |  932819 | 151116590
```

1,435 個 container 對 819 列 partition、221 個投影對 6 個投影、11.99 GB 對 151 MB。看起來像 partitions 漏了 98.7% 的資料。

**實測結論是：沒有漏，兩個視圖量的本來就不是同一個母體。**

1. **819 列不是「6 個投影的其中一部分」，而是那 6 個投影的全部 container。** 789＋11＋9＋4＋3＋3 = 819，與 `storage_containers` 的 container 數逐個投影比對，差值全部是 0。
2. **其餘 215 個投影（97.29%）位於沒有 `partition_expression` 的表上。** 沒有分區鍵就沒有 partition，`partitions` 本來就不該有它們的列。
3. **本機 34 張分區表裡，只有 6 張真的有資料。** 另外 29 張是 0 筆的空表，0 個 container，自然 0 列 partition。
4. **兩張表要用 `ros_id = storage_oid` 串。** 實測 819/819 全中；用 `sal_storage_id` 是 0/819，因為一個是數值 OID、一個是十六進位字串。
5. **容量估算只能看 `storage_containers`。** 只看 `partitions` 會得到 151 MB，實際是 11.99 GB——低估 79 倍，而且最大的表完全不在裡面。

這篇把這五點逐一用本次實跑的輸出證明，最後給三支可以直接排進巡檢的 SQL。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| 資料庫 | `eondb`（EON），3 節點，K-Safe = 0 |
| 版本 | `Vertica Analytic Database v25.4.0-0` |
| shard | 4（`replica` + `segment0001`~`segment0003`） |
| 節點 | `v_eondb_node0001`／`0002`／`0003` |
| 資料規模 | 1,435 個 container、1,099,951,796 筆、11,987,583,195 bytes（11.17 GiB） |
| 投影 | `v_catalog.projections` 486 列／434 個 projection_id；有 container 的 221 個 |
| 分區表 | 34 張（`partition_expression` 非空），其中 6 張有資料 |
| 客戶端 | `vertica_python` 1.4.0，`ssl=False` |
| 實測時間 | 2026-11-21 05:00 起（Asia/Taipei） |

**全程唯讀。** 只跑 `SELECT` 與 `EXPLAIN`；沒有 `CREATE`、`DROP`、`INSERT`、`ALTER`，也沒有任何 config parameter 變更。EON 系列從第一天就守這條線，本次一樣。

補一個容易誤讀的地方：本文的 `2026-11-21` 是台北時間，叢集 `now()` 回的是 `2026-11-20 21:02:15+00`，差 8 小時。跨時區團隊對數字時，先確認這個偏移。

---

## 三、核心原理：兩個視圖，兩個母體

| | `v_monitor.storage_containers` | `v_monitor.partitions` |
|---|---|---|
| 一列代表 | 一個 ROS container 在一個節點上 | 一個 partition key × 一個 ROS container |
| 母體 | 所有有資料的投影 | 只有分區表、而且該 partition 真的有 container |
| 本機列數 | 1,435 | 819 |
| 涵蓋投影 | 221 | 6 |
| 涵蓋 bytes | 11,987,583,195 | 151,116,590（1.261%） |
| 有 `partition_key` | 無 | 有 |
| 主要用途 | 容量、資料分佈、container 數 | 分區粒度、單一分區大小、DROP PARTITION 前確認 |

`storage_containers` 是「資料實際放在哪裡」的底帳；`partitions` 是「分區這個維度上，每個分區有多大」的切片。前者沒有 partition 概念也能列出來，後者沒有 partition 就沒有東西可列。這就是落差的第一層原因。

![EON 中 storage_containers 與 partitions 的母體對照：434 個投影逐層收窄到 6 個有 partition 紀錄]({{ '/assets/images/eon-partitions-scope-funnel.svg' | relative_url }}){: loading="lazy" }

圖上四層數字全部是本次查出來的：434 → 221 → 35 → 6。中間那層「35 個投影位於分區表」是關鍵——它證明 6 不是隨機抽樣，而是「分區表 ∩ 有資料」的交集。

---

## 四、Step-by-step 實測

### 4.1 先把兩邊的總量攤開

```sql
=> SELECT count(*) AS rows_total, count(DISTINCT projection_id) AS ids,
          count(DISTINCT projection_name) AS names,
          count(DISTINCT schema_name) AS schemas, count(DISTINCT node_name) AS nodes
   FROM v_monitor.storage_containers;
 rows_total | ids | names | schemas | nodes
------------+-----+-------+---------+-------
       1435 | 221 |   219 |      19 |     3
```

221 個 id、219 個名稱：`users_super` 與 `short_url_super` 這兩個名稱各出現在兩個 schema 下（本次實測 `count(distinct schema_name)=2`）。所以「219 還是 221」不是資料錯亂，是「名稱」與「id」兩個計數單位不同——**報表要指名用哪一個**。

節點分佈同時顯示一件事：

```sql
=> SELECT node_name, count(*) AS containers, sum(total_row_count) AS rows, sum(used_bytes) AS bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;
     node_name     | containers |   rows    |   bytes
-------------------+------------+-----------+------------
 v_eondb_node0001  |        468 | 366827918 | 3997359299
 v_eondb_node0002  |        494 | 366626885 | 3995251668
 v_eondb_node0003  |        473 | 366496993 | 3994972228
```

container 數 468／494／473（最大差 5.6%），資料列 366,827,918／366,626,885／366,496,993（最大差 **0.090%**）。**container 數不均是正常的**，因為 container 是「每個節點、每個投影各切一份」，投影在哪幾個節點有資料就決定 container 數；資料列才是平衡指標。

### 4.2 找出「有 container、沒有 partition」的差集

```sql
=> WITH c AS (SELECT projection_id, schema_name, projection_name,
                     count(*) AS containers, sum(total_row_count) AS rows, sum(used_bytes) AS bytes
              FROM v_monitor.storage_containers GROUP BY 1,2,3),
        p AS (SELECT projection_id, count(*) AS part_rows FROM v_monitor.partitions GROUP BY 1)
   SELECT count(*) AS projs, sum(c.bytes) AS bytes
   FROM c LEFT JOIN p USING (projection_id) WHERE p.projection_id IS NULL;
 projs |    bytes
-------+--------------
   215 | 11836466605
```

215 個投影、11,836,466,605 bytes 沒有 partition 列。清單前幾筆就是這台機器上最大的幾張表：

```sql
=> WITH c AS (SELECT projection_id, schema_name, projection_name, count(*) AS containers,
                     sum(total_row_count) AS rows, sum(used_bytes) AS bytes
              FROM v_monitor.storage_containers GROUP BY 1,2,3),
        p AS (SELECT projection_id, count(*) AS pr FROM v_monitor.partitions GROUP BY 1)
   SELECT c.schema_name, c.projection_name, c.containers, c.rows, c.bytes
   FROM c LEFT JOIN p USING (projection_id) WHERE p.projection_id IS NULL
   ORDER BY c.bytes DESC LIMIT 6;
 schema_name |         projection_name          | containers |   rows    |   bytes
-------------+----------------------------------+------------+-----------+------------
 mtk         | partition_columns_super          |          3 | 1007772484| 8859731258
 public      | vsmart_test_super                |          3 |    294958 |  382685908
 ADA         | GCIS_Companyinfo_CmpB_super      |          3 |  13245243 |  341623643
 mtk         | projection_usage_super           |          3 |   6891320 |  251250854
 public      | judicial_202604_super            |          3 |    100427 |  233305915
 lowi_rap    | gcis_companyinfo_cmpb_super      |          3 |  17210299 |  178673066
```

排第一的 `mtk.partition_columns_super` 名字裡有 "partition"，但它**不是分區表**：

```sql
=> SELECT table_schema, table_name, partition_expression, partition_group_expression
   FROM v_catalog.tables WHERE table_name = 'partition_columns_super';
(0 rows)
```

表名誤導是這裡最常見的陷阱。判斷是不是分區表，只能問 catalog 的 `partition_expression`，不能看名字。

### 4.3 為什麼是 6？用 catalog 對帳

把「分區表的投影」全部列出來，再左接兩張監控表：

```sql
=> WITH c AS (SELECT projection_id, count(*) AS containers, sum(total_row_count) AS rows
              FROM v_monitor.storage_containers GROUP BY 1),
        p AS (SELECT projection_id, count(*) AS part_rows FROM v_monitor.partitions GROUP BY 1)
   SELECT t.table_schema, t.table_name, pr.projection_name,
          coalesce(c.containers,0) AS containers, coalesce(p.part_rows,0) AS part_rows
   FROM v_catalog.projections pr
   JOIN v_catalog.tables t ON pr.anchor_table_id = t.table_id
   LEFT JOIN c ON c.projection_id = pr.projection_id
   LEFT JOIN p ON p.projection_id = pr.projection_id
   WHERE t.partition_expression IS NOT NULL AND t.partition_expression <> ''
   ORDER BY 4 DESC;
 table_schema |      table_name      |      projection_name      | containers | part_rows
--------------+----------------------+---------------------------+------------+-----------
 ADA          | LOWIDOC_V6           | LOWIDOC_V6_super          |        789 |       789
 lowi_rap     | news_article_i18n    | news_article_i18n_p       |         11 |        11
 public       | tcpd_carcheck        | tcpd_carcheck_super       |          9 |         9
 a2v_test     | wiznew               | wiznew_super              |          4 |         4
 ADA          | LOWI_HASHTAG         | LOWI_HASHTAG_super        |          3 |         3
 lowi         | lowi_hashtag         | lowi_hashtag_super        |          3 |         3
 a2v_test     | target_partitioned   | target_partitioned_super  |          0 |         0
 ami_dg       | data_abnorm_log      | data_abnorm_log_super     |          0 |         0
 ...          | （其餘 27 張皆為 0 個 container）                        |          0 |         0
(35 rows)
```

三件事同時成立：

- **34 張表有 `partition_expression`，對應 35 個投影**；其中 6 個有 container，而且每個的 `part_rows` 與 `containers` **完全相等**（789=789、11=11、9=9、4=4、3=3、3=3）。
- **29 個投影是 0 個 container**，也就是空表；它們沒有 partition 列不是遺漏，是沒有資料。
- **6 + 29 = 35**，一個都沒有少。`partitions` 的 819 列，一列不多、一列不少。

再把檢查寫成「有問題才會回傳列」的形式，排進巡檢才有意義：

```sql
=> WITH c AS (SELECT projection_id, count(*) n FROM v_monitor.storage_containers GROUP BY 1),
        p AS (SELECT projection_id, count(*) n FROM v_monitor.partitions GROUP BY 1),
        part AS (SELECT pr.projection_id FROM v_catalog.projections pr
                 JOIN v_catalog.tables t ON pr.anchor_table_id = t.table_id
                 WHERE t.partition_expression IS NOT NULL AND t.partition_expression <> '')
   SELECT part.projection_id, coalesce(c.n,0) AS containers, coalesce(p.n,0) AS part_rows
   FROM part LEFT JOIN c USING (projection_id) LEFT JOIN p USING (projection_id)
   WHERE coalesce(c.n,0) <> coalesce(p.n,0);
(0 rows)
```

0 列。這支查詢在本次執行花了 29 ms，值得每天跑一次：它會抓到「分區表的 container 與 partition 紀錄不一致」——那才是真的異常（例如 partition 被 detached、container 遺失）。

### 4.4 join 鍵：`ros_id` 對 `storage_oid`，不是 `sal_storage_id`

兩張表沒有同名欄位，直覺會拿看起來像 ID 的 `sal_storage_id` 去串。實測兩種都跑：

```sql
=> SELECT count(*) AS matched FROM v_monitor.partitions p
   JOIN v_monitor.storage_containers s ON p.ros_id = s.sal_storage_id AND p.node_name = s.node_name;
 matched
---------
       0

=> SELECT count(*) AS matched FROM v_monitor.partitions p
   JOIN v_monitor.storage_containers s ON p.ros_id = s.storage_oid AND p.node_name = s.node_name;
 matched
---------
     819
```

819/819 全中。文件對 `storage_oid` 的說明是「同一個 OID 可能出現在多個節點」，所以保險作法是連 `node_name` 一起串。本次實測兩種寫法都回 819：

```sql
=> SELECT count(*) FROM v_monitor.partitions p JOIN v_monitor.storage_containers s ON p.ros_id = s.storage_oid;
 count
-------
   819
```

差別在「現在剛好沒事」與「哪天有事」：`storage_oid` 一旦在兩個節點重複出現，不帶 `node_name` 的 join 就會放大成兩倍、`sum()` 直接多算。多寫一個 `AND p.node_name = s.node_name` 沒有成本，值得寫死。

### 4.5 819 是怎麼來的：789 個 container 的分解

`ADA.LOWIDOC_V6_super` 一個投影就佔了 789 列，值得拆開：

```sql
=> SELECT node_name, count(*) AS part_rows, count(DISTINCT partition_key) AS keys
   FROM v_monitor.partitions WHERE projection_id = 45035996273708334 GROUP BY 1 ORDER BY 1;
     node_name     | part_rows | keys
-------------------+-----------+------
 v_eondb_node0001  |       262 |  262
 v_eondb_node0002  |       265 |  265
 v_eondb_node0003  |       262 |  262

=> WITH k AS (SELECT partition_key, count(DISTINCT node_name) AS nodes, sum(ros_row_count) AS rows
              FROM v_monitor.partitions WHERE projection_id = 45035996273708334 GROUP BY 1)
   SELECT nodes, count(*) AS part_keys, sum(rows) AS rows FROM k GROUP BY 1 ORDER BY 1;
 nodes | part_keys |  rows
-------+-----------+--------
     1 |         3 |      3
     2 |         6 |     36
     3 |       258 | 920832
```

267 個 partition key 裡，258 個在三個節點都有 container、6 個只在兩個節點、3 個只在一個節點：258×3 + 6×2 + 3×1 = **789**。完全對上。

**這件事的實務意義**：`count(DISTINCT partition_key)` 是 267，`count(*)` 是 789。前者回答「這張表有幾個分區」，後者回答「這些分區一共產生幾個 container」。兩個都對，但答的不是同一個問題——文件寫得很清楚：「one row per partition key, per ROS container」。

那 3 個只落在 1 個節點的分區，資料量各是 1 筆；6 個落在 2 個節點的分區合計 36 筆。**分區不是「每個節點都有一份」**，路由到哪個 segment 由 hash 決定，這也是為什麼 EON 的 partition 列數不能拿「分區數 × 節點數」去推。

### 4.6 一致性檢查：沒有孤兒列

```sql
=> SELECT count(*) AS orphans FROM v_monitor.partitions p
   WHERE NOT EXISTS (SELECT 1 FROM v_monitor.storage_containers s WHERE s.storage_oid = p.ros_id);
 orphans
---------
       0

=> SELECT sum(CASE WHEN partition_key IS NULL THEN 1 ELSE 0 END) AS null_keys, count(*) AS total
   FROM v_monitor.partitions;
 null_keys | total
-----------+-------
         0 |   819
```

819 列 partition 全部對得到 container，沒有 NULL partition key。這代表本次量到的 6 個投影是「完整」的，不是抽取到一半的視圖。

---

## 五、效能與計畫形狀

三支核心查詢在本機的實測時間（單節點發起、三節點執行）：

| 查詢 | 首次 | 5 次中位數 |
|------|------|-----------|
| 單表全掃（1,435 列或 819 列） | 10–24 ms | — |
| 差集 215 個投影（4.2） | 27 ms | **26.3 ms**（27.2／23.8／24.4／26.6／26.3） |
| 分區表對帳（4.3） | 45 ms | — |
| 一致性檢查（4.6） | 29 ms | — |
| 依 schema 彙總容量（4.4 的健康版） | 44 ms | — |

全表掃描也不慢的原因在計畫裡：

```sql
=> EXPLAIN WITH c AS (...), p AS (...)
   SELECT c.schema_name, ..., coalesce(p.pr,0) FROM c LEFT JOIN p USING (projection_id)
   ORDER BY c.bytes DESC LIMIT 10;
+-SELECT  LIMIT 10 [Cost: 3K, Rows: 10 (NO STATISTICS)] (PATH ID: 0)
|  Execute on: Query Initiator
| +---> SORT [TOPK] [Cost: 3K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
| |      Order: c.bytes DESC
| |      Execute on: All Nodes
| | +---> JOIN MERGEJOIN(inputs presorted) [LeftOuter] [Cost: 3K, Rows: 10K (NO STATISTICS)] (PATH ID: 2)
| | |      Join Cond: (c.projection_id = p.projection_id)
| | |      Runtime Filter: (SIP1(TopK): c.bytes)
| | | +-- Outer -> GROUPBY HASH (GLOBAL RESEGMENT GROUPS) [Cost: 3K, ...] (PATH ID: 4)
| | | | +---> STORAGE ACCESS for storage_containers [Cost: 1K, ...] (PATH ID: 5)
| | | | |      Projection: default_namespace.v_monitor.storage_containers_p
| | | +-- Inner -> ... STORAGE ACCESS for p (PATH ID: 9)
| | | | |      Projection: default_namespace.v_internal.vs_partitions_p
```

三個細節：

1. **兩個視圖各自有實體投影**（`storage_containers_p`、`vs_partitions_p`），所以查詢掃的是 1,435 列與 819 列的系統表，不是 catalog 展開。
2. **`SIP1(TopK)` runtime filter** 會在 join 前把外側列先過濾掉，這是 26 ms 的主因。
3. **`GLOBAL RESEGMENT GROUPS`** 代表三節點各自 group by 後再 resegment 到發起節點合併——這是 EON 的常態，不是設定錯誤。

反過來說，`v_monitor.partitions` 在 EXPLAIN 裡還有一段值得注意的訊息：

```
The following JGNodes/Tables have been pruned due to uniqueness guarantee:
Subquery for Node 1 (default_namespace.v_internal.vs_partitions):
| +-Node 1 d (default_namespace.v_monitor.delete_vectors)
```

`delete_vectors` 被唯一性優化剪掉了。本次 819 列 partition 與 1,435 個 container 的 `deleted_row_count` 全部是 0，所以「沒有 delete vector」這個推論是安全的；但如果環境有大量 DELETE，這段計畫形狀會改變，`partitions` 的 `ros_size_bytes` 也可能與 `storage_containers.used_bytes` 出現落差。

---

## 六、常見錯誤與反模式

### 反模式 1：用 `partitions` 估算容量

```
只查 partitions：151,116,590 bytes（151 MB）
實際容量：      11,987,583,195 bytes（11.99 GB）
低估倍數：      79.3 倍
```

最糟的不是低估，是**漏掉最大的表**。`mtk.partition_columns_super` 一張就佔 8.86 GB（73.91% 的位元組），而它沒有 partition 列。任何只看 `partitions` 的容量報表，都會回報「這台機器很空」。

### 反模式 2：join 用 `sal_storage_id`

```
p.ros_id = s.sal_storage_id  → 0 列
p.ros_id = s.storage_oid     → 819 列（正確）
```

`sal_storage_id` 長這樣：`02009b2cd7db9cfaad6bf701051f89e600a00000000049af`，是十六進位字串；`ros_id` 是 `45035996273755056` 這種數值。型別不同、命名空間也不同。這個錯誤通常不會報錯，只會回 0 列，然後被誤判成「這張表沒有 partition」。

### 反模式 3：用 `active_partition_count` 數分區

```sql
=> SELECT count(*) AS tables_with_part_expr,
          sum(CASE WHEN active_partition_count IS NULL THEN 1 ELSE 0 END) AS null_cnt
   FROM v_catalog.tables WHERE partition_expression IS NOT NULL AND partition_expression <> '';
 tables_with_part_expr | null_cnt
-----------------------+----------
                    34 |       34
```

34 張分區表，`active_partition_count` 全部是 NULL。這個欄位在 catalog 裡存在，但在本機 EON 叢集沒有值。要數分區只能回 `v_monitor.partitions`：`ADA.LOWIDOC_V6` 是 `count(DISTINCT partition_key) = 267`。

### 反模式 4：把 `start_epoch` 當時間戳

```sql
=> SELECT min(start_epoch) AS min_e, max(start_epoch) AS max_e, count(DISTINCT start_epoch) AS distinct_e
   FROM v_monitor.storage_containers;
 min_e | max_e | distinct_e
-------+-------+------------
    10 |   394 |        252

=> SELECT to_timestamp(min(start_epoch)), to_timestamp(max(start_epoch)) FROM v_monitor.storage_containers;
    to_timestamp     |    to_timestamp
---------------------+---------------------
 1970-01-01 00:00:10 | 1970-01-01 00:06:34
```

值是 10 到 394，轉成時間是 1970-01-01。這是 Vertica 的**邏輯 epoch 編號**，不是 Unix 秒。用 `WHERE start_epoch > now() - interval '1 day'` 之類的條件過濾，會得到 0 筆（本次實測就是 0），然後誤判成「沒有新 container」。

### 反模式 5：把 container 總數寫進 SLA

11-16 那篇 ROS 文章量到 1,428 個 container；今天同一台機器是 1,435 個。五天 **+7 個**、資料列 +171 筆、位元組 +4,164。container 是 tuple mover 的產物，mergeout 一跑數字就變。**趨勢可以當指標，絕對值不行。**

---

## 七、風險與建議

### 現在就能做（唯讀、低風險）

1. **容量報表一律以 `storage_containers` 為母體**，`partitions` 只當分區維度的補充視圖。
2. **把 4.3 的一致性檢查排進每日巡檢**：它正常時回 0 列，異常時直接給出 projection_id。
3. **join 一律用 `ros_id = storage_oid` 並帶 `node_name`**，不要用 `sal_storage_id`。
4. **報表註明計數單位**：219 個名稱、221 個 id、1,435 個 container 是三個不同的數字，混用會讓對帳永遠對不完。

三支可直接執行的巡檢 SQL：

```sql
-- 1. 每個 schema 的實際容量（含 partitions 覆蓋量）
WITH c AS (SELECT projection_id, schema_name, count(*) AS containers,
                  sum(total_row_count) AS row_cnt, sum(used_bytes) AS used_bytes
           FROM v_monitor.storage_containers GROUP BY 1,2),
     p AS (SELECT projection_id, count(*) AS part_rows FROM v_monitor.partitions GROUP BY 1)
SELECT c.schema_name,
       sum(c.containers)             AS containers,
       sum(c.row_cnt)                AS row_cnt,
       sum(c.used_bytes)             AS used_bytes,
       sum(coalesce(p.part_rows, 0)) AS part_rows
FROM c LEFT JOIN p ON p.projection_id = c.projection_id
GROUP BY 1 ORDER BY 4 DESC;

-- 2. 分區表對帳（正常回 0 列）
WITH c AS (SELECT projection_id, count(*) n FROM v_monitor.storage_containers GROUP BY 1),
     p AS (SELECT projection_id, count(*) n FROM v_monitor.partitions GROUP BY 1),
     part AS (SELECT pr.projection_id FROM v_catalog.projections pr
              JOIN v_catalog.tables t ON pr.anchor_table_id = t.table_id
              WHERE t.partition_expression IS NOT NULL AND t.partition_expression <> '')
SELECT part.projection_id, coalesce(c.n,0) AS containers, coalesce(p.n,0) AS part_rows
FROM part LEFT JOIN c USING (projection_id) LEFT JOIN p USING (projection_id)
WHERE coalesce(c.n,0) <> coalesce(p.n,0);

-- 3. 有資料卻完全沒有 partition 覆蓋的大表（容量風險清單）
WITH c AS (SELECT projection_id, schema_name, projection_name, count(*) AS containers,
                  sum(total_row_count) AS rows, sum(used_bytes) AS bytes
           FROM v_monitor.storage_containers GROUP BY 1,2,3)
SELECT c.schema_name, c.projection_name, c.containers, c.rows, c.bytes
FROM c LEFT JOIN (SELECT DISTINCT projection_id FROM v_monitor.partitions) p USING (projection_id)
WHERE p.projection_id IS NULL ORDER BY c.bytes DESC LIMIT 20;
```

第 1 支的實跑結果（前 9 名）：

```sql
 schema_name  | containers |   row_cnt  | used_bytes | part_rows
--------------+------------+------------+------------+-----------
 mtk          |         18 | 1021448137 | 9285153505 |         0
 lowi_rap     |        124 |   48073315 |  903421199 |        11
 ADA          |        824 |   18434488 |  762136677 |       792
 public       |         68 |    1331253 |  635920412 |         9
 online_sales |          9 |    5001200 |  172013956 |         0
 store        |          9 |    5300250 |  129906261 |         0
 lowi         |         64 |     336230 |   98373713 |         3
 a2v          |         93 |       3606 |     304464 |         0
 a2v_test     |         84 |      13295 |      98365 |         4
```

`part_rows` 這裡要先按投影聚合再彙總，不能直接 join 後 `sum`：`ADA` 的 792 = 789 + 3，若把 container 列直接跟 partition 列串起來再加總，會變成 789×789 + 3×3 = 622,530，看起來「有數字」但完全是錯的。這也是本次撰文時自己踩到、重跑後才修正的坑。

`ADA` 一個 schema 就佔了 824 個 container（57.42% 的 container 數），但只有 18,434,488 筆、762 MB；`mtk` 只有 18 個 container 卻有 9.29 GB。**container 數與容量幾乎無關**，這是第 5 個反模式的另一個面向。

### 需要變更（要窗口、要核准）

1. **把 215 個無分區投影中的大表改成分區表**：會動 DDL，且既有 projection 要重建，離線視窗與磁碟空間都要先算。
2. **清理 29 張 0 筆的分區表投影**：`DROP PROJECTION` 或整表 `DROP`，要先確認沒有程式依賴。
3. **對沒有 partition group 的分區表補上設計**：34 張分區表裡只有 27 張有 `partition_group_expression`，剩下 7 張是空的——`ADA.LOWIDOC_V6`、`ADA.LOWI_HASHTAG`、`a2v_test.target_partitioned`、`a2v_test.wiznew`、`lowi.lowi_hashtag`、`lowi_rap.news_article_i18n`、`v_internal_tables.v_vbr_ref_count`。這是設計選擇不是缺陷，但分區數一多（`LOWIDOC_V6` 已 267 個）就會變成 ROS container 爆炸的來源；改動前要跟資料擁有者確認保留策略。

---

## 八、參考資料

- Vertica 25.4.x 文件，`PARTITIONS` 系統表（「one row per partition key, per ROS container」）：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/partitions/>
- Vertica 25.4.x 文件，`STORAGE_CONTAINERS` 系統表（`STORAGE_OID`、`SAL_STORAGE_ID` 欄位定義）：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/storage-containers/>
- Vertica 25.4.x 文件，`TABLES` 系統表（`PARTITION_EXPRESSION`、`ACTIVE_PARTITION_COUNT`）：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/tables/>
- Vertica 25.4.x 文件，`PROJECTIONS` 系統表：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/projections/>
