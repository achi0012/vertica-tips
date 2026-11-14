---
layout: post
title: "EON 三節點的資源池與記憶體實況：27 筆 resource pool 狀態怎麼讀"
date: 2026-11-15 05:00:00 +0800
categories: vertica eon performance
tags: [vertica, eon, resource-pool, memory, v-monitor, general-pool, concurrency]
description: "在本機 3 節點 eondb（v25.4.0-0、K-Safe=0、4 shard）實查 v_monitor.resource_pool_status 的 27 列（9 個內建池 × 3 節點）：每個節點的池記憶體總和都剛好是 28,594,832 KB（27.27 GiB），其中 general 與 metadata 相加固定 24,400,528 KB，general 的每查詢預算（query budget）是 3,844,520 KB（3.67 GiB）。實測一條掃 10 億筆的聚合查詢，general 池峰值 3,844,519 KB，與預算只差 1 KB；同時跑兩條則是 7,688,856 KB，剛好兩倍、耗時從 13.57 秒變成 31.90 秒。"
image: /assets/images/og/vertica-eon-resource-pools-memory-zh.png
faq:
  - q: "EON 的記憶體是叢集共用，還是每個節點各一份？"
    a: "每個節點各一份，而且不能相加。本機 3 節點的 `v_monitor.resource_pool_status` 共 27 列，把 27 列的 `memory_size_kb` 全部加起來是 85,784,496 KB（81.81 GiB），但真正的每節點預算只有 28,594,832 KB（27.27 GiB），三個節點相加的三倍就是那個 81.81 GiB。"
  - q: "query budget（查詢預算）是怎麼算出來的？"
    a: "實測 27 列後逐池驗算，與官方文件的公式一致：general 是 `queueing_threshold ÷ planned_concurrency`，也就是 24,281,180 × 95% ÷ 6 = 3,844,520 KB；tm 是 `MEMORYSIZE ÷ 併發` = 3,145,728 ÷ 7 = 449,389 KB；sysquery 是 1,048,576 ÷ 6 = 174,762 KB；dbd／refresh／recovery 的 MEMORYSIZE 是 0，改用 general 的 23,067,120 KB 再除以自己的併發數（4／4／8）。"
  - q: "為什麼三個節點的 general 大小不一樣，總和卻一樣？"
    a: "因為 metadata 池的實際需求不同，而 general 是「剩下的都給我」的池。本機 general 分別是 24,281,180／24,294,544／24,295,238 KB，metadata 是 119,348／105,984／105,290 KB，兩者相加在三個節點都是 24,400,528 KB。同一節點觀察 13 分鐘，general 少 9,722 KB、metadata 正好多 9,722 KB。"
  - q: "一條查詢在 EON 上最多能用多少記憶體？"
    a: "預設是每個參與的節點各發一份 query budget。本機 general 池的預算是 3,844,520 KB，實測一條掃 1,007,772,484 筆的聚合查詢，三個節點的 general 池峰值是 3,844,519／3,846,635／3,846,745 KB，幾乎貼著預算；同時跑兩條就是 7,688,856／7,693,270／7,693,490 KB，剛好兩倍，兩條各跑 31.90 秒（單條 13.57 秒）。"
---
<!--
AI Summary: 在 3 節點 eondb（Vertica v25.4.0-0、K-Safe=0、4 shard）以唯讀查詢解讀 v_monitor.resource_pool_status 的 27 列：9 個內建池 × 3 節點，每節點池記憶體總和固定 28,594,832 KB（27.27 GiB，佔主機 30.71 GiB 的 88.81%），general+metadata 固定 24,400,528 KB；逐池驗算 query budget 與官方公式一致（general 3,844,520 KB、tm 449,389 KB、sysquery 174,762 KB、jvm 332,049 KB、dbd/refresh 5,766,780 KB、recovery 2,883,390 KB）；10 億筆聚合查詢實測 13.57 秒、general 峰值 3,844,519 KB，兩條並行 7,688,856 KB、各 31.90 秒；同一主機的 Enterprise testdb 池總和亦為 28,594,832 KB 但 general 併發 4、每查詢預算 6,076,547 KB。全程唯讀。
AI Keywords: Vertica, EON, resource pool, query budget, planned concurrency, general pool, memory, v_monitor, 25.4
-->
<!--more-->

---

## 一、先講結論

