---
layout: post
title: "Vertica 統計資訊管理：ANALYZE_STATISTICS 與查詢計畫為什麼選錯"
date: 2026-11-06 05:00:00 +0800
categories: vertica statistics performance
tags: [vertica, statistics, analyze-statistics, query-plan, explain, optimizer, performance]
description: "從 stale statistics 的角度解釋 Vertica 查詢計畫為什麼選錯，實測 ANALYZE_STATISTICS 前後的 EXPLAIN、資料列估算與 join order 變化，並整理統計資訊的維護策略。"
image: /assets/images/og/vertica-statistics-query-plan-zh.png
faq:
  - q: "為什麼 EXPLAIN 對 100 萬列的表只估 10,000 列？"
    a: "CTAS 與 COPY 不會自動產生 table_statistics，優化器沒有統計時會用 10,000 列估算並標記 NO STATISTICS；執行 ANALYZE_STATISTICS 後才會更新。"
  - q: "跑了 ANALYZE_STATISTICS 就一定準嗎？"
    a: "不一定。低基數 varchar 等值條件實測仍估 600K、實際 50K（12 倍誤差），percent=100 重收也一樣；統計只能改善，不能保證完美。"
  - q: "DELETE 或 TRUNCATE 之後 row_count 會更新嗎？"
    a: "不會即時反映。DELETE 95% 後 row_count 仍是 1,000,000、is_row_count_valid 仍是 t；TRUNCATE 後顯示 1 而不是 0。需要重新收集統計。"
---

<!--
AI Summary: Vertica 25.4.0-0 單節點實測統計資訊管理。CTAS 與 COPY 載入後 v_monitor.table_statistics 沒有資料列，EXPLAIN 對 100 萬列事實表一律估 10,000 列並標記 NO STATISTICS；手動 ANALYZE_STATISTICS 100 萬列只需 67–122 ms，三表 join 計畫由 dim_customer 當外側改為事實表當外側，執行時間 213–215 ms → 191–193 ms。統計不是萬靈丹：低基數 varchar 等值條件估 600K 實際 50K（12 倍誤差），percent=100 重收也一樣；DELETE 95% 後 row_count 仍顯示 1,000,000、is_row_count_valid 仍是 t；TRUNCATE 後 row_count 顯示 1 不是 0。本庫 409 張 user table 有 37 張完全沒有統計，372 張有統計的有 211 張超過 30 天未更新。
AI Keywords: Vertica, ANALYZE_STATISTICS, 統計資訊, EXPLAIN, 查詢計畫, optimizer, stale statistics, table_statistics
-->
<!--more-->

---

## 一、先講結論

查詢計畫選錯，最常見的原因不是 SQL 寫得不好，而是**最佳化器手上的統計資訊是錯的、或根本不存在**。

我在 Vertica 25.4.0-0 單節點上實測，得到四個會直接影響日常維運的結論：

1. **建表與載入不會自動產生統計**。`CREATE TABLE AS SELECT` 建出的 100 萬列表，以及 `COPY` 載入 10 萬列的新表，在 `v_monitor.table_statistics` 裡**完全沒有資料列**，載入後 1.5–5 分鐘再查仍然沒有。`EXPLAIN` 對這些表一律印出 `Rows: 10K (NO STATISTICS)`。
2. **補統計很便宜**。100 萬列 × 6 欄的 `ANALYZE_STATISTICS`，實測 67、82、90、122 ms；10 萬列只要 19 ms。這比一條跑 200 ms 的查詢便宜太多，沒有理由不做。
3. **統計修好後計畫會改，但時間不一定大幅改善**。三表 join 的計畫從「`dim_customer` 當外側、事實表當內側」改成「事實表當外側、`dim_customer` 當內側」，執行時間 213–215 ms → 191–193 ms（約 10%）。在單節點、100 萬列的規模，統計的價值主要體現在**估算正確**，而不是秒數。
4. **統計不是萬靈丹**。低基數 varchar 的等值條件估算仍然不可靠：`status = 'CANCELLED'` 實際只有 50,000 列，最佳化器估 600,000 列，誤差 12 倍。用 `percent = 100` 全量重收後仍然是 600,000。你不能假設「有統計」等於「估算準」。

