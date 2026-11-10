---
layout: post
title: "Vertica 複雜型別實戰：ARRAY、SET、ROW 與 JSON 半結構化資料"
date: 2026-11-11 05:00:00 +0800
categories: vertica data-types sql
tags: [vertica, array, set, row, json, semi-structured, sql]
description: "Vertica 25.4 的複雜型別跟 PostgreSQL 不是同一套：ARRAY 是 0 基底索引、SET 根本不能當欄位型別（ERROR 5108）、MAP 只給 Parquet/ORC 外部表（ERROR 10329）、JSON 沒有原生型別。實測 20 萬事件、60 萬個 tag 配對，低基數陣列比正規化表省 59% 空間，但 EXPLODE 讓聚合慢 7.0 倍（261.5 ms vs 37.2 ms）。"
image: /assets/images/og/vertica-complex-data-types-zh.png
faq:
  - q: "Vertica 的 ARRAY 是第一基還是第零基索引？"
    a: "0 基底。`(ARRAY['a','b','c','d'])[0]` 回 `a`、`[1]` 回 `b`；切片是右開區間，`[2:3]` 只回一個元素 `[\"c\"]`、`[2:4]` 回 `[\"c\",\"d\"]`。索引超出長度回 NULL 不報錯（`[4]` 回空值），但負索引直接 `ERROR 9225: Negative array index`。"
  - q: "Vertica 可以用 SET 當欄位型別嗎？"
    a: "不行。25.4.0-0 實測 `CREATE TABLE t (s SET)` 直接 `ERROR 5108: Type \"SET\" does not exist`。SET 只是運算式建構子，會自動排序去重（`SET['b','a','b','c']` 回 `[\"a\",\"b\",\"c\"]`），排序是位元組序（`SET['1','10','2','A','B','a','b']` 回 `[\"1\",\"10\",\"2\",\"A\",\"B\",\"a\",\"b\"]`）；而且 `'t02' IN (SET['t01','t02'])` 回 false，不要拿它取代 IN 清單。"
  - q: "Vertica 有 JSON 或 MAP 欄位型別嗎？"
    a: "25.4.0-0 沒有原生 JSON 型別（`ERROR 5108: Type \"JSON\" does not exist`），MAP 也只限 Parquet/Orc 外部表（`ERROR 10329: Only Parquet or Orc External Tables can use map type`）。實務上 JSON 存成 LONG VARCHAR，用 `MAPLOOKUP(MAPJSONEXTRACTOR(doc),'key')` 取頂層鍵；實測巢狀物件與其子鍵都回 NULL，巢狀解析要自己拆字串或落地成正規化欄位。"
  - q: "ARRAY 存標籤比正規化子表省空間嗎？"
    a: "看元素重複度。60 萬個配對、只有 20 個相異標籤時，ARRAY 版總共 1,106,697 bytes、正規化版 2,703,752 bytes，陣列少 59%（2.44 倍）。換成 60 萬個唯一標籤，陣列 2,635,830 bytes、正規化 2,743,239 bytes，只差 3.9%，而且陣列每個值反而貴 13.7%（4.06 vs 3.57 bytes）。"
  - q: "為什麼 ARRAY 的聚合查詢比較慢？"
    a: "因為 EXPLODE 是 transform function，計畫裡會多一個 UserDefined 節點，成本從 764 跳到 65K，無法下推到儲存層。同樣 60 萬個配對做標籤直方圖：EXPLODE 261.5 ms、正規化表 37.2 ms（慢 7.0 倍）。改寫成「元素存取 + 數字表 join」可以降到 165.6 ms，仍比正規化慢 4.5 倍。"
---
<!--
AI Summary: Vertica 25.4.0-0 單節點實測複雜型別：ARRAY[VARCHAR] 為 0 基底索引、切片右開（[2:3] 回 1 個元素）、越界回 NULL 但負索引報 ERROR 9225；SET 不能當欄位型別（ERROR 5108），僅為會排序去重的運算式；ROW 幾乎零成本（3.07 vs 3.06 bytes/row、EXPLAIN cost 665 相同）；JSON 無原生型別、MAP 僅限 Parquet/Orc 外部表，需用 LONG VARCHAR + MAPJSONEXTRACTOR + MAPLOOKUP，且巢狀鍵一律回 NULL。200,000 事件 / 600,000 個 tag 配對：低基數陣列省 59% 空間（1.11 MB vs 2.70 MB），高基數僅差 3.9% 且每值貴 13.7%；EXPLODE 聚合 261.5 ms 對正規化 37.2 ms（7.0 倍），元素存取改寫後 165.6 ms；寫入 79.9 ms 對 1,103.7 ms（13.8 倍）；JSON 過濾 740 ms 對 5.4 ms（138 倍），儲存 21.23 對 2.10 bytes/row（10.1 倍）。
AI Keywords: Vertica, ARRAY, SET, ROW, JSON, MAPJSONEXTRACTOR, MAPLOOKUP, EXPLODE, complex types, semi-structured, 25.4
-->
<!--more-->

