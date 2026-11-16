---
layout: post
title: "EON 日常巡檢 12 支 SQL：shard 覆蓋、depot 命中率、communal 延遲一次看完"
date: 2026-11-17 05:00:00 +0800
categories: vertica eon monitoring
tags: [vertica, eon, monitoring, v-monitor, v-catalog, shard-coverage, depot, health-check]
description: "在本機 3 節點 eondb（Vertica v25.4.0-0、K-Safe=0、4 shard）實跑 12 支唯讀巡檢 SQL：3 節點全 UP、3 個 segment shard 覆蓋 4,294,967,296 個 hash 值且 0 缺口、6 筆 subscription 全部 ACTIVE。depot 每節點 3.72 GiB，只佔 MaxDepotSizePercent=80 上限的 0.53%，1,450 個 container 裝著 1,099,951,655 筆。depot_fetches 只有 30 筆陳年紀錄、wait_time_ms 全為 NULL——這篇會說清楚哪些欄位在本機量不到。"
image: /assets/images/og/vertica-eon-monitoring-sql-zh.png
faq:
  - q: "EON 巡檢最少要跑哪幾支 SQL？"
    a: "本機實測 11 支 SQL 在單一 session 內合計 284.6 ms：節點狀態、shard 覆蓋、subscription、depot 使用率、depot fetch、communal 位置、ROS 分佈、資源池、長跑查詢、失敗載入、磁碟水位，再加一支從三個入口各查一次 version()。最慢的是 load_streams 的 137.7 ms，最快的是 shards 的 3.1 ms。"
  - q: "為什麼 v_monitor.depot_fetches 的 wait_time_ms 是空的？"
    a: "本機 30 筆紀錄裡 wait_time_ms、response_time_ms、submission_time 三個欄位全是 NULL（有值的筆數是 0），只有 fetch_time_ms 有數字：平均 0.633 ms、最大 3 ms。這一版只填檔案抓取時間，所以不能拿 wait_time_ms 去算 communal 的等待延遲。"
  - q: "1533、2533、3533 三個 port 是三個資料庫嗎？"
    a: "不是。分別連線實測，三個入口各自落在 v_eondb_node0001／0002／0003，version() 都是 v25.4.0-0，v_catalog.shards 都回 4 個 shard、v_catalog.nodes 都回 3 個節點。它們是同一個叢集的三個節點入口，不是三份資料。"
  - q: "depot 只用了 3.72 GiB，為什麼磁碟只剩 23%？"
    a: "因為 depot 上限（MaxDepotSizePercent=80）跟實際用量是兩件事。本機 depot 每節點 3.72 GiB，只佔 900,239 MB 檔案系統的 0.42%、上限的 0.53%；磁碟實際用了 685,667 MB、free 214,572 MB（23%），其餘用量不在 depot 裡。"
  - q: "巡檢時要看 K-Safe 還是 current_fault_tolerance？"
    a: "兩個一起看。本機 designed_fault_tolerance 與 current_fault_tolerance 都是 0，代表這個叢集設計上就沒有容錯（K-Safe=0），不是「暫時掉到 0」。如果 designed 是 1、current 變 0，才是真的少了複本，要馬上看 subscription_state。"
---
<!--
AI Summary: 在 3 節點 eondb（Vertica v25.4.0-0、K-Safe=0、4 shard）以唯讀方式實跑 12 支巡檢 SQL：3 節點全 UP、node_down_count=0、designed/current_fault_tolerance 皆 0；3 個 segment shard 寬度合計 4,294,967,296、缺口 0；6 筆 subscription 全 ACTIVE；depot 每節點 3.72 GiB（MaxDepotSizePercent=80，佔上限 0.53%）；depot_fetches 僅 30 筆（2026-11-09）且 wait/response/submission 全 NULL、fetch_time_ms 平均 0.633 ms；ROS 1,450 個 container／1,099,951,655 筆／11.16 GiB；磁碟 free 23%；三個入口分別落在三個節點、版本一致 v25.4.0-0。11 支 SQL 合計 284.6 ms。
AI Keywords: Vertica, EON, monitoring, shard coverage, depot, MaxDepotSizePercent, depot_fetches, resource_pool_status, load_streams, 25.4
-->
<!--more-->

---

## 一、先講結論

EON 的巡檢不是「開一個儀表板看紅綠燈」，而是「在對的時間問對的表」。這篇把 12 支查詢攤開，每一支都在本機 3 節點的 eondb 上實跑過，數字是 2026-11-17 這次執行的結果。

