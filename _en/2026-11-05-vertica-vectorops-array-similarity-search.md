---
layout: post_en
title: "Vertica 25.4 VectorOps in Practice: Vector Similarity Search with ARRAY"
date: 2026-11-05 05:00:00 +0800
categories: vertica vector ai
tags: [vertica, vectorops, cosine-similarity, dot-product, array, ai, similarity-search, 25.4]
description: "Store embeddings as ARRAY[FLOAT] in Vertica 25.4, benchmark COSINE_SIMILARITY, DOT_PRODUCT, VECTOR_L2, and VECTOR_MAGNITUDE, and build a Top-K vector similarity search while understanding the performance trade-offs without a vector index."
---

<!--more-->

---

## 1. The Short Version

Vertica 25.4 can store embeddings in `ARRAY[FLOAT]` and rank them with four built-in functions. **It works, but there is no vector index.** On this machine `CREATE INDEX` returns `ERROR 6200: Command CREATE INDEX is not supported`, so every Top-K query is a full scan plus a TopK heap.

Measured on 100,000 eight-dimensional vectors: a Top-10 cosine sort takes **172–235 ms**, or 1.7 µs per row. Adding a 5% tenant filter drops that to **27–30 ms**. Physically clustering the data by tenant (20 ROS containers, one tenant each) drops it to **21–23 ms**, and the number of rows scanned falls from 100,000 to 5,000.

This article is for two audiences:

1. People who want similarity search on an existing Vertica instance instead of standing up a separate vector database.
2. People already writing `ORDER BY cosine_similarity(...) LIMIT 10` who want to know how much it really scans and when it falls apart.

Every number below comes from a local 25.4.0-0 instance. The SQL is reproducible.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `v25.4.0-0` |
| Nodes | 1 (single node, Community Edition) |
| Database | test database `testdb` |
| Test schema | `article_lab_vectorops` (dropped with `DROP SCHEMA ... CASCADE` at the end) |
| Test data | 100,000 × 8 dims, 100,000 × 64 dims, 5,000 × 1,536 dims |
| Measured | 2026-11-05 |

Two environment details matter if you want to reproduce this:

- **`autocommit` is `off` on this connection** (`SHOW AUTOCOMMIT` returns `off`), so every DML statement below is followed by an explicit `COMMIT`. Without it the data disappears when the session ends — I hit that on the first run.
- `random()` returns a different value on every call, so you cannot compute the raw vector and the normalized vector in the same `SELECT` by calling it twice. Materialize the eight dimensions as scalar columns in a staging table first.

Eight dimensions is not a realistic embedding size; it just keeps a 100,000-row lab light. I measured a real-world dimensionality (1,536) separately on 5,000 rows — see section 6.

---

## 3. How It Works: Four Functions and One Storage Format

### 3.1 VectorOps is exactly four functions

There is no fifth vector function in 25.4:

| Function | Meaning | Arguments | Measured |
|----------|---------|-----------|----------|
| `COSINE_SIMILARITY(a, b)` | Cosine of the angle between two vectors; higher is closer | 2 equal-length arrays | `[1,0]` vs `[0,1]` → `0` |
| `DOT_PRODUCT(a, b)` | Inner product | 2 equal-length arrays | `[1,2]`·`[3,4]` → `11` |
| `VECTOR_L2(a, b)` | Euclidean distance; lower is closer | **2** equal-length arrays | `[3,4]` vs `[0,0]` → `5` |
| `VECTOR_MAGNITUDE(v)` | Vector length (L2 norm) | **1** array | `[3,4]` → `5` |

One name is misleading: **`VECTOR_L2` is the distance between two points, not the length of a vector.** I assumed it was the norm and passed one argument:

```
ERROR 3399: Failure in UDx RPC call InvokeSetup(): ... [vector_l2] ...
message: VectorDistance expects exactly 2 arguments, but got 1.
```

Use `VECTOR_MAGNITUDE` for length. And although `VECTOR_L2(a, b)` is conceptually `VECTOR_MAGNITUDE(a - b)`, Vertica **does not support array arithmetic**, so you cannot write `a - b`:

```
ERROR 4286: Operator does not exist: array[float] / numeric
```

You cannot even divide an array by a scalar. That single limitation dictates the ETL design: **normalization has to happen before load, using scalar columns.** There is no `v / |v|` in SQL.

### 3.2 The storage type is float8, not float4

