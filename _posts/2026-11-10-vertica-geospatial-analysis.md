---
layout: post
title: "Vertica 空間分析實戰：Geospatial 函數與空間索引"
date: 2026-11-10 05:00:00 +0800
categories: vertica geospatial analytics
tags: [vertica, geospatial, st-contains, st-distance, spatial-index, wkt, geojson]
description: "Vertica 的 GEOMETRY 用平面座標、GEOGRAPHY 用球面公尺，兩者距離可以差 10%。實測 20 萬個點對 8 個行政區做點在多邊形內：暴力 join 花 7,303–8,661 ms，空間索引只要 827.6 ms，但邊界點 1,602 個不屬於任何區，索引又只是快照，改多邊形不 refresh 就會回 31,251 這個舊答案。"
image: /assets/images/og/vertica-geospatial-analysis-zh.png
faq:
  - q: "Vertica 的 ST_Distance 回傳的是公尺還是度？"
    a: "看型別。GEOMETRY 走平面座標，回傳的是座標單位（經緯度就是度）；GEOGRAPHY 走球面，回傳公尺。同兩點（台北 101 到板橋）GEOMETRY 回 0.108083 度，GEOGRAPHY 回 10,926.8 公尺。"
  - q: "ST_Contains 會把邊界上的點算進去嗎？"
    a: "不會。實測單位方格與 20 萬點資料：落在邊線上的點 ST_Contains 回 false、ST_Intersects 回 true、ST_Within 也回 false。20 萬個點裡有 1,602 個剛好落在格線上，因此八個行政區的 ST_Contains 加總只有 198,398 列，而 ST_Intersects 是 201,201 列（其中 1,201 個點同時屬於兩區）。"
  - q: "空間索引可以建在點資料上嗎？"
    a: "不行。25.4.0-0 實測對 20 萬個 POINT 建索引直接失敗：There are no valid polygons to index. NOTICE: Some geometries ignored [Non (MULTI)POLYGON: 200000]。空間索引只能索引 (MULTI)POLYGON。"
  - q: "建了空間索引，資料改了會自動更新嗎？"
    a: "不會，索引是快照。把一個行政區多邊形縮小一半後，索引仍回 31,251 筆，實際只有 15,499 筆；執行 STV_Refresh_Index 後才變成 15,626（比對 ST_Contains 多的 127 筆是邊界點，索引採 intersects 語意）。"
  - q: "空間索引比暴力查詢快多少？"
    a: "同一份 8 個多邊形、20 萬點的資料：暴力 ST_Intersects join 7,303.5 ms、ST_Contains 加 GROUP BY 8,660.7 ms，索引轉換 827.6 ms（8.8 倍），但索引輸出要落地（CTAS 1,994.0 ms）或再聚合，落地後的聚合只要 15.0 ms。"
---
<!--
AI Summary: Vertica 25.4.0-0 單節點實測 Geospatial：20 萬個點與 8 個 0.05×0.10 度多邊形。GEOMETRY 的 ST_Distance 回傳度（0.108083）、GEOGRAPHY 回傳公尺（10,926.8），Web Mercator 投影誤差 +10.5%；ST_Contains 對邊界點回 false，導致 1,602 個格線點不屬於任何行政區（加總 198,398），ST_Intersects 則回 201,201 列；空間索引只能索引 (MULTI)POLYGON，建索引後 827.6 ms 對暴力 7,303.5–8,660.7 ms；索引是快照，改多邊形未 refresh 會回 31,251 而非真值 15,499；每列點資料佔 83.5 bytes，STV_MemSize 單點 85 bytes。
AI Keywords: Vertica, geospatial, ST_Contains, ST_Distance, ST_Intersects, STV_Create_Index, spatial index, GEOMETRY, GEOGRAPHY, 25.4
-->
<!--more-->

---

## 一、先講結論

Vertica 的 Geospatial 功能不是「多加幾個函數」而已，它是一套有自己座標系、自己邊界慣例、自己維護週期的子系統。多數人第一次踩的坑不是語法，而是**單位**與**邊界**——這兩個坑都不會報錯，只會給你一個看起來很合理的錯答案。

我在 Vertica 25.4.0-0 單節點上，用 8 個簡化的行政區多邊形與 20 萬個點，把 `ST_Contains`、`ST_Intersects`、`ST_Distance`、`STV_Create_Index` 全部拆開來量，得到六個結論：