`v_monitor.resource_pool_status` 在 EON 上會回 27 列。這個數字不是巧合，是 9 個內建池 × 3 個節點。問題是大部分人打開這張表之後，會做出兩個錯誤的動作：把 27 列的記憶體加起來當成「叢集記憶體」，以及把 `memory_size_kb` 當成「這個池最多可以用多少」。我在本機 3 節點的 eondb（v25.4.0-0、K-Safe=0、4 個 shard）上把 27 列逐格讀完，並用一條掃 10 億筆的查詢驗證，七個結論：

1. **27 列不能相加。** 全部加起來是 `85,784,496` KB（81.81 GiB），但每個節點的池預算只有 `28,594,832` KB（27.27 GiB）——27 列剛好是三倍。記憶體在 EON 是每節點一份，不是叢集共用。
2. **三個節點的總和一模一樣，組成卻不一樣。** 總和都是 `28,594,832` KB；但 `general` 是 `24,281,180` / `24,294,544` / `24,295,238`，`metadata` 是 `119,348` / `105,984` / `105,290`，兩者相加在三個節點都剛好是 `24,400,528` KB。
3. **general 是「剩下的都給我」的池，所以它會動。** 同一節點觀察 13 分鐘，general 少了 `9,722` KB，metadata 正好多了 `9,722` KB。看到 general 的數字在變不代表有人在改設定。
4. **query budget 不是上限，是每條查詢的起始預算。** general 的 `query_budget_kb` 是 `3,844,520`（3.67 GiB）；一發下去就整份配給查詢，所以 `memory_inuse_kb` 會貼著它，而不是「用到多少算多少」。
5. **八個有預算的池，逐池驗算全部對得上官方公式。** general：`24,281,180 × 95% ÷ 6 = 3,844,520`；tm：`3,145,728 ÷ 7 = 449,389`；sysquery：`1,048,576 ÷ 6 = 174,762`；jvm：`2,097,152 × 95% ÷ 6 = 332,049`；dbd／refresh：`23,067,120 ÷ 4 = 5,766,780`；recovery：`23,067,120 ÷ 8 = 2,883,390`。只有 metadata 與 blobdata 沒有預算（NULL）。
6. **實測貼著預算跑。** 一條 `GROUP BY` 掃 `1,007,772,484` 筆的查詢，13.57 秒完成，三個節點的 general 池峰值 `3,844,519` / `3,846,635` / `3,846,745` KB，與各自的預算差 1 KB 以內；同時跑兩條是 `7,688,856` / `7,693,270` / `7,693,490` KB，剛好兩倍，但單條時間從 13.57 秒變成 31.90 秒。
7. **同樣 30.71 GiB 主機上的 Enterprise 資料庫，池總和一模一樣。** `testdb`（單節點、同版本、主機記憶體同為 `32,969,703,424` bytes）的 9 個池總和也是 `28,594,832` KB，但分配不同：general 的併發是 4，每查詢預算 `6,076,547` KB（5.80 GiB），是 EON 這邊的 1.58 倍。

這篇寫給三種人：巡檢時把 27 列加起來、發現「叢集有 81 GiB 記憶體」的人；看到查詢明明只要幾百 MB、`memory_inuse_kb` 卻吃到 3.8 GB 而懷疑漏記憶體的人；以及被問「EON 三個節點，每個節點記憶體怎麼分」時想拿真實數字回答的人。**所有標「實查」的數字都來自 2026-11-15 這次執行；全程唯讀，沒有對叢集做任何變更。**

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 資料庫 | `eondb`（EON 模式） |
| 節點 | 3（`v_eondb_node0001/0002/0003`，全部 `UP`） |
| Shard | 4（1 replica + 3 segment），K-Safe = 0 |
| Communal storage | `s3://verticadb/eondb` |
| 主機 | `Intel(R) Core(TM) i5-9400 @ 2.90GHz`、1 socket / 6 cores、記憶體 `32,969,703,424` bytes（30.71 GiB） |
| 資源池 | 9 個，`is_internal = true` 全部為內建池（本叢集沒有自訂池） |
| 對照組 | 同機 `testdb`（Enterprise 模式、單節點、同版本 v25.4.0-0） |
| 存取方式 | `eondb` 走 `vertica_python`（1533/2533/3533 三個入口指向同一叢集）、`testdb` 走 `vsql-testdb`，全部 `SELECT` |

