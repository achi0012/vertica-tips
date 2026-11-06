---
layout: post
title: "Vertica 投影設計實戰：Segmentation、Sort Order、Encoding 與 ROS Pushback"
date: 2026-11-07 05:00:00 +0800
categories: vertica projection performance
tags: [vertica, projection, segmentation, sort-order, encoding, ros-pushback, performance]
description: "從 segmentation、sort order、encoding 到投影數量控管，實測 Vertica 投影設計如何影響 EXPLAIN、資料傾斜與 ROS pushback，並整理可操作的設計檢查表。"
---

<!--
AI Summary: Vertica 25.4.0-0 單節點實測投影設計。30 萬列事實表上，ORDER BY customer_id 讓點查的 EXPLAIN cost 從 657 降到 193，PROFILE 顯示 valindex 修剪 195,552/300,000 列；低基數 status 做 segmentation 時 HASH(status)%8 有 91.1% 落在同一個 bucket、6/8 為空；強制 BLOCK_DICT 讓 payload 膨脹 52.1%、amount 膨脹 29.5%，全部強制編碼反而比 AUTO 大 75.9%；4 個投影讓儲存變 3.93 倍、載入多 22%、10 次載入後 ROS 容器 10 變 40；新建投影要 REFRESH 才會被查詢使用（WARNING 4468）。
AI Keywords: Vertica, projection, segmentation, sort order, encoding, ROS, valindex, EXPLAIN, 25.4
-->
<!--more-->

---

## 一、先講結論

投影（projection）是 Vertica 唯一真正儲存資料的地方，但多數人只會 `CREATE TABLE`，讓資料庫自動決定一切。這在資料量小的時候不會出事，等到查詢變慢、載入變慢、磁碟突然不夠，才發現每個決定都有帳單。

我在 Vertica 25.4.0-0 單節點上，用同一份 30 萬列資料建了多組投影，量到五個結論：

1. **排序鍵決定查詢能不能「跳過列」，不只是決定壓縮率**。點查 `customer_id = 12345` 時，有 `ORDER BY customer_id` 的投影 EXPLAIN cost 是 **193**，沒有排序的是 **657**；PROFILE 顯示前者用 valindex 修剪掉 **195,552 / 300,000** 列（65.2%），後者為 **0**。
2. **容器層級的 min/max 修剪跟排序無關，跟「載入批次」有關**。我按月分 10 批載入，即使完全沒有 ORDER BY，查 9 月也只掃到 9 月那個容器（29,708 列）。真正差別在容器內部：排序過的投影再用 valindex 多修剪 22,413 列（73.2%）。
3. **低基數欄位做 segmentation 會嚴重傾斜**。用 `HASH(status) % 8` 模擬 8 個 segment，`CLOSED` 那一桶拿了 **273,280 列（91.1%）**，另 6 個 bucket 完全沒有資料。單節點只有 1 個 segment，所以本機看不到節點熱點，但多節點環境這是災難。
4. **強制編碼通常更糟，不是更好**。同一份資料、同一個排序，AUTO 是 17.06 bytes/row；`payload ENCODING BLOCK_DICT` 變成 25.94（**+52.1%**），`amount ENCODING BLOCK_DICT` 變成 22.09（**+29.5%**），全部欄位一起強制編碼是 30.00（**+75.9%**）。只有 `event_ts ENCODING DELTAVAL` 這種「排序鍵且相鄰值接近」的欄位真的變小（-3.9%）。
5. **投影數量是乘法成本**。1 個投影存 5,116,967 bytes；4 個投影存 20,111,106 bytes（**3.93 倍**），載入從 208.6–221.7 ms 變成 258.4–266.0 ms（**+22%**），10 次載入後 ROS 容器從 10 個變成 40 個。投影不是免費的索引。

