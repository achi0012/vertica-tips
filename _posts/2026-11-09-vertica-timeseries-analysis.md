---
layout: post
title: "Vertica 時序分析實戰：TIMESERIES 插值、補洞與事件視窗"
date: 2026-11-09 05:00:00 +0800
categories: vertica timeseries analytics
tags: [vertica, timeseries, interpolation, gap-filling, window-functions, analytics]
description: "Vertica 的 TIMESERIES 子句會把資料重採樣成完整時間格並插值補洞。實測 98,784 筆 5 分鐘資料產生 103,680 個 slice（多 4,896 個是補出來的），但缺口 15 天時 linear 插值會捏造出 23.89→24.00 的平滑斜坡。成本在切片與插值而非掃描：PROFILE 顯示 Scan 1,061 µs、Analytical 節點 23,446 µs。"
image: /assets/images/og/vertica-timeseries-analysis-zh.png
faq:
  - q: "TIMESERIES 會補出沒有資料的時間點嗎？"
    a: "會，而且是整格補滿。實測 12 個感測器、30 天、5 分鐘一筆共 98,784 列，TIMESERIES 輸出 103,680 個 slice，其中 4,896 個完全沒有原始資料；感測器 9 缺 15 天，仍照樣輸出 8,640 個 slice（實際只有 4,320 筆）。"
  - q: "linear 插值在長缺口上安全嗎？"
    a: "不安全。感測器 9 從 2026-01-04 23:55 的 23.89 直接連到 2026-01-20 00:00 的 24.00，中間 4,320 個 slice 全部是算出來的斜坡（2026-01-12 12:00 得到 23.945）；constant（預設）則一路沿用 23.89。兩者都不是真實量測。"
  - q: "TIMESERIES 比 GROUP BY TIME_SLICE 快嗎？"
    a: "沒有。同樣 5 分鐘粒度，TIMESERIES 三次為 45.5/40.2/34.7 ms，GROUP BY TIME_SLICE 為 15.8/15.2/15.3 ms；PROFILE 顯示 Scan 只花 1,061 µs，Analytical（切片＋插值）花 23,446 µs。TIMESERIES 的價值是補洞，不是加速。"
  - q: "為什麼 TIMESERIES 不能配 AVG 或 SUM？"
    a: "本機 25.4.0-0 實測直接報 ERROR 2640（欄位必須在 GROUP BY 或聚合函數中），GROUP BY 也不行（ERROR 3500）。做法是先用 TS_FIRST_VALUE / TS_LAST_VALUE 取樣，再外包一層子查詢做聚合。"
---
<!--
AI Summary: Vertica 25.4.0-0 單節點實測 TIMESERIES。98,784 筆 5 分鐘資料（12 感測器 × 30 天）重採樣成 103,680 個 slice，其中 4,896 個無原始資料；slice 邊界對齊日曆而非首筆資料（1 週 slice 錨在 2025-12-27 星期六，90 秒 slice 從 00:03:00 起）；TS_FIRST_VALUE 取 slice 起點、TS_LAST_VALUE 取終點，linear 會在 15 天缺口上捏造 23.89→24.00 的斜坡；PROFILE 顯示 Scan 1,061 µs、Analytical 23,446 µs、保留 10.6 MB 記憶體；GROUP BY TIME_SLICE 同粒度 15.3 ms 比 TIMESERIES 40.2 ms 快。
AI Keywords: Vertica, TIMESERIES, TS_FIRST_VALUE, TS_LAST_VALUE, interpolation, gap filling, TIME_SLICE, time series, 25.4
-->
<!--more-->

---

## 一、先講結論

Vertica 的 `TIMESERIES` 子句解決的是「資料有空隙」這個問題：它把你的資料重採樣到固定的時間格上，沒有資料的格子也會補一列出來。但它的行為跟多數人預期的不太一樣——它補的是**格線**，不是**值**，而補出來的值可能是插值算出來的假資料。

我在 Vertica 25.4.0-0 單節點上，用 98,784 筆實測資料把 `TIMESERIES` 拆開來看，量到六個結論：

