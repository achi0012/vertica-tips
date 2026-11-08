---
layout: post
title: "Vertica 25.4 VectorOps 實戰：用 ARRAY 做向量相似度搜尋"
date: 2026-11-05 05:00:00 +0800
categories: vertica vector ai
tags: [vertica, vectorops, cosine-similarity, dot-product, array, ai, similarity-search, 25.4]
description: "在 Vertica 25.4 用 ARRAY[FLOAT] 儲存 embedding，實測 COSINE_SIMILARITY、DOT_PRODUCT、VECTOR_L2、VECTOR_MAGNITUDE，打造 Top-K 向量相似度搜尋，並解析沒有向量索引時的效能取捨。"
image: /assets/images/og/vertica-vectorops-array-similarity-search-zh.png
faq:
  - q: "Vertica 25.4 支援向量索引嗎？"
    a: "不支援。本機實測 CREATE INDEX 直接回 ERROR 6200: Command CREATE INDEX is not supported；每一次 Top-K 都是完整掃描加 TopK heap。"
  - q: "ARRAY[FLOAT] 最多可以存幾維？"
    a: "單一向量上限是 65,000 bytes，float8 每維 8 bytes，所以最多 8,125 維；1,536 維與 3,072 維的 embedding 都在範圍內。"
  - q: "NULL embedding 為什麼會排第一？"
    a: "COSINE_SIMILARITY 對 NULL 回 NULL，而 ORDER BY ... DESC 預設把 NULL 當最大值。查詢加 WHERE emb IS NOT NULL 或 NULLS LAST 即可。"
---

<!--
AI Summary: Vertica 25.4.0-0 單節點實測 VectorOps。10 萬筆 8 維、10 萬筆 64 維、5,000 筆 1,536 維三組資料，驗證 COSINE_SIMILARITY／DOT_PRODUCT／VECTOR_L2／VECTOR_MAGNITUDE 四個函數與 Top-K 寫法。全掃 Top-10 需 172–235 ms（每列 1.7 µs），pre-filter 可降到 21–30 ms，post-filter 會丟掉 9/10 結果。實測 25.4 不支援 CREATE INDEX 與 VECTOR 型別，ARRAY[FLOAT] 以 float8 儲存、單值上限 65,000 bytes（8,125 維），且 NULL 向量會在 ORDER BY DESC 排第一。
AI Keywords: Vertica, VectorOps, cosine similarity, dot product, ARRAY, embedding, Top-K, 向量搜尋, 25.4
-->
<!--more-->

---

## 一、先講結論

Vertica 25.4 可以用 `ARRAY[FLOAT]` 存 embedding，並用四個內建函數做相似度計算。**它可以跑，但沒有向量索引** — 本機實測 `CREATE INDEX` 直接回 `ERROR 6200: Command CREATE INDEX is not supported`，所以每一次 Top-K 查詢都是一次完整掃描加一個 TopK heap。

我在 10 萬筆 8 維向量上量到：Top-10 cosine 排序 **172–235 ms**，平均每列 1.7 µs。把查詢加上 5% 的 tenant 過濾條件後降到 **27–30 ms**；若資料按 tenant 實體分叢（20 個 ROS 容器、每容器一個 tenant），再降到 **21–23 ms**，掃描列數從 100,000 降到 5,000。

這篇適合兩種人：

1. 想在既有的 Vertica 上做 RAG／相似度搜尋，不想另外架一套向量資料庫的人。
2. 已經在寫 `ORDER BY cosine_similarity(...) LIMIT 10`，但想搞清楚它到底掃了多少、什麼時候會失控的人。

本文所有數字都來自本機 25.4.0-0 實測，SQL 可複製重跑。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| Vertica 版本 | `v25.4.0-0` |
| 節點數 | 1（單節點，Community Edition） |
| 資料庫 | 測試庫 `testdb` |
| 測試 schema | `article_lab_vectorops`（結束已 `DROP SCHEMA ... CASCADE`） |
| 測試資料 | 100,000 列 × 8 維、100,000 列 × 64 維、5,000 列 × 1,536 維 |
| 量測時間 | 2026-11-05 |

兩個環境細節會影響你重現實驗：