這篇寫給需要自己設計投影、或正在懷疑「為什麼加了投影卻沒變快」的人。所有數字都來自本機實測，schema `article_lab_proj` 在結束時已 `DROP SCHEMA ... CASCADE`。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 節點數 | 1（Community Edition） |
| 資料庫 / 使用者 | `testdb` / `dbadmin` |
| 測試 schema | `article_lab_proj`（實驗後刪除） |
| 事實表 | 300,000 列 × 6 欄 |
| 欄位 | `event_id`、`customer_id`、`event_ts`、`status`、`amount`、`payload` |
| 維度分佈 | `customer_id` 1–20,000；`status` CLOSED 270,248 / OPEN 26,720 / CANCELLED 3,032 |
| 時間範圍 | 2026-01-01 至 2026-10-30 |
| 載入方式 | `COPY ... DIRECT`，單檔 300,000 列 |
| 量測日期 | 2026-11-07 |

三個會影響重現的環境細節：

- **這個連線的 `autocommit` 是 `off`**。DDL 與 `COPY` 在 Vertica 會自行提交，但如果你在同一個 session 做 `DELETE`/`INSERT` 再換 session 查，會看不到結果。本次所有測試都在單一 `vsql -f` session 內完成。
- **單節點只有 1 個 segment**。`v_monitor.storage_containers` 對所有測試投影都回傳 `segment_lower_bound = 0`、`segment_upper_bound = 4294967295`，代表一個容器覆蓋全部 segment 範圍。這表示**本機無法觀測節點間的資料傾斜**，只能用 `HASH()` 模擬，本文會明確標示哪些是模擬。
- **`COPY` 的檔案由伺服器端讀取**，所以檔案要放在 `dbadmin` 讀得到的位置；這次放在 `/tmp/opencode/`，權限 644。

---

## 三、核心原理

### 3.1 一張表、多個投影

Vertica 的表格定義（`CREATE TABLE`）只是邏輯外殼，實際資料存在投影裡。每個投影有自己的：

| 設計維度 | 決定什麼 | 主要影響 |
|---|---|---|
| Segmentation | 每一列放到哪個 segment | 多節點資料分佈、join 的網路搬移 |
| Sort order（`ORDER BY`） | 容器內資料的排列 | 壓縮率、valindex 修剪、GROUP BY 是否 pipelined |
| Encoding | 每個欄位的壓縮方式 | 磁碟用量、掃描 I/O |
| 投影數量 | 同一份資料要存幾份 | 儲存、載入時間、mergeout/ROS 數量 |

四個維度彼此牽制。排序可以讓編碼更有效，但也讓載入多一次排序成本；多一個投影讓某條查詢變快，但所有載入都要多寫一份。

### 3.2 Segmentation 的實際長相

建立投影時如果寫 `SEGMENTED BY HASH(customer_id) ALL NODES`，`v_catalog.projections.segment_expression` 會老實顯示：

```
hash(fact_cust.customer_id)
```

沒有指定時，Vertica 自動建立的 super projection 會用**所有欄位**當 segmentation key：

```
hash(fact_auto.event_id, fact_auto.customer_id, fact_auto.event_ts,
      fact_auto.amount, fact_auto.status, fact_auto.payload)
```

語意是「把這一串值丟進 hash，再對 segment 數取餘數」。所以 segmentation 欄位的**基數與分佈**直接決定每個 segment 拿多少列。用 `status`（3 個值）當 key，等於只把資料分成三堆，而且 hash 不會按比例分。

### 3.3 Sort order 的兩個作用

1. **壓縮**：相鄰值重複或接近時，RLE、DELTAVAL 這類編碼才有東西可壓。
2. **修剪**：PROFILE 的 `container rows pruned by valindex` 只在有 `ORDER BY` 的投影上出現。點查時它可以跳過容器內大部分列，這是 EXPLAIN cost 657 → 193 的來源。

要注意：**容器層級的 min/max 修剪不需要排序**。每個 ROS 容器都記錄各欄位的 min/max；只要載入時資料有區段性（例如按月分批載入），就算沒排序，查單月也能只掃到那一個容器。排序影響的是「容器內部」能不能再往下跳。

### 3.4 投影數量的成本模型

每多一個投影，等於：

- 磁碟多一份完整資料（加上各自的編碼差異）
- 每次 `COPY` 多寫一份 ROS
- tuple mover 多一份 mergeout 工作
- 每個投影各自累積 ROS 容器，太多會觸發 ROS pushback