---

## 一、先講結論

Vertica 的「複雜型別」很容易讓人用 PostgreSQL 的直覺去寫，然後在三個地方同時撞牆：**索引基準**、**型別存在性**、**巢狀解析**。這三個坑都不會給你編譯錯誤，只會給你一個看起來很合理的錯答案。

我在 Vertica 25.4.0-0 單節點上，用 20 萬個事件、60 萬個 (event, tag) 配對，把 ARRAY、SET、ROW 與 JSON 全部拆開量過，得到八個結論：

1. **ARRAY 是 0 基底索引，切片右開。** `[0]` 是第一個元素，`[2:3]` 只回一個元素，`[4]` 越界回 NULL，`[-1]` 直接 `ERROR 9225`。這四個行為沒有一個會出現在語法錯誤裡。
2. **SET 不是欄位型別。** `CREATE TABLE ... (s SET)` 回 `ERROR 5108: Type "SET" does not exist`。它只是運算式建構子，會自動排序去重，而且 `'t02' IN (SET['t01','t02'])` 回 **false**。
3. **ROW 幾乎免費。** 同資料量下結構欄位 3.07 bytes/row、扁平欄位 3.06 bytes/row（差 0.34%），`EXPLAIN` 成本 665 對 665，過濾查詢 7.8 ms 對 7.9 ms。代價在治理，不在效能。
4. **JSON 沒有原生型別，MAP 只給外部表。** `Type "JSON" does not exist`（5108）、`Only Parquet or Orc External Tables can use map type`（10329）。JSON 只能存 `LONG VARCHAR`，用 `MAPLOOKUP(MAPJSONEXTRACTOR(doc), 'key')` 取鍵，而且**只看得到頂層鍵**：巢狀物件與其子鍵都回 NULL。
5. **陣列省不省空間，取決於元素重複度。** 20 個相異標籤：陣列 1,106,697 bytes 對正規化 2,703,752 bytes，**少 59%（2.44 倍）**。60 萬個唯一標籤：2,635,830 對 2,743,239，**只差 3.9%**，且陣列每個值貴 13.7%。
6. **`contains()` 能下推，`EXPLODE` 不能。** 成員查詢 20.9 ms 對 20.4 ms，幾乎打平；但標籤直方圖 EXPLODE 要 **261.5 ms**，正規化表只要 **37.2 ms**，差 7.0 倍。計畫裡多一個 `UserDefined: Explode` 節點，成本 65K 對 764。
7. **寫入是陣列的絕對優勢。** 200,000 列陣列 79.9 ms；同樣資料拆成 600,000 列正規化表要 1,103.7 ms，**13.8 倍**。
8. **JSON 是最貴的選項。** 100,000 份文件存成 `LONG VARCHAR` 是 21.23 bytes/row，拆成具名欄位只要 2.10 bytes/row（**10.1 倍**）；過濾同一個鍵 740 ms 對 5.4 ms（**138 倍**）。

這篇寫給正在決定「這個欄位要存陣列、拆表、還是塞 JSON」的人，也寫給已經塞了、現在查詢很慢的人。所有數字都來自本機實測，`article_lab_types` schema 在結束時已 `DROP SCHEMA ... CASCADE`。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `Vertica Analytic Database v25.4.0-0` |
| 節點數 | 1（Community Edition） |
| 資料庫 / 使用者 | `testdb` / `dbadmin` |
| 測試 schema | `article_lab_types`（實驗後刪除） |
| 事件表 | `events_arr`：200,000 列，4 欄（`event_id`、`user_id`、`ts`、`tags ARRAY[VARCHAR]`） |
| 正規化表 | `events_norm`：600,000 列，4 欄（`event_id`、`user_id`、`ts`、`tag VARCHAR`） |
| 標籤分佈 | 每事件 1–5 個，平均 3.0，全部取自 20 個相異值 |
| 高基數對照 | `events_arr_hc` / `events_norm_hc`：每事件 3 個唯一標籤，共 60 萬個相異值 |
| ROW 對照 | `t_flat` / `t_struct`：200,000 列，4 個欄位（`uid`、`device`、`region`、`signup`） |
| JSON 對照 | `docs_json` / `docs_flat`：100,000 列，6 個鍵 |
| 量測日期 | 2026-11-11 |

三個會影響重現的細節：