- **這個連線的 `autocommit` 是 `off`**（`SHOW AUTOCOMMIT` 回 `off`），所以每個 DML 後面我都有明確 `COMMIT`。沒 commit 就換 session，資料會消失 — 我第一次跑就踩到。
- `random()` 每次呼叫都會回不同值，所以「同一列同時算原始向量與正規化向量」不能寫在同一個 `SELECT` 裡呼叫兩次，要先落地成 staging table 的 8 個純量欄位。

8 維不是真實 embedding 的維度，只是為了讓 10 萬列的實驗夠輕。真實維度（1,536）我另外用 5,000 列測，結果在第六節。

---

## 三、核心原理：四個函數與一種儲存格式

### 3.1 VectorOps 只有四個函數

25.4 的向量函數就是這四個，沒有第五個：

| 函數 | 語意 | 參數 | 實測 |
|------|------|------|------|
| `COSINE_SIMILARITY(a, b)` | 兩向量夹角餘弦，越大越相似 | 2 個同長度陣列 | `[1,0]` vs `[0,1]` → `0` |
| `DOT_PRODUCT(a, b)` | 內積 | 2 個同長度陣列 | `[1,2]`·`[3,4]` → `11` |
| `VECTOR_L2(a, b)` | 歐氏距離，越小越近 | **2 個**同長度陣列 | `[3,4]` vs `[0,0]` → `5` |
| `VECTOR_MAGNITUDE(v)` | 向量長度（L2 norm） | **1 個**陣列 | `[3,4]` → `5` |

這裡有個容易誤會的點：**`VECTOR_L2` 是「兩點的距離」，不是「向量的長度」**。我一開始照名字以為是 norm，傳一個參數進去，得到：

```
ERROR 3399: Failure in UDx RPC call InvokeSetup(): ... [vector_l2] ...
message: VectorDistance expects exactly 2 arguments, but got 1.
```

要算長度請用 `VECTOR_MAGNITUDE`。這兩個函數的關係是 `VECTOR_L2(a, b) = VECTOR_MAGNITUDE(a - b)`（本質上），但 Vertica **不支援陣列加減法**，所以你不能自己寫 `a - b`：

```
ERROR 4286: Operator does not exist: array[float] / numeric
```

連除以純量都不行。這一點直接決定了 ETL 的寫法：**正規化必須在寫入前用純量欄位算好，不能在 SQL 裡對陣列做 `v / |v|`**。

### 3.2 底層是 float8，不是 float4

`CREATE TABLE ... (emb ARRAY[FLOAT])` 建出來的欄位，在 `v_catalog.columns` 長這樣：

```
 table_name | column_name |      data_type       | data_type_length
------------+-------------+----------------------+------------------
 docs       | emb         | array[float8](65000) |            65000
```

`FLOAT` 在 Vertica 是 8 bytes 的 double，所以每個維度實際吃 8 bytes，不是 4 bytes。三張表的實測儲存密度：

| 表 | 列數 | 維度 | 陣列欄位數 | `used_bytes` | 每元素 |
|----|-----:|-----:|-----------:|-------------:|-------:|
| `docs` | 100,000 | 8 | 2 | 12,174,735 | 7.6 B |
| `docs64` | 100,000 | 64 | 2 | 94,872,049 | 7.4 B |
| `docs1536` | 5,000 | 1,536 | 1 | 59,175,799 | 7.7 B |

每元素 7.4–7.7 bytes，比原始 8 bytes 略低 — 預設 encoding 只擠出一點點，**不要期待陣列欄位有明顯壓縮**。1,536 維 × 100 萬列的集合，光向量本體就是 11.8 GB。

### 3.3 單一向量上限：65,000 bytes

`data_type_length` 的 65,000 不是裝飾，是硬上限。我逐步逼近：

| 維度 | 結果 |
|-----:|------|
| 5,000 | 寫入成功 |
| 8,125 | 寫入成功 |
| 8,126 | `ERROR 9227: Output array isn't big enough`<br>`DETAIL: Type limit is 65000 bytes, but value needs 65008 bytes` |

65,000 / 8 = 8,125，剛好。所以 `ARRAY[FLOAT]` 存得下 8,125 維以內；OpenAI `text-embedding-3-large` 的 3,072 維、`ada-002` 的 1,536 維都在範圍內。

順帶一個陷阱：`ARRAY[FLOAT](8)` 的括號是 **bytes 不是維度**。想限制「8 維」而宣告成 `(8)`，結果任何 8 維寫入都會失敗：