A column declared `ARRAY[FLOAT]` shows up in `v_catalog.columns` like this:

```
 table_name | column_name |      data_type       | data_type_length
------------+-------------+----------------------+------------------
 docs       | emb         | array[float8](65000) |            65000
```

`FLOAT` in Vertica is an 8-byte double, so each dimension costs 8 bytes, not 4. Measured density across three tables:

| Table | Rows | Dims | Array columns | `used_bytes` | Per element |
|-------|-----:|-----:|--------------:|-------------:|------------:|
| `docs` | 100,000 | 8 | 2 | 12,174,735 | 7.6 B |
| `docs64` | 100,000 | 64 | 2 | 94,872,049 | 7.4 B |
| `docs1536` | 5,000 | 1,536 | 1 | 59,175,799 | 7.7 B |

7.4–7.7 bytes per element, slightly below the raw 8. The default encoding barely helps. **Do not expect meaningful compression on array columns.** A million 1,536-dimension vectors is 11.8 GB of vectors alone.

### 3.3 One vector caps at 65,000 bytes

The `65000` in `data_type_length` is a hard limit, not decoration. I walked up to it:

| Dimensions | Result |
|-----------:|--------|
| 5,000 | inserted |
| 8,125 | inserted |
| 8,126 | `ERROR 9227: Output array isn't big enough`<br>`DETAIL: Type limit is 65000 bytes, but value needs 65008 bytes` |

65,000 / 8 = 8,125, exactly. So `ARRAY[FLOAT]` holds up to 8,125 dimensions. OpenAI `text-embedding-3-large` at 3,072 and `ada-002` at 1,536 both fit.

A related trap: in `ARRAY[FLOAT](8)` the number is **bytes, not dimensions**. Declaring `(8)` to mean "8 dimensions" makes every valid insert fail:

```
ERROR 9227: Output array isn't big enough
DETAIL: Type limit is 8 bytes, but value needs 64 bytes
```

Eight dimensions requires `ARRAY[FLOAT](64)`. It is a genuinely counter-intuitive design; my advice is **do not declare a width at all** and validate `array_length()` in the ETL layer instead.

### 3.4 No index, so the plan looks like this

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

Three levels: **scan → score → keep the top 10**. No index scan, no ANN pre-filtering. `LIMIT 10` does not make it read one row less — the limit only sizes the TopK heap, not the scan.

Another clue: in `\df` these functions are classified as **User Defined Function**, and their errors read `Failure in UDx RPC call ...`. They are implemented through the UDx interface, which is why "172 ms to do 800,000 multiply-adds" is not a CPU number.

---

## 4. Step by Step

### 4.1 Create the table: store raw and normalized vectors separately

```sql
CREATE TABLE article_lab_vectorops.docs (
  id       int,
  tid      int,              -- tenant, used later for pre-filtering
  emb      ARRAY[FLOAT],     -- raw vector
  emb_unit ARRAY[FLOAT]      -- L2-normalized
);

INSERT INTO article_lab_vectorops.docs
SELECT id, tid,
       ARRAY[d1,d2,d3,d4,d5,d6,d7,d8]::ARRAY[FLOAT],
       ARRAY[d1/m,d2/m,d3/m,d4/m,d5/m,d6/m,d7/m,d8/m]::ARRAY[FLOAT]
FROM (SELECT *, sqrt(d1*d1+d2*d2+d3*d3+d4*d4+d5*d5+d6*d6+d7*d7+d8*d8) AS m
      FROM article_lab_vectorops.stage) s;
COMMIT;
```

100,000 rows in 525 ms. Note that `m` is computed from eight **columns** of the staging table, not from calling `random()` twice inside the `SELECT` — the latter would make `emb` and `emb_unit` disagree.

Verify the normalization:

```sql
=> SELECT id, round(VECTOR_MAGNITUDE(emb)::numeric,6) AS raw_mag,
              round(VECTOR_MAGNITUDE(emb_unit)::numeric,6) AS unit_mag
   FROM docs WHERE id = 1;
 id |      raw_mag      |      unit_mag
----+-------------------+-------------------
  1 | 1.617282000000000 | 1.000000000000000
```

### 4.2 The Top-K query

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

`Time: 172–235 ms` (warm runs of 235 and 172 ms; one cold run at 475 ms).

### 4.3 Normalization: the DOT_PRODUCT gap is not theoretical

Same data, same query vector, two rankings:

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

**Zero overlap.** The two Top-10 lists share nothing. The reason is in the data:

| Ranking | Avg. magnitude of the top 10 | Whole table |
|---------|-----------------------------:|------------:|
| `COSINE_SIMILARITY` | 1.5165 | 1.6109 |
| `DOT_PRODUCT` | **2.3799** | 1.6109 |

`DOT_PRODUCT` picks vectors averaging 2.38 in length against a table average of 1.61 — it rewards long vectors, not aligned ones. That is not noise; it is the wrong answer.

Normalize both sides and the two metrics become identical:

```sql
SELECT round(MAX(abs(COSINE_SIMILARITY(emb_unit, q_unit)
                   - DOT_PRODUCT(emb_unit, q_unit)))::numeric, 9) AS max_abs_diff
FROM docs;
  max_abs_diff
----------------
 0.000000000000
```

The maximum difference across 100,000 rows is 0 to nine decimal places, and Top-10 overlap is **10/10**.

The practical rule: **either use COSINE on raw vectors, or normalize and use DOT_PRODUCT. Never mix.** Mixing does not raise an error — it quietly returns wrong results, which is the worst kind of bug.

Speed-wise there is nothing to choose: cosine measured 177–217 ms, dot product 164–178 ms, same band. Normalization buys correctness, not performance.

### 4.4 Pre-filtering: a 6x difference, and the syntax is not the point

Multi-tenant search must filter before ranking. I tested three shapes on the same 100,000 rows, where `tid = 1` covers 5% (5,000 rows).

**Shape A: no filter** — 172–235 ms.
**Shape B: `WHERE tid = 1`** — 27–30 ms.
**Shape C: take the global Top-10, then filter (anti-pattern)** — 256 ms, and only **1 row survives**:

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

Shape C asks "which of the 10 most similar documents in the entire corpus belong to tenant 1", which is the wrong question, and it is slower as well. `WHERE` first, `ORDER BY` second.

**What does Shape B actually save?** `PROFILE` shows the scan operator's real row counts:

| Data layout | Scan `rows processed` | Scan `rows produced` | Query time |
|-------------|----------------------:|---------------------:|-----------:|
| Random, 1 container | 100,000 | 5,000 | 27–30 ms |
| Clustered by tenant, 20 containers | **5,000** | 5,000 | **21–23 ms** |

I built the second layout with 20 `INSERT ... WHERE tid = k` statements, one ROS container per tenant. Rows scanned fell from 100,000 to 5,000 — **zone maps skipped 19 containers based on per-container min/max**. No SQL syntax controls that; the physical layout does.

So the pre-filter benefit has two layers:

1. **SQL layer**: put `WHERE` before `ORDER BY` so filtered-out rows never reach the UDF. This layer is worth 6–8x.
2. **Physical layer**: keep one tenant's rows in a few containers so zone maps can skip whole batches. This layer only added 20–25% here (the table is 12 MB, so I/O was never the bottleneck), but the gap widens with data volume.

Without partitioning or sort order, `WHERE tid = 1` still reads all 100,000 rows to discard 95%. That matches general OLAP intuition; people just assume vector search magically avoids it.

### 4.5 Batch queries: LATERAL is unavailable

Comparing many query vectors at once is common (score 20 user questions in one pass). 25.4 does not support `LATERAL`:

```
ERROR 11320:  JOIN LATERAL does not support subqueries
```

(`CROSS JOIN LATERAL` is a plain syntax error.) The workable pattern is `CROSS JOIN` plus a window function:

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

Three queries against 100,000 rows — 300,000 similarity computations — took **341 ms**. Sequential execution (3 × ~172 ms = 516 ms) is slower because it scans three times. The plan shows the optimizer choosing `TopK[HEAP][PREPASS]` with `PartitionKeys: 1`, i.e. one heap per query.

---

## 5. Performance and Cost

### 5.1 Time: K barely matters, dimensionality matters

Top-K over 100,000 rows, warm:

| Query | Time |
|-------|-----:|
| 8 dims, K=10 | 172–235 ms |
| 8 dims, K=100 | 183 ms |
| 8 dims, K=1,000 | 185 ms |
| 8 dims, K=10,000 | 192 ms |
| 8 dims, `AVG(cosine)` with no sort | 322 ms |
| 64 dims, K=10 | 616–739 ms |
| 1,536 dims (5,000 rows), K=10 | 566–726 ms |

Two counter-intuitive results:

1. **Going from K=10 to K=10,000 costs 20 ms.** The heap is not the bottleneck; calling a UDF once per row is. "Fetch more and filter later" buys nothing.
2. **A plain `AVG()` with no sort is slower (322 ms) than Top-10.** The cost lives in scanning and UDF evaluation, not sorting. The first, cold query took 475 ms and then dropped to 172 ms — a 12 MB table fits entirely in cache. A star-schema-sized table will not be this lucky.

Per-row cost at 8 dims: 172 ms / 100,000 = **1.72 µs/row**. At 64 dims: 616 ms / 100,000 = 6.16 µs/row. Eight times the dimensions costs 3.6x the time, because the fixed per-row UDF overhead gets amortized.

Extrapolating 1.72 µs/row: 1 million rows is about 1.7 s, 10 million about 17 s. That is a single-node, single-stream order of magnitude; concurrency and extra projections will move it. **Fine for interactive search; not fine as a synchronous API backend.**

### 5.2 Storage: 7.5 bytes per dimension

| Table | `used_bytes` | MB | Per row | Per element |
|-------|-------------:|----:|--------:|------------:|
| `docs` (8 dims × 2 cols) | 12,174,735 | 11.6 | 121.7 B | 7.6 B |
| `docs64` (64 dims × 2 cols) | 94,872,049 | 90.5 | 948.7 B | 7.4 B |
| `docs1536` (1,536 dims × 1 col) | 59,175,799 | 56.4 | 11,835 B | 7.7 B |

90.5 MB for 100,000 64-dimension rows is reasonable, but remember it stores **the same data twice** (raw plus normalized). If you only need one metric, keep one column.

The 1,536-dimension table was loaded with `COPY` from a 66 MB TSV: 5,000 rows in 1,540 ms (about 3,250 rows/s). It is also where I hit `MaxParsedQuerySizeMB`. Building it with `INSERT ... ARRAY[1536 literals]` fails outright:

```
ERROR 8617: Request size too big
DETAIL: Total allocated memory (MiB) = 1024.000 is about to exceed limit
        imposed by MaxParsedQuerySizeMB (MiB) = 1024.000
```

Real workloads use `COPY` anyway, but if you plan to generate test data in SQL, that road is closed at 1,536 dimensions.

### 5.3 Query memory: where the query vector comes from changes it 83x

`PROFILE` prints the memory Vertica claims for the query (`NOTICE 5077`):

| Query | Claimed memory |
|-------|---------------:|
| `SELECT count(*) FROM docs` | 7,075 KB |
| 8-dim Top-10, query vector as a **literal** | 71,873 KB |
| 8-dim Top-1,000, literal | 135,310 KB |
| 8-dim Top-10, query vector from a **table** (join / CTE) | 6,079,897 KB |
| 1,536-dim (5,000 rows) Top-10, vector from a table | 6,078,780 KB |

Same query, same table. Moving the query vector from an inline literal to a join against a `queries` table raises the claim from 71 MB to **5.8 GB**, and both 5,000 rows and 100,000 rows report the same 5.8 GB — no visible scaling with row count.

I did not measure actual RSS, so I cannot claim it really consumed 5.8 GB. But that number feeds resource pool admission control, and on a busy `general` pool it can queue or reject the query. **Inline the literal when you can; if you need batch mode, verify the pool can absorb it before shipping.**

### 5.4 Statistics: not a cure, but collect them anyway

Before any statistics, `EXPLAIN` estimates 10K rows for a 100,000-row table — a 10x underestimate:

```
+-SELECT  LIMIT 10 [Cost: 625K, Rows: 10 (NO STATISTICS)] (PATH ID: 0)
| +---> SORT [TOPK] [Cost: 625K, Rows: 10K (NO STATISTICS)] (PATH ID: 1)
| | +---> STORAGE ACCESS for docs [Cost: 34K, Rows: 10K (NO STATISTICS)] (PATH ID: 2)
```

After `SELECT ANALYZE_STATISTICS('article_lab_vectorops.docs');` (21 ms):

```
+-SELECT  LIMIT 10 [Cost: 7M, Rows: 10 (ROW COUNT ONLY)] (PATH ID: 0)
| +---> SORT [TOPK] [Cost: 7M, Rows: 100K (ROW COUNT ONLY)] (PATH ID: 1)
| | +---> STORAGE ACCESS for docs [Cost: 318K, Rows: 100K (ROW COUNT ONLY)] (PATH ID: 2)
```

