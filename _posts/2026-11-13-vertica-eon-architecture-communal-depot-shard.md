---
layout: post
title: "EON 架構總覽：7 個 storage location 與 4 個 shard，看懂 communal storage、depot 與 segment"
date: 2026-11-13 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, communal-storage, depot, shard, storage-location, architecture, 25.4]
description: "在本機 3 節點 EON 叢集（v25.4.0-0）實查：v_catalog.storage_locations 是 7 筆，不是 7 份資料——1 個 communal（s3://verticadb/eondb，has_catalog=True）加 3 個 depot 加 3 個 DATA,TEMP。v_catalog.shards 是 4 筆：3 個 segment 把 0–4,294,967,295 的 hash 空間切成 1,431,655,739／1,431,655,739／1,431,655,818 個值，合計正好 2^32、缺口 0；第 4 個 replica shard 不切區間，三個節點各持一份。6 筆 node subscription 決定誰是 primary，而 1,394 個 ROS container 的分布證明資料真的只落在自己的 segment 上。"
image: /assets/images/og/vertica-eon-architecture-communal-depot-shard-zh.png
faq:
  - q: "EON 的 storage location 有 7 個，是不是代表資料有 7 份？"
    a: "不是。本機實查的 7 筆是 1 個 communal（`s3://verticadb/eondb`，`sharing_type = COMMUNAL`、`has_catalog = True`）加 3 個節點各自的 DEPOT 加 3 個節點各自的 DATA,TEMP。真正的資料本體在 communal 那一份，depot 只是各節點的本機快取，DATA,TEMP 放的是暫存與本機資料，三者不是同一份資料的三種副本。"
  - q: "4 個 shard 是怎麼算出來的？為什麼 namespaces 表說 default_shard_count 是 3？"
    a: "`v_catalog.shards` 實查 4 筆：`replica`（`shard_type = Replica`，沒有 hash 上下界）加 `segment0001`～`segment0003`。`v_catalog.namespaces` 的 `default_shard_count` 是 3，指的是**資料 segment** 的數量，不含 replica shard。兩個數字都對，只是問的問題不同：一個問「有幾個 shard 物件」，一個問「資料切幾片」。"
  - q: "3 個 segment 的 hash 區間有沒有缺口或重疊？"
    a: "沒有。實測 `upper_hash_bound - lower_hash_bound + 1` 三段分別是 1,431,655,739、1,431,655,739、1,431,655,818，加總 4,294,967,296，正好等於 `power(2,32)`，`uncovered = 0`。三段邊界連續：0–1,431,655,738、1,431,655,739–2,866,311,477、2,866,311,478–4,294,967,295。"
  - q: "replica shard 是備份嗎？K-Safe=0 的時候它有什麼用？"
    a: "不是備份。本機的 replica shard 上只有 5 個 unsegmented projection（`public.customer_dimension_flex2_1_unseg`、`public.xxx_unseg`、`lowi` 與 `lowi_vai` 的 3 個小表），每個節點各 5 個 container、各 100,043 筆、各 2,786,722 bytes，內容完全相同。它的用途是讓「不切 hash 的表」在每個節點都讀得到本機副本；segment shard 的容錯才由 K-Safe 決定，本叢集每個 segment 只有 1 筆 subscription，所以 K-Safe = 0。"
  - q: "怎麼確認資料真的只落在自己的 segment 上？"
    a: "把 `v_monitor.storage_containers` 按節點與 shard 分組：node0001 只有 `segment0001` 的 450 個 container（366,727,801 筆），node0002 只有 `segment0002` 的 484 個（366,526,739 筆），node0003 只有 `segment0003` 的 445 個（366,396,860 筆），三個節點都另有 5 個 replica container。全叢集 1,394 個 container、1,099,951,529 筆，三段筆數最大與最小只差 0.09%。"
---
<!--
AI Summary: 以本機 3 節點 EON 叢集（Vertica v25.4.0-0、K-Safe=0、4 shard）實查 v_catalog.storage_locations（7 筆）、v_catalog.shards（4 筆）、v_catalog.node_subscriptions（6 筆）、v_catalog.namespaces（default_shard_count=3）與 v_monitor.storage_containers（1,394 個 container、1,099,951,529 筆、disk_size 合計 12,047,783,315 bytes），證明三段 segment hash 區間合計 4,294,967,296 = 2^32、缺口 0，replica shard 僅承載 5 個 unsegmented projection（每節點 100,043 筆、2,786,722 bytes），並用 EXPLAIN 的 Participating subscriptions 與 All Nodes Vector 驗證單一連線仍會扇出到三個節點。全程唯讀，未對叢集做任何變更。
AI Keywords: Vertica, EON, communal storage, depot, shard, segment, node subscription, hash ring, K-Safe, storage location, 25.4
-->
<!--more-->