這篇適合兩種人：一種是 `EXPLAIN` 看到 `NO STATISTICS` 卻不知道該怎麼辦的人，另一種是已經排了 `ANALYZE_STATISTICS` 排程、卻發現計畫還是怪的人。所有數字都來自本機實測，SQL 可複製重跑。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `v25.4.0-0` |
| 節點數 | 1（單節點，Community Edition） |
| 資料庫 | `testdb` |
| 連線使用者 | `dbadmin` |
| 測試 schema | `article_lab_stats`（實驗結束已 `DROP SCHEMA ... CASCADE`） |
| 事實表 | `fact_sales` 1,000,000 列 × 6 欄；`fact_join` 1,000,000 列 × 5 欄 |
| 維度表 | `dim_region` 20 列、`dim_customer` 4,519 列 |
| 資料分佈 | `status`：CLOSED 700,000／OPEN 250,000／CANCELLED 50,000；`region_id = 1` 佔 40% |
| 量測時間 | 2026-11-06 |

三個環境細節會影響你重現實驗：

- **這個連線的 `autocommit` 是 `off`**（`SHOW AUTOCOMMIT` 回 `off`）。`DELETE` 之後一定要明確 `COMMIT`，否則換 session 就看不到變化——我第一次量測就踩到，數字全部對不上。
- 產生測試資料時，`row_number() OVER ()` 搭配 CROSS JOIN 會**每個 stream 各自編號**，我第一版 100 萬列只產生 4,000 個唯一 `order_id`。後來改成算術組合 `(a.i - 1) * 250 + b.i` 才拿到真正的唯一鍵。這不是本文主題，但會讓你以為「主鍵重複」是 Vertica 的 bug。
- `DO_TM_TASK('mergeout')` 不指定表名時會對**全資料庫**跑 mergeout。我在測試機上誤觸一次，跑了 5 分鐘以上還沒結束，最後用 `CLOSE_SESSION()` 中止。不要在生產環境隨手執行。

---

## 三、核心原理

### 3.1 統計資訊存在哪

統計資訊的入口是 `v_monitor.table_statistics`。這張表只有 9 個欄位，25.4 實測如下：

| 欄位 | 型別 | 意義 |
|------|------|------|
| `table_oid` | int | 對應 `v_catalog.tables.table_id`（**不是** `table_id` 這個名字） |
| `table_name` | varchar(128) | 表名 |
| `row_count` | int | 收集當下的資料列數 |
| `is_row_count_valid` | boolean | 標記，實測**不可信**（見 4.6） |
| `stat_collection_time` | timestamptz | 收集時間 |
| `min_partition_key` / `max_partition_key` | varchar(128) | 分區表的邊界 |
| `logical_stats_oid` / `schema_oid` | int | 內部識別碼 |

要 join 回 schema 名稱，得自己接 `v_catalog.tables`：

```sql
SELECT t.table_schema, ts.table_name, ts.row_count,
       ts.is_row_count_valid, ts.stat_collection_time
FROM v_monitor.table_statistics ts
JOIN v_catalog.tables t ON t.table_id = ts.table_oid
WHERE t.table_schema = 'article_lab_stats'
ORDER BY 2;
```

注意這裡用 `t.table_id`，不是 `ts.table_oid` 對 `t.table_oid`——`v_catalog.tables` 沒有 `table_oid` 欄位，寫錯會直接得到 `ERROR 2624: Column "table_oid" does not exist`。

### 3.2 最佳化器拿統計做什麼