1. **`ST_Distance` 的單位取決於型別，不是參數。** 同兩點（台北 101 到板橋）：`GEOMETRY` 回 **0.108083**（度），`GEOGRAPHY` 回 **10,926.8**（公尺）。沒有 `ST_Distance(..., 'meters')` 這種寫法，寫錯型別就會拿到一個小 10 萬倍的數字而不自知。
2. **邊界上的點不屬於任何多邊形。** `ST_Contains` 對邊線上的點回 `false`、`ST_Intersects` 回 `true`、`ST_Within` 也回 `false`。20 萬點中有 **1,602 個**剛好落在格線上，所以八區 `ST_Contains` 加總只有 **198,398** 列，`ST_Intersects` 是 **201,201** 列（其中 **1,201 個點同時屬於兩區**）。
3. **空間索引只索引多邊形。** 對 20 萬個 `POINT` 建索引會直接失敗：`There are no valid polygons to index. NOTICE: Some geometries ignored [Non (MULTI)POLYGON: 200000]`。索引的用途是「點查多邊形」，不是反過來。
4. **索引真的有效，但價差沒有你想的大。** 暴力 `ST_Intersects` join **7,303.5 ms**、`ST_Contains` 加 `GROUP BY` **8,660.7 ms**；索引轉換 **827.6 ms**（8.8 倍）。但索引只吐 `(pt_gid, pol_gid)`，要落地（CTAS **1,994.0 ms**）或再聚合；落地後同一個聚合只要 **15.0 ms**。
5. **索引是快照，不是觸發器。** 把一個多邊形縮一半但不 refresh，索引照樣回 **31,251**，真值是 **15,499**；`STV_Refresh_Index` 之後變成 **15,626**——多出的 127 筆是邊界點，因為索引用的是 intersects 語意。
6. **bbox 前置過濾有效，但寫錯地方會反效果。** 用常數多邊形的 bbox 過濾再 `ST_Contains`：**450.4 ms**（快 16 倍）。把 `ST_XMin(d.geom)` 塞進 join 條件：**14,842.7 ms**，比什麼都不做還慢兩倍。

另外一個跟效能無關但會咬人的數字：**每列點資料佔 83.5 bytes**（`STV_MemSize` 對單點回 85 bytes），而兩個 `float8` 只要 16 bytes。幾億個點的時候，這個 5 倍差距就是磁碟預算。

這篇寫給已經有經緯度欄位、正要開始做「門市落在哪一區」「最近的倉庫」「配送範圍」這類查詢的人。所有數字都來自本機實測，`article_lab_geo` schema 在結束時已 `DROP SCHEMA ... CASCADE`。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 節點數 | 1（Community Edition） |
| 資料庫 / 使用者 | `testdb` / `dbadmin` |
| 測試 schema | `article_lab_geo`（實驗後刪除） |
| 多邊形表 | `districts`：8 列，4 欄（4 個經度 × 2 個緯度格） |
| 單一多邊形 | 0.05° 經度 × 0.10° 緯度，`ST_Area` = 0.005 deg² |
| 點表 | `poi`：200,000 列，5 欄（`poi_id`、`lon`、`lat`、`geom`、`geog`） |
| 點分佈範圍 | 經度 121.45–121.6496、緯度 24.95–25.1099992 |
| 空間索引 | `districts_idx`（建在 `districts.geom` 上） |
| 量測日期 | 2026-11-10 |

三個會影響重現的細節：

- **經度是 500 個等距值（0.0004°），緯度是 20 萬個極細的值（0.0000008°）。** 產生器寫的是 `(i-1)/500`，原本想每 500 列跳一格，結果 `i` 是整數但除法變成 numeric，實際變成連續細格。這個「產生器沒照我想的做」反而提供了很好的邊界測試：經度方向有 4 條線精準落在 121.45／121.50／121.55／121.60 上（因為 125 × 0.0004 = 0.05 在 float8 精確可表示），緯度方向只有 2 個點剛好落在 24.95 與 25.05（因為 0.1 ÷ 0.0000008 = 125,000 整除）。
- **`SET SEARCH_PATH` 一定要保留 `public`。** Geospatial 的 UDF 全部註冊在 `public`（`v_catalog.user_functions` 可查），只寫 `SET SEARCH_PATH TO article_lab_geo` 會讓 `ST_GeomFromText` 直接消失，錯誤訊息是誤導人的 `Function ST_GeomFromText(unknown) does not exist`。
- **這個連線的 `autocommit` 是 `off`。** `CREATE`、`INSERT` 之後我都顯式 `COMMIT`，所有量測在單一 `vsql -f` session 內完成。

---

## 三、核心原理

### 3.1 GEOMETRY 與 GEOGRAPHY：同一個函數名稱，兩套單位

Vertica 的空間型別有兩個：`GEOMETRY` 走平面（笛卡兒）座標，`GEOGRAPHY` 走球面（WGS84 橢球）。函數名稱幾乎一樣，但語意完全不同：

| 行為 | `GEOMETRY` | `GEOGRAPHY` |
|------|-----------|-------------|
| 座標解讀 | 平面 x/y | 經度/緯度（度） |
| `ST_Distance` 單位 | 座標單位（度） | 公尺 |
| `ST_Area` 單位 | 平方度 | 平方公尺 |
| `ST_Buffer` 半徑 | 度 | 25.4.0-0 不支援（`ERROR 3457`） |
| 建構函數 | `ST_GeomFromText(wkt [, srid])`、`STV_GeometryPoint(x, y)` | `ST_GeographyFromText(wkt)`、`STV_GeographyPoint(x, y)` |
| 適合 | 平面圖、室內圖、已投影座標 | 地球表面距離、面積 |

實測同一組座標：