---

## 一、先講結論

EON 最常被誤解的一句話是「資料放在 S3」。這句話只對一半：**資料本體在 communal storage，但你查詢時讀的是 depot**。要判斷一個 EON 叢集的資料在哪裡、有沒有風險，只需要看懂三張表：`v_catalog.storage_locations`、`v_catalog.shards`、`v_catalog.node_subscriptions`。我在本機 3 節點的 eondb（v25.4.0-0、K-Safe=0）上把這三張表連同 `v_monitor.storage_containers` 全部實查了一遍，六個結論：

1. **7 個 storage location 不是 7 份資料。** 實查 7 筆：1 個 communal（`s3://verticadb/eondb`，`sharing_type = COMMUNAL`、`has_catalog = True`）、3 個節點各自的 `DEPOT`、3 個節點各自的 `DATA,TEMP`。只有 communal 那一筆 `has_catalog = True`，也就是 catalog 的權威版本在 S3，不在節點本機。
2. **4 個 shard = 3 個 segment 加 1 個 replica。** `v_catalog.shards` 實查 4 筆，其中 `replica` 沒有 hash 上下界；`v_catalog.namespaces` 的 `default_shard_count` 是 3——它只算資料 segment，不含 replica。兩個數字不衝突，但拿錯一個就會算錯容量。
3. **hash 空間剛好切滿，缺口 0。** 三段區間各 1,431,655,739、1,431,655,739、1,431,655,818 個值，加總 `4,294,967,296`，等於 `power(2,32)`，`uncovered = 0.0`。區間連續、不重疊：`0–1,431,655,738`、`1,431,655,739–2,866,311,477`、`2,866,311,478–4,294,967,295`。
4. **replica shard 不是備份，是「不切 hash 的表」的本機副本。** 它上面只有 5 個 unsegmented projection，三個節點各 5 個 container、各 100,043 筆、各 `2,786,722` bytes，數字完全一致。
5. **資料真的只落在自己的 segment 上。** node0001 只有 `segment0001` 的 450 個 container（366,727,801 筆）、node0002 只有 `segment0002` 的 484 個（366,526,739 筆）、node0003 只有 `segment0003` 的 445 個（366,396,860 筆）。全叢集 1,394 個 container、1,099,951,529 筆，三段筆數最大與最小只差 **0.09%**。
6. **K-Safe=0 可以直接從 subscription 數看出來。** 每個 segment 只有 1 筆 subscription（就是它的 primary），replica 有 3 筆。沒有 buddy 副本，所以是 0-safe——連線時驅動程式就丟了 `WARNING 6046`。

這篇寫給三種人：第一次接觸 EON、被 `storage_locations` 的 7 筆嚇到的人；要估算 depot 容量、卻用錯 shard 數的人；以及做 EON 巡檢、需要知道「該相信哪張表」的人。**本文所有標「實查」的數字都來自 2026-11-13 這次執行；機制面的說明屬官方文件範疇，文末附連結。全程唯讀，沒有對叢集做任何變更。**

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 資料庫 | `eondb`，owner `dbadmin`，`load_balance_policy = none`，`backend_address_family = ipv4` |
| 節點 | 3（`v_eondb_node0001/0002/0003`，10.89.10.11/12/13，`node_state = UP`，`is_readonly = False`） |
| Shard | 4（1 replica + 3 segment） |
| K-Safe | 0（連線時 `WARNING 6046: The database is currently 0-safe`） |
| Communal storage | `s3://verticadb/eondb` |
| 授權與資料量 | raw data `0.10TB`、license `1.00TB`、utilization `10%`、node limit 3 |
| 物件規模 | 使用者表 406 張、catalog 內 projection 486 個、有 container 的 projection 221 個 |
| 容器與筆數 | 1,394 個 ROS container、1,099,951,529 筆、`disk_size` 合計 `12,047,783,315` bytes |
| 存取方式 | `vertica_python` 連 1533（三個入口 1533/2533/3533 指向同一叢集），全部 `SELECT` / `EXPLAIN` |