- **資料用 `v_catalog.columns` × `v_catalog.types` 產生**，不需要外部檔案；這台機器的 `v_catalog.columns` 有 5,005 列，交叉後 `LIMIT 200000` 就足夠。
- **這個連線的 `autocommit` 是 `off`。** 所有 `CREATE`／`INSERT` 之後我都顯式 `COMMIT`，量測都在單一 `vsql -f` session 內完成。
- **時間用 vsql 的 `\timing on`。** 我取「All rows formatted」的數字，同一個查詢跑兩次，表裡同時列兩次，避免快取造成的假象。

---

## 三、核心原理

### 3.1 三種型別，三套規則

25.4.0-0 實測的支援狀況，跟文件寫的「complex types」不是等號：

| 型別 | 可以當欄位型別？ | 建構語法 | 索引／欄位 | 實測備註 |
|------|------------------|----------|------------|----------|
| `ARRAY[T]` | 可以，含 `ARRAY[ARRAY[INT]]` | `ARRAY[1,2,3]`、`ARRAY[]::ARRAY[INT]` | 0 基底，切片右開 | 元素型別必須完全相符，不做隱式轉換 |
| `SET` | **不行**（5108） | `SET['a','b']` | `array_length()` 可用 | 自動排序去重，僅供運算式 |
| `ROW(...)` | 可以，可具名、可放 ARRAY | `ROW(1,'x')` | 具名用 `r.a`，匿名用 `f0`、`f1` | 可比較大小、可 `ORDER BY` |
| `MAP` | **不行**（10329，僅 Parquet/Orc 外部表） | 無 `MAP{...}` 語法（4856） | — | 只能由 `MAPJSONEXTRACTOR` 產生 |
| `JSON` | **不行**（5108） | — | — | 存 `LONG VARCHAR` 自理 |

### 3.2 索引語意：0 基底、右開切片、越界與負索引不對稱

這是最容易靜默出錯的一段。實測：

```
=> SELECT (ARRAY['a','b','c','d'])[0] AS i0, (ARRAY['a','b','c','d'])[1] AS i1,
          (ARRAY['a','b','c','d'])[2:3] AS slice_2_3,
          (ARRAY['a','b','c','d'])[4] AS i4_oob;
 i0 | i1 | slice_2_3 | i4_oob
----+----+-----------+--------
 a  | b  | ["c"]     |
```

四個行為一次看完：

- `[0]` 回 `a`，`[1]` 回 `b` → **0 基底**。
- `[2:3]` 回 `["c"]`、`[2:4]` 回 `["c","d"]` → **切片是右開區間**。
- `[4]`（長度 4 的陣列）回空值，**不報錯** → 越界是 NULL，不是例外。
- `[-1]` 回 `ERROR 9225: Negative array index`，`DETAIL: Requested position -1` → 負索引才是硬錯誤。

`array_find` 也是 0 基底：`array_find(ARRAY['a','b','c'],'c')` 回 **2**，找不到回 **-1**。所以「找不到」與「在第一個位置」不會混淆，但「在第 3 個位置」指的是索引 2。

`array_dims` 回的是維度數不是長度：一維回 `1`、`ARRAY[ARRAY[1,2],ARRAY[3,4]]` 回 `2`，要長度請用 `array_length` 或 `array_count`（兩者對一般陣列同值）。

還有一個 PostgreSQL 慣例在這裏不成立的清單。`\df array*` 在 25.4.0-0 只列出這些：`array_cat`、`array_collation`、`array_contains`、`array_count`、`array_dims`、`array_eq`、`array_find`、`array_ge`、`array_gt`、`array_le`、`array_length`、`array_lt`、`array_neq`、`array_nullsafe`。**沒有** `array_append`、`array_prepend`、`array_position`、`array_remove`、`array_replace`、`array_to_string`、`array_fill`、`cardinality`、`unnest`、`array_agg`——每一個都是 `ERROR 3457: Function ... does not exist`。要串接陣列只有 `array_cat`，而它遇到 NULL 會整組回 NULL：

```
=> SELECT array_cat(ARRAY[1,2], NULL) AS cat_null;
 cat_null
----------
```

要小心的是這個 NULL 會一路傳下去，`WHERE contains(array_cat(tags, extra), 'x')` 只要 `extra` 是 NULL 就永遠不成立。

### 3.3 SET 是運算式，不是型別

```
=> CREATE TABLE t_set (id INT, s SET);
ERROR 5108:  Type "SET" does not exist
HINT:  Expected user-defined type
```

它唯一能做的事是「在查詢裡產生一個已排序、已去重的清單」：

```
=> SELECT SET['b','a','b','c'] AS s;
   s
---------
 ["a","b","c"]

=> SELECT SET['1','10','2','A','B','a','b'] AS s;
            s
----------------------------
 ["1","10","2","A","B","a","b"]
```