1. **輸出的是時間格，不是你的資料列。** 4 筆原始資料（00:00、00:05、00:15、00:30）會產生 7 個 slice；98,784 筆資料產生 103,680 個 slice，其中 **4,896 個（4.7%）完全沒有原始資料**。
2. **slice 邊界對齊日曆，不對齊你的第一筆資料。** `'1 week'` 把 2026-01-01 的資料放進一個從 **2025-12-27（星期六）** 開始的 slice；`'90 seconds'` 從 00:03:00 開始切。第一格常常落在第一筆資料之前，值就是 `NULL`。
3. **`TS_FIRST_VALUE` 取 slice 起點的值、`TS_LAST_VALUE` 取終點的值。** 預設是「沿用前一筆」（constant 行為），`'linear'` 才是線性插值；但 `'linear'` 只接受整數／浮點（`ERROR 5005`），而且 **`'CONSTANT'` 這個字面值反而會被拒絕（`ERROR 3604`）**。
4. **linear 插值在長缺口上會捏造資料。** 感測器 9 缺了 15 天，插值把它畫成 23.89 → 24.00 的平滑斜坡：2026-01-12 12:00 得到 23.9450127285351，中間 **4,320 個 slice 全部是算出來的**。constant 則一路沿用 23.89。兩者都不是量測值。
5. **成本在切片與插值，不在掃描。** PROFILE：`Scan` 處理 98,784 列只花 **1,061 µs**，`Analytical`（Timeseries）節點輸出 103,680 列花 **23,446 µs**，並保留 10,660,392 bytes。同樣粒度用 `GROUP BY TIME_SLICE()` 只要 15.3 ms，`TIMESERIES` 要 40.2 ms。
6. **它不能配一般聚合函數。** `AVG()`／`SUM()`／`COUNT(*)` 一律 `ERROR 2640`，`GROUP BY` 一律 `ERROR 3500`。要算平均只能先用 `TS_*` 取樣，再外包一層子查詢。

這篇寫給需要做感測器／監控／金融時序報表的人，以及那些發現「圖表畫出一條漂亮直線，但那個時間根本沒開機」的人。所有數字都來自本機實測，`article_lab_ts` schema 在結束時已 `DROP SCHEMA ... CASCADE`。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 節點數 | 1（Community Edition） |
| 資料庫 / 使用者 | `testdb` / `dbadmin` |
| 測試 schema | `article_lab_ts`（實驗後刪除） |
| 主表 | `readings`：98,784 列 × 5 欄 |
| 維度 | 12 個感測器 × 30 天 × 每 5 分鐘一筆（理論值 103,680） |
| 時間範圍 | 2026-01-01 00:00:00 ～ 2026-01-30 23:55:00 |
| 刻意缺口 | 感測器 5 缺 2026-01-10～01-11、感測器 9 缺 2026-01-05～01-19 |
| 載入 | `COPY ... DIRECT`，CSV 3.8 MB |
| 量測日期 | 2026-11-09 |

三個影響重現的細節：

- **`readings` 用預設排序 `(sensor_id, ts)`**，所以 `TIMESERIES ... ORDER BY ts` 可以直接吃儲存順序。我另外建了 `readings_unsorted`（`ORDER BY hum`）當對照組，兩者列數與資料完全相同。
- **單節點只有 1 個 segment**，本篇不談跨節點切分與傾斜。
- **`AUTOCOMMIT` 相關行為已在單一 `vsql -f` session 內完成**；所有 `INSERT` 都顯式 `COMMIT`，避免出現「資料看起來少一筆」的假象。

---

## 三、核心原理

### 3.1 `TIMESERIES` 在查詢計畫裡長什麼樣

```sql
=> EXPLAIN SELECT slice_time, sensor_id,
            TS_FIRST_VALUE(temp,'linear') AS f,
            TS_LAST_VALUE(temp,'linear')  AS l
   FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts);

 +-ANALYTICAL [Cost: 1K, Rows: 99K] (PATH ID: 1)
 |  Analytic Timeseries Group
 |   Functions: time_slice(), ts_first_value(), ts_last_value()
 | +---> STORAGE ACCESS for readings [Cost: 741, Rows: 99K] (PATH ID: 2)
 | |      Projection: article_lab_ts.readings_super
```

它不是「函數」，而是一個**分析節點**（`ANALYTICAL`，內部名稱 `Analytic Timeseries Group`）。這個節點做三件事：把 `ORDER BY` 的時間欄位切成格、依 `PARTITION BY` 逐組套用、對 `TS_*` 函數算值。GraphViz 版本會標出細節：

```
3[label = "Timeseries:
PartitionBy: sensor_id
OrderBy: ts
Analytical Functions:
  time slice alias
  ts_first_value(readings.temp, false, 'linear') ...
```

`sorted input` 這件事很關鍵。`readings_super` 的排序是 `(sensor_id, ts)`，計畫直接接 `StorageMergeStep`；換成 `ORDER BY hum` 的 `readings_unsorted` 就多一條：

```
 |   Group Local Resegment: readings_unsorted.sensor_id
 |   Group Sort: readings_unsorted.sensor_id ASC, readings_unsorted.ts ASC NULLS FIRST
 ...
 5[label = "Sort: (keys = A,A,N)" ...]
```