```
ERROR 9227: Output array isn't big enough
DETAIL: Type limit is 8 bytes, but value needs 64 bytes
```

要限制 8 維要寫 `ARRAY[FLOAT](64)`。這個命名方式非常反直覺。

### 3.4 沒有索引，所以計畫長這樣

```
+-SELECT  LIMIT 10 [Cost: 625K, Rows: 10 (NO STATISTICS)] (PATH ID: 0)
|  Output Only: 10 tuples
| +---> SORT [TOPK] [Cost: 625K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
| |      Order: public.cosine_similarity(docs.emb, ARRAY[0.9, 0.1, ...]) DESC
| |      Output Only: 10 tuples
| | +---> STORAGE ACCESS for docs [Cost: 34K, Rows: 10K (NO STATISTICS)] (PATH ID: 2)
| | |      Projection: article_lab_vectorops.docs_super
| | |      Materialize: docs.id, docs.tid, docs.emb
```

整棵樹只有三層：**掃描 → 算分數 → 保留前 10 名**。沒有 index scan、沒有 ANN 預過濾，`LIMIT 10` 不會讓它少讀一列 — `LIMIT` 只影響 TopK heap 的大小，不影響掃描量。

還有一個線索：這些函數在 `\df` 裡的分類是 **User Defined Function**，錯誤訊息也都是 `Failure in UDx RPC call ...`。它們是走 UDx 介面實作的，所以成本結構跟內建 operator 不同 — 這也是為什麼下面「算 800,000 次乘加要 172 ms」不會是純 CPU 時間。

---

## 四、Step-by-step 實測

### 4.1 建表：原始向量與正規化向量分開存

```sql
CREATE TABLE article_lab_vectorops.docs (
  id       int,
  tid      int,              -- tenant，之後做 pre-filter
  emb      ARRAY[FLOAT],     -- 原始向量
  emb_unit ARRAY[FLOAT]      -- L2 正規化後
);

INSERT INTO article_lab_vectorops.docs
SELECT id, tid,
       ARRAY[d1,d2,d3,d4,d5,d6,d7,d8]::ARRAY[FLOAT],
       ARRAY[d1/m,d2/m,d3/m,d4/m,d5/m,d6/m,d7/m,d8/m]::ARRAY[FLOAT]
FROM (SELECT *, sqrt(d1*d1+d2*d2+d3*d3+d4*d4+d5*d5+d6*d6+d7*d7+d8*d8) AS m
      FROM article_lab_vectorops.stage) s;
COMMIT;
```

10 萬列插入 525 ms。注意 `m` 是從 staging table 的 8 個**欄位**算出來的，不是在 `SELECT` 裡呼叫兩次 `random()` — 後者會讓 `emb` 和 `emb_unit` 對不起來。

驗證正規化正確：

```sql
=> SELECT id, round(VECTOR_MAGNITUDE(emb)::numeric,6) AS raw_mag,
              round(VECTOR_MAGNITUDE(emb_unit)::numeric,6) AS unit_mag
   FROM docs WHERE id = 1;
 id |      raw_mag      |      unit_mag
----+-------------------+-------------------
  1 | 1.617282000000000 | 1.000000000000000
```

### 4.2 Top-K 查詢

```sql
=> SELECT id, tid,
          round(COSINE_SIMILARITY(emb,
            ARRAY[0.9,0.1,0.4,0.7,0.2,0.95,0.05,0.3]::ARRAY[FLOAT])::numeric, 6) AS score
   FROM article_lab_vectorops.docs
   ORDER BY score DESC
   LIMIT 10;
  id   | tid |       score
-------+-----+-------------------
 87038 |  19 | 0.996290000000000
 93913 |  14 | 0.995424000000000
 55400 |   1 | 0.993299000000000
 85777 |  18 | 0.990120000000000
 13375 |  16 | 0.987861000000000
 ...
(10 rows)
```

`Time: 172–235 ms`（暖機後三次：235、172 ms，另有一次冷啟動 475 ms）。

### 4.3 正規化：DOT_PRODUCT 與 COSINE 的差距不是理論問題

同一批資料、同一個查詢向量，比較兩種排序的前 10 名：