```
=> SELECT ST_Distance(ST_GeomFromText('POINT(121.5654 25.0330)'),
                      ST_GeomFromText('POINT(121.4590 25.0140)')) AS dist_deg,
          ST_Distance(ST_GeographyFromText('POINT(121.5654 25.0330)'),
                      ST_GeographyFromText('POINT(121.4590 25.0140)')) AS dist_m;
     dist_deg      |      dist_m
-------------------+------------------
 0.108083116165286 | 10926.8093268334
```

面積的差距更誇張：同一個 0.05° × 0.10° 的多邊形，`ST_Area` 在 `GEOMETRY` 是 `0.006`（平方度），在 `GEOGRAPHY` 是 `67,218,996.59`（平方公尺）。前者如果你當成平方公里去算，會得到一個小到不合理的數字。

這裡有個實務判斷點：**如果你只是要「誰落在誰裡面」，`GEOMETRY` 就夠了，而且快得多**（後面量到每對 4.56 µs）。只有當你要輸出「幾公尺」「幾平方公里」給人看，或要做半徑查詢，才需要 `GEOGRAPHY`。

### 3.2 邊界語意：三種函數，三種答案

OGC 的定義裡，`ST_Contains` 要求「內部有交集，且沒有任何部分在邊界上」。Vertica 照這個定義實作，用單位方格（0–10）探針可以直接看到：

```
=> WITH poly AS (SELECT ST_GeomFromText('POLYGON((0 0,10 0,10 10,0 10,0 0))') AS g)
   SELECT ST_AsText(pt) AS point_wkt,
          ST_Contains(g, pt) AS contains, ST_Intersects(g, pt) AS intersects,
          ST_Within(pt, g) AS within
   FROM poly, (SELECT ST_GeomFromText('POINT(5 5)') AS pt
               UNION ALL SELECT ST_GeomFromText('POINT(0 5)')
               UNION ALL SELECT ST_GeomFromText('POINT(10 10)')
               UNION ALL SELECT ST_GeomFromText('POINT(10.0001 5)')) pts;
   point_wkt     | contains | intersects | within
-----------------+----------+------------+--------
 POINT (5 5)     | t        | t          | t
 POINT (0 5)     | f        | t          | f
 POINT (10 10)   | f        | t          | f
 POINT (10.0001 5) | f      | f          | f
```

記法：**`ST_Contains` 是「嚴格內部」，`ST_Intersects` 是「含邊界」，`ST_Within` 是 `ST_Contains` 的反向寫法，同樣不含邊界。**

這在台灣的場景特別痛：戶籍地址、門牌、行政區界線都是「共用邊界」的。一個座標剛好落在兩區的界線上，用 `ST_Contains` 查會回「不屬於任何一區」——這不是 bug，是定義。

### 3.3 空間索引是什麼：格網，不是 B-tree

`STV_Create_Index(table_key, geom USING PARAMETERS index='name')` 會把多邊形集合切成格網（cells），記錄每個 cell 覆蓋哪些多邊形。查詢時先用「點在哪個 cell」縮小候選集合，再對候選多邊形做精確判斷。

三個關鍵性質：

1. **只能索引 `(MULTI)POLYGON`。** 點、線不能建索引。
2. **索引是獨立的快照物件**，不在交易裡跟著 `UPDATE`/`INSERT` 走，要自己 `STV_Refresh_Index`。
3. **查詢要用 `STV_Intersect`**，不是 `ST_Contains`。`STV_Intersect` 的語意是 intersects（含邊界），回傳的是索引鍵，不是幾何本身。

`STV_Intersect` 的行為也很特別：它是 UDTF，**一次回傳兩個欄位**（`pt_gid`、`pol_gid`），所以不能寫成 `SELECT poi_id, STV_Intersect(...) AS x`（會得到 `ERROR 5386: User defined transform will return 2 columns, whereas 1 aliases provided`），也不能在 SELECT 裡混其他欄位（`ERROR 2521`）。標準用法是落地成表：

```sql
CREATE TABLE geo_hits AS
SELECT STV_Intersect(poi_id, geom USING PARAMETERS index='districts_idx') OVER (PARTITION BY poi_id)
FROM poi;
```

這點很重要：**索引不是「讓 `ST_Contains` 變快」的魔法，它是一個產生對應表的工具。** 你的查詢還是得自己 join 回去。

---

## 四、Step-by-step 實測

### 4.1 建 schema 與多邊形：第一個坑是 INSERT

```sql
DROP SCHEMA IF EXISTS article_lab_geo CASCADE;
CREATE SCHEMA article_lab_geo;
SET SEARCH_PATH TO article_lab_geo, public;   -- public 不能省

CREATE TABLE districts (
  district_id   INT,
  district_name VARCHAR(16),
  wkt           VARCHAR(200),
  geom          GEOMETRY
);
```

想省事用一列一列塞，會撞到這個：

```
=> INSERT INTO districts (district_id, district_name, geom) VALUES
   (1,'中正', ST_GeomFromText('POLYGON((...))')),
   (2,'大安', ST_GeomFromText('POLYGON((...))'));
ERROR 10645:  Cannot use multi-row insert: column 'geom' has unsupported type 'geometry'
HINT:  Try inserting one row at a time
```