`get_compliance_status()` 的原始輸出（節錄）：

```
=> SELECT get_compliance_status();

Raw Data Size: 0.10TB +/- 0.00TB
 License Size : 1.00TB
 Utilization  : 10%
 Node count : 3
 License Node limit : 3
 Compliance Status : The database is in compliance with respect to raw data size.
```

這裡先講清楚一件事：**這篇文章沒有建立任何 schema、沒有寫入任何資料**。EON 的架構問題全部可以用唯讀查詢回答，需要改變叢集才能驗證的部分（例如 K-Safe 從 0 改成 1 之後 subscription 怎麼長）我不做，也不會假裝做過。

---

## 三、三個名詞先分清楚

| 名詞 | 回答什麼問題 | 實查來源 | 本機數字 |
|------|--------------|----------|----------|
| storage location | 資料可以放在哪個**路徑** | `v_catalog.storage_locations` | 7 筆 |
| shard | 資料切成幾個**hash 區間** | `v_catalog.shards` | 4 筆（3 segment + 1 replica） |
| node subscription | 哪個**節點**負責哪個 shard | `v_catalog.node_subscriptions` | 6 筆 |
| namespace | shard 屬於哪個**命名空間** | `v_catalog.namespaces` | 1 筆（`default_shard_count = 3`） |
| ROS container | 資料實際的**檔案單位** | `v_monitor.storage_containers` | 1,394 筆 |

這五個東西的關係是：**namespace 決定有幾組 shard；shard 決定 hash 怎麼切；subscription 決定 shard 落在哪個節點；storage location 決定節點把檔案寫到哪個目錄；container 是檔案本身。**

![EON 資料層級：communal storage、三個節點的 depot 與 DATA,TEMP 共 7 個 storage location，以及 3 個 segment shard 切滿 2^32 的 hash 空間]({{ '/assets/images/eon-data-layers.svg' | relative_url }}){: loading="lazy" }

上圖的每個數字都來自這次實查：7 個 location、4 個 shard、三段 hash 值數、每個節點的 container 數與 depot 大小。

---

## 四、7 個 storage location 怎麼讀

```
=> SELECT location_id, node_name, location_path, location_usage, sharing_type, has_catalog
   FROM v_catalog.storage_locations ORDER BY location_usage, node_name, location_id;

     location_id     |     node_name     |              location_path               | location_usage | sharing_type | has_catalog
---------------------+-------------------+------------------------------------------+----------------+--------------+-------------
 45035996273705022   |                   | s3://verticadb/eondb                     | DATA           | COMMUNAL     | t
 45035996273705020   | v_eondb_node0001  | /data/eondb/v_eondb_node0001_data        | DATA,TEMP      | NONE         | f
 45035996273705160   | v_eondb_node0002  | /data/eondb/v_eondb_node0002_data        | DATA,TEMP      | NONE         | f
 45035996273705164   | v_eondb_node0003  | /data/eondb/v_eondb_node0003_data        | DATA,TEMP      | NONE         | f
 45035996273705260   | v_eondb_node0001  | /data/depot/eondb/v_eondb_node0001_depot | DEPOT          | NONE         | f
 45035996273705262   | v_eondb_node0002  | /data/depot/eondb/v_eondb_node0002_depot | DEPOT          | NONE         | f
 45035996273705264   | v_eondb_node0003  | /data/depot/eondb/v_eondb_node0003_depot | DEPOT          | NONE         | f
(7 rows)
```

三個欄位決定這 7 筆怎麼讀，少看一個就會誤判：

**1. `sharing_type`：只有 communal 是 `COMMUNAL`，其餘 6 筆都是 `NONE`。** `COMMUNAL` 代表這個路徑由整個叢集共用（這裡是 S3 bucket），`NONE` 代表只有那一個節點看得到。所以 7 筆裡真正「大家共用」的只有 1 筆。

**2. `location_usage`：`DATA`、`DATA,TEMP`、`DEPOT` 是三種用途。** 注意 communal 那筆是單獨的 `DATA`，不是 `DATA,TEMP`——S3 上不該有暫存檔。三個節點的 `DATA,TEMP` 同時掛了 DATA 與 TEMP 兩種用途，三個 depot 則只有 `DEPOT`。