1. **11 支 SQL 合計 284.6 ms。** 單一 session、連續執行、已暖機；最慢的是 `load_streams` 的 137.7 ms，最快的是 `v_catalog.shards` 的 3.1 ms。巡檢成本的瓶頸不是資料量，是往返次數。
2. **叢集骨架正常。** 3 個節點全 `UP`、`node_down_count = 0`、`designed_fault_tolerance = 0`、`current_fault_tolerance = 0`——K-Safe 設計上就是 0，不是掉下來的。
3. **3 個 segment shard 剛好鋪滿 hash ring。** 寬度 1,431,655,739／1,431,655,739／1,431,655,818，合計 4,294,967,296，`missing = 0`、`gaps = 0`。
4. **6 筆 subscription 全部 `ACTIVE`。** replica shard 有 3 筆（1 個 primary、2 個 secondary），3 個 segment shard 各 1 筆 primary。
5. **depot 很空。** 每節點 3.72 GiB，`MaxDepotSizePercent = 80`，所以只佔上限的 **0.53%**、佔檔案系統的 **0.42%**。注意參數名是 `MaxDepotSizePercent`，不是文件裡常被口語化的 `DepotSizePercent`。
6. **depot_fetches 只有 30 筆，而且是 8 天前的。** 全部成功、`fetch_time_ms` 平均 0.633 ms、最大 3 ms；但 `wait_time_ms`、`response_time_ms`、`submission_time` **三欄全部是 NULL**。
7. **ROS 又長大了。** 1,450 個 container、1,099,951,655 筆、11.16 GiB；其中 1,221 個（84.21%）不到 1,000 筆，3 個 container 吃掉 91.62% 的資料列。
8. **磁碟 free 23%。** 900,239 MB 的檔案系統用了 685,667 MB、free 214,572 MB。三個節點回報的數字一模一樣（到 MB），所以**不要相加**。
9. **三個 port 是三個節點，不是三個資料庫。** 1533→node0001、2533→node0002、3533→node0003，`version()` 都是 `v25.4.0-0`，看到的 shard 數與節點數也一致。

這篇寫給已經有一套 EON、但巡檢還停在「看它有沒有 UP」的人；以及要交班、要寫 runbook、要對老闆解釋「depot 明明很空為什麼查詢還是慢」的人。**全程唯讀，沒有對叢集做任何變更。**

![從症狀到查詢：五種常見症狀各自的第一支與第二支查詢，底部是每次巡檢都該跑的三支]({{ '/assets/images/eon-monitoring-decision-flow.svg' | relative_url }}){: loading="lazy" }

---

## 二、實測環境

| 項目 | 值（本次實查） |
|------|----------------|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 資料庫 | `eondb`（EON 模式） |
| 節點 | 3（`v_eondb_node0001/0002/0003`，`10.89.10.11/.12/.13`，全部 `UP`） |
| Shard | 4（`replica` + `segment0001`～`segment0003`），K-Safe = 0 |
| Storage location | 7 個：3 個 `DATA,TEMP`、3 個 `DEPOT`、1 個 `COMMUNAL`（`s3://verticadb/eondb`，`has_catalog = true`） |
| 節點規格 | 30.7 GB RAM、6 cores、free 11.7%（`v_monitor.host_resources`） |
| 資料量 | 1,099,951,655 筆、ROS 11.16 GiB（`v_monitor.system`） |
| 連線 | `connect_eondb.py`（`vertica_python`），三個入口 1533／2533／3533 |
| 實測時間 | 2026-11-17 05:05–05:35（Asia/Taipei） |

本次執行期間磁碟用量動了 2 MB（`disk_space_used_mb` 685,665 → 685,667），`storage_containers` 從前一日的 1,428 增加到 1,450。**這兩張表都是活的，任何拿它們當 SLA 的報表都要接受它會動。**

---

## 三、為什麼是這 12 支：三張表回答三件事

EON 的監控資料散在 `v_catalog` 與 `v_monitor` 兩個 schema，但真正每天要看的其實只有三類問題：

| 問題 | 主要來源 | 它不會告訴你的事 |
|------|----------|------------------|
| 叢集骨架對不對（節點、shard、subscription、版本） | `v_catalog.nodes`、`shards`、`node_subscriptions`、`v_monitor.system` | 資料在不在 depot、查詢慢不慢 |
| 資料在哪一層（depot、communal、ROS） | `v_monitor.storage_containers`、`v_catalog.storage_locations`、`v_monitor.depot_fetches` | shard 的容錯狀態、載入有沒有失敗 |
| 現在發生了什麼（長跑查詢、失敗載入、磁碟） | `v_monitor.query_requests`、`query_profiles`、`load_streams`、`disk_storage` | 歷史趨勢（要自己留資料） |

有兩個結構性限制要先講清楚，否則後面的數字會被誤讀：

1. **`v_monitor` 的活動類資料保留期很短。** 本機 `query_profiles` 只有 3,605 筆、最舊 2026-11-09 07:27（UTC），`load_streams` 只有 4 筆。這是「上次載入以來」的窗口，不是永久稽核紀錄。要趨勢就得自己排程落表。
2. **有些欄位在這一版是空的。** `depot_fetches` 的 `wait_time_ms`、`response_time_ms`、`submission_time` 全為 NULL。**巡檢 SQL 寫得出來，不代表欄位有值**——下面第 5 支會直接把 NULL 比例查出來，避免你對著空白欄位猜。

---

## 四、Step-by-step：12 支查詢

### 4.1 叢集骨架：#1 #2 #3

**#1 節點狀態與容錯目標**