```sql
WITH q AS (SELECT ARRAY[0.9,0.1,0.4,0.7,0.2,0.95,0.05,0.3]::ARRAY[FLOAT] AS v),
cos AS (SELECT d.id, COSINE_SIMILARITY(d.emb, q.v) AS s
        FROM docs d, q ORDER BY s DESC LIMIT 10),
dot AS (SELECT d.id, DOT_PRODUCT(d.emb, q.v) AS s
        FROM docs d, q ORDER BY s DESC LIMIT 10)
SELECT (SELECT COUNT(*) FROM cos JOIN dot USING (id)) AS overlap;
 overlap
---------
       0
```

**重疊 0 筆。** 兩份 Top-10 完全沒有交集。原因寫在資料裡：

| 排名方式 | 前 10 名平均向量長度 | 全表平均 |
|----------|---------------------:|---------:|
| `COSINE_SIMILARITY` | 1.5165 | 1.6109 |
| `DOT_PRODUCT` | **2.3799** | 1.6109 |

`DOT_PRODUCT` 挑出來的平均長度 2.38，明顯高於全表平均 1.61 — 它在獎勵「長向量」，不是在獎勵「方向接近」。這不是誤差，是選錯人。

把兩邊都正規化後，兩種算法就完全等價：

```sql
SELECT round(MAX(abs(COSINE_SIMILARITY(emb_unit, q_unit)
                   - DOT_PRODUCT(emb_unit, q_unit)))::numeric, 9) AS max_abs_diff
FROM docs;
  max_abs_diff
----------------
 0.000000000000
```

10 萬列的最大差異是 0（到小數 9 位），Top-10 重疊 **10/10**。

實務結論：**要嘛用 COSINE 配原始向量，要嘛先正規化再用 DOT_PRODUCT，不要混用。** 混用不會報錯，只會安安靜靜地回傳錯的結果 — 這種 bug 最難查。

至於兩者速度，本機沒有可量測差異（cosine 177–217 ms、dot 164–178 ms，落在同一區間）；正規化的價值在正確性，不在效能。

### 4.4 Pre-filter：差 6 倍，但關鍵不是語法

多租戶場景一定要先篩 tenant 再排序。我測三種寫法，資料都是 100,000 列、`tid=1` 佔 5%（5,000 列）。

**寫法 A：全掃不加條件** — 172–235 ms。
**寫法 B：`WHERE tid = 1`** — 27–30 ms。
**寫法 C：先取全域 Top-10 再過濾（反模式）** — 256 ms，而且只活下來 **1 筆**：

```sql
SELECT count(*) FROM (
  SELECT id FROM docs
  ORDER BY COSINE_SIMILARITY(emb, ARRAY[...]::ARRAY[FLOAT]) DESC
  LIMIT 10) t
WHERE t.id IN (SELECT id FROM docs WHERE tid = 1);
 count
-------
     1
```

寫法 C 是「先取全站最像的 10 篇，再問其中哪幾篇屬於 tenant 1」— 語意上就錯了，耗時還更長。正確順序是 `WHERE` 先、`ORDER BY` 後。

**寫法 B 到底省了什麼？** 用 `PROFILE` 看掃描運算子的實際列數：

```sql
PROFILE SELECT id FROM docs WHERE tid = 1 ORDER BY COSINE_SIMILARITY(...) DESC LIMIT 10;
```

| 資料排列 | Scan `rows processed` | Scan `rows produced` | 查詢時間 |
|----------|----------------------:|---------------------:|---------:|
| 隨機分佈，1 個 container | 100,000 | 5,000 | 27–30 ms |
| 依 tenant 分叢，20 個 container | **5,000** | 5,000 | **21–23 ms** |

第二種排列是我用 20 次 `INSERT ... WHERE tid = k` 造出來的（每個 tenant 一個 ROS 容器）。掃描列數從 100,000 掉到 5,000 — **zone map 依 container 的 min/max 統計直接跳過 19 個容器**。這不是 SQL 語法能控制的，是資料怎麼落地決定的。

所以 pre-filter 的效益分兩層：

1. **SQL 層**：把 `WHERE` 放在 `ORDER BY` 之前，過濾後的列不必進 UDF。這一層是 6–8 倍。
2. **實體層**：讓同一個 tenant 的列集中在少數容器，zone map 才能跳過整批。這一層在這次實驗只再省 20–25%（因為表只有 12 MB，I/O 本來就不是瓶頸），但資料量放大後差距會拉開。