| 決策 | 依賴的統計 | 統計錯了會怎樣 |
|------|-----------|----------------|
| Join 順序（誰當 outer／build side） | 兩側的 `row_count` 與過濾後選擇性 | 用大表當 build side，hash 表爆記憶體或 spill |
| 投影選擇 | 投影的排序與列數 | 選到沒有 sort order 優勢的投影，多一次排序 |
| `GROUPBY` 型別（PIPELINED／HASH） | 輸入列數與 group 數量 | 該 pipeline 的變成 hash，多一次 spill |
| 網路傳輸（resegment） | 兩側 segmentation 與列數 | 多一次廣播 |

沒有統計時，最佳化器不會停下來，它會用**預設值 10,000 列**填進去，並在 `EXPLAIN` 印上 `(NO STATISTICS)`。這個預設值對 20 列的小表太大、對 1 億列的事實表太小，兩邊都錯。

### 3.3 統計什麼時候會失效

| 操作 | 實測結果（25.4.0-0） |
|------|----------------------|
| `CREATE TABLE AS SELECT` | **不產生**統計資料列 |
| `COPY` 到新建的空表 | **不產生**統計資料列（載入後 1.5–5 分鐘仍無） |
| `DELETE` 大量資料 | `row_count` **不變**，`is_row_count_valid` 仍為 `t` |
| `TRUNCATE` | `row_count` 變成 **1**（不是 0），`stat_collection_time` 不變 |
| `ALTER TABLE ... ADD COLUMN` | `stat_collection_time` 會刷新，`row_count` 保留 |
| 背景 mergeout | 不保證會更新；我有一個回合約 6 分鐘後仍是舊值 |

這張表就是本文的核心。它說明「統計」不是資料庫自動幫你維護的東西，而是**需要你主動觸發的一次性快照**。文件也間接證實了這一點：`ANALYZE_STATISTICS` 是 `ANALYZE_HISTOGRAM` 的 alias，而 `ANALYZE_HISTOGRAM` 已經不再獨立列在文件裡；兩者都是 meta-function，必須用 `SELECT` 呼叫。

![統計資訊生命週期：載入、ANALYZE、DELETE、TRUNCATE 對 row_count 與 EXPLAIN 估算的影響]({{ '/assets/images/stats-lifecycle.svg' | relative_url }}){: loading="lazy" }

圖中左側是狀態與觸發動作，右側是同一張表在該狀態下 `EXPLAIN` 的估算值；綠色代表統計新鮮、橘色代表統計過期或不存在。

---

## 四、Step-by-step 實測

### 4.1 建立測試資料

```sql
CREATE SCHEMA article_lab_stats;
CREATE TABLE article_lab_stats.seed AS
SELECT row_number() OVER () AS i FROM v_catalog.columns;   -- 4,519 列

CREATE TABLE article_lab_stats.fact_sales AS
SELECT k.id AS order_id,
       CASE WHEN k.id % 10 < 4 THEN 1 ELSE (k.id % 19) + 2 END AS region_id,
       (k.id % 4519) + 1 AS customer_id,
       CASE WHEN k.id % 100 < 70 THEN 'CLOSED'
            WHEN k.id % 100 < 95 THEN 'OPEN'
            ELSE 'CANCELLED' END AS status,
       ((k.id % 1000)::numeric / 10) AS amount,
       TIMESTAMP '2025-01-01 00:00:00' + ((k.id % 365) * INTERVAL '1 day') AS order_ts
FROM (SELECT (a.i - 1) * 250 + b.i AS id
      FROM (SELECT i FROM article_lab_stats.seed WHERE i <= 4000) a
      CROSS JOIN (SELECT i FROM article_lab_stats.seed WHERE i <= 250) b) k;
```

實測載入 100 萬列耗時 549–884 ms，`status` 分佈為 700,000／250,000／50,000，`count(DISTINCT order_id)` = 1,000,000。

### 4.2 沒有統計的計畫

載入後立刻查統計表：

```
 table_name | row_count | is_row_count_valid
------------+-----------+--------------------
 dim_customer |      4519 | t
 dim_region   |        20 | t
(2 rows)
```