EON 那邊的「主機」是三台不同 IP（`10.89.10.11`／`.12`／`.13`），三台的 CPU 與記憶體規格完全相同，這也是後面「三個節點的池總和一致」的前提。如果叢集是混規格，這一節的結論要重算。

---

## 三、核心原理：兩層記帳，先分池、再分查詢

讀這張表只需要記住兩層：

| 層 | 回答什麼 | 實查欄位 | 本機數字（node0001） |
|----|----------|----------|----------------------|
| 節點 → 池 | 這台機器把記憶體切成幾份 | `memory_size_kb` | 9 個池加總 `28,594,832` KB |
| 池 → 查詢 | 一條查詢進來能拿多少 | `query_budget_kb` | general `3,844,520` KB |
| 池的滿水位 | 超過就排隊 | `queueing_threshold_kb` | general `23,067,120` KB（= size × 95%） |
| 池的硬上限 | 不能再借 | `max_memory_size_kb` | general `24,281,180` KB；tm `27,546,256` KB |

一句話版本：**`memory_size_kb` 是「這個池分到多少」，`query_budget_kb` 是「這個池的每條查詢分到多少」，`queueing_threshold_kb` 是「這個池用到多少之後新查詢開始排隊」。** 三個數字都叫「記憶體」，但回答三個不同的問題。

![一台 EON 節點的記憶體怎麼分：主機 30.71 GiB 中 27.27 GiB 進資源池（general 23.16、tm 3.00、sysquery 1.00、metadata 0.11），其餘 3.44 GiB 不在池內；general 的 23.16 GiB 有 95%（22.00 GiB）是可排隊上限，剛好等於 6 條查詢 × 3.67 GiB 預算]({{ '/assets/images/eon-memory-pool-budget.svg' | relative_url }}){: loading="lazy" }

上圖每個數字都來自本次實查：`28,594,832` KB、`24,400,528` KB、`3,844,520` KB、`23,067,120` KB，以及 `32,969,703,424` bytes 的主機記憶體。

### 3.1 九個內建池各自的角色

| 池 | 本機 `memory_size_kb` | 角色（官方文件） | 本機實查 |
|----|----------------------|------------------|----------|
| `general` | 24,281,180 | 沒有指定池的查詢都走這裡 | 併發 6，預算 3.67 GiB |
| `tm` | 3,145,728 | Tuple Mover（mergeout／moveout） | 併發 7，預算 449,389 KB |
| `sysquery` | 1,048,576 | 系統表與監控查詢 | 併發 6，預算 174,762 KB；實測常駐 48–56 MB |
| `metadata` | 119,348 | catalog 與 metadata 快取 | 併發 1，沒有預算 |
| `jvm` | 0 | Java 虛擬機（UDF／ML） | 上限 2 GiB，預算 332,049 KB |
| `dbd` | 0 | 背景守護任務 | 借 general，預算 5,766,780 KB |
| `refresh` | 0 | 投影刷新 | 借 general，預算 5,766,780 KB |
| `recovery` | 0 | 節點復原 | 借 general，預算 2,883,390 KB |
| `blobdata` | 0 | ML 的 in-memory blob | 沒有預算，超過就 spill |

`memory_size_kb = 0` 的五個池（jvm、dbd、refresh、recovery、blobdata）不是「沒有記憶體」，而是「沒有保留，跟 general 借」。這件事在巡檢時很重要：**看到 0 不要以為池壞了。**

---

## 四、Step-by-step：把 27 列讀完

### 4.1 27 列長什麼樣