也就是**多一次完整排序**。這是設計投影時要看的第一件事。

### 3.2 slice 的邊界：對齊日曆，不對齊你的資料

這是誤解最多的地方。`slice_time` 不是「第一筆資料 + n × interval」，而是**對齊到日曆或固定網格**：

| interval | 第一格起點（實測） | 說明 |
|---|---|---|
| `'5 minutes'` | 00:00:00 | 對齊整 5 分鐘 |
| `'90 seconds'` | 00:03:00 | 對齊 90 秒網格，不是從 00:03:30 開始 |
| `'1 hour'` | 00:00:00 | 對齊整點 |
| `'1 day'` | 00:00:00 | 對齊午夜 |
| `'1 week'` | **2025-12-27** | 對齊星期六（連續三格為 01-03、01-10、01-17） |

`'1 week'` 那組資料是 2026-01-01 起，第一格卻是 2025-12-27——**比你的資料早了 5 天**，那一格的值當然是 `NULL`。做週報表時，這一格會被當成「那週沒有資料」，但實際上你根本沒有那週的資料。

還有一個同樣重要的限制：**格線只會延伸到資料的頭尾之間**。把感測器 9 的資料截到 2026-01-02 為止，5 分鐘 slice 就只有 288 個（1 天 × 288），不會補到整整 30 天。所以要「補出未來」或「補出指定區間」是做不到的；`generate_series()` 在 Vertica 也不存在（`ERROR 3457: Function generate_series(int, int) does not exist`）。

### 3.3 `TS_FIRST_VALUE` / `TS_LAST_VALUE` 與兩種插值

用 4 筆資料（00:00=20、00:05=21、00:15=23、00:30=26）跑 5 分鐘 slice，輸出 7 列：

```sql
=> SELECT slice_time, TS_FIRST_VALUE(temp) f_def, TS_LAST_VALUE(temp) l_def,
          TS_FIRST_VALUE(temp,'linear') f_lin, TS_LAST_VALUE(temp,'linear') l_lin
   FROM article_lab_ts.tiny
   TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
     slice_time      | f_def | l_def | f_lin | l_lin
---------------------+-------+-------+-------+-------
 2026-01-01 00:00:00 |    20 |    20 |    20 |    21
 2026-01-01 00:05:00 |    21 |    21 |    21 |    22
 2026-01-01 00:10:00 |    21 |    21 |    22 |    23
 2026-01-01 00:15:00 |    23 |    23 |    23 |    24
 2026-01-01 00:20:00 |    23 |    23 |    24 |    25
 2026-01-01 00:25:00 |    23 |    23 |    25 |    26
 2026-01-01 00:30:00 |    26 |    26 |    26 |
(7 rows)
```

![TIMESERIES 補洞與插值：同一組 4 筆原始資料，constant 是階梯、linear 是斜坡]({{ '/assets/images/timeseries-interpolation-shape.svg' | relative_url }}){: loading="lazy" }

讀法：

- **`TS_FIRST_VALUE` 取 slice 起點時的值**，`TS_LAST_VALUE` 取 slice 終點（起點 + interval）時的值。
- **預設（不寫第二個參數）等於沿用前一筆**：00:10、00:20、00:25 這三個沒有資料的格子，`f_def` 都沿用前一個已知值（21、23、23）。
- **`'linear'` 則是依前後兩筆連線插值**：00:10 得到 (21+23)/2 = 22，00:20 得到 24，00:25 得到 25。
- 最後一格 00:30 的 `l_lin` 是 `NULL`，因為 00:35 已經超過最後一筆資料——**`TIMESERIES` 不做外插**。

補充兩個實測到的細節：

- `'linear'` 只能用在 `INTEGER` 或 `FLOAT` 型別，對 `VARCHAR` 會報 `ERROR 5005: Time Series Aggregate Function with interpolation scheme LINEAR may only have an INTEGER or FLOAT type as its first argument`。`NUMERIC(6,2)` 可以正常用。
- 明示寫 `'CONSTANT'` 會失敗：`ERROR 3604: Interpolation scheme constant for Time Series Aggregate Function ts_first_value is not supported`。也就是**預設行為是 constant，但不能把 constant 寫出來**——只有 `'linear'` 是可指定的合法字面值（本機 25.4.0-0 實測；`'nearest'`、`'ignore_null'` 同樣被 `ERROR 3604` 拒絕）。

---

## 四、Step-by-step 實測

### 4.1 建表與載入