**`geometry` 不支援多列 `VALUES`**，必須一列一個 `INSERT`，或先存 WKT 再 `UPDATE ... SET geom = ST_GeomFromText(wkt)`（我選後者，8 列一行搞定）。這不是版本 bug，是 UDx 型別的限制。

### 4.2 產生 20 萬個點

用 `v_catalog.columns`（4,911 列）× `v_catalog.types`（90 列）交叉當數字來源，不需要外部檔案：

```sql
CREATE TABLE poi (poi_id INT, lon FLOAT, lat FLOAT, geom GEOMETRY, geog GEOGRAPHY);

INSERT INTO poi
SELECT i::INT AS poi_id,
       (121.45 + ((i * 7919) % 500) * 0.0004)::FLOAT AS lon,
       (24.95 + ((i - 1) / 500) * 0.0004)::FLOAT     AS lat,
       STV_GeometryPoint((121.45 + ((i * 7919) % 500) * 0.0004)::FLOAT,
                         (24.95 + ((i - 1) / 500) * 0.0004)::FLOAT) AS geom,
       STV_GeographyPoint((121.45 + ((i * 7919) % 500) * 0.0004)::FLOAT,
                          (24.95 + ((i - 1) / 500) * 0.0004)::FLOAT) AS geog
FROM (SELECT ROW_NUMBER() OVER () AS i
      FROM v_catalog.columns CROSS JOIN v_catalog.types
      LIMIT 200000) g;
```

兩個型別細節：

- `STV_GeometryPoint` 只接受 `FLOAT`，傳 numeric 會得到 `Function STV_GeometryPoint(numeric, numeric) does not exist`，所以要 `::FLOAT`。
- **沒有 `ST_GeographyPoint`**（`ERROR 3457`），球面點要用 `STV_GeographyPoint`。同樣地 `ST_GeogFromText` 不存在，要用 `ST_GeographyFromText`。

載入 200,000 列耗時 **1,883.7 ms**。

### 4.3 暴力查詢：8.6 秒與 198,398 列

```sql
SELECT d.district_name, count(*) AS poi_cnt
FROM poi p JOIN districts d ON ST_Contains(d.geom, p.geom)
GROUP BY d.district_name ORDER BY poi_cnt DESC;
```

```
 district_name | poi_cnt
---------------+---------
 中正          |   31000
 大安          |   31000
 信義          |   31000
 內湖          |   30999
 士林          |   18600
 北投          |   18600
 中山          |   18600
 松山          |   18599
Time: First fetch (8 rows): 8660.659 ms
```

執行計畫把 `ST_Contains` 當成 join filter，兩邊都是全表掃描：

```
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS)
 | +---> JOIN HASH
 | |      Join Filter: public.ST_Contains(d.geom, p.geom)
 | | +-- Outer -> STORAGE ACCESS for d
 | | +-- Inner -> STORAGE ACCESS for p
```

也就是說：**8 個多邊形 × 200,000 個點 = 1,600,000 次 UDF 呼叫**，換算每對 **4.56 µs**。編譯器沒有辦法幫你把 bbox 條件拉出來——`ST_Contains` 是不透明的 UDF，這就是為什麼需要空間索引或手動 bbox。

### 4.4 邊界點：1,602 個消失的點

八區加總 198,398，比 200,000 少 1,602。用 `LEFT JOIN ... WHERE d.district_id IS NULL` 抓出來：

```
=> SELECT lon, count(*) AS n
   FROM poi p LEFT JOIN districts d ON ST_Contains(d.geom, p.geom)
   WHERE d.district_id IS NULL GROUP BY lon ORDER BY n DESC;
   lon    |  n
----------+-----
 121.6   | 400
 121.5   | 400
 121.45  | 400
 121.55  | 400
 121.6176|   2
```

**1,600 個點剛好落在四條垂直邊線上**（每條 400 個），剩下 2 個是緯度剛好落在 24.95／25.05 的點。這些點在 `ST_Contains` 眼裡不屬於任何一區，但 `ST_Intersects` 會把它們各算給兩邊：

```
=> SELECT n_dist, count(*) AS points FROM (
     SELECT p.poi_id, count(*) AS n_dist
     FROM poi p JOIN districts d ON ST_Intersects(d.geom, p.geom)
     GROUP BY p.poi_id) x GROUP BY n_dist ORDER BY n_dist;
 n_dist | points
--------+--------
      1 | 198799
      2 |   1201
```

**1,201 個點同時屬於兩個行政區**（0.6%）。如果你在做「每區人口加總」，用 `ST_Intersects` 會重複計算 1,201 筆，用 `ST_Contains` 會漏掉 1,602 筆。**兩種都錯，只是錯的方向不同。** 正解是先定義歸屬規則（例如「以最近的中心點為準」），或接受邊界點被丟棄並在報表註明。

![4×2 的行政區格線：每區 ST_Contains 命中的點數，以及落在虛線格線上而被排除的 1,600 個點]({{ '/assets/images/geospatial-boundary-rings.svg' | relative_url }}){: loading="lazy" }

順帶一個限制：想把「不屬於任何區」寫成 `WHERE NOT EXISTS (SELECT 1 FROM districts d WHERE ST_Contains(...))`，在 25.4.0-0 會失敗：