`fact_sales` 完全不見了。`EXPLAIN` 的反應很直接：

```
+-SORT [Cost: 2K, Rows: 20 (NO STATISTICS)] (PATH ID: 1)
| +---> JOIN HASH [Cost: 2K, Rows: 400 (NO STATISTICS)] (PATH ID: 3)
| | +-- Outer -> STORAGE ACCESS for f [Cost: 1K, Rows: 10K (NO STATISTICS)] (PATH ID: 4)
| | |      Filter: (f.status = 'CLOSED')
| | +-- Inner -> STORAGE ACCESS for r [Cost: 34, Rows: 20] (PATH ID: 5)
```

三個觀察點：

- `fact_sales` 被估成 **10K**，實際是 1,000,000——低估 100 倍。
- `dim_region` 是 **20**，沒有 `NO STATISTICS` 標記，因為它先前被分析過。**同一個計畫裡可能只有部分表沒有統計**，不要看到一個表有數字就以為全部沒問題。
- 過濾條件 `status = 'CLOSED'` 完全沒有反映在估算上，`Rows: 10K` 就是整張表的預設值。

### 4.3 ANALYZE_STATISTICS：成本與效果

```sql
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_sales');
```

回傳值是 `0`，這不是錯誤碼，也不是列數。實測耗時：

| 表 | 列數 × 欄數 | 耗時 |
|----|-------------|------|
| `t_copy` | 100,000 × 2 | 19 ms |
| `t_sel` | 1,000,000 × 3 | 74 ms |
| `fact_join` | 1,000,000 × 5 | 99 ms |
| `fact_sales` | 1,000,000 × 6 | 67 / 82 / 90 / 122 ms（四次） |
| `fact_sales`（單欄 `status`） | 1,000,000 × 1 | 52 ms |

`v_monitor.table_statistics` 馬上出現：

```
 table_name | row_count | is_row_count_valid |     stat_collection_time
------------+-----------+--------------------+-------------------------------
 fact_sales |   1000000 | t                  | 2026-11-06 05:12:27.843561+08
```

`EXPLAIN` 的估算從 10K 變成 905K，成本從 2K 變成 8K：

```
+-SORT [Cost: 8K, Rows: 20] (PATH ID: 1)
| +---> JOIN HASH [Cost: 7K, Rows: 905K] (PATH ID: 3)
| | +-- Outer -> STORAGE ACCESS for f [Cost: 3K, Rows: 905K] (PATH ID: 4)
| | +-- Inner -> STORAGE ACCESS for r [Cost: 34, Rows: 20] (PATH ID: 5)
```

注意 **905K 而不是 700K**（`status = 'CLOSED'` 的實際列數）。這不是誤差一點點，是 29%。原因在下一節。

### 4.4 直方圖：整數準，字串不準

我對同一個 100 萬列的表，逐個條件比對「估算」與「實際」：

| 條件 | `EXPLAIN` 估算 | 實際列數 | 誤差 |
|------|----------------|----------|------|
| `region_id = 1` | 400K | 400,000 | 0% |
| `status = 'CLOSED'` | 905K | 700,000 | +29% |
| `status = 'OPEN'` | 143K | 250,000 | −43% |
| `status = 'CANCELLED'` | 600K | 50,000 | **+1,100%** |
| `status = 'ZZZ'`（不存在） | 1 | 0 | — |
| `id <= 1000`（10 萬列的表） | 1K | 1,000 | 0% |

整數欄位的等值條件估得幾乎完全準確；**低基數 varchar 的等值條件完全不可靠**，`CANCELLED` 差了 12 倍。而且不是取樣不足造成的——我用三參數版本指定 100% 取樣重收：

```sql
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status', 100);
```