```sql
=> CREATE TABLE article_lab_ts.readings (
     sensor_id INT NOT NULL,
     ts        TIMESTAMP NOT NULL,
     temp      NUMERIC(6,2),
     hum       NUMERIC(6,2),
     status    VARCHAR(8)
   );
=> COPY article_lab_ts.readings FROM '/tmp/opencode/ts_readings.csv' DELIMITER '|' DIRECT;
 Rows Loaded
-------------
       98784
Time: First fetch (1 row): 168.253 ms.
```

同一份 CSV 載入 `ORDER BY hum` 的對照表只要 **94.7 ms**——**按時間排序的表反而慢 78%**（168.3 vs 94.7 ms）。我沒有進一步拆解這個差異（載入時的排序、編碼與壓縮都混在一起），但方向與上一篇分區文章一致：**載入成本取決於「輸入順序是否符合投影排序」，不是資料量**。

### 4.2 補洞到底補了多少：4,896 個空 slice

```sql
=> SELECT COUNT(*) FROM (
     SELECT slice_time, sensor_id, TS_FIRST_VALUE(temp,'linear') f
     FROM article_lab_ts.readings
     TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)) x;
 103680
```

理論值 12 感測器 × 8,640 格 = 103,680，完全吻合。真實資料只有 98,784 筆，差額 **4,896** 全部是補出來的：

| 感測器 | 原始列數 | 輸出 slice | 補出 | 原因 |
|---|---|---|---|---|
| 1–4、6–8、10–12（10 個） | 8,640 | 8,640 | 0 | 無缺口 |
| 5 | 8,064 | 8,640 | 576 | 缺 2 天 |
| 9 | 4,320 | 8,640 | **4,320** | 缺 15 天 |
| 合計 | 98,784 | 103,680 | **4,896** | |

換成 1 分鐘粒度：

```sql
=> -- 1 minute slices
 518352
```

518,352 = 12 × 43,196（最後一格是 23:55，所以不是 43,200）。**輸出列數放大 5.2 倍**，而原始資料完全沒變。一張 30 天、12 個感測器的表就要輸出半百萬列——這是把 `TIMESERIES` 接上 BI 工具前必須先算的帳。

### 4.3 插值會發明資料：15 天缺口被畫成斜坡

感測器 9 的缺口是 2026-01-05 00:00 ～ 2026-01-19 23:55。缺口前後的真實資料：

```sql
=> SELECT ts, temp FROM article_lab_ts.readings
   WHERE sensor_id=9 AND ts BETWEEN '2026-01-04 23:40' AND '2026-01-05 00:10'
    OR sensor_id=9 AND ts BETWEEN '2026-01-19 23:45' AND '2026-01-20 00:10' ORDER BY ts;
         ts          | temp
---------------------+-------
 2026-01-04 23:40:00 | 23.56
 2026-01-04 23:45:00 | 23.67
 2026-01-04 23:50:00 | 23.78
 2026-01-04 23:55:00 | 23.89
 2026-01-20 00:00:00 | 24.00
```

把 `TIMESERIES` 的輸出包一層子查詢，挑幾個時間點：

| slice_time | linear | 預設（constant） |
|---|---|---|
| 2026-01-04 23:50 | 23.78 | 23.78 |
| 2026-01-05 00:00 | 23.8900254570701 | 23.89 |
| **2026-01-12 12:00** | **23.9450127285351** | 23.89 |
| 2026-01-19 23:55 | 23.9999745429299 | 23.89 |
| 2026-01-20 00:00 | 24 | 24.00 |

`linear` 在 15 天的缺口上畫出一條從 23.89 到 24.00 的完美斜坡，小數點後十幾位都有值。**那 4,320 個 slice 全部是算出來的，沒有任何一筆量測**。把這個 view 接上溫度告警，你會得到一條永遠在緩慢上升、永遠不會觸發門檻的線。

constant 看起來保守，但它一樣是假的：23.89 連續 15 天代表「感測器穩定輸出」，而事實是「感測器離線」。**兩種插值都不能回答「這段時間有沒有資料」**，要回答那個問題必須另外標記。

### 4.4 標記哪些列是補出來的

`TIMESERIES` 的輸出不帶「是否為真實資料」的旗標。實務做法是自己 join 回原始資料的計數：