官方的經驗法則是「先用最少投影滿足主要查詢，再看瓶頸加」。下面的實測就是這句話的數字版。

---

## 四、Step-by-step 實測

### 4.1 建立四張同資料、不同設計的表

四張表資料完全相同，差別只在投影：

```sql
CREATE TABLE article_lab_proj.fact_auto (
  event_id INT NOT NULL, customer_id INT NOT NULL, event_ts TIMESTAMP NOT NULL,
  status VARCHAR(12) NOT NULL, amount NUMERIC(12,2), payload VARCHAR(16));

CREATE TABLE article_lab_proj.fact_cust ( ...相同欄位... );
CREATE PROJECTION article_lab_proj.fact_cust_p (
  event_id, customer_id, event_ts, status, amount, payload
) AS SELECT * FROM article_lab_proj.fact_cust
ORDER BY customer_id, event_ts
SEGMENTED BY HASH(customer_id) ALL NODES;

CREATE TABLE article_lab_proj.fact_ts ( ...相同欄位... );
CREATE PROJECTION article_lab_proj.fact_ts_p (
  event_id, customer_id, event_ts, status, amount, payload
) AS SELECT * FROM article_lab_proj.fact_ts
ORDER BY event_ts
SEGMENTED BY HASH(event_id) ALL NODES;

CREATE TABLE article_lab_proj.fact_status ( ...相同欄位... );
CREATE PROJECTION article_lab_proj.fact_status_p (
  event_id, customer_id, event_ts, status, amount, payload
) AS SELECT * FROM article_lab_proj.fact_status
ORDER BY status
SEGMENTED BY HASH(status) ALL NODES;
```

每次 `COPY ... DIRECT` 載入 300,000 列，實測 174.0–236.5 ms（四次分別 215.4、212.3、236.5、174.0 ms）。這個階段的載入時間差不多，因為都只有一個投影。

四張表的投影與儲存：

```
 projection_name | segment_expression                          | used_bytes | bytes/row
-----------------+---------------------------------------------+------------+-----------
 fact_auto_super | hash(event_id, customer_id, event_ts, ...)   |  5,098,062 |     16.99
 fact_cust_p     | hash(customer_id)                            |  5,116,967 |     17.06
 fact_ts_p       | hash(event_id)                               |  4,885,658 |     16.29
 fact_status_p   | hash(status)                                 |  5,018,078 |     16.73
```

`ORDER BY event_ts` 的投影最小（16.29 bytes/row），因為時間欄位排序後 DELTAVAL 效果好。`fact_status_p` 雖然排序基數低，但只有 3 個值，壓縮率也不錯（16.73）。差異都在 5% 以內——**在這個資料量，編碼與排序的儲存差異不是重點，查詢行為才是**。

### 4.2 Segmentation：單節點看不到傾斜，用 HASH() 模擬

先確認單節點的 segment 結構：

```sql
SELECT projection_name, segment_lower_bound, segment_upper_bound,
       count(*) AS containers, sum(total_row_count) AS rows
FROM v_monitor.storage_containers
WHERE schema_name = 'article_lab_proj'
GROUP BY 1,2,3;
```

```
 fact_auto_super | 0 | 4294967295 | 1 | 300000
 fact_cust_p     | 0 | 4294967295 | 1 | 300000
 fact_status_p   | 0 | 4294967295 | 1 | 300000
 fact_ts_p       | 0 | 4294967295 | 1 | 300000
```

全部是「一個容器覆蓋 0–4294967295」，也就是 1 個 segment。**所以本機不可能觀察到節點傾斜**，我也不會假裝有。

改用同一個 `HASH()` 函數對實際 30 萬列計算 `MOD(HASH(key), 8)`，模擬 8 個 segment 的分佈（這是用真實資料算出來的模擬，不是多節點實測）：

| Segmentation key | 最大 bucket | 最小 bucket | max/min | 空 bucket |
|---|---|---|---|---|
| `status` | 273,280（91.1%） | 26,720 | 10.23 | 6 / 8 |
| `customer_id` | 38,988 | 36,149 | 1.08 | 0 / 8 |
| `event_id` | 37,791 | 37,285 | 1.01 | 0 / 8 |