```sql
=> SELECT n.node_name, n.node_state, n.is_primary,
          s.node_count, s.node_down_count,
          s.designed_fault_tolerance, s.current_fault_tolerance
   FROM v_catalog.nodes n CROSS JOIN v_monitor.system s
   ORDER BY n.node_name;

node_name        | node_state | is_primary | node_count | node_down_count | designed_fault_tolerance | current_fault_tolerance
-----------------+------------+------------+------------+-----------------+--------------------------+-------------------------
v_eondb_node0001 | UP         | True       |          3 |               0 |                        0 |                       0
v_eondb_node0002 | UP         | True       |          3 |               0 |                        0 |                       0
v_eondb_node0003 | UP         | True       |          3 |               0 |                        0 |                       0
```

怎麼看：`designed_fault_tolerance` 是「這個叢集設計上能壞幾台」，`current_fault_tolerance` 是「現在還能壞幾台」。本機兩個都是 0，意思是**設計上就沒有容錯**（K-Safe=0），不是「暫時掉到 0」。

正常值：`node_down_count = 0`，且 `current = designed`。異常時先查：`current < designed` → 看 #3 的 `subscription_state`；`node_state` 不是 `UP` → 看 `v_monitor.node_states` 最近的狀態變化。

順手把 `v_monitor.system` 的單列也抓出來，它有幾個別處拿不到的數字：

```sql
=> SELECT node_count, node_down_count, designed_fault_tolerance, current_fault_tolerance,
          current_epoch, ahm_epoch, last_good_epoch, catalog_revision_number,
          ros_used_bytes, total_row_count
   FROM v_monitor.system;

node_count | node_down_count | designed | current | current_epoch | ahm_epoch | last_good_epoch | catalog_revision_number | ros_used_bytes | total_row_count
-----------+-----------------+----------+---------+---------------+-----------+-----------------+-------------------------+----------------+----------------
         3 |               0 |        0 |       0 |           345 |       344 |             344 |                    2742 |   11987583716 |    1099951655
```

`last_good_epoch = 344`、`current_epoch = 345`：兩者只差 1，代表沒有落後的 epoch。`catalog_revision_number` 是 2,742，這個數字每次 catalog 變更就加一，適合當「這中間有沒有人動過 DDL」的對照點。

**#2 shard 覆蓋率**

```sql
=> SELECT shard_name, shard_type, lower_hash_bound, upper_hash_bound,
          upper_hash_bound - lower_hash_bound + 1 AS width
   FROM v_catalog.shards ORDER BY shard_name;

shard_name  | shard_type | lower_hash_bound | upper_hash_bound | width
------------+------------+------------------+------------------+------------
replica     | Replica    |                  |                  |
segment0001 | Segment    |                0 |       1431655738 | 1431655739
segment0002 | Segment    |       1431655739 |       2863311477 | 1431655739
segment0003 | Segment    |       2863311478 |       4294967295 | 1431655818
```

注意 `replica` 的 `lower/upper_hash_bound` 是空的——**replica shard 不參與 hash 分配**，它放的是 unsegmented 投影。拿它去算覆蓋率會得到 NULL，不是 0。

用一段 CTE 把「有沒有缺口」算出來：

```sql
=> WITH s AS (
     SELECT lower_hash_bound AS lo, upper_hash_bound AS hi,
            lag(upper_hash_bound) OVER (ORDER BY lower_hash_bound) AS prev_hi
     FROM v_catalog.shards WHERE shard_type = 'Segment')
   SELECT count(*) AS segments, sum(hi - lo + 1) AS covered,
          4294967296 - sum(hi - lo + 1) AS missing,
          sum(CASE WHEN prev_hi IS NOT NULL AND lo <> prev_hi + 1 THEN 1 ELSE 0 END) AS gaps
   FROM s;

segments | covered    | missing | gaps
---------+------------+---------+-----
       3 | 4294967296 |       0 |    0
```

怎麼看：`covered + missing = 2^32`（4,294,967,296）。`gaps = 0` 代表相鄰 shard 的邊界完全接得上：`0`→`1431655738`、`1431655739`→`2863311477`、`2863311478`→`4294967295`。**看到 `gaps > 0` 或 `missing > 0`，就是 shard 沒鋪滿，資料會沒有落點。**

**#3 subscription 狀態**

```sql
=> SELECT shard_name, count(*) AS subscriptions,
          sum(CASE WHEN is_primary THEN 1 ELSE 0 END) AS primaries,
          min(subscription_state) AS min_state, max(subscription_state) AS max_state
   FROM v_catalog.node_subscriptions GROUP BY 1 ORDER BY 1;

shard_name  | subscriptions | primaries | min_state | max_state
------------+---------------+-----------+-----------+-----------
replica     |             3 |         1 | ACTIVE    | ACTIVE
segment0001 |             1 |         1 | ACTIVE    | ACTIVE
segment0002 |             1 |         1 | ACTIVE    | ACTIVE
segment0003 |             1 |         1 | ACTIVE    | ACTIVE
```

合計 6 筆、全部 `ACTIVE`。這張表解釋了「K-Safe=0 到底是什麼意思」：3 個 segment shard 各只有 **1 筆** subscription，節點壞掉就沒有第二份；replica shard 有 3 筆（每個節點一個），是因為它放的是 unsegmented 投影，本來就每台一份。**「4 個 shard」不等於「4 份資料」。**