```sql
=> SELECT v.slice_time, ROUND(v.temp_at_start,3) AS t, COALESCE(r.n,0) AS real_rows
   FROM article_lab_ts.v5 v
   LEFT JOIN (SELECT TIME_SLICE(ts,5,'MINUTE') AS b, sensor_id, COUNT(*) AS n
              FROM article_lab_ts.readings GROUP BY 1,2) r
     ON v.slice_time = r.b AND v.sensor_id = r.sensor_id
   WHERE v.sensor_id = 9
     AND v.slice_time IN ('2026-01-04 23:55','2026-01-12 12:00','2026-01-20 00:00');
     slice_time      |   t    | real_rows
---------------------+--------+-----------
 2026-01-04 23:55:00 |  23.89 |         1
 2026-01-12 12:00:00 | 23.945 |         0
 2026-01-20 00:00:00 |     24 |         1
```

`real_rows = 0` 就是補出來的列。整個 view 掃一遍，補出的列數正好是 **4,896**，與 4.2 節的差額一致。代價是查詢時間從 25.2 ms 變成 **97.5 ms**（多了 hash join 與 98,784 列的聚合）。

順帶一提，`TIMESERIES` **可以包成 view**（`CREATE VIEW` 成功），查詢時再過濾 `slice_time`：

```sql
=> CREATE VIEW article_lab_ts.v5 AS
   SELECT slice_time, sensor_id, TS_FIRST_VALUE(temp,'linear') AS temp_at_start,
          TS_LAST_VALUE(temp,'linear') AS temp_at_end
   FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts);
CREATE VIEW
```

但**不能在 view 外面用 `WHERE slice_time = ...` 期待它下推**——view 會先算出 103,680 列。大表搭配窄時間範圍時，過濾條件要寫在 `TIMESERIES` 之前的 `WHERE`（過濾原始資料）：

```sql
=> -- 這樣只掃一天，缺口仍是缺口
   SELECT slice_time, TS_FIRST_VALUE(temp,'linear') AS f
   FROM article_lab_ts.readings
   WHERE ts >= '2026-01-10' AND ts < '2026-01-11'
   TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
```

### 4.5 資料點是 NULL 時，carry-forward 會斷

```sql
=> SELECT slice_time, TS_FIRST_VALUE(v,'linear') lin, TS_FIRST_VALUE(v) c
   FROM article_lab_ts.nulls TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
     slice_time      | lin |  c
---------------------+-----+------
 2026-02-01 00:00:00 |   1 | 1.00
 2026-02-01 00:05:00 |     |
 2026-02-01 00:10:00 |   5 | 5.00
 2026-02-01 00:15:00 |   6 | 5.00
 2026-02-01 00:20:00 |   7 | 7.00
```

原始資料是 `(00:00,1) (00:05,NULL) (00:10,5) (00:20,7)`。00:05 那格有一筆真實資料，但值是 `NULL`，於是 **`lin` 與 `c` 都是 `NULL`**——不會沿用 1.00。下一格 00:10 才恢復。也就是說：**空 slice 會沿用前一值，值為 NULL 的 slice 不會**。這兩種「沒資料」在輸出上長得不一樣，卻都很容易被下游當成 0 或當成缺值。

### 4.6 不能寫的 SQL

這幾條是實際撞到的，每一條都附原訊息：

```sql
=> SELECT slice_time, AVG(temp) FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY ts);
ERROR 2640:  Column "readings.ts" must appear in the GROUP BY clause or be used in an aggregate function

=> SELECT slice_time, sensor_id, COUNT(*) FROM article_lab_ts.readings
   TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts);
ERROR 2640:  Column "readings.sensor_id" must appear in the GROUP BY clause or be used in an aggregate function

=> SELECT slice_time, COUNT(*) FROM ... TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)
   GROUP BY slice_time, sensor_id;
ERROR 3500:  HAVING / GROUP BY not allowed with Time Series query
```

`AVG()`、`SUM()`、`COUNT(*)`、`MIN()` 全都不行，`GROUP BY` 也不行。**`TIMESERIES` 的 select list 只允許 `slice_time`、partition 欄位、以及 `TS_*` 函數的結果。** 要算平均就得外包子查詢：

```sql
=> SELECT slice_time, AVG(temp) FROM (
     SELECT slice_time, sensor_id, TS_FIRST_VALUE(temp,'linear') AS temp
     FROM article_lab_ts.readings
     TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)) x
   GROUP BY 1;
```

另外兩個語法坑：

```sql
=> ... TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id);
ERROR 4856:  Syntax error at or near ")" at character 136        -- ORDER BY 是必填

=> ... TIMESERIES slice_time AS '5 minutes' OVER (PARTITION BY sensor_id ORDER BY ts) WHERE sensor_id = 9;
ERROR 4856:  Syntax error at or near "WHERE" at character 224    -- WHERE 不能放在 OVER 之後

=> ... TIMESERIES slice_time AS 5 OVER (ORDER BY ts);
ERROR 5012:  Time slice length must be an interval constant       -- 不能寫整數

=> ... TIMESERIES slice_time AS '5 minutes' OVER (ORDER BY sensor_id);
ERROR 4297:  ORDER BY column in timeseries OVER clause must be Timestamp type

=> -- 把 TS_* 用到 TIMESERIES 子查詢的外層
   SELECT slice_time, TS_FIRST_VALUE(f) FROM (SELECT ... TS_FIRST_VALUE(temp) AS f ... TIMESERIES ...) x;
ERROR 2100:  A query with Time Series Aggregate Function ts_first_value must have a timeseries clause
```