`status` 只有 CLOSED / OPEN / CANCELLED 三個值，hash 後只落在 bucket 5 和 7，其中 91.1% 集中在 bucket 5。多節點環境下，這代表其中一個節點要處理九成資料，其他節點閒置——這是典型的「查詢沒有變慢，是慢在等一個節點」。

`customer_id` 與 `event_id` 的分佈都在 ±4% 內，這才是 segmentation key 該有的樣子。

> 這段是 `MOD(HASH(...), 8)` 的模擬。Vertica 內部使用的 segment 數量由節點數與 K-safety 決定，單節點 CE 是 1，所以無法用 `v_monitor` 直接驗證 8 段分佈。

### 4.3 Sort order：EXPLAIN 的成本差 3.4 倍

查同一筆客戶資料：

```sql
EXPLAIN SELECT count(*), sum(amount)
FROM article_lab_proj.fact_cust WHERE customer_id = 12345;
```

三張表的 `STORAGE ACCESS` 成本：

| 表 | 投影 | STORAGE ACCESS cost | 總 cost |
|---|---|---|---|
| `fact_auto` | `fact_auto_super`（無 ORDER BY） | 656 | 657 |
| `fact_cust` | `fact_cust_p`（ORDER BY customer_id） | 192 | **193** |
| `fact_ts` | `fact_ts_p`（ORDER BY event_ts） | 656 | 657 |

資料實際只有 9 列符合。EXPLAIN 的 rows 估算都是 2K（有統計資訊後），差別完全來自投影能不能用排序鍵定位。

`fact_cust` 的成本少了 70.6%，但**實際執行時間只差約 26%**：

```
cust_sorted    4.10 / 4.21 / 4.39 ms
cust_unsorted  5.44 / 5.77 / 5.72 ms
```

原因很簡單：30 萬列對單節點來說太小，資料都在記憶體裡，修剪省下的是 CPU 而不是磁碟 I/O。資料量放大後差距才會拉開。

### 4.4 PROFILE：valindex 到底修剪了多少列

用 `PROFILE` 跑同一條查詢，再讀 `v_monitor.execution_engine_profiles`：

```sql
PROFILE SELECT count(*), sum(amount)
FROM article_lab_proj.cust_sorted WHERE customer_id = 12345;
```

```
 operator      | counter                                   | value
---------------+-------------------------------------------+--------
 StorageUnion  | container rows total                      | 300,000
 StorageUnion  | container rows pruned by valindex         | 149,998
 StorageUnion  | container rows pruned by query predicates  |  45,554
 Scan          | rows pruned by valindex                   | 195,552
```

沒有 ORDER BY 的 `cust_unsorted`：

```
 StorageUnion  | container rows total                      | 300,000
 StorageUnion  | container rows pruned by valindex         |       0
 StorageUnion  | container rows pruned by query predicates  |       0
 Scan          | rows pruned by valindex                   |       0
```

195,552 + 45,554 = 241,106，和 300,000 對不起來；149,998 + 45,554 = 195,552 才對得上。`Scan` 的 `rows pruned by valindex` 與 `StorageUnion` 的兩個計數器是不同階段的統計，不要相加。重點是：**沒有 ORDER BY 的投影，valindex 修剪是 0**。

### 4.5 容器 min/max 與排序無關：按月載入的實驗

把同一份資料拆成 10 個月份檔，各表分 10 次 `COPY` 載入。查 9 月：

```
ts_sorted    3.71 / 3.78 / 4.40 ms    container rows total 29,708
ts_unsorted  3.76 / 3.82 / 3.80 ms    container rows total 29,708
```

**兩張表都只掃到 29,708 列**，因為每個 ROS 容器只裝一個月份的資料，min/max 自然把其他 9 個容器剃掉。這證明容器修剪靠的是「載入區段性」，不是排序。

但查 3 天（3/1–3/4）時，差別就出來了：

```
ts_sorted    container rows total 30,605
             container rows pruned by valindex 21,418
             container rows pruned by query predicates 995
             Scan rows pruned by valindex 22,413   (73.2%)
ts_unsorted  container rows total 30,605
             container rows pruned by valindex 0
```