```sql
=> SELECT node_name, pool_name, memory_size_kb, memory_inuse_kb,
          planned_concurrency, query_budget_kb
   FROM v_monitor.resource_pool_status
   ORDER BY node_name, pool_name;

node_name        | pool_name | memory_size_kb | memory_inuse_kb | pc | query_budget_kb
-----------------+-----------+----------------+-----------------+----+----------------
v_eondb_node0001 | blobdata  | 0              | 0               |  2 |
v_eondb_node0001 | dbd       | 0              | 0               |  4 | 5,766,780
v_eondb_node0001 | general   | 24,281,180     | 0               |  6 | 3,844,520
v_eondb_node0001 | jvm       | 0              | 0               |  6 | 332,049
v_eondb_node0001 | metadata  | 119,348        | 0               |  1 |
v_eondb_node0001 | recovery  | 0              | 0               |  8 | 2,883,390
v_eondb_node0001 | refresh   | 0              | 0               |  4 | 5,766,780
v_eondb_node0001 | sysquery  | 1,048,576      | 56,063          |  6 | 174,762
v_eondb_node0001 | tm        | 3,145,728      | 0               |  7 | 449,389
v_eondb_node0002 | blobdata  | 0              | 0               |  2 |
v_eondb_node0002 | dbd       | 0              | 0               |  4 | 5,769,954
v_eondb_node0002 | general   | 24,294,544     | 0               |  6 | 3,846,636
v_eondb_node0002 | jvm       | 0              | 0               |  6 | 332,049
v_eondb_node0002 | metadata  | 105,984        | 0               |  1 |
v_eondb_node0002 | recovery  | 0              | 0               |  8 | 2,884,977
v_eondb_node0002 | refresh   | 0              | 0               |  4 | 5,769,954
v_eondb_node0002 | sysquery  | 1,048,576      | 48,036          |  6 | 174,762
v_eondb_node0002 | tm        | 3,145,728      | 0               |  7 | 449,389
v_eondb_node0003 | blobdata  | 0              | 0               |  2 |
v_eondb_node0003 | dbd       | 0              | 0               |  4 | 5,770,119
v_eondb_node0003 | general   | 24,295,238     | 0               |  6 | 3,846,746
v_eondb_node0003 | jvm       | 0              | 0               |  6 | 332,049
v_eondb_node0003 | metadata  | 105,290        | 0               |  1 |
v_eondb_node0003 | recovery  | 0              | 0               |  8 | 2,885,059
v_eondb_node0003 | refresh   | 0              | 0               |  4 | 5,770,119
v_eondb_node0003 | sysquery  | 1,048,576      | 48,036          |  6 | 174,762
v_eondb_node0003 | tm        | 3,145,728      | 0               |  7 | 449,389
(27 rows)
```

幾個一眼可見的形狀：

- **`tm`、`sysquery` 三個節點完全一樣**（3,145,728／1,048,576）：它們是 `MEMORYSIZE` 明確指定的池。
- **`general` 與 `metadata` 每節點都不同**：它們沒有固定大小，是算出來的。
- **`dbd`、`refresh`、`recovery` 的預算每節點也不同**（5,766,780 / 5,769,954 / 5,770,119）：因為它們借的是「該節點 general 的 95%」，而 general 每節點不同。

### 4.2 千萬不要把 27 列加起來

```sql
=> SELECT count(*) AS rows_, sum(memory_size_kb) AS sum_kb
   FROM v_monitor.resource_pool_status;

rows_ |  sum_kb
------+-----------
   27 | 85,784,496
```

85,784,496 KB = 81.81 GiB。這台機器只有 30.71 GiB 實體記憶體，81.81 GiB 是三個節點各 27.27 GiB 的總和。**這個數字唯一正確的用途是「三個節點的池預算合計」，不是「叢集有多少記憶體」。**

正確的看法是按節點分組：

```sql
=> SELECT node_name, sum(memory_size_kb) AS pools_kb,
          sum(CASE WHEN pool_name IN ('general','metadata') THEN memory_size_kb ELSE 0 END) AS gen_md_kb
   FROM v_monitor.resource_pool_status GROUP BY 1 ORDER BY 1;

node_name        | pools_kb   | gen_md_kb
-----------------+------------+-----------
v_eondb_node0001 | 28,594,832 | 24,400,528
v_eondb_node0002 | 28,594,832 | 24,400,528
v_eondb_node0003 | 28,594,832 | 24,400,528
```

兩個數字都值得記住：**每節點 28,594,832 KB（27.27 GiB）**，佔主機 30.71 GiB 的 88.81%；**general + metadata 24,400,528 KB（23.27 GiB）**，三個節點一字不差。

### 4.3 general 與 metadata 是一組連通管

連續三次快照（間隔 12 秒），三節點的 general／metadata／相加：

```sql
=> SELECT node_name, 
          max(CASE WHEN pool_name='general' THEN memory_size_kb END) AS general_kb,
          max(CASE WHEN pool_name='metadata' THEN memory_size_kb END) AS metadata_kb,
          max(CASE WHEN pool_name='general' THEN memory_size_kb END)
        + max(CASE WHEN pool_name='metadata' THEN memory_size_kb END) AS sum_kb
   FROM v_monitor.resource_pool_status GROUP BY 1 ORDER BY 1;

node_name        | general_kb | metadata_kb | sum_kb
-----------------+------------+-------------+-----------
v_eondb_node0001 | 24,281,180 |     119,348 | 24,400,528
v_eondb_node0002 | 24,294,544 |     105,984 | 24,400,528
v_eondb_node0003 | 24,295,238 |     105,290 | 24,400,528
```