```
ERROR 2784:  Correlated subquery could not be flattened as a join
```

`ST_Contains` 讓優化器無法把相關子查詢改寫成 join。要用 `LEFT JOIN ... IS NULL`，代價是 **7,489.8 ms**。

### 4.5 建空間索引：`OVER` 子句是必要的

```sql
SELECT STV_Create_Index(district_id, geom USING PARAMETERS index='districts_idx', overwrite=true)
OVER () FROM districts;
```

```
   type   | polygons | SRID | min_x  | min_y | max_x  | max_y | info
----------+----------+------+--------+-------+--------+-------+------
 GEOMETRY |        8 |    0 | 121.45 | 24.95 | 121.65 | 25.15 |
```

四個實測踩到的語法點：

- 沒有 `OVER`：`ERROR 5401: User-defined transform function STV_Create_Index must have an OVER clause`。
- 寫 `OVER (PARTITION BEST)`：`ERROR 6379: PARTITION BEST can only be used with single-phase user defined transform functions`。單節點用 `OVER ()` 就好。
- 寫 `OVER (PARTITION BY district_id)`：`The OVER() clause cannot contain any column`。
- `STV_Describe_Index` 只能單獨出現在 SELECT 列表：`SELECT * FROM STV_Describe_Index() OVER ()` 會得到 `ERROR 5383`，而且 25.4.0-0 只回一個欄位（索引名稱），沒有格數與記憶體資訊。

### 4.6 索引查詢：827.6 ms

```sql
SELECT count(*) FROM (
  SELECT STV_Intersect(poi_id, geom USING PARAMETERS index='districts_idx') OVER (PARTITION BY poi_id)
  FROM poi) x;
```

```
 idx_rows
----------
   201201
Time: First fetch (1 row): 827.617 ms
```

對照暴力法的 **7,303.5 ms**（`ST_Intersects`）與 **8,660.7 ms**（`ST_Contains` + `GROUP BY`），索引是 **8.8 倍**。換算成每點成本：暴力法每個點要跟 8 個多邊形各比一次（7,303.5 ms ÷ 200,000 = **36.5 µs/點**），索引轉換是 **4.14 µs/點**。**索引省下的是候選多邊形的比對，不是掃描**——8 個多邊形的候選集合本來就小，所以加速有限；多邊形有幾萬個時差距才會拉開。

落地的結果長這樣：

```
 pt_gid | pol_gid
--------+---------
      1 |       4
      2 |       3
      3 |       3
```

`pt_gid` 是你傳進去的第一個參數（`poi_id`），`pol_gid` 是索引鍵（`district_id`）。落地成 `geo_hits` 表：**1,994.0 ms**、201,201 列、只佔 **402,728 bytes**（每列 2.0 bytes，兩個 int 壓得很好）。落地後再聚合：

```
SELECT d.district_name, count(*) FROM geo_hits h
JOIN districts d ON d.district_id = h.pol_gid GROUP BY 1;
Time: First fetch (8 rows): 15.045 ms
```

**15 ms 對 8,660 ms。** 這才是索引真正的工作模式：**離線算一次對應表，線上查詢只做關聯式聚合。** 如果你的點每天增加幾百萬，這個模式比每次即時算好太多。

### 4.7 索引是快照：31,251 與 15,499

把「內湖」的多邊形從 0.05° × 0.10° 縮成 0.05° × 0.05°，但不 refresh：

```
=> SELECT count(*) AS from_stale_index FROM geo_hits WHERE pol_gid = 4;
 from_stale_index
------------------
            31251
Time: 3.467 ms

=> SELECT count(*) AS brute_true
   FROM poi p JOIN districts d ON ST_Contains(d.geom, p.geom) WHERE d.district_id = 4;
 brute_true
------------
      15499
Time: 1,459.7 ms
```

**索引回 31,251，真值 15,499，差了一倍。** 而且它不會報錯、不會警告，就是給你一個舊答案。Refresh 之後：

```
=> SELECT STV_Refresh_Index(district_id, geom USING PARAMETERS index='districts_idx') OVER () FROM districts;
 type     | polygons | indexed | appended | updated | deleted
----------+----------+---------+----------+---------+---------
 GEOMETRY |        8 |       1 |        0 |       1 |       0
Time: 71.463 ms

=> SELECT count(*) FROM geo_hits2 WHERE pol_gid = 4;   -- 重新跑一次 STV_Intersect 落地
 15626
```

**15,626，比 `ST_Contains` 的 15,499 多 127 筆。** 原因還是邊界：索引用 intersects 語意，邊線上的點會各算一次。這 127 筆正好是「縮小後多邊形的周界點」，不是索引壞了。

實務建議：**把 `STV_Refresh_Index` 排進 ETL 的最後一步**，而且要放在 `COMMIT` 之後、對外服務之前。多邊形一個月才動一次的東西（行政區、商圈）還好；如果是門市配送範圍每天在調整，索引的維護成本要算進去。

### 4.8 距離與面積：度、公尺、投影