**3. `has_catalog`：只有 communal 是 `True`。** 這正是 EON 與 Enterprise 最大的差別：**catalog 的權威版本在 communal storage**，節點本機的 catalog 目錄只是工作副本。所以「S3 掛掉」不是「查詢變慢」，是「資料庫沒了」。

`node_name` 對 communal 那筆是空的，不是資料缺失——它是叢集層級的 location，不屬於任何單一節點。

### 4.1 為什麼 `disk_storage` 有 9 筆，`storage_locations` 只有 7 筆

```
=> SELECT node_name, storage_usage, storage_path, storage_status, throughput, latency
   FROM v_monitor.disk_storage ORDER BY node_name, storage_usage, storage_path;

     node_name     | storage_usage |               storage_path                | storage_status | throughput | latency
-------------------+---------------+-------------------------------------------+----------------+------------+---------
 v_eondb_node0001  | CATALOG       | /data/eondb/v_eondb_node0001_catalog/Catalog | Active      | 0          | 0
 v_eondb_node0001  | DATA,TEMP     | /data/eondb/v_eondb_node0001_data            | Active      | 0          | 0
 v_eondb_node0001  | DEPOT         | /data/depot/eondb/v_eondb_node0001_depot     | Active      | 200        | 60
 v_eondb_node0002  | CATALOG       | /data/eondb/v_eondb_node0002_catalog/Catalog | Active      | 0          | 0
 v_eondb_node0002  | DATA,TEMP     | /data/eondb/v_eondb_node0002_data            | Active      | 0          | 0
 v_eondb_node0002  | DEPOT         | /data/depot/eondb/v_eondb_node0002_depot     | Active      | 200        | 60
 v_eondb_node0003  | CATALOG       | /data/eondb/v_eondb_node0003_catalog/Catalog | Active      | 0          | 0
 v_eondb_node0003  | DATA,TEMP     | /data/eondb/v_eondb_node0003_data            | Active      | 0          | 0
 v_eondb_node0003  | DEPOT         | /data/depot/eondb/v_eondb_node0003_depot     | Active      | 200        | 60
(9 rows)
```

9 = 3 節點 × 3 種本機路徑（CATALOG、DATA,TEMP、DEPOT）。`storage_locations` 的 7 筆裡沒有 CATALOG 本機路徑，而 communal 那筆不屬於任何節點，所以兩張表的列數永遠對不上。**要盤點「節點上有幾份本機儲存」看 `disk_storage`；要盤點「叢集的資料路徑有幾種」看 `storage_locations`。**

---

## 五、4 個 shard：3 片資料加 1 份 replica

```
=> SELECT shard_oid, shard_name, shard_type, lower_hash_bound, upper_hash_bound, is_replicated, has_objects
   FROM v_catalog.shards ORDER BY shard_name;

      shard_oid      | shard_name  | shard_type | lower_hash_bound | upper_hash_bound | is_replicated | has_objects
---------------------+-------------+------------+------------------+------------------+---------------+-------------
 45035996273705000   | replica     | Replica    |                  |                  | t             | t
 45035996273705002   | segment0001 | Segment    | 0                | 1431655738       | f             | t
 45035996273705004   | segment0002 | Segment    | 1431655739       | 2863311477       | f             | t
 45035996273705006   | segment0003 | Segment    | 2863311478       | 4294967295       | f             | t
(4 rows)
```

四個 shard 全部 `has_objects = true`，包括 replica——它不是「空的預留位」，上面確實有資料（下一節給數字）。`replica` 的 `is_replicated = true`、`segment*` 全是 `false`，這是兩者語意上最直接的差異：segment 是「切一段 hash」，replica 是「每個節點都放一份」。

### 5.1 hash ring 的數學：三段加起來剛好 2^32

只用加減法就能驗證邊界有沒有缺口：

```
=> SELECT shard_name, lower_hash_bound, upper_hash_bound,
          upper_hash_bound - lower_hash_bound + 1 AS hash_values
   FROM v_catalog.shards WHERE shard_type = 'Segment' ORDER BY shard_name;

  shard_name  | lower_hash_bound | upper_hash_bound | hash_values
--------------+------------------+------------------+-------------
 segment0001  | 0                | 1431655738       |  1431655739
 segment0002  | 1431655739       | 2863311477       |  1431655739
 segment0003  | 2863311478       | 4294967295       |  1431655818
(3 rows)

=> SELECT sum(upper_hash_bound - lower_hash_bound + 1) AS total_covered,
          power(2,32) AS hash_space,
          power(2,32) - sum(upper_hash_bound - lower_hash_bound + 1) AS uncovered
   FROM v_catalog.shards WHERE shard_type = 'Segment';

 total_covered |  hash_space  | uncovered
---------------+--------------+-----------
    4294967296 | 4294967296.0 |       0.0
(1 row)
```