沒有 partition 或 sort order 支撐時，`WHERE tid = 1` 仍然要讀完 100,000 列才能濾掉 95%。這一點跟一般 OLAP 查詢的直覺一致，只是很多人以為向量搜尋會自動變快。

### 4.5 批次查詢：LATERAL 不能用

多個 query vector 一次算很常見（例如一次比對 20 個使用者問題）。25.4 不支援 `LATERAL`：

```
ERROR 11320:  JOIN LATERAL does not support subqueries
```

（連 `CROSS JOIN LATERAL` 也是語法錯誤。）可行的寫法是 `CROSS JOIN` 加窗口函數：

```sql
SELECT qid, id, score FROM (
  SELECT q.qid, d.id, COSINE_SIMILARITY(d.emb, q.q) AS score,
         ROW_NUMBER() OVER (PARTITION BY q.qid
                            ORDER BY COSINE_SIMILARITY(d.emb, q.q) DESC) AS rn
  FROM article_lab_vectorops.queries q
  CROSS JOIN article_lab_vectorops.docs d) x
WHERE rn <= 5
ORDER BY qid, score DESC;
```

3 個 query 對 10 萬列 = 30 萬次相似度計算，實測 **341 ms**。相比逐條執行（3 × 約 172 ms = 516 ms）快一些，因為掃描只做一次。計畫裡可以看到 planner 自己用了 `TopK[HEAP][PREPASS]` 搭配 `PartitionKeys: 1`，也就是「每個 query 各留一份 heap」。

---

## 五、效能與成本分析

### 5.1 時間：K 幾乎不影響，維度影響很大

100,000 列的 Top-K，暖機後：

| 查詢 | 時間 |
|------|-----:|
| 8 維，K=10 | 172–235 ms |
| 8 維，K=100 | 183 ms |
| 8 維，K=1,000 | 185 ms |
| 8 維，K=10,000 | 192 ms |
| 8 維，只算 `AVG(cosine)` 不排序 | 322 ms |
| 64 維，K=10 | 616–739 ms |
| 1,536 維（5,000 列），K=10 | 566–726 ms |

兩個反直覺的地方：

1. **K 從 10 拉到 10,000，時間只多 20 ms。** TopK heap 不是瓶頸，瓶頸是「每一列都要呼叫一次 UDF」。所以想靠「先取多一點再過濾」優化是沒用的。
2. **只算 `AVG()` 不排序，反而比 Top-10 慢（322 ms）。** 這說明成本主體在掃描與 UDF 計算，不在排序。冷啟動第一次查詢 475 ms，第二第三次掉到 172 ms — 12 MB 的表完全進得了 cache，star schema 那種規模就不會這麼幸運。

換算成單位成本（8 維）：172 ms / 100,000 列 = **1.72 µs/列**；64 維時 616 ms / 100,000 = 6.16 µs/列。維度變 8 倍，時間變 3.6 倍 — 每個維度的邊際成本反而下降，因為固定開銷（每列的 UDF 呼叫）被攤掉了。

以 1.72 µs/列外推：100 萬列約 1.7 秒、1,000 萬列約 17 秒。這是單節點、單執行緒的數量級，實際會隨並發與投影數變化。**這種延遲要做互動式搜尋可以，要做 API 的即時後端就不行。**

### 5.2 儲存：7.5 bytes/維

| 表 | `used_bytes` | MB | 每列 | 每元素 |
|----|-------------:|----:|-----:|-------:|
| `docs`（8 維 × 2 欄） | 12,174,735 | 11.6 | 121.7 B | 7.6 B |
| `docs64`（64 維 × 2 欄） | 94,872,049 | 90.5 | 948.7 B | 7.4 B |
| `docs1536`（1,536 維 × 1 欄） | 59,175,799 | 56.4 | 11,835 B | 7.7 B |

`docs64` 的 90.5 MB 對 100,000 列來說不誇張，但別忘了這是**同一份資料存兩份**（原始 + 正規化）。如果只需要一種算法，就只存一欄。

1,536 維那張表是用 `COPY` 從 66 MB 的 TSV 載入的，5,000 列花 1,540 ms（約 3,250 列/秒）。它同時也是我踩到 `MaxParsedQuerySizeMB` 的地方：用 `INSERT ... ARRAY[1536 個字面值]` 會直接撞上限 —