兩者都先被 min/max 修剪到 3 月那個容器（30,605 列），但排序過的投影在容器內再靠 valindex 跳過 22,413 列。執行時間 3.42–3.46 ms 對 3.59–3.69 ms，差距一樣不大，因為基數太小。

結論：**sort order 的價值在「容器內」；容器間的 pruning 是載入策略的功勞**。如果你的資料是一次性大批匯入、每次查詢都跨整個時間範圍，排序的效益會比按月載入小很多。

### 4.6 GROUP BY 的計畫形狀

排序還會改變聚合方式。`GROUP BY status`：

```
fact_auto    GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 419]
             STORAGE ACCESS [Cost: 272, Rows: 300K]

fact_status  GROUPBY PIPELINED [Cost: 339]
             STORAGE ACCESS [Cost: 192, Rows: 300K]
```

`ORDER BY status` 讓 `GROUP BY status` 可以用 pipelined 聚合，不用建 hash 表再 resegment，成本 419 → 339（**-19.1%**）。這是「排序鍵要對齊 GROUP BY 前綴」的實際回報。

### 4.7 Encoding：AUTO 通常已經很好，亂指定會更糟

同一份資料、同一個 `ORDER BY customer_id, event_ts`、同一個 segmentation，只改欄位編碼：

| 編碼設定 | used_bytes | bytes/row | 相對 AUTO |
|---|---|---|---|
| AUTO（基準） | 5,116,967 | 17.06 | — |
| 全部 `ENCODING NONE` | 5,461,646 | 18.21 | +6.7% |
| `event_ts ENCODING DELTAVAL` | 4,916,531 | 16.39 | **-3.9%** |
| `status ENCODING RLE` | 5,116,967 | 17.06 | 0.0% |
| `amount ENCODING BLOCK_DICT` | 6,627,774 | 22.09 | +29.5% |
| `payload ENCODING BLOCK_DICT` | 7,781,972 | 25.94 | **+52.1%** |
| 全部強制（DELTAVAL×3 + RLE + BLOCK_DICT×2） | 8,998,702 | 30.00 | **+75.9%** |

兩個觀察：

1. **AUTO 不是「不壓縮」**。全部關掉編碼只多 6.7%，代表 AUTO 在這個資料集上選的編碼本來就不差。
2. **`BLOCK_DICT` 對高基數欄位是反效果**。`payload` 有 5,000 個不同值、`amount` 幾乎每列都不同，字典本身加上索引的比重超過省下的空間。

排序與 RLE 的搭配則有效。把表改成 `ORDER BY status, event_ts`：

| 設定 | used_bytes | bytes/row | 相對 `fact_status` |
|---|---|---|---|
| AUTO（`ORDER BY status`） | 5,018,078 | 16.73 | — |
| `status ENCODING RLE` | 4,831,850 | 16.11 | -3.7% |
| `status RLE` + 其他 `DELTAVAL` | 4,756,913 | 15.86 | **-5.2%** |

RLE 只有在相同值連續出現時才有用，所以它必須搭配「把該欄位放在排序前綴」。

### 4.8 投影數量：3.93 倍儲存、+22% 載入、4 倍 ROS

建兩個資料相同的表：一個 1 個投影，一個 4 個投影（分別排序 customer_id、event_ts、status、amount），各載入 300,000 列兩次：

| 表 | 投影數 | 載入時間 | 總儲存 |
|---|---|---|---|
| `single_a` | 1 | 221.7 ms | 5,116,967 |
| `single_b` | 1 | 208.6 ms | 5,116,967 |
| `multi_a` | 4 | 258.4 ms | 20,111,106 |
| `multi_b` | 4 | 266.0 ms | 20,112,739 |

平均 215.2 ms → 262.2 ms（**+21.9%**），儲存 3.93 倍。

再把同一份資料分 10 批載入 `single_b` 與 `multi_b`：

```
single_b  single_b_p    ros_count 10   row_count 569,168   used_bytes  9,630,816
multi_b   multi_b_p1    ros_count 10   row_count 569,168   used_bytes  9,630,816
multi_b   multi_b_p2    ros_count 10   row_count 569,168   used_bytes  9,269,325
multi_b   multi_b_p3    ros_count 10   row_count 569,168   used_bytes  9,313,602
multi_b   multi_b_p4    ros_count 10   row_count 569,168   used_bytes  9,443,703
```