`uncovered = 0.0`，而且三段是連續的（前一段的 upper + 1 = 後一段的 lower），所以沒有「算得出 hash 但找不到 shard」的區間。反過來說，如果哪天有人在 rebalance 之後看到 `uncovered > 0`，那就是 hash ring 沒接好，不用查別的地方。

**邊界值本身是包含的。** `segment0001` 是 `0 ≤ h ≤ 1,431,655,738`，`segment0002` 從 `1,431,655,739` 開始。算區間長度要 `upper - lower + 1`，不是 `upper - lower`；差 1 在單段看起來沒什麼，三段加起來就會出現 3 的缺口，然後你會花半天找一個不存在的 bug。

### 5.2 replica shard 上到底有什麼

```
=> SELECT schema_name, projection_name, count(*) AS containers,
          sum(total_row_count) AS rows, count(DISTINCT node_name) AS nodes
   FROM v_monitor.storage_containers WHERE shard_name = 'replica'
   GROUP BY 1,2 ORDER BY 4 DESC;

 schema_name |         projection_name          | containers |  rows  | nodes
-------------+----------------------------------+------------+--------+-------
 public      | customer_dimension_flex2_1_unseg |          3 | 150000 |     3
 public      | xxx_unseg                        |          3 | 150000 |     3
 lowi        | lowi_user_save_source_unseg      |          3 |    114 |     3
 lowi_vai    | scheduled_task_unseg             |          3 |      9 |     3
 lowi        | lowi_user_save_channel_unseg     |          3 |      6 |     3
(5 rows)
```

5 個 projection、每個 3 個 container、每個都在 3 個節點上——**名字裡的 `_unseg` 就是答案：unsegmented projection 不切 hash，所以放進 replica shard，每個節點各一份。** 換算成每節點的持有量是 100,043 筆、`2,786,722` bytes（50,000 + 50,000 + 38 + 3 + 2 = 100,043）。

這裡有個實務推論：**replica shard 不是備份，也不能拿來算 K-Safe。** 它保護的是「這張表在每個節點都讀得到」，不是「節點掛掉資料還在」。真正決定容錯的是 segment shard 的 subscription 數，而本叢集每個 segment 只有 1 筆。

### 5.3 `default_shard_count = 3` 與 4 個 shard 不衝突

```
=> SELECT namespace_oid, namespace_name, is_default, default_shard_count
   FROM v_catalog.namespaces;

   namespace_oid    |   namespace_name   | is_default | default_shard_count
--------------------+--------------------+------------+---------------------
 45035996273704994  | default_namespace  | t          |                   3
(1 row)
```

`default_shard_count` 是「這個 namespace 的資料要切幾片」，答案是 3；`v_catalog.shards` 數的是 shard 物件，包含 replica，所以是 4。**估算 depot 容量用 3（每節點只放自己那一片），盤點 shard 物件用 4。** 兩個都用錯的場合我看過：拿 4 去乘節點數，會多算 33% 的空間。

---

## 六、6 筆 node subscription：誰是 primary

```
=> SELECT node_name, shard_name, is_primary, subscription_state
   FROM v_catalog.node_subscriptions ORDER BY node_name, shard_name;

     node_name     | shard_name  | is_primary | subscription_state
-------------------+-------------+------------+--------------------
 v_eondb_node0001  | replica     | t          | ACTIVE
 v_eondb_node0001  | segment0001 | t          | ACTIVE
 v_eondb_node0002  | replica     | f          | ACTIVE
 v_eondb_node0002  | segment0002 | t          | ACTIVE
 v_eondb_node0003  | replica     | f          | ACTIVE
 v_eondb_node0003  | segment0003 | t          | ACTIVE
(6 rows)
```

6 筆 = 3 個 segment 各 1 筆 + replica 的 3 筆。讀法：