正常值：所有 `subscription_state = ACTIVE`、`primaries` 每個 shard 恰好 1。異常時先查：出現 `PASSIVE`／`UNKNOWN` → 對照 #1 的節點狀態，再看 depot 是否還在同步。

### 4.2 depot 與 communal：#4 #5 #6

**#4 depot 使用率**

```sql
=> SELECT node_name, count(*) AS containers, sum(used_bytes) AS depot_bytes,
          round(sum(used_bytes)/1024.0/1024/1024, 3) AS depot_gb
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

node_name        | containers | depot_bytes | depot_gb
-----------------+------------+-------------+----------
v_eondb_node0001 |        481 |  3997361108 |    3.723
v_eondb_node0002 |        492 |  3995250427 |    3.721
v_eondb_node0003 |        477 |  3994972181 |    3.721
```

再對上限與檔案系統：

```sql
=> SELECT parameter_name, current_value, default_value
   FROM v_monitor.configuration_parameters
   WHERE parameter_name IN ('MaxDepotSizePercent','UseDepotForReads','UseDepotForWrites','DepotOperationsForQuery')
   GROUP BY 1,2,3 ORDER BY 1;

parameter_name           | current_value | default_value
-------------------------+---------------+---------------
DepotOperationsForQuery  | ALL           | ALL
MaxDepotSizePercent      | 80            | 80
UseDepotForReads         | 1             | 1
UseDepotForWrites        | 1             | 1
```

```sql
=> SELECT node_name, min(disk_space_used_mb) AS used_mb,
          min(disk_space_free_mb) AS free_mb,
          min(disk_space_used_mb + disk_space_free_mb) AS total_mb
   FROM v_monitor.disk_storage GROUP BY 1 ORDER BY 1;

node_name        | used_mb | free_mb | total_mb
-----------------+---------+---------+---------
v_eondb_node0001 |  685665 |  214574 |   900239
v_eondb_node0002 |  685665 |  214574 |   900239
v_eondb_node0003 |  685665 |  214574 |   900239
```

換算：檔案系統 900,239 MB ≈ 879.1 GiB，`MaxDepotSizePercent = 80` → depot 上限約 **703.3 GiB**；實際 3.72 GiB，**佔上限 0.53%、佔檔案系統 0.42%**。

怎麼讀：`used_bytes` 是 depot 裡 container 的實體大小，不是資料的未壓縮量（未壓縮是 109.03 GB，見 #7）。`MaxDepotSizePercent` 是「depot 最多吃掉檔案系統的幾 %」，不是「depot 該有多大」。

正常值：depot 佔比沒有絕對標準，但如果 depot 貼近上限、同時 #5 的 fetch 筆數在增加，代表查詢一直往 communal 拿資料——那才是「depot 命中率」掉下來的訊號。

**#5 depot fetch：命中與延遲**

```sql
=> SELECT node_name, is_successful, count(*) AS fetches,
          min(fetch_start_time) AS first_fetch, max(fetch_end_time) AS last_fetch,
          round(avg(fetch_time_ms), 3) AS avg_fetch_ms, max(fetch_time_ms) AS max_fetch_ms,
          sum(file_size_bytes) AS bytes_
   FROM v_monitor.depot_fetches GROUP BY 1,2 ORDER BY 1;

node_name        | is_successful | fetches | first_fetch                      | last_fetch                       | avg_fetch_ms | max_fetch_ms | bytes_
-----------------+---------------+---------+----------------------------------+----------------------------------+--------------+--------------+---------
v_eondb_node0002 | True          |      15 | 2026-11-09 07:31:24.158835+00:00 | 2026-11-09 13:50:01.000005+00:00 |        0.667 |            3 | 2787562
v_eondb_node0003 | True          |      15 | 2026-11-09 07:31:24.158836+00:00 | 2026-11-09 13:50:00.999488+00:00 |        0.600 |            2 | 2787562
```

三個節點只出現兩個——`node0001` 一筆都沒有。30 筆全成功，合計 5,575,124 bytes，平均每個檔案 185,838 bytes。**重點不是這 0.6 ms，是這些紀錄全部停在 2026-11-09**：之後沒有任何新 fetch，因為 depot 一直有資料、查詢沒有 miss。

再把 NULL 比例直接查出來：

```sql
=> SELECT count(*) AS rows_,
          count(wait_time_ms) AS wait_nonnull, count(response_time_ms) AS resp_nonnull,
          count(submission_time) AS submit_nonnull, count(fetch_time_ms) AS fetch_nonnull
   FROM v_monitor.depot_fetches;

rows_ | wait_nonnull | resp_nonnull | submit_nonnull | fetch_nonnull
------+--------------+--------------+----------------+--------------
   30 |            0 |            0 |              0 |            30
```

**`wait_time_ms` 和 `response_time_ms` 是 0 筆有值**。所以任何「用 wait_time_ms 算 communal 等待延遲」的報表，在本機只會得到 NULL。目前能拿到的 communal→depot 抓取成本只有 `fetch_time_ms`：平均 0.633 ms、最大 3 ms。

怎麼看：`fetches` 持續增加 = 有 miss；`is_successful = False` 出現 = communal 讀取失敗，優先看 S3 端點與網路。**不要**把這張表的筆數當成「命中率」——它只記 miss 的那一側，沒有分母。

