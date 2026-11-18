---
layout: post
title: "Enterprise 轉 EON 遷移檢查表：14 個上線前必須重新檢視的設定"
date: 2026-11-19 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, migration, enterprise-mode, configuration, checklist, operations]
description: "同一台機器上跑著同版本的 testdb（Enterprise，1 節點）與 eondb（EON，3 節點 v25.4.0-0、K-Safe=0、4 shard），把 14 項上線前該重看的設定逐項對照。實測三個結構性落差：storage location 1→7、shard 0→4、depot 每節點 540,144 MB（檔案系統的 60%）；授權是 Community Edition 1TB、節點上限 3，節點擴充會先撞到授權而不是技術。"
image: /assets/images/og/vertica-eon-migration-checklist-zh.png
faq:
  - q: "Enterprise 轉 EON 一定要停機嗎？"
    a: "文件寫得很清楚：MIGRATE_ENTERPRISE_TO_EON 在前景執行，函式返回前會阻擋同一 session 的所有操作，之後還要 revive 一個新的 EON 資料庫，那個階段資料庫必須是 DOWN。本機沒有實際執行（會改動 testdb），但從轉換結果來看，DATA／TEMP,DATA 儲存位置不會被遷移，revive 後新的 DATA 位置就在 depot 路徑上，等於資料要重新落地一次。"
  - q: "為什麼本機 EON 的 depot 是 60% 而不是 80%？"
    a: "v_catalog.storage_locations 顯示每個節點的 depot 上限是 566,381,859,840 bytes（540,144 MB），而檔案系統是 900,239 MB（used 695,635 + free 204,604），相除剛好 60.0%。MaxDepotSizePercent=80 是上限不是實際值；文件也寫預設 60%、最多 80%，因為要留 20% 給 catalog、Data Collector 與暫存檔。"
  - q: "K-Safe=0 的三節點叢集算有高可用嗎？"
    a: "不算。本機 3 節點、4 個 shard、6 筆 subscription，但只有 4 筆是 primary：node0001 持有 replica 與 segment0001 兩個 primary，node0002 與 node0003 各只有一個。K-Safe=0 代表每個 segment shard 只有一個訂閱者，任一節點故障就有三分之一資料不可用；replica shard 只存 unsegmented projection 的 metadata，不是資料複本。"
  - q: "把三個 port 當成三個資料庫，會有什麼問題？"
    a: "實測 1533、2533、3533 分別連到 v_eondb_node0001、node0002、node0003，三個入口回報的版本都是 v25.4.0-0，是同一個叢集。把它們當成三個獨立資料庫，會導致連線全部集中在一台（EON 的 Elastic Throughput Scaling 就失效），也會讓備份與監控的節點對應錯亂。"
  - q: "EON 的 vbr 備份可以還原回 Enterprise 嗎？"
    a: "不行。官方文件明確寫「You cannot back up an Enterprise Mode database and restore it in Eon Mode, or vice versa」。而且 EON 備份必須備到支援的雲端儲存，要設 cloud_storage_backup_path 與 cloud_storage_backup_file_system_path；還原時目標資料庫要同名、同節點名稱、至少同數量的 primary 節點、同 catalog 路徑與同 port。"
---
<!--
AI Summary: 以本機同版本（v25.4.0-0）的 testdb（Enterprise，1 節點）與 eondb（EON，3 節點、K-Safe=0、4 shard）並排實測，整理 Enterprise 轉 EON 上線前必須重看的 14 項設定：storage location 1→7、shard 0→4、depot 540,144 MB/節點（60%）、9 個資源池、3 個連線入口對應 3 個節點、CE 授權 1TB/3 節點。MIGRATE_ENTERPRISE_TO_EON 流程依官方文件整理並標示未實測。
AI Keywords: Vertica, EON, migration, MIGRATE_ENTERPRISE_TO_EON, shard, depot, communal storage, K-Safe, vbr, 25.4
-->
<!--more-->

---

## 一、先講結論

「Enterprise 轉 EON」在文件裡是一條函式 `MIGRATE_ENTERPRISE_TO_EON('s3://bucket', '/depot/path', false)`，在實務上則是 14 個假設的總檢查。這篇不是文件翻譯，是因為這台機器上同時有兩個同版本資料庫可以對照：