`WHERE` 只能放在 `TIMESERIES` **之前**（過濾原始資料），要過濾 `slice_time` 必須包成子查詢。

### 4.7 tumbling slice 抽樣 vs 滑動視窗聚合

`TIMESERIES` 做的是「在每個格子起點取一個樣」，**它不做區間聚合**。要算「過去 15 分鐘的平均」，得用 window function：

```sql
=> SELECT slice_time, ROUND(f,3) AS slice_start_temp FROM (
     SELECT sensor_id, slice_time, TS_FIRST_VALUE(temp,'linear') AS f
     FROM article_lab_ts.readings WHERE sensor_id=1 AND ts < '2026-01-01 01:00'
     TIMESERIES slice_time AS '15 minutes' OVER (PARTITION BY sensor_id ORDER BY ts)
   ) x WHERE slice_time IN ('2026-01-01 00:15','2026-01-01 00:30') ORDER BY 1;
     slice_time      | slice_start_temp
 --------------------+------------------
 2026-01-01 00:15:00 |            21.33
 2026-01-01 00:30:00 |            21.65

=> SELECT ts, rolling15, n FROM (
     SELECT ts,
            ROUND(AVG(temp) OVER (ORDER BY ts RANGE BETWEEN INTERVAL '14 minutes' PRECEDING
                                  AND CURRENT ROW),3) AS rolling15,
            COUNT(*) OVER (ORDER BY ts RANGE BETWEEN INTERVAL '14 minutes' PRECEDING
                           AND CURRENT ROW) AS n
     FROM article_lab_ts.readings WHERE sensor_id=1) y
   WHERE ts IN ('2026-01-01 00:15','2026-01-01 00:30') ORDER BY 1;
          ts          | rolling15 | n
 ---------------------+-----------+---
  2026-01-01 00:15:00 |    21.22 | 3
  2026-01-01 00:30:00 |    21.543 | 3
```

原始資料是 00:00 起每 5 分鐘一筆（21.00 / 21.11 / 21.22 / 21.33 / 21.44 / 21.54 / 21.65）。所以 21.33 是「00:15 這個時點的值」，21.22 是「00:01–00:15 三筆的平均」——**一個是抽樣、一個是聚合，語意不同，不能互換**。`RANGE` 框不含 00:00 那筆（視窗是 00:01 起算），所以 `n = 3` 不是 4。

還有一個實測踩到的坑：把 `WHERE ts IN (...)` 寫在同一層，`n` 會變成 **1**（只剩自己那一列），因為 `WHERE` 在視窗計算之前就先把其他列濾掉了。**視窗要在子查詢裡算完，外層才過濾。**

---

## 五、效能與成本分析

同一份資料、同樣 5 分鐘粒度，三種寫法各跑三次（`vsql \timing`，輸出用 `COUNT(*)` 包住以避免傳輸成本）：

| 寫法 | 三次耗時 (ms) | 中位數 | 輸出列數 |
|---|---|---|---|
| `TIMESERIES`（排序表 `(sensor_id, ts)`） | 45.512 / 40.199 / 34.675 | **40.2** | 103,680 |
| `TIMESERIES`（`ORDER BY hum` 表） | 22.637 / 21.314 / 22.530 | **22.5** | 103,680 |
| `GROUP BY TIME_SLICE(ts,5,'MINUTE')` | 15.760 / 15.219 / 15.286 | **15.3** | 98,784 |
| `TIMESERIES` 1 分鐘粒度 | 60.791 / 61.942 / 59.999 | **60.8** | 518,352 |
| `LAG()` 找缺口（不做補洞） | 20.345 | **20.3** | 2 列（缺口清單） |

`TIMESERIES` 比 `GROUP BY TIME_SLICE` 慢 **2.6 倍**，因為它多做兩件事：產生 4,896 個空 slice、對每個 slice 算插值。反過來說，`GROUP BY TIME_SLICE` 拿不到補洞——它只回傳真的有資料的 98,784 格。