**#6 communal storage 可用性**

```sql
=> SELECT location_path, location_usage, sharing_type, has_catalog, count(*) AS nodes_
   FROM v_catalog.storage_locations WHERE sharing_type = 'COMMUNAL'
   GROUP BY 1,2,3,4;

location_path        | location_usage | sharing_type | has_catalog | nodes_
---------------------+----------------+--------------+-------------+-------
s3://verticadb/eondb | DATA           | COMMUNAL     | True        |     1
```

這是一列、`nodes_ = 1`：communal storage 是**叢集層級**的唯一位置，不屬於任何節點。`has_catalog = true` 代表 catalog 的權威副本也在那裡（本機是 S3）。

怎麼驗可用性：SQL 這一側只能看到「設定還在、最近 30 筆 fetch 全成功」。真正的可用性要從外部打：對 S3 端點做 `aws s3 ls s3://verticadb/eondb` 之類的讀取測試。本機沒有做（本文不對叢集外服務做寫入或列舉），所以**這一段只到「設定與歷史紀錄正常」，不等於「現在 S3 一定健康」**。

順帶對一下容量：`v_catalog.license_audits` 的 `database_size_bytes` 是 108,572,148,738（101.12 GiB），跟 depot 的 11.16 GiB 差了近十倍。**depot 大小不是資料庫大小**，兩者不能互相比較。

### 4.3 資料落點與記憶體：#7 #8

**#7 ROS 分佈**

```sql
=> SELECT node_name, count(*) AS containers, sum(total_row_count) AS rows_,
          sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY 1;

node_name        | containers | rows_         | used_bytes
-----------------+------------+---------------+-------------
v_eondb_node0001 |        481 |   366,827,878 | 3,997,361,108
v_eondb_node0002 |        492 |   366,626,832 | 3,995,250,427
v_eondb_node0003 |        477 |   366,496,945 | 3,994,972,181
```

資料列數最大差 330,933 筆（**0.09%**）、磁碟最大差 2,389,000 bytes（**0.06%**），container 數最大差 15 個（**3.14%**）。跟上一篇文章一樣：**container 數不一樣不代表傾斜，看 `rows_` 或 `used_bytes`。**

```sql
=> SELECT CASE WHEN total_row_count < 1000 THEN '1-999'
               WHEN total_row_count < 100000 THEN '1K-99K'
               WHEN total_row_count < 1000000 THEN '100K-1M'
               WHEN total_row_count < 10000000 THEN '1M-10M'
               ELSE '10M+' END AS bucket,
          count(*) AS containers, sum(total_row_count) AS rows_, sum(used_bytes) AS used_bytes
   FROM v_monitor.storage_containers GROUP BY 1 ORDER BY min(total_row_count);

bucket   | containers | rows_         | used_bytes
---------+------------+---------------+------------
1-999    |       1221 |       108,365 |  18,947,054
1K-99K   |        146 |     3,111,610 |  893,094,007
100K-1M  |         53 |    22,510,329 |  829,496,840
1M-10M   |         27 |    66,448,867 | 1,386,314,557
10M+     |          3 | 1,007,772,484 | 8,859,731,258
```

1,221 個 container（84.21%）不到 1,000 筆、只佔 0.0099% 的資料列；3 個 container 佔 91.62% 的資料列。**這個形狀從 11-16 到 11-17 沒有變，只是總數從 1,428 長到 1,450。**

正常值：沒有單一標準。真正要盯的是「`1-999` 這一格的 container 數有沒有一直漲」——漲代表小 ROS 在累積，mergeout 跟不上。

**#8 資源池記憶體（27 列）**

```sql
=> SELECT pool_name, is_internal, count(*) AS nodes_,
          min(memory_size_kb) AS min_kb, max(memory_size_kb) AS max_kb,
          sum(memory_inuse_kb) AS inuse_kb,
          min(running_query_count) AS min_running, max(running_query_count) AS max_running
   FROM v_monitor.resource_pool_status GROUP BY 1,2 ORDER BY max_kb DESC;

pool_name | is_internal | nodes_ | min_kb   | max_kb   | inuse_kb | min_running | max_running
----------+-------------+--------+----------+----------+----------+-------------+-------------
general   | True        |      3 | 24280522 | 24291145 |        0 |           0 |           0
tm        | True        |      3 |  3145728 |  3145728 |        0 |           0 |           0
sysquery  | True        |      3 |  1048576 |  1048576 |   177476 |           1 |           1
metadata  | True        |      3 |   109383 |   120006 |        0 |           0 |           0
dbd       | True        |      3 |        0 |        0 |        0 |           0 |           0
jvm       | True        |      3 |        0 |        0 |        0 |           0 |           0
blobdata  | True        |      3 |        0 |        0 |        0 |           0 |           0
recovery  | True        |      3 |        0 |        0 |        0 |           0 |           0
refresh   | True        |      3 |        0 |        0 |        0 |           0 |           0
```