| 面向 | `testdb`（Enterprise） | `eondb`（EON） |
|------|------------------------|-----------------|
| 版本 | `Vertica Analytic Database v25.4.0-0` | 同 |
| 節點 | 1 | 3 |
| `v_catalog.shards` | **0 列** | **4 列**（replica + segment0001~0003） |
| `v_catalog.storage_locations` | **1 列** | **7 列** |
| 授權 | Community Edition 1TB、節點上限 3 | 同 |
| 投影數 | 448 | 486 |
| 資料列數 | 1,506,960,224 | 1,099,951,727 |
| 磁碟 | 75.59 GB | 12.05 GB |
| Container | 1,366（258 個投影） | 1,387（221 個投影） |
| 資源池 | 9 | 9（27 列 = 9 × 3 節點） |
| `MergeOutInterval` | 300 | 600 |

（兩邊的資料集不同，列數與磁碟量不能直接比較，只用來估算遷移搬運量。）

實測得到三個結構性落差，其餘 11 項都是繞著這三個轉：

1. **儲存位置從 1 個變 7 個。** `testdb` 只有一個 `DATA,TEMP` 本機路徑；`eondb` 是 1 個 communal（`s3://verticadb/eondb`，`has_catalog = true`）＋ 3 個 `DATA,TEMP` ＋ 3 個 `DEPOT`。catalog 跟著 communal 走，這是 EON 備份與災難復原完全不同的原因。
2. **shard 從 0 變 4。** Enterprise 靠 projection 的 segmentation 決定資料落點，查 `v_catalog.shards` 是空的；EON 的 4 個 shard hash 邊界加起來剛好是 2³²（1,431,655,739 + 1,431,655,739 + 1,431,655,818 = 4,294,967,296），沒有缺口。
3. **depot 從 0 變每節點 540,144 MB。** `max_size = 566,381,859,840 bytes`，除以檔案系統 900,239 MB 剛好 **60.0%**——這正是文件寫的預設值，而 `MaxDepotSizePercent = 80` 只是上限。

14 項裡，**9 項現在就能用唯讀 SQL 查清楚**，3 項要改設定或停機，2 項只能靠文件與演練（EON 的 vbr 備份路徑、跨模式還原）。`MIGRATE_ENTERPRISE_TO_EON` 本機**沒有實際執行**——它會改動 `testdb`，違反本系列「只對自己的 schema 做 DDL」的規則，所以第五節的流程全部標示為文件整理。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Enterprise | `testdb`，1 節點 `v_testdb_node0001`，`PERMANENT` |
| EON | `eondb`，3 節點 `v_eondb_node0001`~`0003`，全部 `PERMANENT`、`is_primary = true` |
| 版本 | 兩邊皆 `v25.4.0-0` |
| EON shard | 4（K-Safe = 0、`ElasticKSafety = 1`、`elastic_cluster.scaling_factor = 4`、`maximum_skew_percent = 15`） |
| EON depot | 每節點 `566,381,859,840` bytes（540,144 MB），`disk_percent = 60%` |
| EON 檔案系統 | used 695,635 MB、free 204,604 MB（22%），合計 900,239 MB（879 GiB） |
| EON communal | `s3://verticadb/eondb`，`sharing_type = COMMUNAL`、`has_catalog = true` |
| 授權 | 兩邊皆 Community Edition、1TB、`node_restriction = 3`、Perpetual |
| 連線 | eondb 用 `connect_eondb.py`（`vertica_python`），入口 192.168.100.9 的 1533／2533／3533 |
| 實測時間 | 2026-11-19 05:00 起（Asia/Taipei） |

**全程唯讀**：只跑 `SELECT`、`SHOW` 與系統表查詢，沒有 `CREATE`、`INSERT`、`ALTER`、`REBALANCE_SHARDS`，也沒有對 `eondb` 執行任何 `vbr`。

---

## 三、核心原理：三個責任轉移

EON 不是「把資料搬到 S3 的 Enterprise」，而是把三件事的責任換人：

| 責任 | Enterprise | EON | 本機實測依據 |
|------|------------|-----|--------------|
| 資料落點 | projection 的 `SEGMENTED BY` | namespace 的 shard | `testdb` shards 0 列；`eondb` 4 列 |
| catalog 位置 | 各節點本機 catalog 目錄 | communal storage | communal 那列 `has_catalog = true` |
| 讀取快取 | 本機 ROS（就是資料） | depot（快取）＋ communal（正本） | `UseDepotForReads = 1`、depot 上限 540,144 MB |