排序是**位元組序**，不是自然序，也不是大小寫不敏感：`"10"` 排在 `"2"` 前面。數值則正常：`SET[9,3,3,1]` 回 `[1,3,9]`。

還有一個不能踩的雷：SET 不能用在 `IN`。

```
=> SELECT ('t02' IN (SET['t01','t02'])) AS in_set;
 in_set
--------
 f
```

`t02` 明明在裡面卻回 `false`。要嘛老實寫 `IN ('t01','t02')`，要嘛用 `contains(ARRAY['t01','t02'],'t02')`。

### 3.4 ROW：欄位編號從 f0 開始

具名 ROW 用名字取，匿名 ROW 用 `f0`、`f1`……：

```
=> SELECT ROW(10,'x','2026-11-11') AS anon_row,
          (ROW(10,'x','2026-11-11')).f0 AS f0,
          (ROW(10,'x','2026-11-11')).f2 AS f2;
               anon_row               | f0 |     f2
--------------------------------------+----+------------
 {"f0":10,"f1":"x","f2":"2026-11-11"} | 10 | 2026-11-11
```

**`f1` 是第二個欄位，不是第一個。** 從 PostgreSQL 過來的人常在這裡拿到錯的欄位。

ROW 可以直接比較，是逐欄位字典序：`ROW(1,'x') = ROW(1,'x')` 回 `t`、`ROW(1,'x') < ROW(2,'a')` 回 `t`。這讓它可以當排序鍵，也讓 `ROW` 型別的 `ORDER BY` 行為可預測。

### 3.5 JSON 在 25.4 的兩條路

```
=> CREATE TABLE t_json (id INT, j JSON);
ERROR 5108:  Type "JSON" does not exist

=> CREATE TABLE t_map (id INT, m MAP<VARCHAR,INT>);
ERROR 10329:  Only Parquet or Orc External Tables can use map type
```

所以只有兩條路：**外部表**（Parquet/ORC 才有真正的 MAP），或 **`LONG VARCHAR` + `MAPJSONEXTRACTOR`**。後者的用法是兩個函數串起來：

```
=> SELECT MAPLOOKUP(MAPJSONEXTRACTOR('{"a":1,"b":"x","c":true}'), 'a') AS va,
          MAPLOOKUP(MAPJSONEXTRACTOR('{"a":1,"b":"x","c":true}'), 'b') AS vb,
          MAPLOOKUP(MAPJSONEXTRACTOR('{"a":1,"b":"x","c":true}'), 'c') AS vc;
 va | vb | vc
----+----+----
 1  | x  | T
```

值是 `LONG VARCHAR`，型別要自己轉。找不到的鍵回 NULL，巢狀物件也回 NULL：

```
=> SELECT MAPLOOKUP(MAPJSONEXTRACTOR('{"a":{"b":2}}'), 'a') AS nested,
          MAPLOOKUP(MAPJSONEXTRACTOR('{"a":{"b":2}}'), 'b') AS child;
 nested | child
--------+-------
        |
```

**沒有錯誤、沒有警告，就是 NULL。** 這是本篇文章裡最危險的一個行為：你的 ETL 會成功，儀表板會空白，而日誌裡什麼都沒有。

---

## 四、Step-by-step 實測

### 4.1 建資料：20 萬事件、60 萬個配對

```sql
CREATE TABLE events_arr (
  event_id INT, user_id INT, ts TIMESTAMP, tags ARRAY[VARCHAR]
);

INSERT INTO events_arr
SELECT i::INT AS event_id,
       (i % 5000)::INT AS user_id,
       (TIMESTAMP '2026-01-01 00:00:00' + ((i % 86400) * INTERVAL '1 second'))::TIMESTAMP AS ts,
       CASE i % 5
         WHEN 0 THEN ARRAY['t' || lpad(((i*7) % 20)::varchar, 2, '0')]
         WHEN 1 THEN ARRAY['t' || lpad(((i*7) % 20)::varchar, 2, '0'),
                            't' || lpad(((i*13+5) % 20)::varchar, 2, '0')]
         -- …3、4、5 個元素同理
       END AS tags
FROM (SELECT ROW_NUMBER() OVER () AS i
      FROM v_catalog.columns CROSS JOIN v_catalog.types
      LIMIT 200000) g;
COMMIT;
```

結果：`200000` 列、`sum(array_length(tags)) = 600000`、`min 1 / max 5 / avg 3`。

正規化表用元素存取展開（**不要**用 `INSERT ... SELECT ... FROM (SELECT EXPLODE(...))`，會踩到 `ERROR 2521`，見第六節）：