The estimate is now correct (100K) and the cost estimate jumps from 625K to 7M, while **runtime does not improve** (176 ms, still inside the 172–235 ms band). A single-table Top-K has no alternative plan shape, so statistics only move the cost numbers — but with multiple candidate plans (joins before the sort, for example), a 10x underestimate is enough to pick the wrong one.

Also, **array columns cannot have column statistics**:

```sql
=> SELECT ANALYZE_STATISTICS('article_lab_vectorops.docs', 'emb');
ERROR 9618:  Cannot analyze statistics of a collection column
DETAIL:  Column docs_super.emb is of array[float8](65000) type
```

So `EXPLAIN` will always say `ROW COUNT ONLY`. That is a type limitation, not a configuration problem.

---

## 6. Common Errors and Anti-Patterns

### Mistake 1: a NULL vector ranks first

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

`COSINE_SIMILARITY(NULL, v)` returns `NULL` (the function is strict), and Vertica's `ORDER BY ... DESC` treats NULL as the largest value. **One row with a missing embedding takes first place, permanently.** Batch mode is worse — it displaced the 5th result for all three queries I tested.

Fix it with `WHERE emb IS NOT NULL`, or `ORDER BY score DESC NULLS LAST`. The `WHERE` is better: it also avoids computing scores you will throw away.

### Mistake 2: assuming the number in `ARRAY[FLOAT](n)` is dimensions

`ARRAY[FLOAT](8)` is 8 bytes — one float8 — not 8 dimensions. Declare it and every legitimate insert fails:

```
ERROR 9227: Output array isn't big enough
DETAIL: Type limit is 8 bytes, but value needs 64 bytes
```

To constrain dimensions you have to convert to bytes yourself (8 dims → `(64)`), or leave it at the 65,000-byte default. As noted above, I recommend not declaring a width and validating `array_length()` in the ETL instead.

### Mistake 3: assuming arrays support arithmetic

```
=> SELECT ARRAY[1.0,2.0]::ARRAY[FLOAT] / 2.0;
ERROR 4286:  Operator does not exist: array[float] / numeric
```

There is no `array / scalar` and no `array + array`. Normalization must happen before load using scalar columns, or by spelling out `ARRAY[v1/m, v2/m, ...]`. "Normalize at query time" is not a thing in 25.4.

### Mistake 4: assuming arrays are 1-based

```
=> SELECT (ARRAY[10.0,20.0,30.0]::ARRAY[FLOAT])[0] AS i0,
          (ARRAY[10.0,20.0,30.0]::ARRAY[FLOAT])[1] AS i1;
 i0 | i1
----+----
 10 | 20
```

**0-based.** Indexing from 1 silently returns the second element. Also, `array_upper(arr)` on `ARRAY[FLOAT]` returns `function does not exist` in this version; use `array_length(arr)`.

### Mistake 5: reaching for `CREATE INDEX`

```
ERROR 6200:  Command CREATE INDEX is not supported
HINT:  Did you mean 'CREATE TEXT INDEX'?
```

25.4 has no B-tree and no ANN index. `CREATE TEXT INDEX` is full-text search, unrelated to vectors. Any expectation of "indexing the embedding column" is void in this release — the vendor KB says so explicitly (see references).

### Mistake 6: treating `VECTOR` as a type

```
=> CREATE TABLE t (v VECTOR(8));
ERROR 5108:  Type "catalog.VECTOR" does not exist
HINT:  Expected user-defined type
```

There is no native `VECTOR` type in 25.4, only `ARRAY[FLOAT]`. Plenty of 26.x examples online use `VECTOR`; pasting them here fails immediately.

---

## 7. Risks and Recommendations

### Do now (read-only / low risk)

1. **Measure before tuning.** Run `EXPLAIN` and confirm the plan is just "Scan → TopK". If it is, you have no index and the only lever is scanning fewer rows.
2. **Check for NULL embeddings:**
   ```sql
   SELECT COUNT(*) FROM your_table WHERE emb IS NULL;
   ```
   Anything above zero means your Top-K can return empty scores.
3. **Confirm the metric matches the vectors.** `COSINE_SIMILARITY` on raw vectors, or `DOT_PRODUCT` on normalized ones. The `max_abs_diff` query in 4.3 settles it in one run.
4. **Make sure the pre-filter is before `ORDER BY`**, and use `PROFILE` to check that the `Scan` operator's `rows processed` actually drops.
5. **Check dimensions against the 65,000-byte limit**: `SELECT MAX(array_length(emb)) FROM ...`. Above 8,125 you must split columns.