### 3.1 七個 storage location 長什麼樣

```sql
=> SELECT node_name, location_path, location_usage, sharing_type, has_catalog, max_size
   FROM v_catalog.storage_locations ORDER BY location_usage, node_name;
```

```
              | s3://verticadb/eondb                        | DATA      | COMMUNAL | t |          0
v_eondb_node0001 | /data/eondb/v_eondb_node0001_data        | DATA,TEMP | NONE     | f |          0
v_eondb_node0002 | /data/eondb/v_eondb_node0002_data        | DATA,TEMP | NONE     | f |          0
v_eondb_node0003 | /data/eondb/v_eondb_node0003_data        | DATA,TEMP | NONE     | f |          0
v_eondb_node0001 | /data/depot/eondb/v_eondb_node0001_depot | DEPOT     | NONE     | f | 566381859840
v_eondb_node0002 | /data/depot/eondb/v_eondb_node0002_depot | DEPOT     | NONE     | f | 566381859840
v_eondb_node0003 | /data/depot/eondb/v_eondb_node0003_depot | DEPOT     | NONE     | f | 566381859840
```

同樣的查詢在 `testdb` 只有一列：

```
v_testdb_node0001 | /opt/data/testdb/v_testdb_node0001_data | DATA,TEMP | NONE | f |
```

**communal 那列的 `node_name` 是空的**，因為它不是任何一個節點的磁碟；`has_catalog = true` 表示 catalog 也在裡面。這一欄就是「為什麼 EON 的備份不能只備節點」的答案。

### 3.2 shard 與 subscription

```sql
=> SELECT shard_name, shard_type, is_replicated, lower_hash_bound, upper_hash_bound
   FROM v_catalog.shards ORDER BY shard_name;
```

```
replica     | Replica | t |          |
segment0001 | Segment | f |        0 | 1431655738
segment0002 | Segment | f | 1431655739 | 2863311477
segment0003 | Segment | f | 2863311478 | 4294967295
```

```sql
=> SELECT node_name, shard_name, is_primary, subscription_state
   FROM v_catalog.node_subscriptions ORDER BY node_name, shard_name;
```

```
v_eondb_node0001 | replica     | t | ACTIVE
v_eondb_node0001 | segment0001 | t | ACTIVE
v_eondb_node0002 | replica     | f | ACTIVE
v_eondb_node0002 | segment0002 | t | ACTIVE
v_eondb_node0003 | replica     | f | ACTIVE
v_eondb_node0003 | segment0003 | t | ACTIVE
```

6 筆 subscription、4 筆 primary。**replica shard 在三個節點各一份，但只有 node0001 是 primary**；它的用途是存 unsegmented projection 的 metadata，不是資料備份。

---

## 四、14 項檢查表

每一項都給「Enterprise 實測／EON 實測／上線前確認什麼／最常見的誤解」。前三欄有數字的都是本次實跑。

### 14 項總覽

| # | 檢查項目 | 本機實測落差 | 怎麼驗 |
|---|----------|--------------|--------|
| 1 | 資料落點／shard | 0 → 4 個 shard | `v_catalog.shards` |
| 2 | K-Safe | 單節點 → K-Safe 0、6 筆 subscription | `node_subscriptions`、warning 6046 |
| 3 | depot 大小 | 無 → 540,144 MB/節點（60%） | `storage_locations`、`disk_storage` |
| 4 | communal storage | 無 → `s3://verticadb/eondb`、`has_catalog` | `storage_locations` |
| 5 | Tuple Mover | 8,391 次 mergeout → 6 次、無 moveout | `tuple_mover_operations` |
| 6 | 資源池 | 9 池 → 27 列（每節點一份） | `resource_pool_status` |
| 7 | 監控指標 | 1,366 → 1,387 個 container | `storage_containers` |
| 8 | 載入路徑 | 本機 ROS → communal + depot | `load_streams` |
| 9 | 連線入口 | 1 個 → 3 個（各自對應節點） | 三個 port 各連一次 |
| 10 | 授權 | CE 1TB、節點上限 3 | `licenses`、`GET_COMPLIANCE_STATUS()` |
| 11 | 時間同步 | 三節點同時事件差 19 微秒 | `node_states` |
| 12 | 版本升級 | 皆 25.4.0-0；重啟約 45 秒 | `version()`、`node_states` |
| 13 | 備份 | 本機目錄 → 雲端儲存 + 兩個必填參數 | 文件 + `storage_locations` |
| 14 | 災難復原 | 還原 → `revive_db` | 文件 + 演練 |