同一張表在預設取樣下 `CLOSED` 估 905K；100% 取樣重收後變成 923K，`OPEN` 143K、`CANCELLED` 600K——**幾乎沒變**。所以這是字串直方圖的估算方式問題，不是樣本量問題。實務上的意思是：如果你的 join order 或投影選擇高度依賴某個 varchar 條件的選擇性，不要相信 `EXPLAIN` 的 `Rows`，要自己 `count(*)` 驗證。

### 4.5 Join order 實驗：統計前後計畫形狀真的會變

用同一份 100 萬列資料建 `fact_join`，跑三表 join。**沒有統計**時：

```
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 2K, Rows: 100 (NO STATISTICS)]
| +---> JOIN HASH [Cost: 2K, Rows: 201 (NO STATISTICS)]
| | +-- Outer -> STORAGE ACCESS for c [Cost: 18, Rows: 5K]
| | +-- Inner -> JOIN HASH [Cost: 2K, Rows: 200 (NO STATISTICS)] (PUSHED GROUPING)
| | | +-- Outer -> STORAGE ACCESS for r [Cost: 17, Rows: 20]
| | | +-- Inner -> STORAGE ACCESS for f [Cost: 2K, Rows: 10K (NO STATISTICS)]
```

`ANALYZE_STATISTICS` 之後：

```
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 9K, Rows: 100]
| +---> JOIN HASH [Cost: 7K, Rows: 905K]
| | +-- Outer -> JOIN HASH [Cost: 4K, Rows: 905K]
| | | +-- Outer -> STORAGE ACCESS for f [Cost: 3K, Rows: 905K]
| | | +-- Inner -> STORAGE ACCESS for c [Cost: 40, Rows: 5K]
| | +-- Inner -> STORAGE ACCESS for r [Cost: 34, Rows: 20]
```

計畫形狀變了：沒有統計時，最佳化器以為事實表只有 10K 列，於是拿 `dim_customer`（5K）當外側；有統計後知道事實表有 905K 列，改成事實表當外側、維度表當 build side，並且把兩個 runtime filter（`SIP1`／`SIP2`）都推到掃描層。

![同一個三表 join 在統計前後的計畫形狀：事實表從內側改為外側]({{ '/assets/images/stats-plan-shape.svg' | relative_url }}){: loading="lazy" }

左邊是沒有統計的計畫，右邊是 `ANALYZE_STATISTICS` 之後的計畫；藍色節點是外側（outer），灰色是內側（build side）。

執行時間（同一條 SQL，各跑三次）：

| 情境 | 第 1 次 | 第 2 次 | 第 3 次 |
|------|---------|---------|---------|
| 沒有統計 | 294.8 ms | 215.0 ms | 212.7 ms |
| 有統計 | 192.2 ms | 192.8 ms | 191.5 ms |

排除第一次的暖機，**215 → 192 ms，約 10%**。而兩表 join（事實表 + 20 列維度表）則是 120–145 ms → 120–125 ms，**在雜訊範圍內**。也就是說：統計的效益取決於「錯誤的估算會不會導致錯誤的計畫形狀」。計畫形狀不變時，補統計只會讓 `EXPLAIN` 變好看，不會變快。

順帶說明那個 294.8 ms 的第一次：三次都是同一條 SQL、同一份資料，只有第一次明顯慢，後兩次穩定在 212–215 ms。這是第一次掃描把 ROS 容器讀進記憶體的暖機成本，跟統計無關。量測計畫改動時，**一定要先丟掉第一次的數字**，否則很容易把暖機效應誤讀成「統計讓查詢變慢 40%」。

### 4.6 DELETE 之後：`is_row_count_valid` 會騙你

這是最容易被忽略的情境。`fact_sales` 有 100 萬列且統計是新鮮的，接著刪掉 95%：

```sql
DELETE FROM article_lab_stats.fact_sales WHERE order_id > 50000;
COMMIT;
```

`DELETE` 回報 `OUTPUT 950000`，`count(*)` 剩 50,000。但統計表：

```
 row_count | is_row_count_valid |     stat_collection_time
-----------+--------------------+-------------------------------
   1000000 | t                  | 2026-11-06 05:14:04.431932+08
```