`readings_unsorted` 反而比排序表快（22.5 vs 40.2 ms），這個結果與直覺相反，我不建議外推：兩張表都只有 1 個 ROS container、資料量在 100 MB 等級、全部在頁快取裡，排序成本被掃描與節點初始化稀釋掉了。可以確定的是：**這個規模下排序不是瓶頸；規模放大後才會**。要看的是計畫形狀（有沒有多一個 `Sort` 節點），而不是這 18 ms 的差距。

PROFILE 把成本拆得更清楚（自建實驗表，非生產表）：

| 節點 / 寫法 | rows processed | rows produced | execution time |
|---|---|---|---|
| `Scan`（兩種寫法都是） | 98,784 | 98,784 | 1,061 µs |
| `Analytical`（TIMESERIES 5 分鐘） | — | 103,680 | **23,446 µs** |
| `GroupByPipe`（GROUP BY TIME_SLICE） | — | 98,784 | 10,929 µs |
| `Analytical`（TIMESERIES 1 分鐘） | — | 518,352 | **46,315 µs** |

**掃描只佔 4.5%（1,061 / 23,446），其餘全在切片與插值。** 而切片節點保留 **10,660,392 bytes（約 10.2 MB）** 給 103,680 列輸出。這件事在併發時才有殺傷力：20 條這種查詢同時跑就是 200 MB 以上的資源池佔用。

而且**縮小時間範圍幾乎不會讓記憶體變小**。把 `WHERE ts >= '2026-01-10' AND ts < '2026-01-11'` 加在 `TIMESERIES` 之前，輸出從 103,680 列掉到 **2,880 列**（36 倍），切片的 execution time 從 23,446 µs 掉到 398 µs，但 `Analytical` 的 `memory reserved` 只從 10,660,392 降到 **8,467,152 bytes（−21%）**。原因是切片節點依 partition 數與批次配置記憶體，不是依輸出列數。

```sql
=> -- 一天、12 個 partition 的切片
   operator_name | rows_out | exec_us |    mem
 -----------------+----------+---------+-----------
   Scan          |    2,880 |     948 |   102,576
   Analytical    |    2,880 |     398 | 8,467,152
```

所以在資源池規劃上，**`TIMESERIES` 的記憶體要用「並行查詢數 × 8–10 MB」估，不是用輸出列數估**。

還有一個容易忽略的計畫問題：**優化器不知道切片會放大列數**。

```sql
=> -- 收集統計前
   ANALYTICAL [Cost: 2K, Rows: 10K (NO STATISTICS)]
=> SELECT ANALYZE_STATISTICS('article_lab_ts.readings');
=> -- 收集統計後
   ANALYTICAL [Cost: 1K, Rows: 99K]     -- 實際輸出 103,680
=> -- 改成 1 分鐘粒度
   ANALYTICAL [Cost: 1K, Rows: 99K]     -- 實際輸出 518,352
```

`EXPLAIN` 的 `Rows` 永遠是**輸入列數**，不管 slice 是 5 分鐘還是 1 分鐘。所以 `Rows: 99K` 對應 103,680（差 4.7%）還算合理，對應 518,352 就低估了 5.2 倍。**不要用 `EXPLAIN` 的 Rows 去估 `TIMESERIES` 的輸出**，要自己算：`partition 數 × (時間跨度 / interval)`。

---

## 六、常見錯誤與反模式

### 1. 把 `TIMESERIES` 當成「加速時序聚合」的工具

它比較慢（40.2 vs 15.3 ms），而且輸出更多列。**它的價值是補洞與對齊，不是效能。** 如果只是要「每 5 分鐘一桶」，`GROUP BY TIME_SLICE(ts,5,'MINUTE')` 就夠了。

### 2. 用 `linear` 補長時間缺口，然後相信它

15 天缺口被補成 23.89 → 24.00 的斜坡，中間 4,320 個值全是算的。實務原則：**缺口長度超過「一個正常取樣間隔的 2–3 倍」就不要插值**，改用 constant（或直接保留 `NULL` 讓下游知道缺資料）。25.4 沒有任何控制插值距離的參數：

```sql
=> SELECT parameter_name, current_value FROM configuration_parameters
   WHERE parameter_name ILIKE '%timeseries%' OR parameter_name ILIKE '%interpolat%';
 parameter_name | current_value
----------------+---------------
(0 rows)
```

所以這個判斷必須寫在 SQL 外層：用 4.4 節的 `real_rows` 旗標把長缺口濾掉，或直接 `WHERE temp IS NOT NULL` 只留真實值。

### 3. 在 `TIMESERIES` 之後才過濾時間

```sql
ERROR 4856:  Syntax error at or near "WHERE" at character 224
```