- **每個 segment 只有 1 筆 subscription，而且 `is_primary = t`。** 沒有第二個節點 subscribe 同一個 segment，所以沒有 buddy 副本，**K-Safe = 0**。這與連線時收到的 `WARNING 6046: The database is currently 0-safe` 完全一致——一個是從 catalog 推出來的，一個是伺服器自己說的，兩個對得上就代表你讀懂了。
- **replica 有 3 筆，只有 node0001 是 `is_primary = t`。** primary 的那一份負責寫入，其餘兩份是同一份資料的副本。這也是為什麼三個節點的 replica container 筆數一模一樣（各 100,043 筆）。
- **`subscription_state` 全部 `ACTIVE`。** 如果有節點出現非 ACTIVE，第一個要看的是節點狀態與 depot，不是查詢語法。

```
=> SELECT s.shard_name, s.shard_type, count(ns.node_name) AS subscriptions,
          sum(CASE WHEN ns.is_primary THEN 1 ELSE 0 END) AS primaries
   FROM v_catalog.shards s
   LEFT JOIN v_catalog.node_subscriptions ns ON ns.shard_name = s.shard_name
   GROUP BY 1,2 ORDER BY 1;

  shard_name  | shard_type | subscriptions | primaries
--------------+------------+---------------+-----------
 replica      | Replica    |             3 |         1
 segment0001  | Segment    |             1 |         1
 segment0002  | Segment    |             1 |         1
 segment0003  | Segment    |             1 |         1
(4 rows)
```

這支查詢值得放進巡檢：`subscriptions = 1` 的 segment 就是沒有容錯的 segment。3 節點叢集裡 K-Safe 最多到 1，而本叢集是 0——**任何一個節點掉，對應那 1/3 的資料就直接不可用**。

---

## 七、Step-by-step：從一個連線看整個叢集

EON 的連線是「一個入口、三個執行者」。從 1533 連進去跑 `EXPLAIN`，計畫裡會直接寫出哪些 subscription 參與：

```
=> EXPLAIN SELECT count(*) FROM online_sales.online_sales_fact WHERE sale_date_key = 100;

+-GROUPBY NOTHING [Cost: 2K, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*)
|  Execute on: All Nodes
| +---> STORAGE ACCESS for online_sales_fact [Cost: 2K, Rows: 5M (ROW COUNT ONLY)] (PATH ID: 2)
| |      Projection: default_namespace.online_sales.online_sales_fact_super
| |      Filter: (online_sales_fact.sale_date_key = 100)
| |      Execute on: All Nodes
```

同一份計畫的 GraphViz 段把節點與 subscription 列出來（節錄）：

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

三個可以直接拿來用的觀察：

1. **`node[0]` 是 initiator，另兩個是 executor。** 你連的是 node0001，但計畫會在三個節點上執行——「連一個節點等於只用一個節點」在 EON 是錯的。
2. **`replica: [<0, 1, 2>]` 對應三個節點索引，segment 各對應一個。** 這跟 `node_subscriptions` 的 6 筆完全對應，等於計畫自己把 shard→node 的地圖印出來了。
3. **`Rows: 5M (ROW COUNT ONLY)`** 表示優化器只有統計上的筆數、沒有實際讀資料——`EXPLAIN` 不會真的掃表，在共用叢集上可以放心跑。

**這裡有個容易踩的坑：不能用 SQL 的 `HASH()` 推算 shard。** 實測 `HASH()` 回傳的是 64-bit 值：

```
=> SELECT hash('abc') AS h1, hash('hello world') AS h2, hash(42) AS h3;

         h1          |         h2         |         h3
---------------------+--------------------+---------------------
 1193732203864995963 |  671172152162102109 | 4619354834034727894
(1 row)
```

`1,193,732,203,864,995,963` 遠大於 shard 上界 `4,294,967,295`（2^32 − 1），所以「`hash(col) BETWEEN lower AND upper`」這種寫法算不出資料會落在哪個 shard。**要判斷落點，看 `v_monitor.storage_containers` 的 `shard_name`／`segment_lower_bound`，或看 `EXPLAIN` 的 Participating subscriptions；不要自己算。**

---

## 八、資料實際落在哪：1,394 個 container