以下逐項展開。

### 4.1 資料落點：shard 與 segmentation

| | 內容 |
|---|---|
| Enterprise | 448 個投影、417 張表；`elastic_cluster.segment_layout` = `v_testdb_node0001[100.0%]`，local 是 4 × 25% |
| EON | 4 個 shard／3 個節點 = 每節點 1.33 個；node0001 有 2 個 primary，node0002、node0003 各 1 個 |
| 上線前確認 | shard 數在建立 namespace 時就固定；文件建議 shard:node 不超過 2:1（本機 1.33 在範圍內），要改只能用 `RESHARD_DATABASE` |
| 常見誤解 | 「遷移會保留原本的 segmentation」。文件寫轉換結果是「same number of nodes, and an equal number of segmented shards」——shard 數等於節點數，不是原本的投影分段 |

### 4.2 K-Safe 與容錯

| | 內容 |
|---|---|
| Enterprise | 單節點，沒有複本概念；`v_catalog.shards` 0 列 |
| EON | K-Safe = 0：連線時伺服器直接回 `WARNING 6046: The database is currently 0-safe ...`；`ElasticKSafety = 1` 只是允許彈性訂閱的參數，不代表有複本 |
| 上線前確認 | K-Safe 決定每個 shard 有幾個訂閱者。本機 4 個 segment shard 各只有 1 個 primary subscriber，掉一台就少三分之一資料 |
| 常見誤解 | 「3 節點就是 3 份資料」。3 節點只是 3 個運算單元；資料複本要看 subscription 的 `is_primary` 與 shard 數 |

### 4.3 depot 大小與 `DiskSizePercent`

| | 內容 |
|---|---|
| Enterprise | 沒有 DEPOT 類型的 storage location；depot 概念只在 EON 出現 |
| EON | 每節點 `max_size = 566,381,859,840` bytes = 540,144 MB = 檔案系統 900,239 MB 的 **60.0%**；`disk_percent = 60%`、`MaxDepotSizePercent = 80` |
| 上線前確認 | 用「熱資料大小」估 depot，不要用「資料總量」。本機全庫投影只佔 12.05 GB，depot 540 GB，命中率必然 100%——這也是為什麼本機量不到 depot miss |
| 常見誤解 | 把 `MaxDepotSizePercent = 80` 當成 depot 現在的大小。它是上限；實際值看 `storage_locations.max_size` 與 `disk_percent`，要改用 `ALTER_LOCATION_SIZE` |

### 4.4 communal storage 與憑證

| | 內容 |
|---|---|
| Enterprise | 無；資料只在本機 `/opt/data/testdb/...` |
| EON | `s3://verticadb/eondb`、`sharing_type = COMMUNAL`、`has_catalog = true`、`max_size = 0`（無上限，受物件儲存容量限制） |
| 上線前確認 | 物件儲存的頻寬與延遲、bucket 的生命週期政策、備份 bucket 是否同一組憑證可寫。實測 `S3BucketConfig`、`S3BucketCredentials` 都是空字串，代表憑證不在資料庫參數裡（走環境或 instance profile） |
| 常見誤解 | 「communal 只是另一個 DATA 路徑」。`has_catalog = true` 說明 catalog 也在裡面，communal 掉就等於整個資料庫掉 |

### 4.5 Tuple Mover 行為

| | 內容 |
|---|---|
| Enterprise | `MergeOutInterval = 300`；`v_monitor.tuple_mover_operations` 累積 8,391 次 Mergeout Start |
| EON | `MergeOutInterval = 600`；只有 6 次 Mergeout（全部 Complete、0 執行中），對象是 `v_internal_tables.license_consumption_metrics`，**沒有任何 moveout 紀錄** |
| 上線前確認 | EON 的 TM 只對「自己是 primary subscriber」的 shard 做 mergeout；資料上傳 communal 由 epoch 管理（`EpochMapInterval = 180` 秒），不是 moveout |
| 常見誤解 | 以為 EON 不做 mergeout，或以為有 moveout 把 ROS 搬去 communal。實際觀察到的操作名稱只有 Mergeout |