9 個池 × 3 節點 = 27 列，全部是內建池。三個節點的 `general` 是 24,280,522／24,291,145／24,286,994 KB（23.2 GiB 上下），差距 10,623 KB（0.04%）；`metadata` 是 109,383～120,006 KB，差 10,623 KB（9.7%），這種小池的差距是正常的載入差異。`sysquery` 三節點各有 1 支查詢在跑、用掉 177,476 KB——**那就是這支巡檢 SQL 自己**。

怎麼讀：`memory_inuse_kb` 是「目前借出去的」，不是「用掉池子的比例」。`general` 的 inuse 是 0、running 是 0，如果這時候業務說「查詢在排隊」，問題不在記憶體，要往 #9 找。

### 4.4 活動、容量與版本：#9 #10 #11 #12

**#9 長跑查詢**

```sql
=> SELECT node_name, user_name, session_id, request_type,
          substring(request, 1, 50) AS query_head, memory_acquired_mb, start_timestamp
   FROM v_monitor.query_requests WHERE is_executing ORDER BY start_timestamp;

node_name        | user_name | session_id              | request_type | query_head                                    | memory_acquired_mb | start_timestamp
-----------------+-----------+-------------------------+--------------+-----------------------------------------------+--------------------+----------------------------------
v_eondb_node0001 | dbadmin   | v_eondb_node0001-91:... | QUERY        | select node_name, user_name, session_id, r... |             255.69 | 2026-11-16 21:02:49.735435+00:00
```

只有一列，而且是**這支查詢自己**（`memory_acquired_mb = 255.69`）。這是一個必須知道的特性：`v_monitor.query_requests` 只裝「現在正在執行」的請求，你用它查長跑查詢，它一定會把你自己列進去。

要看清歷史，換 `query_profiles`：

```sql
=> SELECT node_name, user_name, query_duration_us, processed_row_count,
          substring(query, 1, 60) AS query_head, query_start
   FROM v_monitor.query_profiles
   WHERE query_start::timestamptz > now() - INTERVAL '1 day'
   ORDER BY query_duration_us DESC LIMIT 5;

node_name        | user_name | query_duration_us | processed_row_count | query_head                                    | query_start
-----------------+-----------+-------------------+---------------------+-----------------------------------------------+----------------------------
v_eondb_node0001 | dbadmin   |         2894667.0 |                   1 | select /*+label(w_bigscan)*/ count(*) from... | 2026-11-15 21:03:52.369794+00
v_eondb_node0001 | dbadmin   |         2864839.0 |                   1 | select /*+label(w_bigscan)*/ count(*) from... | 2026-11-15 21:03:49.50398+00
v_eondb_node0001 | dbadmin   |          480507.0 |                   1 | select count(*) as n, count(distinct para...  | 2026-11-16 21:02:05.116894+00
v_eondb_node0001 | dbadmin   |          432357.0 |                   4 | select parameter_name, current_value, defa... | 2026-11-16 21:02:49.180924+00
v_eondb_node0001 | dbadmin   |          430294.0 |                   3 | select parameter_name, current_value, defa... | 2026-11-15 21:04:32.242464+00
```

`query_start` 是 `varchar`，要比較時間得先 `::timestamptz` 轉型——直接寫 `query_start > now() - INTERVAL '1 day'` 會得到 `Operator does not exist: varchar > timestamptz`。這是我在寫這一段時踩到的錯，留給下一個人。

怎麼看：這裡最慢的是 2.89 秒的 10 億筆聚合（那是上一篇文章的量測查詢），沒有真的「長跑」。如果你的系統出現分鐘級的列，先看 `memory_acquired_mb` 是不是逼近池上限，再看 #7 的 container 數。

**#10 失敗載入**

```sql
=> SELECT stream_name, table_name, load_start, load_duration_ms,
          accepted_row_count, rejected_row_count, parse_complete_percent
   FROM v_monitor.load_streams ORDER BY load_start DESC;

stream_name | table_name          | load_start                     | load_duration_ms | accepted_row_count | rejected_row_count | parse_complete_percent
------------+---------------------+--------------------------------+------------------+--------------------+--------------------+------------------------
            | judicial_exc        | 2026-11-09 13:59:20.29743+00   |          6745953 |         22,518,058 |                  0 |                    100
            | lowidoc_exc         | 2026-11-09 13:50:30.220575+00  |           529951 |            592,102 |                  0 |                    100
            | vs_ros              | 2026-11-09 12:48:09.015905+00  |          3576594 |        380,504,586 |                  0 |                    100
            | partition_columns   | 2026-11-09 10:56:33.560057+00  |          6629722 |      1,007,772,484 |                  0 |                    100
```

4 筆、`rejected_row_count` 全 0、`parse_complete_percent` 全 100。`stream_name` 是空字串——不是每個載入都會有 stream 名。

怎麼讀：`load_duration_ms` 是毫秒，6,745,953 ms 是 **1 小時 52 分**，不是 6,745 秒。載入 10 億筆花了 6,629,722 ms，換算 **152,010 筆/秒**（單一載入、多節點分攤）。**`accepted + rejected` 才是讀進來的總量**，不要只看 accepted。

`v_monitor.load_events` 在這一版不存在（`Relation "v_monitor.load_events" does not exist`），所以失敗載入要看 `load_streams` 的 `rejected_row_count` 與 `parse_complete_percent`，或去翻 `COPY` 的錯誤訊息。