一個投影 10 個 ROS，四個投影總共 **40 個 ROS 容器**，總儲存 37,657,446 bytes（3.91 倍）。ROS 容器越多，tuple mover 要做的 mergeout 越多；如果你的載入頻率很高、每個投影又各自累積容器，這就是 ROS pushback 的起點。

### 4.9 在已經有資料的表上建投影：一定要 REFRESH

```sql
CREATE PROJECTION article_lab_proj.fact_auto_p2 (...)
AS SELECT ... FROM article_lab_proj.fact_auto
ORDER BY event_ts SEGMENTED BY HASH(event_id) ALL NODES;
```

實際輸出：

```
WARNING 4468:  Projection <article_lab_proj.fact_auto_p2> is not available for
query processing. Execute the select start_refresh() function to copy data into
this projection.
```

`v_catalog.projections` 顯示 `is_up_to_date = f`。執行 `REFRESH` 後才變 `t`，實測耗時 **233.3 ms**（30 萬列，refresh method = `scratch`）。在這之前，優化器不會用這個投影，你也量不到它帶來的加速——很容易誤判「新投影沒有效」。

---

## 五、效能與成本分析

把上面的數字整理成決策表：

| 設計動作 | 好處（實測） | 成本（實測） | 什麼時候值得 |
|---|---|---|---|
| 加 `ORDER BY` 對齊點查 / 範圍查 | cost 657 → 193；valindex 修剪 65.2% 列 | 載入多一次排序；儲存 +0.5% | 有明確的過濾鍵，且資料量大 |
| 對齊 GROUP BY 前綴 | GROUPBY HASH 419 → PIPELINED 339 | 同上 | 報表常用固定維度聚合 |
| 用高基數均勻欄位 segmentation | max/min 1.01–1.08 | 無 | 幾乎永遠 |
| 用低基數欄位 segmentation | 無 | 91.1% 集中一個 bucket | 不要做 |
| `DELTAVAL` 用在排序過的時間欄位 | -3.9% | 幾乎無 | 時序資料 |
| `BLOCK_DICT` 用在整個 payload | 無 | +52.1% | 除非基數很低且重複多 |
| 多一個投影 | 特定查詢變快 | 儲存 ×3.93、載入 +22%、ROS ×4 | 有一條查詢長期佔用大量資源 |

兩個容易被忽略的成本：

- **EXPLAIN 的 cost 不等於執行時間**。30 萬列時 cost 657→193 只換到 26% 的實際時間；cost 是相對指標，不是秒數。
- **容器修剪與排序是兩件事**。按月載入讓沒有排序的表也能 pruning，所以「加了 ORDER BY 卻沒感覺」不代表排序沒用，而是你的查詢剛好被容器 min/max 滿足了。

---

## 六、常見錯誤與反模式

### 6.1 用低基數欄位做 segmentation

```sql
SEGMENTED BY HASH(status) ALL NODES
```

本機實測 `HASH(status) % 8`：273,280 列落在 bucket 5、26,720 列落在 bucket 7，其餘 6 個 bucket 為空，max/min = 10.23。多節點時這是「一節點跑、其他節點看」的來源。請改用高基數、分佈均勻且常用於 join 的欄位。

### 6.2 以為 `CREATE TABLE` 上的 ENCODING 會套用到所有投影

```sql
CREATE TABLE t (payload VARCHAR(16) ENCODING NONE, ...);
CREATE PROJECTION t_p (...) AS SELECT ... FROM t ORDER BY ...;
```

實測結果：自動建立的 `t_super` 是 `NONE`，但手動建的 `t_p` 全部是 `AUTO`。**表格層級的 ENCODING 只影響自動投影**，手動投影要在欄位清單裡自己寫。這是靜默行為，不會報錯。

### 6.3 `ENCODING DELTA` 在 25.4 是語法錯誤