### 4.6 資源池與記憶體

| | 內容 |
|---|---|
| Enterprise | 9 個資源池（`blobdata`、`dbd`、`general`、`jvm`、`metadata`、`recovery`、`refresh`、`sysquery`、`tm`） |
| EON | 同樣 9 個，但 `resource_pool_status` 是 **27 列**（9 × 3 節點）。`general` 實測 `memory_size_kb` 24,278,522／24,288,148／24,286,161（約 23.15 GiB／節點），`maxmemorysize` 是 `Special: 95%`；`tm` 3 GB、`sysquery` 1 GB、`metadata` 122,006／112,380／114,367 KB |
| 上線前確認 | 記憶體是「每節點一份」，三個節點的 general 合計 69.48 GiB 不是一個池。遷移後查詢預算要按單節點算 |
| 常見誤解 | 把 `maxmemorysize` 當成查詢可用記憶體。query budget = 池記憶體 ÷ planned concurrency，`plannedconcurrency` 是 `AUTO` 時由系統推算 |

### 4.7 監控指標

| | 內容 |
|---|---|
| Enterprise | 1,366 個 container、258 個投影；`projection_storage` 448 列、1,506,960,224 列、75.59 GB |
| EON | 1,387 個 container、221 個投影；每節點 456／475／456（最大差 19 個，4.2%）；`projection_storage` 1,302 列、1,099,951,727 列、12.05 GB |
| 上線前確認 | 巡檢要換表：`storage_containers` 看 container 與 shard 分佈、`storage_locations` 看 depot、`node_states` 看重啟、`depot_fetches` 看 communal 讀取 |
| 常見誤解 | 用 `v_monitor.partitions` 估容量。本機只有 819 列、6 個投影有 partition 紀錄，221 個有 container 的投影裡絕大多數沒有 partition 資料 |

### 4.8 資料載入路徑

| | 內容 |
|---|---|
| Enterprise | COPY 寫本機 ROS，載完才是 mergeout |
| EON | COPY 同時寫 communal 與 depot；`v_monitor.load_streams` 4 筆，最大一筆 1,007,772,484 列、6,629,722 ms、rejected 0 → 約 **152,008 列/秒** |
| 上線前確認 | 這條 15.2 萬列/秒是單一 load stream、本機 S3 的實測值；正式環境要按 ETL 主機到 communal 的頻寬重算，並確認 `REJECTED` 與 `parse_complete_percent` |
| 常見誤解 | 以為 EON 載入一定比較慢。慢的是「小批量高頻 commit」，不是 EON 本身；本機這個量體看得出來瓶頸在 commit 與 catalog |

### 4.9 連線入口與 client

| | 內容 |
|---|---|
| Enterprise | `testdb` 單一節點、單一入口，沒有「入口對應哪個節點」的問題 |
| EON | 1533 → `v_eondb_node0001`、2533 → `node0002`、3533 → `node0003`，三次連線都是 `v25.4.0-0`；連線耗時 277 ms（冷）、22 ms、25 ms。節點 `node_address` 是 10.89.10.11~13，入口卻是 192.168.100.9 |
| 上線前確認 | client 要用可路由的入口，並把連線輪詢到三個入口；EON 的 Elastic Throughput Scaling 前提是連線分散，全部連同一台就沒有彈性 |
| 常見誤解 | 「三個 port = 三個資料庫或三份資料」。實測三個入口各自落到不同節點，同一個叢集、同一份 catalog |

### 4.10 授權

| | 內容 |
|---|---|
| Enterprise | Community Edition、1TB、`node_restriction = 3`、`is_size_limit_enforced = true`、Perpetual |
| EON | 同一張授權；`GET_COMPLIANCE_STATUS()` 顯示 raw 0.10TB／1TB = 10%、node count 3／license node limit 3 |
| 上線前確認 | **節點擴充會先撞到授權**：CE 上限 3 節點，本機已經用滿。容量以 raw data 計，不是壓縮後磁碟量（本機 12.05 GB 磁碟對應 0.10TB raw） |
| 常見誤解 | 以為授權看磁碟用量，所以壓縮率高就可以超用。審計看的是 raw data size，`license_audits` 每天 23:59 跑一次 |