三次快照的 `sum_kb` 全部是 `24,400,528`，`general_kb` 與 `metadata_kb` 一個 KB 都沒動。但把時間拉長就看得出它是活的：本次執行開始時（05:05）node0001 的 general 是 `24,290,902`、metadata 是 `109,626`；執行到一半（05:23）變成 `24,281,180` 與 `119,348`。**general 少了 9,722 KB，metadata 正好多了 9,722 KB，相加不變。**

這就是「general 是剩下的都給我」的意思：metadata（catalog、系統表快取）先拿走它需要的量，剩下的才歸 general。所以：

- 三個節點的 general 不一樣是正常的，不要當成設定漂移。
- catalog 變胖（例如建了很多投影、執行大量 DDL）會直接壓縮 general 的可用量，進而壓縮每條查詢的預算。
- 想比較節點之間的差異，要比 `general + metadata`，不是只比 general。

### 4.4 逐池驗算 query budget

官方文件寫得很清楚（`Query budgeting`）：general 的預算是 `queuing threshold ÷ PLANNEDCONCURRENCY`，而 queuing threshold 是 `MAXMEMORYSIZE` 的 95%；`MEMORYSIZE = 0` 且沒有 `MAXMEMORYSIZE` 的池，用 **general 的 queuing threshold** 再除以自己的併發數；有 `MEMORYSIZE` 的池則直接用 `MEMORYSIZE ÷ PLANNEDCONCURRENCY`。把本機 node0001 的九個池丟進去驗：

| 池 | memory_size_kb | 併發 | query_budget_kb | 驗算 | 對得上？ |
|----|---------------:|-----:|----------------:|------|----------|
| general | 24,281,180 | 6 | 3,844,520 | 24,281,180 × 0.95 ÷ 6 = 3,844,520.3 | ✅ |
| tm | 3,145,728 | 7 | 449,389 | 3,145,728 ÷ 7 = 449,389.7 | ✅ |
| sysquery | 1,048,576 | 6 | 174,762 | 1,048,576 ÷ 6 = 174,762.7 | ✅ |
| jvm | 0 | 6 | 332,049 | 2,097,152 × 0.95 ÷ 6 = 332,049.1 | ✅ |
| dbd | 0 | 4 | 5,766,780 | 23,067,120 ÷ 4 = 5,766,780 | ✅ |
| refresh | 0 | 4 | 5,766,780 | 23,067,120 ÷ 4 = 5,766,780 | ✅ |
| recovery | 0 | 8 | 2,883,390 | 23,067,120 ÷ 8 = 2,883,390 | ✅ |
| metadata | 119,348 | 1 | NULL | 不配預算（只給 catalog） | — |
| blobdata | 0 | 2 | NULL | 沒有預算，超過即 spill | — |

八個有預算的池全部對得上，誤差都在 1 KB 的進位內。`23,067,120` 就是 general 的 queuing threshold（`24,281,180 × 0.95`），這也是 dbd／refresh／recovery 的預算基數——它們借的是 general 的水位，不是自己的。

順帶一個很好用的檢查：**general 的「預算 × 併發」會剛好等於 queuing threshold。**

```sql
=> SELECT pool_name, query_budget_kb, planned_concurrency,
          query_budget_kb * planned_concurrency AS product_kb, queueing_threshold_kb
   FROM v_monitor.resource_pool_status
   WHERE node_name='v_eondb_node0001' AND pool_name='general';

pool_name | query_budget_kb | pc | product_kb | queueing_threshold_kb
----------+-----------------+----+------------+-----------------------
general   | 3,844,520       |  6 | 23,067,120 | 23,067,120
```

`3,844,520 × 6 = 23,067,120`，一位不差。意思是：**6 條查詢剛好把 general 灌到排隊水位，第 7 條開始排隊**。這也解釋了為什麼 general 的 `planned_concurrency` 是 6。

### 4.5 AUTO 的 6 是怎麼來的