`WHERE` 放在 `OVER (...)` 之後直接語法錯誤；包成子查詢可以跑，但 **103,680 列已經算完了**。要省成本，條件必須寫在 `TIMESERIES` 前、作用在原始資料上。

### 4. 以為 slice 會從第一筆資料開始

`'1 week'` 把 2026-01-01 的資料放進 2025-12-27 起的格子，第一格是 `NULL`。週報表如果直接 `WHERE slice_time = 週一`，會整張表查不到資料。

### 5. 以為空 slice 和 NULL 值一樣

空 slice 會沿用前一值；**值為 `NULL` 的 slice 不會**（4.5 節實測）。清洗資料時如果把 `NULL` 改成 0，插值結果會完全不同。

### 6. 用 `ORDER BY` 非時間欄位的投影跑 `TIMESERIES`

計畫會多一個 `Sort` 節點。本次 100 MB 資料量還看不出代價，但這是少數「改了投影就變好、不用改 SQL」的項目，值得先檢查：

```sql
=> EXPLAIN ... | grep -E 'Group Sort|StorageMergeStep|Sort:'
```

### 7. 對 5 分鐘資料開 1 秒或 1 分鐘粒度

1 分鐘粒度輸出 518,352 列（5.2 倍）；照同樣比例，1 秒粒度約 3,100 萬列（12 × 2,592,000）。**粒度要跟原始取樣頻率對齊**，比原始資料細的格子全部是插值，沒有任何資訊量。

---

## 七、風險與建議

### 現在就能做（唯讀／低風險）

1. **盤點「有多少查詢在用 `TIMESERIES`」**，並確認粒度是否與取樣頻率一致：

```sql
=> SELECT user_name, LEFT(request, 120) AS req, request_duration_ms
   FROM v_monitor.query_requests
   WHERE request ILIKE '%TIMESERIES%' AND start_timestamp > now() - INTERVAL '7 days'
   ORDER BY request_duration_ms DESC LIMIT 20;
```

2. **檢查高頻時序查詢的計畫有沒有多餘的 `Sort`**：用 `EXPLAIN` 看有沒有 `Group Sort` / `Sort:` 節點；如果有，代表投影排序前綴與 `TIMESERIES ... ORDER BY` 不一致。
3. **在測試環境用 `PROFILE` 量 `Analytical` 節點的 `rows produced` 與 `memory reserved`**，換算出併發時的資源池需求（本次 103,680 列約 10.2 MB）。
4. **把「補出來的列」標記起來**（4.4 節的 `real_rows`），至少在報表層區分真實值與插值。

### 需要變更（要窗口／要核准）

1. **調整投影排序**讓 `(partition 欄位, 時間欄位)` 成為前綴，消掉 `TIMESERIES` 的 `Sort`。這會影響載入與 mergeout，建議一次一張表並觀察一週。
2. **把 `TIMESERIES` 從 BI 查詢路徑移到預先計算的表**（materialized 概念，Vertica 無原生 MV，需自行排程 `INSERT ... SELECT`）。理由是每次查詢都要重算切片，成本無法攤提。
3. **把粒度改成與來源一致的 5 分鐘**，淘汰 1 分鐘粒度的報表。這會改變既有報表的數字（補值變少），需要跟使用者確認。
4. **對長時間缺口改採 constant 或保留 `NULL`**。這是數值語意變更，務必先在非生產環境比對新舊輸出。

---

## 八、參考資料

- Vertica 25.4 文件：Time series analytics（`TIMESERIES`、`TS_FIRST_VALUE`、`TS_LAST_VALUE`、gap filling 與 interpolation 概念）— <https://docs.vertica.com/25.4.x/en/data-analysis/time-series-analytics/>
- Vertica 25.4 文件：TIMESERIES clause（語法、`slice_time`、`PARTITION BY`、`ORDER BY` 必填）— <https://docs.vertica.com/25.4.x/en/sql-reference/statements/select/timeseries-clause/>
- Vertica 25.4 文件：`TS_FIRST_VALUE`（回傳 slice 起點的值，interpolation 參數）— <https://docs.vertica.com/25.4.x/en/sql-reference/functions/aggregate-functions/ts-first-value/>
- Vertica 25.4 文件：`TIME_SLICE`（不補洞的分桶函數，本文比較組）— <https://docs.vertica.com/25.4.x/en/sql-reference/functions/time-slice/>
- OpenText/Vertica 社群：Time Series Gap Filling at Scale（大規模補洞的實務討論）— <https://community.opentext.com/data-analytics/b/data-analytics-blog/posts/time-series-gap-filling-at-scale-opentext-analytics-database-vertica-s-built-in-magic>