### 4.11 時間同步

| | 內容 |
|---|---|
| Enterprise | `SHOW TimeZone` 回 `UCT`，時間欄位帶 `+00:00` |
| EON | 同樣 `UCT`；`v_monitor.node_states` 顯示 2026-11-13 23:00:02 三節點同時 SHUTDOWN，時間戳分別是 `.922700`、`.922702`、`.922719`——**最大差 19 微秒** |
| 上線前確認 | EON 的 epoch、catalog 版本與備份時間點都依賴節點時鐘；遷移前先確認三台 chrony/NTP 同源、offset 在毫秒級 |
| 常見誤解 | 以為差幾秒無所謂。19 微秒是這台機器的實況，不是所有環境的保證；時鐘漂移會讓 epoch 與備份/還原的時間點對不上 |

### 4.12 版本升級

| | 內容 |
|---|---|
| Enterprise | `v25.4.0-0`；`MIGRATE_ENTERPRISE_TO_EON` 要求來源版本 ≥ 10.0 |
| EON | `v25.4.0-0`；`node_states` 顯示 2026-11-13 23:00:02 SHUTDOWN → 23:00:47 UP，三節點重啟約 45 秒 |
| 上線前確認 | 先升級 Enterprise 到目標版本再遷移；跨模式還原不支援（Enterprise 備份不能還原到 EON，反之亦然），還原到更高版本最多差一個 minor |
| 常見誤解 | 「先遷移再升級比較快」。跨模式 restore 被文件明確禁止，先遷移只會把升級與架構變更綁在同一個窗口 |

### 4.13 備份

| | 內容 |
|---|---|
| Enterprise | `vbr` 備到本機或遠端主機目錄（第八篇實測過 `init`／`backup`／`listbackup`／還原到 `vbr_demo`） |
| EON | 必須備到支援的雲端儲存，且要設 `cloud_storage_backup_path` 與 `cloud_storage_backup_file_system_path`；HDFS communal 不支援 `copycluster` |
| 上線前確認 | 還原條件：同資料庫名稱、同節點名稱、至少同數量的 primary 節點、同 catalog 目錄、同 port。物件級還原要求 shard 數與邊界一致，所以 `REBALANCE_SHARDS` 之後要用新的完整備份 |
| 常見誤解 | 「communal 裡有資料就不用備份」。communal 被刪、憑證失效或 bucket 政策誤刪，資料與 catalog 一起消失；in-db restore point 也在 communal 裡 |

> 本篇**沒有**對 `eondb` 執行任何 `vbr` 操作，EON 段落為官方文件整理加上實測的 `storage_locations`／`shards` 佐證。

### 4.14 災難復原

| | 內容 |
|---|---|
| Enterprise | 還原到同節點數、同節點名的叢集；`vbr restore` 需要資料庫 DOWN |
| EON | 兩條路：`vbr restore`（跨叢集、需符合同名同節點條件）或 `revive_db` 從 communal 復活；`CLEAN_COMMUNAL_STORAGE(true)` 清掉失敗還原留下的檔案 |
| 上線前確認 | 演練 `revive_db` 到臨時叢集，不要覆蓋生產；確認 communal 的版本與加密金鑰可取得 |
| 常見誤解 | 以為 EON 有 communal 就等於有 DR。communal 是「正本」不是「備份」，備份要另一個 bucket／另一組憑證 |

---

## 五、`MIGRATE_ENTERPRISE_TO_EON` 實際流程（文件整理，未實測）

資料庫管理員最常問的一句是「所以到底要停多久」。官方流程是五段：檢查前置 → 合規驗證 → 執行遷移 → 檢查結果 → 啟用 EON 資料庫。以下每一步都標明是文件或本機實測。

**Step 0：前置檢查（可在 `testdb` 唯讀執行）**

```sql
-- 版本 >= 10.0、節點狀態、elastic cluster
=> SELECT version();
Vertica Analytic Database v25.4.0-0

=> SELECT is_enabled, scaling_factor FROM v_catalog.elastic_cluster;
 t | 4
```

`testdb` 的 `elastic_cluster` 是 `t`、`scaling_factor = 4`，符合文件要求的「來源資料庫必須是 elastic cluster」。文件說 9.2.1 之後建立的資料庫預設就是，本機符合。