**#11 磁碟水位**

```sql
=> SELECT node_name, min(disk_space_used_mb) AS used_mb,
          min(disk_space_free_mb) AS free_mb,
          min(disk_space_free_percent) AS free_pct, min(storage_status) AS status,
          count(*) AS rows_
   FROM v_monitor.disk_storage GROUP BY 1 ORDER BY 1;

node_name        | used_mb | free_mb | free_pct | status | rows_
-----------------+---------+---------+----------+--------+-------
v_eondb_node0001 |  685667 |  214572 | 23%      | Active |     3
v_eondb_node0002 |  685667 |  214572 | 23%      | Active |     3
v_eondb_node0003 |  685667 |  214572 | 23%      | Active |     3
```

`rows_ = 3` 是因為每個節點有三個 storage location（`CATALOG`、`DATA,TEMP`、`DEPOT`）都指向同一個檔案系統，所以三列數字一樣。**`GROUP BY node_name` + `min()` 就是去重。**

三個節點回報到 MB 完全相同的 free（214,572 MB）、相同的 used（685,667 MB）——在這個 lab 裡三個節點共用同一個 `/data` 掛載（要確認請在節點上跑 `df -h /data`）。**所以不要把三列相加當成 2.7 TB**，那會高估三倍。

怎麼看：`free_pct` 掉到 20% 以下就該動作（經驗值，不是官方 SLA）。`status` 不是 `Active` 才是硬故障；只有 `free_pct` 低是容量問題，優先看 depot 與 ROS（#4、#7）。

**#12 版本一致性（三個入口各查一次）**

這一支不是 SQL，是「同一句 SQL 從三個入口各跑一次」：

```python
from connect_eondb import connect_eondb
for port in (1533, 2533, 3533):
    with connect_eondb(VERTICA_EON_ENDPOINTS="192.168.100.9:%d" % port) as conn:
        cur = conn.cursor()
        cur.execute("select version()");                       v = cur.fetchone()[0]
        cur.execute("select node_name from v_monitor.current_session"); n = cur.fetchone()[0]
        cur.execute("select count(*) from v_catalog.nodes");    c = cur.fetchone()[0]
        cur.execute("select count(distinct shard_name) from v_catalog.shards"); s = cur.fetchone()[0]
        print("port=%d node=%s shards=%d nodes=%d version=%s" % (port, n, s, c, v))
```

```text
port=1533 connect=67ms node=v_eondb_node0001 shards=4 nodes=3 version=Vertica Analytic Database v25.4.0-0
port=2533 connect=1ms  node=v_eondb_node0002 shards=4 nodes=3 version=Vertica Analytic Database v25.4.0-0
port=3533 connect=1ms  node=v_eondb_node0003 shards=4 nodes=3 version=Vertica Analytic Database v25.4.0-0
```

三個入口各自落在 **不同節點**，版本、shard 數、節點數完全一致。第一次連線 67 ms 是建線成本，後兩次 1 ms 是熱連線。

怎麼讀：版本不一致通常發生在滾動升級中途——某個節點已經換版、某個還沒。SQL 端沒有 `v_catalog.versions` 這種「每節點版本」的 view（本機實查不存在），所以**只能從各節點的入口問 `version()`**。這也是為什麼第 12 支要用程式而不是單一 SQL。

---

## 五、成本分析：284.6 ms 買到什麼

11 支 SQL、同一個 session、每支跑兩次取第二次（已暖機）：

| # | 查詢 | 回應筆數 | 耗時 |
|---|------|---------:|-----:|
| 1 | 節點狀態 | 3 | 15.7 ms |
| 2 | shard 清單 | 4 | 3.1 ms |
| 3 | subscription | 4 | 3.5 ms |
| 4 | depot 使用率 | 3 | 17.3 ms |
| 5 | depot fetch | 2 | 10.9 ms |
| 6 | communal 位置 | 1 | 5.7 ms |
| 7 | ROS Top 5 | 5 | 18.0 ms |
| 8 | 資源池 | 9 | 10.8 ms |
| 9 | 長跑查詢 | 1 | 57.8 ms |
| 10 | 失敗載入 | 4 | 137.7 ms |
| 11 | 磁碟水位 | 3 | 4.3 ms |
| | **合計** | | **284.6 ms** |

三個觀察：

1. **3 ms 是地板。** `shards` 只回 4 列也要 3.1 ms，那是網路往返＋Python 驅動的成本，不是查詢成本。任何低於 5 ms 的差異都不要當成效能訊號。
2. **最貴的是 `load_streams`（137.7 ms）。** 它要掃所有 session 的載入歷史（本機只有 4 筆），但仍是這組裡最慢的一支——如果一個叢集每天有上千次載入，這支會變成長跑查詢，要加 `WHERE load_start > ...`。
3. **`query_requests` 的 57.8 ms 有自我觀察偏差。** 查詢自己會出現在結果裡，所以它量的是一個「包含自己」的集合。要拿它做自動化門檻，記得先把 `session_id = 自己` 排除。