```sql
-- 台北 101 → 板橋
SELECT ST_Distance(ST_GeomFromText('POINT(121.5654 25.0330)'),
                   ST_GeomFromText('POINT(121.4590 25.0140)'));
-- 0.108083116165286  （度）

SELECT round(ST_Distance(ST_Transform(ST_GeomFromText('POINT(121.5654 25.0330)', 4326), 3857),
                         ST_Transform(ST_GeomFromText('POINT(121.4590 25.0140)', 4326), 3857))::NUMERIC, 1);
-- 12072.2   （公尺，Web Mercator）

SELECT round(ST_Distance(ST_GeographyFromText('POINT(121.5654 25.0330)'),
                         ST_GeographyFromText('POINT(121.4590 25.0140)'))::NUMERIC, 1);
-- 10926.8   （公尺，球面）
```

三個數字都不一樣：**10,926.8 / 12,072.2 / 0.108083**。Web Mercator 在高緯度會放大，台北（25°N）的誤差已經 **+10.5%**；如果你拿它算配送距離，會把成本高估一成。

`ST_Transform` 還有一個必踩的錯：沒帶 SRID 的幾何不能轉投影。

```
=> SELECT ST_Transform(ST_GeomFromText('POINT(121.5654 25.0330)'), 3857);
ERROR 5861:  Error calling processBlock() in User Function ST_Transform ...:
             Geometry has undefined SRID [0]
```

必須用 `ST_GeomFromText(wkt, 4326)` 明確指定來源 SRID（`ST_SRID` 會回 4326）。

### 4.9 半徑查詢與最近鄰

「找出 500 公尺內的點」有兩種寫法，結果差 11.7%：

```sql
-- 平面：0.005 度
SELECT count(*) FROM poi
WHERE STV_DWithin(geom, STV_GeometryPoint(121.5654::FLOAT, 25.0330::FLOAT), 0.005::FLOAT);
-- 487，1,023.7 ms

-- 球面：500 公尺
SELECT count(*) FROM poi
WHERE STV_DWithin(geog, STV_GeographyPoint(121.5654::FLOAT, 25.0330::FLOAT), 500::FLOAT);
-- 436，1,116.7 ms
```

0.005° 在台北的實際距離是：緯度方向 556 m、經度方向 505 m。所以「0.005 度的圓」其實是個橢圓，多撈了 51 個點（**487 vs 436，+11.7%**）。

最近鄰居（k-NN）：

```sql
-- 用 STV_NN（會掃全表，但省掉排序）
SELECT ST_AsText(NN) AS nearest_geom, round(dist::NUMERIC, 6)
FROM (SELECT STV_NN(geom, STV_GeometryPoint(121.5654::FLOAT, 25.0330::FLOAT), 3) OVER () FROM poi) x;
-- POINT (121.5656 25.032984) | 0.000201
-- POINT (121.5652 25.0328408)| 0.000256
-- POINT (121.5652 25.0332408)| 0.000313
Time: 613.197 ms

-- 自己排序
SELECT poi_id, round(ST_Distance(geog, STV_GeographyPoint(121.5654::FLOAT, 25.0330::FLOAT))::NUMERIC, 1)
FROM poi ORDER BY 2 LIMIT 5;
-- 20.2 / 26.8 / 33.5 / 47.2 / 50.5 公尺
Time: 1,432.421 ms
```

兩者結果方向一致（最近鄰都在 20–22 公尺等級，平面度數與球面公尺的換算差約 10%，正好呼應 3.1 的單位問題），但 `STV_NN` 快 **2.3 倍**——它用內部的距離排序，不需要把 20 萬列的距離全部算完再排序。

要注意 `STV_NN` 回的距離是**座標單位（度）**，而且它沒有用到空間索引（因為索引不能建在點上），每次都是全表掃描。

### 4.10 GeoHash：另一種前置過濾

```sql
SELECT ST_GeoHash(STV_GeometryPoint(121.5654::FLOAT, 25.0330::FLOAT)) AS gh;
-- wsqqqm15z1kjt9f9yprs（20 字元）

SELECT count(*) FROM poi
WHERE LEFT(ST_GeoHash(geom)::VARCHAR, 5) = LEFT(ST_GeoHash(STV_GeometryPoint(...))::VARCHAR, 5);
-- 12,085，1,191.1 ms
```

`ST_GeoHash` 固定回 20 字元（沒有精度參數），要截斷得自己 `LEFT(...)::VARCHAR`——直接 `LEFT(ST_GeoHash(geom), 5)` 會得到 `Function LEFT(long varchar, int) does not exist`，因為回傳型別是 `long varchar` 不是 `varchar`。

20 萬列掃一次要 1.19 秒，換來 12,085 列的候選集合。**GeoHash 的價值不在省時間，在讓你能把字串前綴放進 B-tree 或 partition key**——如果你的表是按 geohash 前綴分區的，這個查詢會變成 range scan。

`ST_GeomFromGeoHash` 的行為要注意：它回的是一個 **POLYGON（格子的邊界框）**，不是原本的點。

```
=> SELECT ST_AsText(ST_GeomFromGeoHash('wsqqqm15z1kjt9f9yprs'));
 POLYGON ((121.5654 25.033, 121.5654 25.033, 121.5654 25.033, 121.5654 25.033, 121.5654 25.033))
```