**Step 1：四種 EON 不接受的投影**

文件列了四類會讓遷移 rollback 的投影：

| 原因 | 文件給的解法 |
|------|--------------|
| 與叢集 segmentation 不一致（例如加過節點） | `REBALANCE_CLUSTER()` 或 `REBALANCE_TABLE()` |
| 用 `NODES` 選項建立、不支援 elastic segmentation | 刪掉重建，改用 `ALL NODES` |
| 使用 `GROUPED` 子句 | 刪掉重建，不加 `GROUPED` |
| 資料在 unbundled storage container（7.2 之前建立） | `COMPACT_STORAGE()` |

本機 `testdb` 有 448 個投影、1,506,960,224 列、75.59 GB。這四類投影我用目錄查得出來「有幾個投影」，但**無法在單節點上驗證 elastic segmentation 的差異**（所有投影都只在一個節點），所以正式環境一定得先跑一次 dry run 讓它產出 `migrate_enterprise_to_eon_error.log`。

**Step 2：dry run**

```sql
=> SELECT migrate_enterprise_to_eon('s3://dbbucket', '/vertica/depot', true);
```

最後一個參數 `true` 代表只驗合規、不執行。有問題會寫進資料庫目錄下的 `migrate_enterprise_to_eon_error.log`。**這一步不改資料，是整個流程裡最該先做的。**

**Step 3：執行遷移**

```sql
=> SELECT migrate_enterprise_to_eon('s3://dbbucket', '/vertica/depot', false);
```

文件寫明：函式在前景執行，返回前會阻擋同一 session 的所有操作；中斷（client 斷線、網路中斷）會讓遷移失敗，重跑會先比對 communal 已有的資料，只補未處理的部分。也就是說**可以續傳，但不能在遷移期間繼續寫入**。進度看 `v_monitor.database_migration_status` 的 `phase`／`status`／`bytes_to_transfer`／`bytes_transferred`。

**Step 4：轉換結果（這裡最容易誤判）**

| 物件 | 遷移後 |
|------|--------|
| 節點數／segmented shard 數 | 與來源相同（本機 testdb 1 節點 → 1 shard） |
| 多個 buddy 投影 | 每組只遷一個 |
| `USER`、`TEMP` storage location | 遷移 |
| `DATA`、`DATA,TEMP` storage location | **不遷移**，新的預設位置在 depot 同一路徑 |
| fault group、storage policy | **不遷移** |
| external procedure | **不遷移** |
| 網路設定（load balance group、routing rule、subnet） | **不遷移** |
| depot 大小 | **revive 後預設 80%**（不是來源的 60%） |

最後一列是本文最想提醒的：本機 EON 現在是 60%（540,144 MB），但從 Enterprise 遷過去的資料庫 revive 後預設是 80%。上線後要記得 `ALTER_LOCATION_SIZE` 調回來，否則 catalog 與暫存檔只剩 20% 的空間可以呼吸。

**Step 5：啟用**

1. `admintools -t revive_db --communal-storage-location=... -d <db> -s <nodes>`（或 MC）。
2. 檢查 `admintools.conf` 的 `controlmode`；S3/AWS 走 unicast，要用 `point-to-point`，原本 `broadcast` 的叢集要 `admintools -t re_ip -d <db> -T`。
3. 啟動資料庫。
4. `CLEAN_COMMUNAL_STORAGE(true)` 清掉遷移留下的檔案。
5. 重新檢視 depot 大小與 shard 數（必要時 `RESHARD_DATABASE`）。

---

## 六、常見錯誤與反模式

**錯誤 1：以為 shard 數之後再調就好。**
shard 數在 namespace 建立時決定；`RESHARD_DATABASE` 會產生新的 shard 邊界，storage container 在對齊前，查詢要多做一層過濾。本機 4 個 shard 對 3 個節點是 1.33，算是舒服的比例；如果你打算遷移後立刻加節點，先算好 shard 數再遷。

**錯誤 2：把 K-Safe=0 的三節點當成有 HA。**
實測 6 筆 subscription、4 筆 primary，segment0002 只有 node0002 一個訂閱者。node0002 掉，三分之一的 segment 就沒有 primary。要 K-Safe ≥ 1，得先有足夠的 shard 與 subscription，這是建立資料庫時的決定，不是事後 `ALTER`。