**`row_count` 還是 1,000,000，`is_row_count_valid` 還是 `t`，時間戳完全沒動。** 我隔了約 6 分鐘再查，仍然是 1,000,000（期間我誤觸了一次全庫 mergeout 並中止，這可能影響背景工作，但結論不變：不能依賴它）。

計畫當然也還是舊的：

| 階段 | `EXPLAIN` 估算 | Cost | 實際列數 | 查詢時間 |
|------|----------------|------|----------|----------|
| DELETE 後、未重收 | 905K | 8K | 50,000 | 34–40 ms |
| `ANALYZE_STATISTICS` 後（221 ms） | 45K | 6K | 50,000 | 33 ms |

重收之後估算 45K（實際 CLOSED 是 35,000，仍高估 29%，同 4.4 的字串問題）。查詢時間幾乎沒差，因為表只剩 5 萬列——**但重收前的估算 905K 對上實際 50,000 列，差了 18 倍，這個錯誤會沿著 join 往上放大**。在真實的三表、五表 join 裡，這種誤差就是選錯 join order 的來源。

順帶一個實測細節：`TRUNCATE` 之後 `row_count` 顯示 **1**（不是 0），`stat_collection_time` 不變。所以「`row_count = 1`」不代表表裡有一列，也不代表統計是新的。

### 4.7 參數變體與常見寫法

```sql
-- 只收單欄
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status');

-- 多欄用逗號串在同一個字串
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status, region_id');

-- 指定取樣百分比（第三個參數）
SELECT ANALYZE_STATISTICS('article_lab_stats.fact_join', 'status', 100);

-- 同一張表的所有欄位
SELECT ANALYZE_HISTOGRAM('article_lab_stats.fact_join');
```

四個都成功，回傳 `0`。單欄重收 100 萬列耗時 52 ms，100% 取樣 67 ms。

### 4.8 一次掃出「沒有統計」與「統計太舊」的表

這條是唯讀的 catalog 查詢，不含任何資料表掃描，可以直接在生產跑：

```sql
SELECT count(*) AS user_tables,
       count(ts.table_oid) AS tables_with_stats,
       count(*) - count(ts.table_oid) AS tables_without_stats
FROM v_catalog.tables t
LEFT JOIN v_monitor.table_statistics ts ON ts.table_oid = t.table_id
WHERE t.is_system_table = false;

SELECT count(*) AS stats_older_than_30d
FROM v_monitor.table_statistics
WHERE stat_collection_time < now() - interval '30 days';
```

本機 `testdb`（清掉測試 schema 後）實測：

```
 user_tables | tables_with_stats | tables_without_stats
-------------+-------------------+----------------------
         409 |               372 |                   37

 stats_older_than_30d
----------------------
                  211
```

409 張 user table 有 37 張完全沒有統計；有統計的 372 張裡，**211 張的統計超過 30 天**。這不是測試庫的特例，而是「沒有排程就沒人管」的常態。

### 4.9 分區表：`ANALYZE_STATISTICS_PARTITION`

文件另外列了 `ANALYZE_STATISTICS_PARTITION`，用來只收集指定分區範圍的統計，適合「每天新增一個分區」的事實表——不必每次重掃歷史分區。這台測試機上的表沒有分區，**本文沒有實測這個函數的行為**。不過我用一個故意打錯的表名確認它存在於這個 build：

```
ERROR 3457:  Function ANALYZE_STATISTICS_PARTITION(unknown) does not exist ...
HINT:  Candidates are:
ANALYZE_STATISTICS_PARTITION(database.schema.table::varchar, min‑range‑value::varchar,
  max‑range‑value::varchar, column‑list::varchar, percent::float)
```

錯誤是因為 `unknown` 無法解析成 varchar，不是函數不存在——HINT 直接把五個參數的簽名印出來了。如果你的表是 `PARTITION BY` 設計，先用 `EXPLAIN` 確認分區裁剪有生效，再決定要不要把排程從全表改成逐分區。