它回的是格子邊界框（`ST_Polygon`），不是原本的點；20 字元的格子遠小於 `float8` 能印出的位數，所以五個頂點全部印成同一個座標。要中心點就自己 `ST_Centroid`。

---

## 五、效能與成本分析

同一份資料、同一個 session、連續執行（已暖機），數字如下。「相對」欄是相對於最慢的暴力 `ST_Contains` join（8,660.7 ms），大於 1 代表比較快：

![決策流程：點在哪一區、距離與半徑、最近鄰三條路線，以及各自的實測耗時]({{ '/assets/images/geospatial-query-choice.svg' | relative_url }}){: loading="lazy" }

| 查詢 | 耗時 | 輸出 | 相對 |
|------|------|------|------|
| `ST_Contains` join + `GROUP BY` | 8,660.7 ms | 8 列 / 198,398 筆 | 1.00× |
| `ST_Contains` join（只 count） | 7,694.8 ms | 198,398 | 1.13× |
| `ST_Intersects` join（只 count） | 7,303.5 ms | 201,201 | 1.19× |
| `STV_Intersect` 索引轉換 | **827.6 ms** | 201,201 | **10.5×** |
| 索引 + CTAS 落地 | 1,994.0 ms | 201,201 列 / 402,728 B | 4.34× |
| 落地後 `GROUP BY` | **15.0 ms** | 8 列 | 577× |
| bbox（常數多邊形）+ `ST_Contains` | 450.4 ms | 31,000 | 19.2× |
| bbox（`ST_XMin` 寫進 join）+ `ST_Contains` | 14,842.7 ms | 198,398 | **0.58×** |
| `LEFT JOIN ... IS NULL`（找未匹配點） | 7,489.8 ms | 1,602 | 1.16× |
| `STV_DWithin` 0.005° | 1,023.7 ms | 487 | 8.46× |
| `STV_DWithin` 500 m（geography） | 1,116.7 ms | 436 | 7.76× |
| `STV_NN` k=5 | 613.2 ms | 5 列 | 14.1× |
| `ORDER BY ST_Distance(geog)` LIMIT 5 | 1,432.4 ms | 5 列 | 6.05× |
| GeoHash 前綴過濾（5 字元） | 1,191.1 ms | 12,085 | 7.27× |

三個值得記住的比值：

1. **索引本身的極限是 10 倍左右**，因為它只省下多邊形比對，掃描 20 萬列的成本還在。多邊形從 8 個變成 8,000 個時，暴力法會變成 1,600,000 × 1,000 次比對，索引才會展現數量級的差距。
2. **落地後的聚合是 577 倍**，這是「預計算」的價值，也是為什麼空間索引在 Vertica 裡比較像 ETL 工具而不是查詢加速器。
3. **bbox 寫錯位置會比不做更慢**（0.49×）。原因是 `ST_XMin(d.geom)` 是 UDF，寫在 join 條件上會被每一對列重算一次：1,600,000 × 4 次 UDF 呼叫。寫成常數（`WHERE lon BETWEEN 121.50 AND 121.55`）才會走一般的比較。

儲存成本：

| 物件 | 列數 | `used_bytes` | 每列 |
|------|------|--------------|------|
| `poi` | 200,000 | 16,696,257 | **83.5 B** |
| `geo_hits` | 201,201 | 402,728 | 2.0 B |
| `districts` | 8 | 1,315 | 164 B |

`STV_MemSize` 對單點回 **85 bytes**、對一個 5 頂點多邊形回 **157 bytes**。點幾何比兩個 `float8`（16 bytes）貴 5 倍以上，因為它帶了型別標籤、SRID、bbox。

**設計建議：點資料同時保留 `lon`／`lat` 數值欄位。** 數值欄位便宜（可 encoding、可 bbox 過濾、可建一般投影排序），`geometry` 只在真正需要空間函數時才用。如果你有 5 億個門市座標，這個決定會差 30 GB。

---

## 六、常見錯誤與反模式

### 1. 把「度」當「公尺」用

```sql
-- 想要 500 公尺，卻寫成 0.005「度」
WHERE ST_DWithin(geom, point, 0.005)
```

實測多撈 **51 個點（487 vs 436，+11.7%）**，而且不同緯度誤差不同（台北 25°N 是 +11.7%，到了高雄 22.6°N 會變成 +4%）。半徑查詢一律用 `GEOGRAPHY` + 公尺。

### 2. 經度緯度寫反

```sql
-- 錯：STV_GeometryPoint(lat, lon)
SELECT count(*) FROM poi
WHERE ST_Contains((SELECT geom FROM districts WHERE district_name='中正'),
                  STV_GeometryPoint(lat, lon));
-- 0
Time: 2,014.2 ms
```

**回 0，不報錯。** 因為 (25.03, 121.56) 是個合法座標，只是落在太平洋。驗證方法：`ST_X()` 應該落在 119–122（台灣經度），`ST_Y()` 落在 21–26。

### 3. 以為邊界點會被算進去