**錯誤 3：把 `MaxDepotSizePercent = 80` 當成 depot 現值。**
現值是 `storage_locations.max_size`：本機 566,381,859,840 bytes。80% 是上限，實際 60%。要看「還剩多少」請用 `v_monitor.disk_storage` 的 `disk_space_free_mb`（本機 204,604 MB、22%），不是看參數。

**錯誤 4：以為遷移會把 DATA/TEMP、fault group、external procedure 一起帶過去。**
文件列得很清楚：這些都不遷移。上線後才發現「怎麼沒有 fault group」是最常見的退版原因之一。

**錯誤 5：以為遷移後 depot 還是 60%。**
revive 後預設 80%。以本機 900,239 MB 的檔案系統算，80% 是 720,191 MB，只剩 180,048 MB 給 catalog 與暫存。文件要求至少留 20%，剛好貼著上限。

**錯誤 6：以為 communal 有資料就不用備份。**
communal 是正本。備份要另一個 bucket，且要有 `cloud_storage_backup_path` 與 `cloud_storage_backup_file_system_path` 兩個參數。in-db restore point 也在 communal 裡，communal 沒了它也沒了。

---

## 七、風險與建議

**現在就能做（唯讀、低風險）**

1. 在來源資料庫跑本文 14 項裡的 9 條唯讀查詢，把 `storage_locations`、`shards`、`resource_pool_status`、`disk_storage`、`license_audits` 的輸出存成遷移前 baseline。
2. 確認 `elastic_cluster.is_enabled = t`（本機 `testdb` 是 `t`）。
3. 量測節點時鐘：用 `v_monitor.node_states` 裡同時發生的事件比對時間戳（本機最大差 19 微秒），或直接比對 NTP offset。
4. 用 `v_monitor.projection_storage` 估算搬運量（本機 `testdb` 1,506,960,224 列、75.59 GB），再乘上物件儲存的實測頻寬，得到遷移時間的下限。
5. 先跑 `migrate_enterprise_to_eon(..., true)` dry run，讀 `migrate_enterprise_to_eon_error.log`。

**需要變更（要窗口、要核准）**

1. 真正執行 `MIGRATE_ENTERPRISE_TO_EON`：會在前景阻擋寫入，且要復活一個新的 EON 資料庫。
2. 調 depot 大小（`ALTER_LOCATION_SIZE`）：本機目前 60%，遷移後預設變 80%，要主動調回。
3. 提高 K-Safe 或增加節點：本機 CE 授權節點上限 3 且已用滿，加節點前先處理授權。
4. 建立 EON 備份路徑與憑證：另一個 bucket、另一組 access key，不能用 communal 那組「順便」。

**不建議**

在沒有完整備份、沒有 `revive_db` 演練、沒有 dry run 記錄的情況下直接遷移。這篇的所有 EON 數字都是唯讀查出來的；遷移與復原段落標示為文件整理，本機沒有執行。

---

## 八、參考資料

- Migrating an enterprise database to Eon Mode（25.4）：<https://docs.vertica.com/25.4.x/en/eon/migrating-an-enterprise-db-to-eon/>
- Eon Mode database requirements（備份／還原）：<https://docs.vertica.com/25.4.x/en/admin/backup-and-restore/eon-db-requirements/>
- Namespaces and shards：<https://docs.vertica.com/25.4.x/en/architecture/eon-concepts/shards-and-subscriptions/>
- Resizing depot caching capacity：<https://docs.vertica.com/25.4.x/en/eon/depot-management/resizing-depot-caching-capacity/>
- CREATE LOCATION（DEPOT 的 60%／80% 規則）：<https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-statements/create-location/>
- REBALANCE_SHARDS：<https://docs.vertica.com/25.4.x/en/sql-reference/functions/management-functions/eon-functions/rebalance-shards/>
- Tuple mover：<https://docs.vertica.com/25.4.x/en/admin/managing-db/tuple-mover/>

---

*本文數字的來源：2026-11-19 在本機 `testdb`（Enterprise，1 節點）與 `eondb`（EON，3 節點、K-Safe=0、4 shard）的唯讀查詢，兩者皆 Vertica v25.4.0-0。沒有對任何叢集做變更，也沒有執行 `MIGRATE_ENTERPRISE_TO_EON` 或 `vbr`。*