```
ERROR 8617: Request size too big
DETAIL: Total allocated memory (MiB) = 1024.000 is about to exceed limit
        imposed by MaxParsedQuerySizeMB (MiB) = 1024.000
```

真實場景本來就該走 `COPY`，但如果你打算用 SQL 生成測試資料，這條路在 1,536 維時會中斷。

### 5.3 查詢記憶體：query vector 的來源會差 83 倍

`PROFILE` 會印出 Vertica 申報的查詢記憶體需求（`NOTICE 5077`）：

| 查詢 | 申報記憶體 |
|------|-----------:|
| `SELECT count(*) FROM docs` | 7,075 KB |
| 8 維 Top-10，query vector 用**字面值** | 71,873 KB |
| 8 維 Top-1,000，字面值 | 135,310 KB |
| 8 維 Top-10，query vector 來自**資料表**（join／CTE） | 6,079,897 KB |
| 1,536 維（5,000 列）Top-10，query vector 來自資料表 | 6,078,780 KB |

同一個查詢、同一張表，只把 query vector 從字面值改成「從一張 queries 表 join 進來」，申報需求從 71 MB 跳到 **5.8 GB**，而且 5,000 列與 100,000 列都是同一個 5.8 GB，看不出隨列數縮放。

我沒有量測實際 RSS，所以無法斷言它真的吃了 5.8 GB；但這個數字會直接進 resource pool 的准入判斷，在 `general` pool 記憶體吃緊時可能讓查詢排隊或被拒。**能內嵌字面值就內嵌，批次模式要先在測試環境確認 pool 撐得住。**

### 5.4 統計資訊：不是萬靈丹，但一定要收

建表後沒收統計，`EXPLAIN` 的估算列數是 10K（表實際 100,000 列，低估 10 倍）：

```
+-SELECT  LIMIT 10 [Cost: 625K, Rows: 10 (NO STATISTICS)] (PATH ID: 0)
| +---> SORT [TOPK] [Cost: 625K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
| | +---> STORAGE ACCESS for docs [Cost: 34K, Rows: 10K (NO STATISTICS)] (PATH ID: 2)
```

跑 `SELECT ANALYZE_STATISTICS('article_lab_vectorops.docs');`（21 ms）之後：

```
+-SELECT  LIMIT 10 [Cost: 7M, Rows: 10 (ROW COUNT ONLY)] (PATH ID: 0)
| +---> SORT [TOPK] [Cost: 7M, Rows: 100K (ROW COUNT ONLY)] (PATH ID: 1)
| | +---> STORAGE ACCESS for docs [Cost: 318K, Rows: 100K (ROW COUNT ONLY)] (PATH ID: 2)
```

估算列數正確了（100K），成本估計從 625K 漲到 7M，**執行時間沒有改善（176 ms，仍落在 172–235 ms 的變異範圍內）**。單表 Top-K 的計畫形狀沒得選，統計只影響成本數字；但如果你有多個候選計畫（例如 join 多張表再排序），10 倍的低估就足以讓 planner 選錯邊。

另外，**陣列欄位收不了欄位統計**：

```sql
=> SELECT ANALYZE_STATISTICS('article_lab_vectorops.docs', 'emb');
ERROR 9618:  Cannot analyze statistics of a collection column
DETAIL:  Column docs_super.emb is of array[float8](65000) type
```

所以 `EXPLAIN` 永遠只會顯示 `ROW COUNT ONLY`。這不是設定問題，是型別限制。

---

## 六、常見錯誤與反模式

### 錯誤 1：NULL 向量排第一名

```sql
INSERT INTO docs VALUES (999999, 1, NULL, NULL);
COMMIT;

=> SELECT id, COSINE_SIMILARITY(emb, ARRAY[...]::ARRAY[FLOAT]) AS score
   FROM docs ORDER BY score DESC LIMIT 5;
   id   |       score
--------+-------------------
 999999 |
  87038 | 0.996290152908361
```

`COSINE_SIMILARITY(NULL, v)` 回 `NULL`（函數是 strict），而 Vertica 的 `ORDER BY ... DESC` 把 NULL 當成最大值排在最前面。**一筆漏寫 embedding 的資料，就能穩坐搜尋結果第一名。** 批次模式更糟 — 它會擠掉每個 query 的第 5 名（我測 3 個 query，3 個的第 5 名都是 999999）。