```sql
INSERT INTO events_norm
SELECT e.event_id, e.user_id, e.ts, (e.tags)[k-1]::VARCHAR AS tag
FROM events_arr e
CROSS JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3
            UNION ALL SELECT 4 UNION ALL SELECT 5) n
WHERE k <= array_length(e.tags);
```

`600000` 列，兩張表資料完全等價。

### 4.2 儲存：低基數省 59%，高基數打平

```sql
SELECT anchor_table_name, sum(used_bytes), max(row_count),
       round(sum(used_bytes)::numeric / max(row_count), 2) AS bytes_per_row
FROM v_monitor.column_storage
WHERE anchor_table_schema = 'article_lab_types'
GROUP BY 1;
```

| 表 | 總 bytes | 列數 | bytes/row |
|----|----------|------|-----------|
| `events_arr`（20 個標籤） | 1,106,697 | 200,000 | 5.53 |
| `events_norm`（20 個標籤） | 2,703,752 | 600,000 | 4.51 |
| `events_arr_hc`（60 萬唯一） | 2,635,830 | 200,000 | 13.18 |
| `events_norm_hc`（60 萬唯一） | 2,743,239 | 600,000 | 4.57 |

![同樣 60 萬個 (event, tag) 配對，低基數時陣列少 59%，高基數時只差 3.9%]({{ '/assets/images/types-array-storage.svg' | relative_url }}){: loading="lazy" }

拆到欄位層看更清楚：低基數時 `tags` 欄位只花 **106,054 bytes**，正規化的 `tag` 欄位花 **501,886 bytes**（4.73 倍）；換算每個標籤 0.177 對 0.836 bytes。但高基數時，`tags` 欄位 2,435,626 bytes、`tag` 欄位 2,142,694 bytes——**陣列存每個值反而貴 13.7%**（4.06 對 3.57 bytes）。

原因不是魔法，是 LZO：低基數陣列整塊高度重複，壓縮率極高；唯一值沒有重複可壓，陣列還要付元素邊界與長度前綴的成本。**「ARRAY 比較省空間」不是通則，是低基數才成立的特例。**

### 4.3 查詢：`contains()` 下推，`EXPLODE` 不下推

成員查詢（有 30,000 個事件帶 `t07`）：

```sql
SELECT count(*) FROM events_arr WHERE contains(tags, 't07');                      -- 20.869 / 17.455 ms
SELECT count(*) FROM (SELECT event_id FROM events_norm
                      WHERE tag='t07' GROUP BY event_id) x;                        -- 20.391 / 16.937 ms
```

幾乎打平，因為 `contains()` 被下推成掃描層的過濾條件：

```
+-GROUPBY NOTHING [Cost: 32K, Rows: 1] (PATH ID: 1)
| +---> STORAGE ACCESS for events_arr [Cost: 32K, Rows: 10K] (PATH ID: 2)
| |      Projection: article_lab_types.events_arr_super
| |      Filter: contains(events_arr.tags, 't07')
```

但「每個標籤出現幾次」就翻盤了：

```sql
-- 陣列：先展開
SELECT tag, count(*) FROM (SELECT EXPLODE(tags) OVER (PARTITION BY event_id)
                           AS (pos, tag) FROM events_arr) x GROUP BY tag;
-- 261.530 / 261.547 ms

-- 正規化：直接聚合
SELECT tag, count(*) FROM events_norm GROUP BY tag;
-- 37.170 / 37.182 ms
```

計畫形狀說明一切：

```
-- 陣列版
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 65K, Rows: 1] (PATH ID: 1)
| +---> ANALYTICAL [Cost: 65K, Rows: 10K] (PATH ID: 3)
| |      Functions: Explode()
| | +---> STORAGE ACCESS for events_arr [Cost: 33K, Rows: 10K] (PATH ID: 4)

-- 正規化版
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 764, Rows: 1] (PATH ID: 1)
| +---> STORAGE ACCESS for events_norm [Cost: 640, Rows: 10K] (PATH ID: 2)
```

![ARRAY 用 EXPLODE 時多一個 UserDefined 節點，成本 65K；正規化只要 764]({{ '/assets/images/types-plan-shape.svg' | relative_url }}){: loading="lazy" }

`EXPLODE` 是 transform function，計畫裡是 `UserDefined` 節點，資料必須先進這個節點才能聚合；它不像 `contains()` 那樣能塞進 `ScanStep`。成本 65K 對 764，實際 7.0 倍。

**如果非用陣列不可，改寫成元素存取可以救回一部分**（同一個查詢，答案都是 40,000／每個標籤）：

```sql
SELECT tag, count(*) FROM (
  SELECT (e.tags)[k-1]::VARCHAR AS tag
  FROM events_arr e
  CROSS JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3
              UNION ALL SELECT 4 UNION ALL SELECT 5) n
  WHERE k <= array_length(e.tags)
) x GROUP BY tag;
-- 165.571 / 161.289 ms，成本 128K
```