### Needs a change window / approval

1. **Reorder how data lands so tenants cluster.** Either load one tenant per batch, or use partitioning / sort order to make zone maps effective. This is the highest-return item in the article (scan 100,000 → 5,000 rows) but changes the load pipeline, so schedule it off-peak.
2. **Leave headroom for query memory.** Batch mode claims 5.8 GB. Run `PROFILE` in a test environment with the production dimensionality and row count before sizing the pool.
3. **Make Top-K asynchronous.** Multiply 1.72 µs/row by your row count to get the expected latency. Above roughly 2 seconds it does not belong on a synchronous API path.
4. **Consider two-stage retrieval.** If you only need to shrink the candidate set, cut the data with cheap scalar predicates (time range, category, tenant) first, then rank. That is the only acceleration verified to work in 25.4.

### Don't

- Don't raise parameters like `MaxParsedQuerySizeMB` to force huge `INSERT` statements; use `COPY`.
- Don't run `PROFILE` on production tables. Every `PROFILE` here ran in my own `article_lab_vectorops` schema; everything else used `EXPLAIN` plus `\timing`.
- Don't expect mergeout or projection rebuilds to speed up Top-K. The bottleneck is the per-row UDF call, not container fragmentation.

### Choosing the right tool

Turning the measurements into a latency budget makes the decision easier:

| Scenario | Extrapolated at 1.72 µs/row | Fit |
|----------|----------------------------:|-----|
| 100K rows, offline reports / nightly batch | ~0.2 s | Great, keep it in Vertica |
| 1M rows, interactive (1–3 s acceptable) | ~1.7 s | Workable, watch concurrency |
| 10M rows, synchronous API (< 200 ms) | ~17 s | Not viable; you need ANN or an external vector store |
| Semantic search needing recall guarantees | — | 25.4 is exact full scan only; no approximate knob |

Exact full scan is both the flaw and the feature: it is slow, but **recall is always 100%**. There is no `ef_search` or `nprobe` to tune, and no index lag producing stale results. Under ten million rows with relaxed latency (reports, batch recommendations, offline clustering), Vertica's advantage is not moving the data at all — vectors sit next to the fact tables, and joins and aggregations are plain SQL.

Conversely, if your use case is "type a query, get results instantly", or you are already at hundreds of millions of rows, VectorOps in 25.4 is a fallback, not a primary path.

---

## 8. Reproducibility

| Item | Value |
|------|-------|
| Version | `v25.4.0-0` |
| Nodes | 1 (CE) |
| `autocommit` | `off` (DML needs explicit `COMMIT`) |
| Data | 100,000 × 8 dims, 100,000 × 64 dims, 5,000 × 1,536 dims |
| Warm Top-10 | 172–235 ms (8 dims) |
| Cleanup | `DROP SCHEMA article_lab_vectorops CASCADE` executed; `v_catalog.schemata` shows nothing left |

Three notes:

1. All figures are single-node measurements. A multi-node cluster adds resegmentation and network transfer, so absolute values differ, but the "scan → TopK" plan shape and the relative benefit of pre-filtering do not.
2. The 8-dimension data is uniformly distributed `random()` output, unlike the clustered structure of real embeddings; score distributions will differ, but the scan and UDF cost structure does not.
3. I found no usable vector index in 25.4. If a later release adds one, the performance conclusions here need to be re-measured.

---

**References** (Vertica 25.4.x):

- Official documentation, Vector Functions: `COSINE_SIMILARITY`, `DOT_PRODUCT`, `VECTOR_L2`, `VECTOR_MAGNITUDE` — <https://docs.vertica.com/25.4.x/en/sql-reference/functions/vector-functions/>
- OpenText community KB, *VectorOps in Vertica — support for Indexing* (states that 25.4.x supports these four functions with INT/FLOAT/NUMERIC arrays, and that **indexing is not supported**) — <https://community.opentext.com/data-analytics/analytics-db/w/tips/52470/knowledge-document-vectorops-in-vertica--support-for-indexing>
- Official documentation, Collection (ARRAY) types and `array_length` / `array_upper` semantics

*All numbers come from a single-node Vertica 25.4.0-0 instance measured on 2026-11-05. The test schema has been dropped; every statement used only self-created objects and no existing table was modified.*