修法：`WHERE emb IS NOT NULL`，或 `ORDER BY score DESC NULLS LAST`。前者更好，因為它同時省下不必要的掃描。

### 錯誤 2：以為 `ARRAY[FLOAT](n)` 的 n 是維度

`ARRAY[FLOAT](8)` 是 8 bytes（= 1 個 float8），不是 8 維。宣告下去，所有正常寫入都會失敗：

```
ERROR 9227: Output array isn't big enough
DETAIL: Type limit is 8 bytes, but value needs 64 bytes
```

要限制維度只能自己換算 bytes（8 維 → `(64)`），或乾脆不宣告、用預設的 65,000 bytes。老實說這個設計很容易誤用，我建議**不要宣告寬度**，改在 ETL 層檢查 `array_length()`。

### 錯誤 3：以為陣列可以算術

```
=> SELECT ARRAY[1.0,2.0]::ARRAY[FLOAT] / 2.0;
ERROR 4286:  Operator does not exist: array[float] / numeric
```

沒有 `array / scalar`、沒有 `array + array`。正規化只能在寫入前用純量欄位算，或寫成 `ARRAY[v1/m, v2/m, ...]` 展開。想要「查詢時才正規化」的寫法在 25.4 行不通。

### 錯誤 4：陣列索引從 1 開始

```
=> SELECT (ARRAY[10.0,20.0,30.0]::ARRAY[FLOAT])[0] AS i0,
          (ARRAY[10.0,20.0,30.0]::ARRAY[FLOAT])[1] AS i1;
 i0 | i1
----+----
 10 | 20
```

**0-based**。從 1 開始取值會拿到第二個元素，而且不會報錯。另外 `array_upper(arr)` 在這個版本對 `ARRAY[FLOAT]` 直接回 `function does not exist`，請改用 `array_length(arr)`。

### 錯誤 5：用 `CREATE INDEX` 想加速

```
ERROR 6200:  Command CREATE INDEX is not supported
HINT:  Did you mean 'CREATE TEXT INDEX'?
```

25.4 沒有 B-tree、沒有 ANN。`CREATE TEXT INDEX` 是全文檢索，跟向量無關。任何「幫 embedding 建索引」的期待在這一版都不成立 — 這是官方 KB 也明講的（見參考資料）。

### 錯誤 6：把 `VECTOR` 當成型別

```
=> CREATE TABLE t (v VECTOR(8));
ERROR 5108:  Type "catalog.VECTOR" does not exist
HINT:  Expected user-defined type
```

25.4 沒有原生 `VECTOR` 型別，只有 `ARRAY[FLOAT]`。網路上有些 26.x 的範例會用 `VECTOR`，貼過來會直接失敗。

---

## 七、風險與建議

### 現在就能做（唯讀／低風險）

1. **先量再調**：用 `EXPLAIN` 確認計畫是不是「Scan → TopK」兩層。如果是，你就沒有索引可用，優化只能從「少掃幾列」下手。
2. **檢查 NULL embedding**：
   ```sql
   SELECT COUNT(*) FROM your_table WHERE emb IS NULL;
   ```
   只要不是 0，你的 Top-K 就可能回傳空分數。
3. **確認正規化與算法一致**：`COSINE_SIMILARITY` 配原始向量，或 `DOT_PRODUCT` 配已正規化向量。用 4.3 節的 `max_abs_diff` 查詢驗一次就知道有沒有混用。
4. **確認 pre-filter 寫在 `ORDER BY` 之前**，並用 `PROFILE` 看 `Scan` 的 `rows processed` 是否真的下降。
5. **檢查向量維度與 65,000 bytes 上限**：`SELECT MAX(array_length(emb)) FROM ...`，超過 8,125 就得拆欄位。

### 需要變更（要窗口／要核准）