```
=> SELECT node_name, shard_name, count(*) AS containers,
          sum(total_row_count) AS rows, sum(disk_size) AS disk_bytes
   FROM v_monitor.storage_containers
   GROUP BY 1,2 ORDER BY 1,2;

     node_name     | shard_name  | containers |   rows    | disk_bytes
-------------------+-------------+------------+-----------+-------------
 v_eondb_node0001  | replica     |          5 |    100043 |     2786722
 v_eondb_node0001  | segment0001 |        450 | 366727801 |  4014633044
 v_eondb_node0002  | replica     |          5 |    100043 |     2786722
 v_eondb_node0002  | segment0002 |        484 | 366526739 |  4012567490
 v_eondb_node0003  | replica     |          5 |    100043 |     2786722
 v_eondb_node0003  | segment0003 |        445 | 366396860 |  4012222615
(6 rows)
```

這張表是整篇文章最有力的一張，因為它同時證明三件事：

- **每個節點只持有自己那一個 segment 的 container。** node0001 沒有 `segment0002` 的 container，反之亦然。EON 的資料落點就是由 shard 決定，不是由 projection 的 segmentation 決定。
- **三段負載相當平均。** 筆數 366,727,801 / 366,526,739 / 366,396,860，最大與最小差 330,941 筆，**0.09%**；`disk_bytes` 4,014,633,044 / 4,012,567,490 / 4,012,222,615，差距在 0.06% 以內。3 個 segment 的 hash 區間本來就只差 79 個值（1,431,655,818 對 1,431,655,739），所以這種均勻是設計出來的，不是巧合。
- **container 數與筆數不成比例。** node0002 只有 484 個 container 卻有 366,526,739 筆，node0003 445 個 container 也有 366,396,860 筆——**平均每個 container 的筆數不同**，因為 mergeout 的時機與 partition 大小不同。用 container 數推估資料量一定失準。

全叢集彙總：

```
=> SELECT sum(total_row_count) AS rows, count(*) AS containers,
          sum(disk_size) AS disk_bytes, count(DISTINCT projection_id) AS projections
   FROM v_monitor.storage_containers;

    rows     | containers |  disk_bytes  | projections
-------------+------------+--------------+-------------
 1099951529  |       1394 |  12047783315 |         221
(1 row)
```

1,099,951,529 筆、1,394 個 container、`12,047,783,315` bytes（約 12.05 GB），分布在 221 個 projection 上。而 catalog 裡的 projection 是 486 個——**「catalog 有幾個 projection」與「有 container 的 projection 有幾個」是兩個問題**，中間那 265 個沒有 container（設計上存在、還沒實體化，或已被其他 projection 取代）。

### 8.1 不要用 `disk_storage` 的 MB 數算 depot 大小

```
=> SELECT node_name, storage_usage, disk_space_used_mb, disk_space_free_mb, disk_space_free_percent
   FROM v_monitor.disk_storage WHERE storage_usage IN ('DEPOT','DATA,TEMP')
   ORDER BY node_name, storage_usage;

     node_name     | storage_usage | disk_space_used_mb | disk_space_free_mb | disk_space_free_percent
-------------------+---------------+--------------------+--------------------+-------------------------
 v_eondb_node0001  | DATA,TEMP     |             687236 |             213003 | 23%
 v_eondb_node0001  | DEPOT         |             687236 |             213003 | 23%
 v_eondb_node0002  | DATA,TEMP     |             687236 |             213003 | 23%
 v_eondb_node0002  | DEPOT         |             687236 |             213003 | 23%
 v_eondb_node0003  | DATA,TEMP     |             687236 |             213003 | 23%
 v_eondb_node0003  | DEPOT         |             687236 |             213003 | 23%
(6 rows)
```

同一節點的 DEPOT 與 DATA,TEMP 兩列**數字完全相同**（687,236 MB 已用、213,003 MB 可用、23%），因為那三個欄位報的是**檔案系統**，不是那個 location 目錄。真正的 depot 內容只有 `12,047,783,315` bytes（12.05 GB），跟 687,236 MB（671 GB）差兩個數量級。

**結論：`v_monitor.disk_storage` 的空間欄位只能回答「這個檔案系統還剩多少」，不能回答「depot 佔了多少」。** 要算 depot 內容用 `storage_containers` 的 `disk_size`；`throughput = 200`、`latency = 60` 這兩欄是設定的效能參數，不是量測值，也不要當成 I/O 監控數據。

---

## 九、常見錯誤與反模式

**1. 把 7 個 storage location 當成 7 份資料**

看到 `storage_locations` 7 筆就以為資料有 7 份，於是容量規劃全部重算。實際上 6 筆是節點本機路徑（3 depot + 3 data），只有 1 筆 communal 是共用資料。**先看 `sharing_type`，只有 `COMMUNAL` 才是共用。**