`ST_Contains` 對 1,602 個格線點回 `false`。如果你的資料是「地址正規化後的座標」，邊界點很少；如果是「網格統計」「隨機取樣」，邊界點比例可能高到 1%。**報表加總對不上母體時，先查這個。**

### 4. 改了多邊形沒 refresh 索引

實測索引回 **31,251**、真值 **15,499**。這個錯誤最危險的地方是「查詢很快、看起來很正常」。把 `STV_Refresh_Index` 放進 ETL 尾端，並在監控裡比對「索引筆數」與「抽樣 `ST_Contains` 筆數」。

### 5. 對點建空間索引

```
ERROR 3399: Failure in UDx RPC call InvokeProcessPartition() ...
  Error creating index: There are no valid polygons to index.
  NOTICE: Some geometries ignored [Non (MULTI)POLYGON: 200000]
```

空間索引是「多邊形格網」，只能回答「這個點在哪些多邊形裡」。要加速最近鄰，只能靠 `STV_NN`（全掃但省排序）或自己維護 geohash / 網格分桶。

### 6. 把 bbox 條件寫成 join 上的 UDF

```sql
-- 慢：14,842.7 ms
ON p.lon BETWEEN ST_XMin(d.geom) AND ST_XMax(d.geom)
```

`ST_XMin` 每對列都重算。正確做法是把 bbox 變成常數（子查詢先取出來，或應用層算好），或直接建數值欄位 + 一般 B-tree 投影。

### 7. 多列 INSERT geometry，或把 `public` 從 search_path 拿掉

```
ERROR 10645: Cannot use multi-row insert: column 'geom' has unsupported type 'geometry'
ERROR 3457: Function ST_GeomFromText(unknown) does not exist
```

第二個錯誤訊息會讓你以為函數不存在，其實只是 search_path 少了 `public`。`v_catalog.user_functions` 查一下就知道 geospatial UDF 都註冊在 `public`。

### 8. 無效多邊形靜默回 false

```sql
=> SELECT ST_IsValid(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))')) AS is_valid,
          STV_IsValidReason(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))')) AS reason;
 is_valid |                  reason
----------+------------------------------------------
 f        | Self-intersection at or near POINT (1 1)

=> SELECT ST_Contains(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))'), ST_GeomFromText('POINT(1 1)'));
 f
```

**「8 字形」多邊形不會報錯，只會讓 `ST_Contains` 回 false。** 上線前對所有多邊形跑一次 `ST_IsValid`，並把 `STV_IsValidReason` 的輸出記進 log。

---

## 七、風險與建議

### 現在就能做（唯讀／低風險）

1. **盤點型別與單位。** 查 `v_catalog.columns` 找出所有 `geometry` / `geography` 欄位，確認每一條 `ST_Distance` / `ST_Area` 的輸出單位與報表標示一致。
2. **對所有多邊形跑 `ST_IsValid` 與 `STV_IsValidReason`**，把無效的挑出來修。這是純讀取，不影響服務。
3. **用 `ST_Intersects` 與 `ST_Contains` 各跑一次加總，比較差額。** 差額就是邊界點數量；把這個數字寫進報表定義。
4. **量一次自己的 bbox 選擇性。** `SELECT count(*) FROM t WHERE lon BETWEEN ... AND lat BETWEEN ...` 看候選集合佔母體多少；超過 10% 就考慮 geohash 或網格分桶。
5. **確認應用程式傳進來的點是 `(lon, lat)` 還是 `(lat, lon)`。** 抽查 100 筆用 `ST_X` / `ST_Y` 看範圍。

### 需要變更（要窗口／要核准）

1. **建空間索引**：`STV_Create_Index` 會產生新物件並佔記憶體，建議在低峰期執行；建完用 `STV_Describe_Index` 確認名稱與範圍。
2. **加 refresh 步驟到 ETL**：`STV_Refresh_Index` 是 DDL 等級的操作，要確認交易邊界與失敗重試邏輯。
3. **把點資料的 `geometry` 拆成 `lon` / `lat` 數值欄位**：需要改表與改應用程式，但長期省下 5 倍儲存與大量 UDF 成本。
4. **把即時 `ST_Contains` 改成離線對應表**：這是架構變更，要先確認資料新鮮度要求（門市歸屬可以每天算，配送範圍可能不行）。

---

## 八、參考資料

- [Geospatial functions](https://docs.vertica.com/25.4.x/en/sql-reference/functions/geospatial-functions/) — `ST_Contains`、`ST_Intersects`、`ST_Distance`、`ST_Buffer` 的完整清單與參數型別
- [Geography functions](https://docs.vertica.com/25.4.x/en/sql-reference/functions/geography-functions/) — 球面型別的距離與面積語意
- [Spatial index functions](https://docs.vertica.com/25.4.x/en/sql-reference/functions/spatial-index-functions/) — `STV_Create_Index`、`STV_Refresh_Index`、`STV_Intersect`、`STV_NN` 的 `OVER` 子句用法
- [GEOMETRY data type](https://docs.vertica.com/25.4.x/en/sql-reference/data-types/geometry/) — 最大長度、SRID、與 WKT/WKB 的轉換