1. **調整資料落地順序，讓 tenant 分叢**：把載入拆成「一個 tenant 一個 batch」，或用 partition／sort order 讓 zone map 有效。這是本文投報率最高的一項（掃描 100,000 → 5,000 列），但會改變載入流程，要在離峰做。
2. **為查詢記憶體留餘裕**：批次模式的申報需求是 5.8 GB 等級，正式上線前在測試環境用相同維度與列數跑一次 `PROFILE`，再決定 pool 大小。
3. **把 Top-K 查詢變成非同步**：1.72 µs/列 × 你的列數，先算出預期延遲。超過 2 秒就不適合放在同步 API 路徑。
4. **考慮「兩階段檢索」**：如果只是要縮小候選集，先用便宜的一般欄位條件（時間、分類、tenant）把資料砍到幾萬列，再做向量排序 — 這是本文驗證過、唯一在 25.4 有效的加速手段。

### 不建議

- 不要為了向量搜尋去改 `MaxParsedQuerySizeMB` 之類的參數來塞大 `INSERT`，改用 `COPY`。
- 不要在生產表上跑 `PROFILE`。本文所有 `PROFILE` 都在自己的 `article_lab_vectorops` schema；其餘量測用 `EXPLAIN` 加 `\timing`。
- 不要期待 mergeout 或重建投影能讓 Top-K 變快 — 瓶頸是每列的 UDF 呼叫，不是容器碎片。

### 選型：什麼時候這個做法不夠用

把實測數字攤成延遲預算，比較容易判斷：

| 情境 | 依 1.72 µs/列外推 | 適用性 |
|------|------------------:|--------|
| 10 萬列，離線報表／每日批次 | ~0.2 秒 | 很適合，直接留在 Vertica |
| 100 萬列，互動式查詢（1–3 秒可接受） | ~1.7 秒 | 可以，但要注意並發 |
| 1,000 萬列，同步 API（< 200 ms） | ~17 秒 | 不適合，需要 ANN 索引或外部向量庫 |
| 需要 recall 保證的語意搜尋 | — | 25.4 只能精確全掃，沒有近似檢索可調 |

「精確全掃」其實是優點也是缺點：缺點是慢，優點是 **recall 永遠是 100%**，不需要調 `ef_search`、`nprobe` 這類參數，也不會因為索引更新落後而漏結果。如果資料量在千萬列以下、延遲要求寬鬆（報表、批次推薦、離線聚類），Vertica 的優勢是「不用把資料搬出去」— 向量跟事實表在同一個地方，join 與 aggregation 都是原生 SQL。

反過來說，如果你的場景是「使用者打字就即時回傳」，或資料量已經到億級，那 25.4 的 VectorOps 只能當備援方案，不是主力。

---

## 八、實測環境與可重現性

| 項目 | 值 |
|------|-----|
| 版本 | `v25.4.0-0` |
| 節點 | 1（CE） |
| `autocommit` | `off`（DML 需明確 `COMMIT`） |
| 資料量 | 100,000 × 8 維、100,000 × 64 維、5,000 × 1,536 維 |
| 熱機後 Top-10 | 172–235 ms（8 維） |
| 清理 | `DROP SCHEMA article_lab_vectorops CASCADE` 已執行，`v_catalog.schemata` 查無殘留 |

三點說明：

1. 所有數字為單節點實測，多節點環境會有 resegmentation 與網路傳輸成本，絕對值會不同，但「掃描 → TopK」的計畫形狀與 pre-filter 的相對效益不變。
2. 8 維資料是 `random()` 生成的均勻分佈向量，與真實 embedding 的聚簇特性不同；真實資料的分數分佈會不一樣，但掃描與 UDF 的成本結構不變。
3. 我沒有在 25.4 找到任何可用的向量索引；如果後續版本支援，本文的效能結論就需要重測。

---

**參考資料**（Vertica 25.4.x）：

- 官方文件 Vector Functions 章節：`COSINE_SIMILARITY`、`DOT_PRODUCT`、`VECTOR_L2`、`VECTOR_MAGNITUDE` — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/vector-functions/>
- OpenText 社群 KB：*VectorOps in Vertica — support for Indexing*（明確記載 25.4.x 支援上述四個函數、ARRAY 可為 INT/FLOAT/NUMERIC，且**不支援索引**）— <https://community.opentext.com/data-analytics/analytics-db/w/tips/52470/knowledge-document-vectorops-in-vertica--support-for-indexing>
- 官方文件 Collection（ARRAY）型別與 `array_length`、`array_upper` 語意

*本文所有數字取自 Vertica 25.4.0-0 單節點實測（2026-11-05）。測試 schema 已刪除；文中 SQL 只使用自建物件，未對任何既有表做變更。*