---

## 五、效能／成本分析

把上面的數字整理成決策表：

| 動作 | 成本 | 換到什麼 |
|------|------|----------|
| `ANALYZE_STATISTICS` 10 萬列 | 19 ms | 估算從 10K 變成 100K，`id <= 1000` 估 1K（完全正確） |
| `ANALYZE_STATISTICS` 100 萬列 × 6 欄 | 67–122 ms | join order 修正，三表查詢 −10% |
| `ANALYZE_STATISTICS` 單欄 | 52 ms | 只修一個條件的選擇性 |
| 不做的代價 | 0 ms | 估算低估 100 倍，join order 靠猜 |

幾個判斷原則：

- **載入後一定要收**。這台 25.4 的 CTAS 與 COPY 都不會自動產生統計資料列，`EXPLAIN` 會直接標 `NO STATISTICS`。這是最容易修、投報率最高的一種。
- **大量 DELETE 後一定要收**。`row_count` 不會自己回來，`is_row_count_valid` 也不會變 `f`。
- **不要對整庫無腦排程**。`ANALYZE_STATISTICS` 是 meta-function，一次只吃一張表（或一張表的欄位清單），沒有 schema 層級的語法。要整庫就跑迴圈，並且把大表與小表分開排。
- **優先收「有 join、有 WHERE」的欄位**。單欄 52 ms 比整表 122 ms 便宜，對 100 張表就是 5 秒 vs 12 秒的差別。
- **順序**：先確認計畫形狀會不會變（比較 `EXPLAIN` 的 join 順序），再決定要不要投資更細的統計。形狀不變時，補統計只是讓數字好看。

用本機的數字換算排程成本：409 張表、以平均 50 ms 估算，一輪約 20 秒。如果只看「有 join 事實表」的 30 張核心表，一輪不到 2 秒。真正貴的不是 `ANALYZE_STATISTICS`，而是**沒有統計時那條查詢多跑的 hash join 與 re-segmentation**——那是以秒為單位在燒。

---

## 六、常見錯誤與反模式

### 6.1 以為「載入就有統計」

現象：`EXPLAIN` 出現 `Rows: 10K (NO STATISTICS)`，但表明明有幾百萬列。

```
+-SORT [Cost: 3K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
```

我實測 CTAS 與 COPY 到新表都不會產生 `table_statistics` 資料列。不要看到「載入成功」就假設統計存在，`EXPLAIN` 是唯一可信的檢查點。

### 6.2 用 `is_row_count_valid` 判斷統計新不新鮮

現象：DELETE 掉 95% 的資料，`row_count` 仍是 1,000,000，`is_row_count_valid` 仍是 `t`。

```
 row_count | is_row_count_valid
-----------+--------------------
   1000000 | t
```

這個旗標只代表「這個數字曾經被視為有效」，不代表「它跟現在的資料量一致」。判斷新鮮度請看 `stat_collection_time`，並且**對照 `count(*)`**。

### 6.3 對 schema 呼叫 `ANALYZE_STATISTICS`

現象：

```
ERROR 2065:  analyze_statistics: Invalid table/projection/column article_lab_stats
```

同一個錯誤碼也會出現在表名打錯、欄位名打錯、schema 不存在：

```
ERROR 2065:  analyze_statistics: Invalid table/projection/column article_lab_stats.no_such_table
ERROR 2065:  analyze_statistics: Invalid table/projection/column article_lab_stats.t_copy.no_such_col
```

`ANALYZE_STATISTICS` 沒有 schema 層級參數。要整庫就得自己產 SQL：

```sql
SELECT 'SELECT ANALYZE_STATISTICS(''' || table_schema || '.' || table_name || ''');'
FROM v_catalog.tables
WHERE is_system_table = false AND table_schema = 'your_schema';
```

### 6.4 參數寫錯，拿到看不懂的錯誤

現象：