`v_catalog.resource_pools` 裡 general 的 `plannedconcurrency` 是 `AUTO`，執行時才解析成 6。官方文件的規則是：AUTO 取「核心數」與「記憶體 ÷ 2 GB」兩者較小值。本機：

- 核心數：`v_monitor.host_resources.processor_core_count` = 6（三節點都是 i5-9400、6 核）
- 記憶體 ÷ 2 GB：23.16 GiB ÷ 2 GiB ≈ 11

`min(6, 11) = 6`，與實查的 `planned_concurrency = 6` 一致。這種「剛好對上」的驗算很有用：如果哪天 general 的併發變成 11，你馬上知道是核心數變了，不是記憶體。

### 4.6 實測：一條查詢吃掉一個預算

拿叢集上 10 億筆的 `mtk.partition_columns` 當樣本（`SELECT count(*)` 會走統計值，不能拿來測記憶體，所以用真的會掃資料的聚合）：

```sql
=> SELECT count(*) AS rows_, count(DISTINCT table_name) AS tables_, sum(row_count) AS ros_rows
   FROM mtk.partition_columns;

     rows_     | tables_ |     ros_rows
---------------+---------+------------------
 1,007,772,484 |   1,056 | 76,946,974,970,101
```

跑的時候每秒採樣 `resource_pool_status`，同一條查詢先跑一次、再同時跑兩次：

| 情境 | 牆上時間 | general 池峰值（node1／node2／node3） | `running_query_count` |
|------|---------:|--------------------------------------|----------------------|
| 單條 | 13.79 s（查詢本身 13.57 s） | 3,844,519／3,846,635／3,846,745 KB | 1 |
| 兩條並行 | 32.22 s（各 31.90 s） | 7,688,856／7,693,270／7,693,490 KB | 2 |

兩個結論：

1. **峰值幾乎等於預算。** node1 的預算當時是 `3,844,429` KB，峰值 `3,844,519` KB（差 90 KB，取樣誤差）；`v_monitor.query_requests.memory_acquired_mb` 記的是 `3,754.32` MB，換算 `3,844,424` KB，比預算少 5 KB。**查詢不是「用到多少算多少」，是「先拿一份預算，再從裡面花」。**
2. **兩條並行就是兩份預算。** 7,688,856 KB ≈ 2 × 3,844,519 KB；`running_query_count` 是 2，兩個數字互相印證。而總量 7.7 GB 只有 general 水位（22.00 GiB）的 35%，所以完全沒有排隊——但兩條各跑 31.90 秒，是單條的 2.35 倍，這是 CPU（6 核、`execution_parallelism = AUTO`）的競爭，不是記憶體。

這也回答了「為什麼我的查詢明明只要幾百 MB，`memory_inuse_kb` 卻顯示 3.8 GB」：那個欄位是「已取得」的記憶體，也就是預算本身，不是實際 touch 的頁數。

### 4.7 監控查詢也吃記憶體

同一時間 `sysquery` 池的 `memory_inuse_kb` 是 `56,063`／`48,036`／`48,036` KB。這是因為所有 `v_monitor`、`v_catalog` 查詢都走 sysquery 池——包含你正在跑的那條巡檢 SQL。所以在「系統很慢」的時候，先看一下 sysquery 的預算（本機 174,762 KB）有沒有被打滿；打滿的話，你的監控查詢會排在自己的池裡，看起來像資料庫卡住。

---

## 五、Enterprise 對照：同樣的總和、不同的分配

同機的 `testdb`（Enterprise、單節點、v25.4.0-0，主機規格與 EON 三節點相同：`32,969,703,424` bytes、6 核）也回 9 個池，總和一樣是 `28,594,832` KB：

```sql
=> SELECT pool_name, memory_size_kb, planned_concurrency, query_budget_kb
   FROM v_monitor.resource_pool_status ORDER BY pool_name;

pool_name | memory_size_kb | pc | query_budget_kb
----------+----------------+----+----------------
blobdata  |              0 |  2 |
dbd       |              0 |  4 |         6076547
general   |      25585464 |  4 |         6076547
jvm       |              0 |  2 |         1992294
metadata  |         149886 |  1 |
recovery  |              0 |  8 |         3038273
refresh   |              0 |  4 |         6076547
sysquery  |        1429741 |  6 |          238290
tm        |        1429741 |  7 |          204248
(9 rows)
```