比 `EXPLODE` 快 1.58 倍，但仍是正規化表的 4.5 倍。**陣列不適合逐值聚合，這點沒有便宜的繞路。**

其餘三個情境的量測（同一份資料）：

| 情境 | ARRAY | 正規化 | 倍率 |
|------|-------|--------|------|
| 全表取 60 萬個 tag（`sum(array_length)` vs `count(*)`） | 18.440 ms | 3.593 ms | 5.1× |
| 取 1,000 個事件的標籤 | 4.141 ms | 2.694 ms | 1.54× |
| 讀回 20,000 個事件（20,000 列 vs 60,000 列） | 17.327 / 12.158 ms | 18.736 / 18.161 ms | 0.65× |

最後一列是陣列唯一穩定贏的讀取情境：**當你要的是「整組值」而不是「值本身」**，陣列少 3 倍的列數，交付給用戶端的成本就少一截。

### 4.4 寫入：13.8 倍

同一份資料重建一次（`INSERT ... SELECT`，單一 session）：

| 目標 | 列數 | 時間 |
|------|------|------|
| `events_arr_w`（陣列） | 200,000 | 79.879 ms |
| `events_norm_w`（正規化） | 600,000 | 1,103.655 ms |

寫入是陣列最實在的優勢：一次 `INSERT` 就是一個事件，不需要「先寫主檔再寫明細」的交易配對，也沒有明細列的索引維護。反過來說，**正規化表的每一列都是一次寫入放大**，13.8 倍不是誤差。

### 4.5 ROW：0.34% 的溢價

```sql
CREATE TABLE t_flat   (id INT, uid INT, device VARCHAR(16), region VARCHAR(8), signup DATE);
CREATE TABLE t_struct (id INT, r ROW(uid INT, device VARCHAR(16), region VARCHAR(8), signup DATE));
```

各 200,000 列，同值：

| 表 | 總 bytes | bytes/row |
|----|----------|-----------|
| `t_flat` | 612,263 | 3.06 |
| `t_struct` | 614,330 | 3.07 |

`column_storage` 會把結構欄位拆成 `uid`、`device`、`region`、`signup` 四個虛擬欄位，`r` 本身只佔 **902 bytes**（200,000 列）。查詢也一樣：

```
=> EXPLAIN SELECT count(*) FROM t_struct WHERE (r).region = 'r03';
| |      Filter: (t_struct.r.region = 'r03')
| |      Projection: article_lab_types.t_struct_super
-- Cost: 665
=> EXPLAIN SELECT count(*) FROM t_flat WHERE region = 'r03';
-- Cost: 665
```

成本一模一樣，實際 7.820 ms 對 7.904 ms；兩個欄位的條件 9.834 ms 對 10.091 ms。`r.region` 與 `(r).region` 兩種寫法都可用。

**ROW 的成本不在儲存與查詢，在治理**：欄位是型別的一部分，加一個欄位要 `ALTER TABLE` 改型別（等於重寫該欄位），沒辦法像扁平欄位那樣獨立加減；而且 `SELECT *` 會把整個結構吐成 JSON 字串，對下游解析不友善。

### 4.6 JSON：10.1 倍空間、138 倍查詢

```sql
CREATE TABLE docs_json (doc_id INT, doc LONG VARCHAR);
-- 100,000 列，每列 6 個鍵，含一個巢狀 owner 物件
CREATE TABLE docs_flat (doc_id INT, status VARCHAR(8), region VARCHAR(8), score INT, device VARCHAR(8));
```

| 表 | 總 bytes | bytes/row |
|----|----------|-----------|
| `docs_json` | 2,122,634 | 21.23 |
| `docs_flat` | 210,329 | 2.10 |

儲存差 10.1 倍（`doc` 欄位單獨就 2,022,514 bytes）。查詢差距更大：

```sql
SELECT count(*) FROM docs_json
WHERE MAPLOOKUP(MAPJSONEXTRACTOR(doc), 'status') = 'active';   -- 740.383 / 693.208 ms
SELECT count(*) FROM docs_flat WHERE status = 'active';        --   5.383 /   4.859 ms
```

**138 倍。** 計畫裡它確實被下推到 `ScanStep`，但下推的是一個「每列都要解析 JSON」的純量運算式：

```
| |      Filter: (public.MapLookup(public.MapJSONExtractor(docs_json.doc), 'status') = 'active')
```

下推不等於便宜。這一點跟 `contains()` 的差別是：`contains()` 是原生陣列運算，`MapLookup(MapJSONExtractor(...))` 是每列一次字串解析。100,000 列要解析 100,000 次。