```
ERROR 4856:  Syntax error at or near "DELTA" at character 82
LINE 1: ... (i ENCODING DELTAVAL, ts ENCODING DELTA, v ENCODING RLE)
```

同一個位置改成 `ENCODING DELTAVAL` 就成功（INT 與 TIMESTAMP 都可）。不要憑舊文件或直覺寫 `DELTA`；先在本機建一張小表驗證語法。

### 6.4 建完投影忘了 REFRESH

```
WARNING 4468:  Projection <...> is not available for query processing.
```

伴隨 `is_up_to_date = f`。此時查詢仍走舊投影，你會得到「加了投影沒變快」的結論。先 `SELECT REFRESH('schema.table')`，再比較 EXPLAIN 與時間。

### 6.5 對已經被自動取代的投影執行 DROP

在空表上 `CREATE PROJECTION` 後，Vertica 會自動移除原本的自動 super projection：

```
ROLLBACK 4451:  Projection "fact_enc_none_super" does not exist
```

如果你依照舊腳本又去 `DROP PROJECTION ..._super`，會得到這個錯誤。建投影後先查 `v_catalog.projections` 確認實際存在的是哪些。

### 6.6 為了單一查詢不斷加投影

四個投影就是 3.93 倍儲存、+22% 載入、ROS 從 10 變 40。專案初期或許無感，資料長到千萬列、每天多次載入時，這些成本會一起回來。先確認瓶頸是掃描、聚合還是 join，再決定要加投影還是改 SQL / 分區。

---

## 七、風險與建議

### 現在就能做（唯讀 / 低風險）

1. 查目前每張表的投影與排序：
   ```sql
   SELECT anchor_table_name, projection_name, is_segmented, segment_expression
   FROM v_catalog.projections
   WHERE projection_schema = 'your_schema'
   ORDER BY 1,2;
   ```
2. 查投影的實際大小與 ROS 數量，找出「肥胖」或「碎片多」的投影：
   ```sql
   SELECT anchor_table_name, projection_name, node_name,
          row_count, ros_count, used_bytes
   FROM v_monitor.projection_storage
   WHERE anchor_table_schema = 'your_schema'
   ORDER BY used_bytes DESC
   LIMIT 20;
   ```
3. 對慢查詢跑 `EXPLAIN`，確認它選了哪個投影、cost 多少；需要更多細節時再對**自己的測試表**跑 `PROFILE`。
4. 用 `HASH()` 檢查候選 segmentation key 的分佈（把 `8` 換成你的 segment 數）：
   ```sql
   SELECT MOD(HASH(status), 8) AS bucket, COUNT(*)
   FROM your_table GROUP BY 1 ORDER BY 1;
   ```

### 需要變更（要窗口 / 要核准）

1. **新增或修改投影**：先用 `CREATE PROJECTION` 建新投影、`REFRESH`、用 EXPLAIN 與實際時間驗證，再考慮移除舊投影。生產環境請在離峰執行，並確認磁碟有足夠空間（一個投影就是一份完整資料）。
2. **改 segmentation**：會觸發資料重分佈，多節點環境影響網路與磁碟 I/O。先確認新 key 的 `MOD(HASH(...), N)` 分佈在 ±10% 內。
3. **改 encoding**：先用小表或抽樣驗證 bytes/row，再套用到生產；不要一次改全部欄位。實測顯示「全部強制」比 AUTO 大 75.9%。
4. **清理多餘投影**：先確認沒有查詢在使用（`v_monitor.query_profiles` 或 `EXPLAIN`），再 `DROP PROJECTION`。刪除投影不影響表格資料，但會少一份執行計畫的選擇。

---

## 八、參考資料

- [CREATE PROJECTION](https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-statements/create-projection/)
- [Projections](https://docs.vertica.com/25.4.x/en/admin/projections/)
- [v_catalog.projections](https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-projections/)
- [ALTER TABLE（含 encoding 語法）](https://docs.vertica.com/25.4.x/en/sql-reference/statements/alter-statements/alter-table/)
- [Projection best practices](https://docs.vertica.com/25.4.x/en/best-practices/projections/)
- [REFRESH](https://docs.vertica.com/25.4.x/en/sql-reference/functions/refresh/)