同樣的公式在 Enterprise 一樣成立：`25,585,464 × 0.95 ÷ 4 = 6,076,547`、`1,429,741 ÷ 7 = 204,248`、`1,429,741 ÷ 6 = 238,290`。

差別在「分配」：

| 項目 | eondb（EON） | testdb（Enterprise） |
|------|--------------|----------------------|
| 池總和 | 28,594,832 KB | 28,594,832 KB |
| general | 24,281,180–24,295,238 KB，併發 6 | 25,585,464 KB，併發 4 |
| general 每查詢預算 | 3,844,520 KB（3.67 GiB） | 6,076,547 KB（5.80 GiB） |
| tm | 3,145,728 KB（3 GiB） | 1,429,741 KB（5%） |
| sysquery | 1,048,576 KB（1 GiB） | 1,429,741 KB（5%） |
| jvm 上限 | 2 GiB | 4 GiB |

兩點誠實說明。第一，**這不是 EON 與 Enterprise 的模式差異**，是這兩個資料庫的池設定不同：`testdb` 的 `general` 併發被明確設成 4（`v_catalog.resource_pools.plannedconcurrency = 4`，預設是 `AUTO`），`tm`／`sysquery` 用 5% 而不是 3G／1G。第二，**池總和相同是因為兩邊的主機記憶體一樣（`32,969,703,424` bytes）**，總量由主機決定，分配由設定決定。把 Enterprise 的池設定直接搬到 EON 不會出事，但每查詢預算會跟著變——搬遷前先把兩個數字算出來對照，比上線後被使用者問「為什麼查詢變慢」便宜很多。

---

## 六、歷史與容量：兩張表決定你看得到多久

`v_monitor.memory_usage` 提供節點層級的記憶體使用率（每節點每分鐘一筆）：

```sql
=> SELECT date_trunc('day', start_time) AS d, node_name,
          round(avg(average_memory_usage_percent),2) AS avg_pct,
          max(average_memory_usage_percent) AS max_pct
   FROM v_monitor.memory_usage GROUP BY 1,2 ORDER BY 1,2;

          d          |     node_name     | avg_pct | max_pct
---------------------+-------------------+---------+---------
 2026-11-09 00:00:00 | v_eondb_node0001  |   41.11 |   67.87
 2026-11-10 00:00:00 | v_eondb_node0001  |   45.01 |   47.43
 2026-11-11 00:00:00 | v_eondb_node0001  |   44.42 |   46.26
 2026-11-12 00:00:00 | v_eondb_node0001  |   44.44 |   91.91
 2026-11-13 00:00:00 | v_eondb_node0001  |   44.71 |   50.43
 2026-11-14 00:00:00 | v_eondb_node0001  |   30.76 |   51.54
```

（node0002／node0003 的數字相同，此處省略。）五天平均 42.02%，峰值 91.91%，發生在 `2026-11-12 15:49`，三個節點同一分鐘一起衝上去——這是「一條大查詢同時打三個節點」的典型形狀，不是某個節點漏水。11-14 的平均掉到 30.76%，是當天有段時間沒有負載。

兩張表的保留期不一樣，這決定了你能回溯多久：

- `v_monitor.memory_usage`：本機保留 `8,014` 筆／節點，從 `2026-11-09 07:28` 開始，約 5 天。
- `v_internal.dc_resource_pool_status`：本機 `87,498` 列，只保留約 1 小時（`2026-11-14 20:01`–`21:01`）。要看「剛剛那 10 分鐘哪個池衝上去」用它，要看「上週有沒有記憶體壓力」用 `memory_usage`。

`v_monitor.resource_pool_move` 本機是 0 列——沒有任何查詢被移到別的池（沒有設定 `CASCADE TO`，也沒有 `RUNTIMECAP` 觸發）。

---

## 七、常見錯誤與反模式

**1. 把 27 列的 `memory_size_kb` 加起來。** 本機得到 85,784,496 KB（81.81 GiB），是實體記憶體 30.71 GiB 的 2.66 倍。這個數字會被拿去跟監控系統的記憶體對帳，然後你就開始找「多出來的 50 GiB 在哪」。正確做法是按 `node_name` 分組。

**2. 以為三個節點的池數字應該一模一樣。** 它們只有「總和」一樣（28,594,832 KB）。`general` 三個節點差 14,058 KB、`metadata` 差 14,058 KB，互相抵消。只比 general 會誤判成設定漂移。