寫入也是：`docs_json` 763.442 ms、`docs_flat` 116.734 ms（6.5 倍）。

### 4.7 巢狀：ARRAY[ROW(...)] 可以，巢狀 JSON 不行

很多人以為「複雜型別」是同一件事，其實兩邊的巢狀支援完全不同。ARRAY 裝 ROW 是可以用的，欄位也能一路取到底：

```sql
CREATE TABLE t_arow (id INT, items ARRAY[ROW(sku VARCHAR, qty INT)]);
INSERT INTO t_arow VALUES (1, ARRAY[ROW('a',1), ROW('b',2)]);

SELECT id, (items)[0].sku AS first_sku, (items)[1].qty AS second_qty FROM t_arow;
 id | first_sku | second_qty
----+-----------+------------
  1 | a         |          2
```

`items` 印出來是 `[{"sku":"a","qty":1},{"sku":"b","qty":2}]`，索引、切片、欄位存取全部照 3.2 的規則走。同一件事在 JSON 上就不成立——`MAPLOOKUP(MAPJSONEXTRACTOR(...),'a')` 對巢狀物件回 NULL，連子鍵也回 NULL，因為 `MAPJSONEXTRACTOR` 只攤平第一層。**要巢狀結構，用 ARRAY/ROW；要保留原始 JSON，就別指望它可查。**

---

## 五、效能／成本分析

把上面所有量測收斂成一張表（同一份 60 萬配對的資料，單節點）：

| 指標 | ARRAY | 正規化表 | 結論 |
|------|-------|----------|------|
| 總佔用（低基數） | 1,106,697 B | 2,703,752 B | 陣列勝 2.44× |
| 總佔用（高基數） | 2,635,830 B | 2,743,239 B | 打平（3.9%） |
| 每值成本（高基數） | 4.06 B | 3.57 B | 正規化勝 13.7% |
| 成員查詢 | 20.9 ms | 20.4 ms | 打平 |
| 逐值聚合 | 261.5 ms | 37.2 ms | 正規化勝 7.0× |
| 逐值聚合（改寫） | 165.6 ms | 37.2 ms | 正規化勝 4.5× |
| 全量讀取 | 18.4 ms | 3.6 ms | 正規化勝 5.1× |
| 整組讀回 | 12.2 ms | 18.2 ms | **陣列勝 1.5×** |
| 寫入 60 萬值 | 79.9 ms | 1,103.7 ms | **陣列勝 13.8×** |
| EXPLAIN 成本（聚合） | 65K | 764 | 85× |

一句話總結：**ARRAY 是「寫多讀少、整組進整組出」的型別；一旦你要對元素做聚合、join、排序，它就是把查詢成本搬到執行期。**

量測上的誠實說明：這兩張表都沒有跑過 `ANALYZE_STATISTICS`，`EXPLAIN` 的 `Rows` 全部是 `(NO STATISTICS)` 的預設估算（10K），所以成本數字（65K／764／128K）只能當「計畫形狀」的相對指標，不是基數估算準確度的證據。7.0 倍、4.5 倍、13.8 倍這些倍率則來自實際執行時間，與統計資訊無關。若你的表已經收集過統計，成本差距會隨真實基數變動，但「多一個 UserDefined 節點」這件事不會變。

---

## 六、常見錯誤與反模式

以下八個都是本次實測直接撞出來的訊息，不是文件抄來的。

**1. 把 SET 當欄位型別**

```
ERROR 5108:  Type "SET" does not exist
HINT:  Expected user-defined type
```

看到 5108 不要懷疑語法，先確認這個型別在 25.4 存不存在。`SET<VARCHAR>` 更糟，會在 `<` 就 `ERROR 4856` 語法錯誤。

**2. 以為有原生 JSON 型別**

```
ERROR 5108:  Type "JSON" does not exist
```

`JSON`、`JSONB` 都不存在。宣告 `LONG VARCHAR` 才是可用的做法。

**3. 想在一般表放 MAP**

```
ERROR 10329:  Only Parquet or Orc External Tables can use map type
```

`MAP{...}` 建構子也不存在（`ERROR 4856`）。MAP 只能從 `MAPJSONEXTRACTOR` 或外部表來。

**4. 用 EXPLODE 但忘了 OVER**

```
ERROR 5401:  User-defined transform function Explode must have an OVER clause
```

正確寫法是 `EXPLODE(tags) OVER (PARTITION BY event_id)`。加上去之後還有一個坑：它回**兩欄**（位置, 值），別名要給兩個。

```
ERROR 5386:  User defined transform will return 2 columns, whereas 1 aliases provided
```