```
ERROR 3457:  Function ANALYZE_STATISTICS() does not exist, or permission is denied for ANALYZE_STATISTICS()
HINT:  No function matches the given name and argument types. Candidates are:
ANALYZE_STATISTICS(database.schema.table::varchar, column‑list::varchar, percent::float)
```

這個 HINT 其實很好用——它告訴你 25.4 有**三參數**版本（取樣百分比），而這個版本在一般教學文章裡很少提到。

### 6.5 以為 varchar 的等值估算會準

現象：`status = 'CANCELLED'` 估 600K，實際 50,000。就算 `percent = 100` 重收，還是 600K。

這不是統計沒收，是字串直方圖的估算方式不夠細。遇到低基數、高度傾斜的 varchar 條件，請自己 `count(*)`，或用 `EXPLAIN` 搭配實際執行數字驗證，不要只看 `Rows`。

### 6.6 `DO_TM_TASK('mergeout')` 當萬用解

現象：

```
ERROR 6809:  Inappropriate usage of meta-function ("do_tm_task")
```

我第一次寫成 `SELECT DO_TM_TASK('mergeout') FROM (SELECT 1) x;`，得到這個錯誤。改成 `SELECT DO_TM_TASK('mergeout');` 後執行成功，但它會對**全資料庫**跑 mergeout，在測試機上跑了 5 分鐘以上還沒結束，最後我用 `CLOSE_SESSION()` 中止。要用就指定物件，而且不要在尖峰時段。

---

## 七、風險與建議

### 現在就能做（唯讀、低風險）

1. 對所有 `EXPLAIN` 的輸出搜尋 `NO STATISTICS`。有這個字串的查詢，計畫都建立在 10,000 列的假設上。
2. 跑 4.8 的兩條 catalog 查詢，算出「沒有統計」與「統計超過 30 天」的表數量。本機是 37 / 211，你的數字可能更難看。
3. 對重要查詢比對 `EXPLAIN` 的 `Rows` 與 `count(*)`。差距超過 10 倍就值得懷疑統計。
4. 把 `DO_TM_TASK` 從你的「快速修復」清單移除，改用 `ANALYZE_STATISTICS`。

### 需要變更（要窗口、要核准）

1. **建立統計排程**：對事實表每日、維度表每週。100 萬列 100 ms 的量級，實務上可以放在離峰時段跑完整庫迴圈。
2. **針對大表分批**：用三參數版本的 `percent` 控制取樣（例如 10），把單表成本壓在 20 ms 以內；但記得 4.4 的結論——取樣率不影響字串估算的偏差。
3. **搭配 `EXPLAIN` 驗證**：變更後比較 join 順序與成本，不要只看「排程跑成功了」。
4. **監控 `stat_collection_time`**：把「超過 N 天未更新」變成告警，而不是靠人記得。

---

## 八、參考資料

- Vertica 文件：`ANALYZE_STATISTICS`（含「是 `ANALYZE_HISTOGRAM` 的 alias」說明）  
  https://docs.rocketsoftware.com/bundle/vertica_25.3/page/sql-reference/functions/performance-analysis-functions/statistics-management-functions/analyze-statistics.html
- Vertica 文件：`v_monitor.table_statistics` 欄位定義  
  https://docs.rocketsoftware.com/bundle/vertica_25.2/page/sql-reference/system-tables/v-monitor-schema/table-statistics.html
- Vertica 文件：Collecting database statistics（`ANALYZE_STATISTICS` 與 `ANALYZE_STATISTICS_PARTITION`）  
  https://docs.rocketsoftware.com/bundle/vertica_26.3/page/admin/collecting-db-statistics/index.html
- Vertica 文件：`ANALYZE_STATISTICS_PARTITION`（分區表的統計收集）  
  https://docs.rocketsoftware.com/bundle/vertica_25.3/page/sql-reference/functions/performance-analysis-functions/statistics-management-functions/analyze-statistics-partition.html