**3. 以為 `memory_size_kb = 0` 的池沒有記憶體。** jvm／dbd／refresh／recovery／blobdata 都是 0，但它們的 `query_budget_kb` 是 332,049／5,766,780／5,766,780／2,883,390，全部向 general 借。看到 0 就從巡檢報表剔除，會漏掉 recovery 這種真的會吃 2.8 GB 的池。

**4. 把 `v_monitor.resource_acquisitions.memory_inuse_kb` 加總。** 本機這張表有 `2,974` 列、從 `2026-11-09` 累積到現在，全部加起來是 `3,316,095,420` KB（3.09 TB）。它是「每次取得資源的歷史紀錄」，不是即時用量；要即時就加 `WHERE is_executing`（本機當時 3 列、合計 524,286 KB）。

**5. 把 `memory_inuse_kb` 當成「實際用掉」。** 本機一條掃 10 億筆的查詢，general 峰值 3,844,519 KB，但它是「取得的預算」，不是 touch 的頁數。反過來說，看到它貼著預算也不要以為查詢在浪費記憶體。

**6. 用 `memory_usage.average_memory_usage_percent` 當「資源池使用率」。** 它是**節點**的記憶體使用率（含 OS cache），不是池。本機 42% 的平均值裡，池自己只佔 28,594,832 KB 的配置，兩者不能互換。

**7. 以為 query budget 可以靠 `ALTER RESOURCE POOL ... MAXMEMORYSIZE` 單獨調大。** 文件明講：降低 `MAXMEMORYSIZE` 會同時降低預算；要維持預算，`MAXMEMORYSIZE` 與 `PLANNEDCONCURRENCY` 要一起調。本機 general 的預算 = `size × 95% ÷ 6`，動任一項都會改預算。

---

## 八、風險與建議

**現在就能做（唯讀、低風險）**

- 巡檢固定看三個數字：每節點池總和（本機應為 28,594,832 KB）、`general + metadata`（24,400,528 KB）、general 的 `query_budget_kb × planned_concurrency` 是否等於 `queueing_threshold_kb`。三者任一變動都值得查原因。
- 用 `WHERE is_executing` 過濾 `resource_acquisitions`，不要直接加總；要看歷史用 `memory_usage`，要看最近一小時用 `dc_resource_pool_status`。
- 把 `sysquery` 的用量納入監控：本機常在 48–56 MB，若接近 174,762 KB 的預算，監控查詢自己就會變慢。
- 比較節點時用 `general + metadata`，不要只比 general。

**需要變更（要窗口、要核准）**

- 調整 `general` 的 `PLANNEDCONCURRENCY`：本機 6 → 4 會把每查詢預算從 3.67 GiB 提高到約 5.5 GiB，代價是併發上限下降（第 5 條開始排隊）。這是本機 testdb 已經做過的調整，可以拿它的數字當參考。
- 建立自訂池並用 `CASCADE TO` 做分流：本叢集目前 9 個池全部是內建池，沒有任何自訂池，等於所有應用都擠在 general。
- 調整 `MEMORYSIZE`：會改變 general 的剩餘量與所有「借 general」池的預算，必須整體重算，不能只改一個池。

---

## 九、參考資料

- Vertica 25.4 文件：Query budgeting — https://docs.vertica.com/25.4.x/en/admin/managing-db/managing-workloads/resource-pool-architecture/query-budgeting/
- Vertica 25.4 文件：Built-in pools — https://docs.vertica.com/25.4.x/en/admin/managing-db/managing-workloads/resource-pool-architecture/built-pools/
- Vertica 25.4 文件：`RESOURCE_POOL_STATUS` — https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/resource-pool-status/
- Vertica 25.4 文件：Monitoring resource pools — https://docs.vertica.com/25.4.x/en/admin/monitoring/monitoring-resource-pools/
- Vertica 25.4 文件：`ALTER RESOURCE POOL` — https://docs.vertica.com/25.4.x/en/sql-reference/statements/alter-resource-pool/
- Vertica 知識庫：Best Practices for Managing Resource Pools — https://vertica.com/kb/BestPracticesforManagingResourcePools/Content/BestPractices/BestPracticesforManagingResourcePools.htm

> 註：`docs.vertica.com` 目前會 302 導向 `docs.rocketsoftware.com/bundle/vertica_25.4/`，路徑對應相同；本文連結於 2026-11-15 檢查皆回應 200。