**2. 用 `default_shard_count` 或 `count(*) FROM shards` 其中一個算容量**

3 與 4 都來自系統表，但回答的問題不同。每節點只放自己那一段，所以「每節點資料量 ≈ 總量 ÷ 3」；用 4 去除會得到 25% 的錯誤答案。

**3. 用 `HASH()` 推算 shard**

`HASH()` 回 64-bit（實測 `1,193,732,203,864,995,963`），shard 邊界是 32-bit（0–4,294,967,295）。兩者不在同一個空間，硬算只會得到看似合理的錯誤答案。

**4. 用 `disk_storage` 的 MB 當 depot 用量**

見 8.1：那是檔案系統，同節點三列同值。671 GB 與 12.05 GB 的差距足以讓擴容決策完全相反。

**5. 以為 replica shard 是備份，于是不做備份**

replica 上只有 5 個 unsegmented projection（每節點 100,043 筆）；segment 資料只存在 communal 與各節點 depot。K-Safe=0 時掉一個節點就是掉 1/3 的資料可用性，掉 communal 就是全毀。**備份與 replica 是兩件事。**

**6. 在 25.4 用 `LISTAGG` 串節點清單**

想把每節點持有的 shard 串成一行時：

```
ERROR 3457:  Function listagg(varchar, unknown) does not exist,
or permission is denied for listagg(varchar, unknown)
HINT:  No function matches the given name and argument types.
       You may need to add explicit type casts.
```

本機 25.4.0-0 沒有這個函式。監控 SQL 裡想做字串聚合，先確認函式存在，或用 `GROUP BY` 拆成多列（本文第六節就是這樣寫的）。

---

## 十、風險與建議

### 現在就能做（唯讀／低風險）

1. **每天跑一次三段 hash 覆蓋檢查。** `uncovered > 0` 就是 hash ring 有洞，屬 P1，不用等症狀出現。
2. **把「每個 segment 的 subscription 數」放進巡檢。** 有 segment 只剩 1 筆就是 0-safe 的那一段；節點掉之前先知道哪 1/3 會消失。
3. **用 `storage_containers` 而不是 `disk_storage` 做 depot 容量趨勢。** 兩者差兩個數量級，用錯會誤判「depot 快满了」。
4. **把 `disk_space_free_percent` 拉到 20% 以下就告警。** 本機三節點都是 23%，已經接近；depot 滿了會觸發 eviction，查詢變成直接讀 communal（慢），而不是報錯——這種退化最難發現。
5. **核對 `has_catalog`。** 只有 communal 是 `True` 才是正常 EON；節點上出現 `has_catalog = True` 的 location 要查清楚是不是設定錯了。

### 需要變更（要窗口／要核准）

1. **K-Safe 0 → 1。** 需要至少 2 倍該 segment 的空間與足夠節點，會改變 subscription 數與 depot 使用率。**本次沒有做**，本文所有關於 K-Safe 的數字都是從既有 subscription 推出來的，不是改過之後量的。
2. **加節點或 rebalance。** shard 數不變、subscription 會變；任何依賴「segment→node 固定對應」的腳本都要重寫。
3. **改 `DepotSizePercent` 或搬 depot 路徑。** 這是叢集層級變更，共用叢集上不該「試一下」。
4. **建第二個 namespace。** 每個 namespace 有自己的一組 shard，`default_shard_count` 會各自計算；多 namespace 會讓「4 個 shard」這種直覺完全失效。

---

## 十一、參考資料

- Vertica 25.4 文件：Eon Mode architecture — https://docs.vertica.com/25.4.x/en/architecture/eon-mode-architecture/
- Vertica 25.4 文件：Eon Mode storage locations — https://docs.vertica.com/25.4.x/en/admin/managing-storage/storage-locations/
- Vertica 25.4 文件：Shards and subscriptions — https://docs.vertica.com/25.4.x/en/architecture/eon-mode-architecture/shards-and-subscriptions/
- Vertica 25.4 文件：v_catalog.shards — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-tables/shards/
- Vertica 25.4 文件：v_catalog.node_subscriptions — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-tables/node-subscriptions/

> 註：`docs.vertica.com` 目前會 302 導向 `docs.rocketsoftware.com/bundle/vertica_25.4/`，路徑對應相同；本文連結於 2026-11-13 檢查皆回應 200。