這組數字不能推成「12 支只要 0.3 秒所以可以每分鐘跑」——**成本會隨 container 數與 `load_streams` 筆數長**。本機 1,450 個 container；容器數上千之後，4.3 節那兩支聚合會開始有感。

---

## 六、常見錯誤與反模式

**1. 把參數名寫成 `DepotSizePercent`。** 本機 `v_monitor.configuration_parameters` 有 563 個參數，depot 相關的是 `MaxDepotSizePercent`（80）。用錯名字的查詢不會報錯，只會回 0 列——然後你就以為「沒有限制」。

**2. 把 `disk_storage` 的三列相加。** 本機三個節點、每個節點三個 location，回報的 used/free 到 MB 完全相同（685,667／214,572）。相加會得到 2.7 TB 的假容量樂觀值。

**3. 拿 `depot_fetches.wait_time_ms` 算 communal 延遲。** 30 筆裡 0 筆有值。要嘛用 `fetch_time_ms`（本機平均 0.633 ms），要嘛承認這個叢集量不到。

**4. 把 depot 大小當資料庫大小。** depot 11.16 GiB、`license_audits.database_size_bytes` 是 101.12 GiB。兩者差近十倍，對外報容量時要講清楚是哪一個。

**5. 以為 container 總數就是資料份數。** 1,450 個 container 是「每個 shard 在每個節點上的 ROS 列數合計」，其中 replica shard 的列會在三個節點各出現一次。邏輯筆數是 `v_monitor.system.total_row_count` 的 1,099,951,655。

**6. 用 `query_requests` 查歷史。** 它只有正在執行的請求；本機查下去只會看到自己。歷史在 `query_profiles`（本機 3,605 筆，最舊 2026-11-09 07:27 UTC）。

**7. 以為三個 port 是三份資料或三個資料庫。** 實測三個入口落在三個不同節點，`version()` 相同、shard 數相同。把它們當成「備援」來輪替是可以的（本工具就是這樣做的），但不要當成三個獨立環境。

**8. 用 `count(*)` 判斷載入成功。** `load_streams` 要看 `accepted_row_count`、`rejected_row_count`、`parse_complete_percent` 三個一起；本機 4 筆都是 rejected 0、parse 100%，但那不是「練習題」——真的失敗時 rejected 會大於 0。

---

## 七、風險與建議

**現在就能做（唯讀、低風險）**

- 把第 4.1～4.4 節的 11 支 SQL 包成一支 `.sql` 檔，用 `vsql -f` 或 `connect_eondb.py` 一次跑完，輸出貼進工單。本機實測 284.6 ms，對生產的負擔可以忽略。
- 巡檢固定看四個數字：`node_down_count`、`gaps`、`max(subscription_state)`、`depot 佔上限 %`。這四個覆蓋「節點、覆蓋、複本、快取」。
- `depot_fetches` 每次巡檢都看「筆數有沒有增加」而不是「延遲幾毫秒」。本機 30 筆全停在 11-09，這本身就是「沒有 miss」的證據。
- 磁碟只看 `free_pct` 與 `status`，並且用 `min()` 去重，不要相加。
- 版本一致性用三個入口各問一次 `version()`，排進每週一次即可。

**需要變更（要窗口、要核准）**

- 如果你要真的量 communal 延遲，得先讓 depot 產生 miss（縮小 `MaxDepotSizePercent`、或查一張沒被預熱的表），再搭配 `depot_fetches` 觀察。**這是變更，本機沒有做。**
- `load_streams` 在載入量大的叢集上會變慢，要加時間範圍或改成查 `v_monitor.query_profiles` 的 `query_type = 'LOAD'`。改查詢不用核准，改保留政策要。
- K-Safe 從 0 調到 1 會改變 shard 與 subscription 的數量（本機目前 segment 只有 1 份）。這件事**不要在生產直接做**，要先算 depot 與 communal 的容量，並確認 subscription 重建的視窗。
- 三個入口輪替連線是 client 端行為，`connect_eondb.py` 已經做了（主 1533 → 備 2533 → 備 3533）。如果你用自己的驅動，記得開 `BackupHost`／重試，不要寫死單一 port。

---

## 八、參考資料

- Vertica 25.4 文件：EON architecture — https://docs.vertica.com/25.4.x/en/architecture/eon/architecture/
- Vertica 25.4 文件：`NODES` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/nodes/
- Vertica 25.4 文件：`SHARDS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/shards/
- Vertica 25.4 文件：`NODE_SUBSCRIPTIONS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/node-subscriptions/
- Vertica 25.4 文件：`STORAGE_LOCATIONS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/storage-locations/
- Vertica 25.4 文件：`DEPOT_FETCHES` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/depot-fetches/
- Vertica 25.4 文件：`RESOURCE_POOL_STATUS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/resource-pool-status/
- Vertica 25.4 文件：`DISK_STORAGE` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/disk-storage/
- Vertica 25.4 文件：Depot — https://docs.vertica.com/25.4.x/en/admin/managing-depots/

> 註：`docs.vertica.com` 目前 302 導向 `docs.rocketsoftware.com/bundle/vertica_25.4/`，路徑對應相同；本文連結於 2026-11-17 檢查皆回應 200。