而且順序是 `(pos, value)`、位置**從 0 開始**：

```
=> SELECT pos, tag FROM (SELECT EXPLODE(ARRAY['a','b']) OVER (PARTITION BY 1) AS (pos, tag)) x;
 pos | tag
-----+-----
   0 | a
   1 | b
```

**5. 想直接 INSERT EXPLODE 的結果**

```
ERROR 2521:  Cannot specify anything other than user defined transforms and
             partitioning expressions in the SELECT list
```

`INSERT ... SELECT` 裡把 UDTF 包進子查詢再取欄位會被拒。要嘛在 SELECT 最外層直接用 transform 並對齊欄位，要嘛改用 4.1 的元素存取展開。

**6. 陣列元素型別不會自動轉**

```
ERROR 2631:  Column "a" is of type array[int] but expression is of type array[varchar]
HINT:  You will need to rewrite or cast the expression
```

`INSERT INTO t (a) VALUES (ARRAY['1','2'])` 進 `ARRAY[INT]` 欄位是錯的，要逐元素或整組 `::ARRAY[INT]`。宣告成 `ARRAY[VARCHAR(3)]` 更容易中：

```
ERROR 4800:  String of 6 octets is too long for type Varchar(3)
```

**7. 用 `contains()` 找多個值**

```
ERROR 9899:  The second argument of type [array[varchar(3), 2]] is not a scalar type
```

`contains(tags, ARRAY['t02','t09'])` 不支援「任一符合」。要嘛 `contains(tags,'t02') OR contains(tags,'t09')`，要嘛展開。

**8. 忘記 NULL 陣列與空陣列的差別**

```
 id |  tags   | len | has_t07 | no_t07
----+---------+-----+---------+--------
  1 |         |     |         |
  2 | []      |   0 | f       | t
  3 | ["t07"] |   1 | t       | f
  4 | ["t01"] |   1 | f       | t
```

`WHERE NOT contains(tags,'t07')` 在 4 列中只回 **2 列**，不是 3 列——`tags IS NULL` 的那一列因為三值邏輯被濾掉。要包含它得寫 `WHERE tags IS NULL OR NOT contains(tags,'t07')`。另外 `array_length(NULL)` 回 NULL、`array_length(ARRAY[]::ARRAY[VARCHAR])` 回 0，兩者語意不同，別用 `= 0` 代替 `IS NULL`。

---

## 七、風險與建議

### 現在就能做（唯讀、低風險）

- **盤點現有欄位型別**：`SELECT table_schema, table_name, column_name, data_type FROM v_catalog.columns WHERE data_type ILIKE 'array%' OR data_type ILIKE 'set%' OR data_type ILIKE 'row%';` 先知道哪些表用了複雜型別、有幾張。
- **量測實際佔用**：用 `v_monitor.column_storage` 依 `anchor_table_name` 加總 `used_bytes`，再除以 `row_count`。同一份資料的新舊設計擺在一起，比看文件快。
- **找出對陣列做逐值聚合的查詢**：在 `v_monitor.query_events` 或 `dc_queries` 裡搜 `EXPLODE`、`array_length`，這些是最可能被陣列拖慢的語句。
- **檢查 JSON 欄位的巢狀解析**：任何 `MAPLOOKUP(MAPJSONEXTRACTOR(...), 'key')` 如果 `key` 指向巢狀物件，永遠回 NULL。這種查詢不會報錯，只會回空值，建議加一筆 `count(*)` 對帳。

### 需要變更（要窗口、要核准）

- **把逐值聚合的陣列欄位拆成正規化子表**：本測 7.0 倍的差距，值得為此改 schema。改完記得同時保留 `event_id` 對主表的關聯。
- **JSON 熱欄位落地**：把常過濾的 3–5 個鍵抽成具名欄位（本測儲存 10.1 倍、查詢 138 倍的差距），JSON 原檔留著當備份欄位，而不是查詢介面。
- **不要對 `ROW` 欄位做頻繁的型別變更**：加欄位要 `ALTER TABLE` 改型別，等於重寫該欄位，200,000 列的規模還行，上億列要排維護窗口。
- **寫入路徑優先選陣列**：如果應用是「一次寫一個事件、之後整組讀出來」，13.8 倍的寫入差距與 1.5 倍的讀回優勢會直接反映在 ETL 時間上。

---

## 八、參考資料

- Vertica 25.4 文件：Complex Types — <https://docs.vertica.com/25.4.x/en/sql-reference/data-types/complex-types/>
- Vertica 25.4 文件：Array Functions — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/array-functions/>
- Vertica 25.4 文件：JSON Functions（MapJSONExtractor、MapLookup） — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/json-functions/>
- Vertica 25.4 文件：Map Functions — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/map-functions/>
