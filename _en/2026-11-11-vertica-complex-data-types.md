---
layout: post_en
title: "Vertica Complex Data Types: ARRAY, SET, ROW, and JSON"
date: 2026-11-11 05:00:00 +0800
categories: vertica data-types sql
tags: [vertica, array, set, row, json, semi-structured, sql]
description: "Vertica 25.4 complex types are not PostgreSQL's: ARRAY is zero-based, SET cannot be a column type at all (ERROR 5108), MAP is limited to Parquet/Orc external tables (ERROR 10329), and there is no native JSON type. On 200,000 events and 600,000 tag pairs, arrays saved 59% of the space at low cardinality but the EXPLODE path made aggregation 7.0x slower (261.5 ms vs 37.2 ms)."
image: /assets/images/og/vertica-complex-data-types-en.png
faq:
  - q: "Is a Vertica ARRAY zero-based or one-based?"
    a: "Zero-based. `(ARRAY['a','b','c','d'])[0]` returns `a` and `[1]` returns `b`. Slices are end-exclusive, so `[2:3]` returns one element `[\"c\"]` and `[2:4]` returns `[\"c\",\"d\"]`. An out-of-range index returns NULL without an error (`[4]` comes back empty), but a negative index raises `ERROR 9225: Negative array index`."
  - q: "Can I use SET as a column type in Vertica?"
    a: "No. On 25.4.0-0, `CREATE TABLE t (s SET)` fails with `ERROR 5108: Type \"SET\" does not exist`. SET is only an expression constructor that sorts and de-duplicates (`SET['b','a','b','c']` returns `[\"a\",\"b\",\"c\"]`). The ordering is bytewise, so `SET['1','10','2','A','B','a','b']` returns `[\"1\",\"10\",\"2\",\"A\",\"B\",\"a\",\"b\"]`, and `'t02' IN (SET['t01','t02'])` returns false."
  - q: "Does Vertica have a JSON or MAP column type?"
    a: "Not on 25.4.0-0. There is no native JSON type (`ERROR 5108: Type \"JSON\" does not exist`), and MAP is restricted to Parquet/Orc external tables (`ERROR 10329: Only Parquet or Orc External Tables can use map type`). In practice you store JSON in LONG VARCHAR and read top-level keys with `MAPLOOKUP(MAPJSONEXTRACTOR(doc),'key')`; nested objects and their child keys return NULL in testing."
  - q: "Do arrays save space compared with a normalized child table?"
    a: "It depends entirely on element repetition. With 600,000 pairs drawn from 20 distinct tags, the array table took 1,106,697 bytes against 2,703,752 for the normalized table, 59% less (2.44x). With 600,000 unique tags the array table took 2,635,830 bytes against 2,743,239, only 3.9% less, and each value cost 13.7% more (4.06 vs 3.57 bytes)."
  - q: "Why is aggregation slower on ARRAY than on a normalized table?"
    a: "Because EXPLODE is a transform function, so the plan gains a UserDefined node costing 65K instead of 764 and cannot push work into storage. A tag histogram over the same 600,000 pairs took 261.5 ms with EXPLODE and 37.2 ms on the normalized table (7.0x). Rewriting it as element access joined to a numbers table brings it to 165.6 ms, still 4.5x slower than normalized."
---

<!--more-->

---

## 1. The Short Version

Vertica's "complex types" invite PostgreSQL instincts, and those instincts fail in three places at once: **index base**, **type existence**, and **nested parsing**. None of the three produces a compile error. Each one produces a plausible wrong answer.

On a single-node Vertica 25.4.0-0 instance I built 200,000 events holding 600,000 (event, tag) pairs and measured ARRAY, SET, ROW and JSON end to end. Eight findings:

1. **ARRAY is zero-based and slices are end-exclusive.** `[0]` is the first element, `[2:3]` returns one element, `[4]` out of range returns NULL, `[-1]` raises `ERROR 9225`. Not one of those four behaviours shows up as a syntax error.
2. **SET is not a column type.** `CREATE TABLE ... (s SET)` returns `ERROR 5108: Type "SET" does not exist`. It is only an expression constructor that sorts and de-duplicates, and `'t02' IN (SET['t01','t02'])` returns **false**.
3. **ROW is almost free.** At the same data volume a struct column costs 3.07 bytes/row against 3.06 for flat columns (0.34% apart), `EXPLAIN` cost is 665 for both, and the filter query runs 7.8 ms vs 7.9 ms. The price of ROW is governance, not performance.
4. **There is no native JSON type, and MAP is external-table only.** `Type "JSON" does not exist` (5108) and `Only Parquet or Orc External Tables can use map type` (10329). JSON lives in `LONG VARCHAR` and is read with `MAPLOOKUP(MAPJSONEXTRACTOR(doc), 'key')`, which sees **top-level keys only**: nested objects and their children all return NULL.
5. **Whether arrays save space depends on element repetition.** With 20 distinct tags the array table is 1,106,697 bytes against 2,703,752 normalized, **59% less (2.44x)**. With 600,000 unique tags it is 2,635,830 against 2,743,239, **3.9% apart**, and each array value costs 13.7% more.
6. **`contains()` is pushed down; `EXPLODE` is not.** Membership queries tie at 20.9 ms vs 20.4 ms, but a tag histogram costs **261.5 ms** with EXPLODE against **37.2 ms** normalized, 7.0x. The plan gains a `UserDefined: Explode` node and cost jumps from 764 to 65K.
7. **Writes are the array's clear win.** 200,000 array rows inserted in 79.9 ms; the same facts as 600,000 normalized rows took 1,103.7 ms, **13.8x**.
8. **JSON is the most expensive option.** 100,000 documents in `LONG VARCHAR` cost 21.23 bytes/row against 2.10 bytes/row for named columns (**10.1x**), and filtering one key took 740 ms against 5.4 ms (**138x**).

This is written for anyone deciding whether a column should be an array, a child table, or a JSON blob - and for anyone who already chose and now has slow queries. Every number comes from a real run, and the `article_lab_types` schema was dropped with `DROP SCHEMA ... CASCADE` at the end.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Nodes | 1 (Community Edition) |
| Database / user | `testdb` / `dbadmin` |
| Test schema | `article_lab_types` (dropped after the lab) |
| Event table | `events_arr`: 200,000 rows, 4 columns (`event_id`, `user_id`, `ts`, `tags ARRAY[VARCHAR]`) |
| Normalized table | `events_norm`: 600,000 rows, 4 columns (`event_id`, `user_id`, `ts`, `tag VARCHAR`) |
| Tag distribution | 1-5 per event, mean 3.0, drawn from 20 distinct values |
| High-cardinality pair | `events_arr_hc` / `events_norm_hc`: 3 unique tags per event, 600,000 distinct values |
| ROW pair | `t_flat` / `t_struct`: 200,000 rows, 4 fields (`uid`, `device`, `region`, `signup`) |
| JSON pair | `docs_json` / `docs_flat`: 100,000 rows, 6 keys |
| Measurement date | 2026-11-11 |

Three details that affect reproduction:

- **The data comes from `v_catalog.columns` x `v_catalog.types`**, so no external file is needed. This machine's `v_catalog.columns` has 5,005 rows, plenty for `LIMIT 200000` after the cross join.
- **This connection has `autocommit` off.** I issued an explicit `COMMIT` after every `CREATE` and `INSERT`, and all measurements ran inside a single `vsql -f` session.
- **Timings come from vsql's `\timing on`.** I report the "All rows formatted" figure and ran every query twice, listing both numbers so cache effects are visible rather than hidden.

---

## 3. How the Types Actually Behave

### 3.1 Three types, three rulebooks

What 25.4.0-0 actually supports is not the same as the documentation's "complex types" umbrella:

| Type | Usable as a column? | Constructor | Index / field | Measured note |
|------|---------------------|-------------|---------------|---------------|
| `ARRAY[T]` | Yes, including `ARRAY[ARRAY[INT]]` | `ARRAY[1,2,3]`, `ARRAY[]::ARRAY[INT]` | Zero-based, end-exclusive slices | Element types must match exactly; no implicit cast |
| `SET` | **No** (5108) | `SET['a','b']` | `array_length()` works | Sorts and de-duplicates; expression only |
| `ROW(...)` | Yes, named or holding an ARRAY | `ROW(1,'x')` | Named fields `r.a`; anonymous `f0`, `f1` | Comparable, orderable |
| `MAP` | **No** (10329, Parquet/Orc external tables only) | No `MAP{...}` syntax (4856) | - | Only produced by `MAPJSONEXTRACTOR` |
| `JSON` | **No** (5108) | - | - | Store as `LONG VARCHAR` and parse it yourself |

### 3.2 Index semantics: zero-based, end-exclusive, asymmetric out-of-range

This is the part that fails silently. Measured:

```
=> SELECT (ARRAY['a','b','c','d'])[0] AS i0, (ARRAY['a','b','c','d'])[1] AS i1,
          (ARRAY['a','b','c','d'])[2:3] AS slice_2_3,
          (ARRAY['a','b','c','d'])[4] AS i4_oob;
 i0 | i1 | slice_2_3 | i4_oob
----+----+-----------+--------
 a  | b  | ["c"]     |
```

Four behaviours in one query:

- `[0]` returns `a` and `[1]` returns `b`, so indexing is **zero-based**.
- `[2:3]` returns `["c"]` while `[2:4]` returns `["c","d"]`, so slices are **end-exclusive**.
- `[4]` on a 4-element array returns NULL, **not an error**: out of range means empty, not exception.
- `[-1]` raises `ERROR 9225: Negative array index` with `DETAIL: Requested position -1`. Only negatives are hard errors.

`array_find` is zero-based too: `array_find(ARRAY['a','b','c'],'c')` returns **2**, and a miss returns **-1**. So "not found" and "first position" never collide, but "third element" means index 2.

`array_dims` returns the number of dimensions, not the length: `1` for a one-dimensional array and `2` for `ARRAY[ARRAY[1,2],ARRAY[3,4]]`. Use `array_length` or `array_count` for length; they agree on ordinary arrays.

The PostgreSQL shortlist also does not apply. On 25.4.0-0, `\df array*` lists exactly: `array_cat`, `array_collation`, `array_contains`, `array_count`, `array_dims`, `array_eq`, `array_find`, `array_ge`, `array_gt`, `array_le`, `array_length`, `array_lt`, `array_neq`, `array_nullsafe` (plus a `pg_catalog.array_upper` that raises `ERROR 3457` when actually called). **Missing:** `array_append`, `array_prepend`, `array_position`, `array_remove`, `array_replace`, `array_to_string`, `array_fill`, `cardinality`, `unnest`, `array_agg` - each one raises `ERROR 3457: Function ... does not exist`. Concatenation is `array_cat` only, and it propagates NULL across the whole array:

```
=> SELECT array_cat(ARRAY[1,2], NULL) AS cat_null;
 cat_null
----------
```

That NULL keeps travelling: `WHERE contains(array_cat(tags, extra), 'x')` is never true as soon as `extra` is NULL.

### 3.3 SET is an expression, not a type

```
=> CREATE TABLE t_set (id INT, s SET);
ERROR 5108:  Type "SET" does not exist
HINT:  Expected user-defined type
```

All it can do is produce an already sorted, already de-duplicated list inside a query:

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

The ordering is **bytewise**, not natural and not case-insensitive: `"10"` sorts before `"2"`. Numbers behave normally - `SET[9,3,3,1]` returns `[1,3,9]`.

And one trap to avoid: SET does not work with `IN`.

```
=> SELECT ('t02' IN (SET['t01','t02'])) AS in_set;
 in_set
--------
 f
```

`t02` is plainly in there and the answer is `false`. Write `IN ('t01','t02')`, or use `contains(ARRAY['t01','t02'],'t02')`.

### 3.4 ROW: field numbering starts at f0

Named structs are read by field name; anonymous ones by `f0`, `f1`, and so on:

```
=> SELECT ROW(10,'x','2026-11-11') AS anon_row,
          (ROW(10,'x','2026-11-11')).f0 AS f0,
          (ROW(10,'x','2026-11-11')).f2 AS f2;
               anon_row               | f0 |     f2
--------------------------------------+----+------------
 {"f0":10,"f1":"x","f2":"2026-11-11"} | 10 | 2026-11-11
```

**`f1` is the second field, not the first.** Anyone arriving from PostgreSQL picks up the wrong column here at least once.

ROWs compare directly, field by field, lexicographically: `ROW(1,'x') = ROW(1,'x')` returns `t` and `ROW(1,'x') < ROW(2,'a')` returns `t`. That makes ROW usable as a sort key and makes `ORDER BY` on a ROW predictable.

### 3.5 The two JSON paths in 25.4

```
=> CREATE TABLE t_json (id INT, j JSON);
ERROR 5108:  Type "JSON" does not exist

=> CREATE TABLE t_map (id INT, m MAP<VARCHAR,INT>);
ERROR 10329:  Only Parquet or Orc External Tables can use map type
```

So there are exactly two paths: **external tables** (only Parquet/ORC carry a real MAP), or **`LONG VARCHAR` plus `MAPJSONEXTRACTOR`**. The latter chains two functions:

```
=> SELECT MAPLOOKUP(MAPJSONEXTRACTOR('{"a":1,"b":"x","c":true}'), 'a') AS va,
          MAPLOOKUP(MAPJSONEXTRACTOR('{"a":1,"b":"x","c":true}'), 'b') AS vb,
          MAPLOOKUP(MAPJSONEXTRACTOR('{"a":1,"b":"x","c":true}'), 'c') AS vc;
 va | vb | vc
----+----+----
 1  | x  | T
```

Values come back as `LONG VARCHAR`, so you cast them yourself. A missing key returns NULL - and so does a nested object:

```
=> SELECT MAPLOOKUP(MAPJSONEXTRACTOR('{"a":{"b":2}}'), 'a') AS nested,
          MAPLOOKUP(MAPJSONEXTRACTOR('{"a":{"b":2}}'), 'b') AS child;
 nested | child
--------+-------
        |
```

**No error, no warning, just NULL.** This is the most dangerous behaviour in this article: your ETL succeeds, the dashboard goes blank, and the log says nothing at all.

---

## 4. Step by Step

### 4.1 Building the data: 200,000 events, 600,000 pairs

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
         -- ...same pattern for 3, 4 and 5 elements
       END AS tags
FROM (SELECT ROW_NUMBER() OVER () AS i
      FROM v_catalog.columns CROSS JOIN v_catalog.types
      LIMIT 200000) g;
COMMIT;
```

Result: `200000` rows, `sum(array_length(tags)) = 600000`, `min 1 / max 5 / avg 3`.

The normalized table is built by element access - **not** by `INSERT ... SELECT ... FROM (SELECT EXPLODE(...))`, which hits `ERROR 2521` (see section 6):

```sql
INSERT INTO events_norm
SELECT e.event_id, e.user_id, e.ts, (e.tags)[k-1]::VARCHAR AS tag
FROM events_arr e
CROSS JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3
            UNION ALL SELECT 4 UNION ALL SELECT 5) n
WHERE k <= array_length(e.tags);
```

`600000` rows, exactly equivalent data.

### 4.2 Storage: 59% saved at low cardinality, a tie at high

```sql
SELECT anchor_table_name, sum(used_bytes), max(row_count),
       round(sum(used_bytes)::numeric / max(row_count), 2) AS bytes_per_row
FROM v_monitor.column_storage
WHERE anchor_table_schema = 'article_lab_types'
GROUP BY 1;
```

| Table | Total bytes | Rows | Bytes/row |
|-------|-------------|------|-----------|
| `events_arr` (20 tags) | 1,106,697 | 200,000 | 5.53 |
| `events_norm` (20 tags) | 2,703,752 | 600,000 | 4.51 |
| `events_arr_hc` (600K unique) | 2,635,830 | 200,000 | 13.18 |
| `events_norm_hc` (600K unique) | 2,743,239 | 600,000 | 4.57 |

![Same 600,000 (event, tag) pairs: arrays are 59% smaller at low cardinality and only 3.9% smaller at high cardinality]({{ '/assets/images/types-array-storage.svg' | relative_url }}){: loading="lazy" }

At column level it is clearer. At low cardinality the `tags` column costs **106,054 bytes** against **501,886** for the normalized `tag` column (4.73x), or 0.177 against 0.836 bytes per tag. At high cardinality `tags` costs 2,435,626 bytes and `tag` costs 2,142,694 - so the array stores **each value 13.7% more expensively** (4.06 vs 3.57 bytes).

The reason is not magic, it is LZO: a low-cardinality array block is highly repetitive and compresses extremely well, while unique values leave nothing to compress and the array still pays for element boundaries and length prefixes. **"Arrays save space" is not a rule, it is a special case that holds at low cardinality.**

### 4.3 Queries: `contains()` is pushed down, `EXPLODE` is not

Membership, where 30,000 events carry `t07`:

```sql
SELECT count(*) FROM events_arr WHERE contains(tags, 't07');                      -- 20.869 / 17.455 ms
SELECT count(*) FROM (SELECT event_id FROM events_norm
                      WHERE tag='t07' GROUP BY event_id) x;                        -- 20.391 / 16.937 ms
```

Effectively a tie, because `contains()` is pushed into the scan as a filter:

```
+-GROUPBY NOTHING [Cost: 32K, Rows: 1] (PATH ID: 1)
| +---> STORAGE ACCESS for events_arr [Cost: 32K, Rows: 10K] (PATH ID: 2)
| |      Projection: article_lab_types.events_arr_super
| |      Filter: contains(events_arr.tags, 't07')
```

"How often does each tag appear" is a different story:

```sql
-- Array: expand first
SELECT tag, count(*) FROM (SELECT EXPLODE(tags) OVER (PARTITION BY event_id)
                           AS (pos, tag) FROM events_arr) x GROUP BY tag;
-- 261.530 / 261.547 ms

-- Normalized: aggregate directly
SELECT tag, count(*) FROM events_norm GROUP BY tag;
-- 37.170 / 37.182 ms
```

The plan shapes explain it:

```
-- Array version
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 65K, Rows: 1] (PATH ID: 1)
| +---> ANALYTICAL [Cost: 65K, Rows: 10K] (PATH ID: 3)
| |      Functions: Explode()
| | +---> STORAGE ACCESS for events_arr [Cost: 33K, Rows: 10K] (PATH ID: 4)

-- Normalized version
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 764, Rows: 1] (PATH ID: 1)
| +---> STORAGE ACCESS for events_norm [Cost: 640, Rows: 10K] (PATH ID: 2)
```

![The ARRAY path gains a UserDefined node at cost 65K; the normalized path stays at 764]({{ '/assets/images/types-plan-shape.svg' | relative_url }}){: loading="lazy" }

`EXPLODE` is a transform function, so the plan carries a `UserDefined` node and rows must pass through it before aggregation. It cannot be folded into `ScanStep` the way `contains()` can. Cost 65K against 764, actual time 7.0x.

**If you are stuck with arrays, rewriting as element access recovers some of it** (same query, same answers - 40,000 per tag):

```sql
SELECT tag, count(*) FROM (
  SELECT (e.tags)[k-1]::VARCHAR AS tag
  FROM events_arr e
  CROSS JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3
              UNION ALL SELECT 4 UNION ALL SELECT 5) n
  WHERE k <= array_length(e.tags)
) x GROUP BY tag;
-- 165.571 / 161.289 ms, cost 128K
```

That is 1.58x faster than `EXPLODE`, but still 4.5x slower than the normalized table. **Arrays are a poor fit for per-element aggregation, and there is no cheap way around it.**

The other three read patterns, same data:

| Scenario | ARRAY | Normalized | Ratio |
|----------|-------|------------|-------|
| Full scan of 600,000 tags (`sum(array_length)` vs `count(*)`) | 18.440 ms | 3.593 ms | 5.1x |
| Tags for 1,000 events | 4.141 ms | 2.694 ms | 1.54x |
| Read back 20,000 events (20,000 rows vs 60,000) | 17.327 / 12.158 ms | 18.736 / 18.161 ms | 0.65x |

That last row is the one read pattern where arrays reliably win: **when you want the whole list rather than individual values**, arrays ship 3x fewer rows and the delivery cost drops with them.

### 4.4 Writes: 13.8x

Rebuilding the same facts once each (`INSERT ... SELECT`, single session):

| Target | Rows | Time |
|--------|------|------|
| `events_arr_w` (array) | 200,000 | 79.879 ms |
| `events_norm_w` (normalized) | 600,000 | 1,103.655 ms |

Writes are the array's most concrete advantage: one `INSERT` is one event, with no header/detail transaction pairing and no secondary-row maintenance. Put the other way: **every normalized row is write amplification**, and 13.8x is not noise.

### 4.5 ROW: a 0.34% premium

```sql
CREATE TABLE t_flat   (id INT, uid INT, device VARCHAR(16), region VARCHAR(8), signup DATE);
CREATE TABLE t_struct (id INT, r ROW(uid INT, device VARCHAR(16), region VARCHAR(8), signup DATE));
```

200,000 rows each, identical values:

| Table | Total bytes | Bytes/row |
|-------|-------------|-----------|
| `t_flat` | 612,263 | 3.06 |
| `t_struct` | 614,330 | 3.07 |

`column_storage` reports the struct fields as separate virtual columns (`uid`, `device`, `region`, `signup`); `r` itself accounts for **902 bytes** across 200,000 rows. Queries behave the same:

```
=> EXPLAIN SELECT count(*) FROM t_struct WHERE (r).region = 'r03';
| |      Filter: (t_struct.r.region = 'r03')
| |      Projection: article_lab_types.t_struct_super
-- Cost: 665
=> EXPLAIN SELECT count(*) FROM t_flat WHERE region = 'r03';
-- Cost: 665
```

Identical cost, 7.820 ms against 7.904 ms in practice, and 9.834 ms against 10.091 ms for a two-field predicate. Both `r.region` and `(r).region` work.

**ROW's cost is in governance, not storage or queries**: the field list is part of the type, so adding a field means `ALTER TABLE` on the type - effectively rewriting that column - instead of adding an independent column. And `SELECT *` emits the whole struct as a JSON string, which downstream parsers rarely enjoy.

### 4.6 JSON: 10.1x the space, 138x the query time

```sql
CREATE TABLE docs_json (doc_id INT, doc LONG VARCHAR);
-- 100,000 rows, 6 keys each, including one nested owner object
CREATE TABLE docs_flat (doc_id INT, status VARCHAR(8), region VARCHAR(8), score INT, device VARCHAR(8));
```

| Table | Total bytes | Bytes/row |
|-------|-------------|-----------|
| `docs_json` | 2,122,634 | 21.23 |
| `docs_flat` | 210,329 | 2.10 |

10.1x on storage (`doc` alone is 2,022,514 bytes). Queries diverge further:

```sql
SELECT count(*) FROM docs_json
WHERE MAPLOOKUP(MAPJSONEXTRACTOR(doc), 'status') = 'active';   -- 740.383 / 693.208 ms
SELECT count(*) FROM docs_flat WHERE status = 'active';        --   5.383 /   4.859 ms
```

**138x.** The plan does push the predicate into `ScanStep`, but what it pushes is a scalar expression that parses JSON once per row:

```
| |      Filter: (public.MapLookup(public.MapJSONExtractor(docs_json.doc), 'status') = 'active')
```

Pushdown is not the same as cheap. The difference from `contains()` is that `contains()` is a native array operation, while `MapLookup(MapJSONExtractor(...))` is a string parse per row - 100,000 parses for 100,000 rows.

Writes are the same story: `docs_json` 763.442 ms against `docs_flat` 116.734 ms (6.5x).

### 4.7 Nesting: ARRAY[ROW(...)] works, nested JSON does not

It is tempting to treat all "complex types" as one feature. Nesting support is where they split. An array of ROWs works, and fields are reachable all the way down:

```sql
CREATE TABLE t_arow (id INT, items ARRAY[ROW(sku VARCHAR, qty INT)]);
INSERT INTO t_arow VALUES (1, ARRAY[ROW('a',1), ROW('b',2)]);

SELECT id, (items)[0].sku AS first_sku, (items)[1].qty AS second_qty FROM t_arow;
 id | first_sku | second_qty
----+-----------+------------
  1 | a         |          2
```

`items` prints as `[{"sku":"a","qty":1},{"sku":"b","qty":2}]`, and indexing, slicing and field access all follow the rules in 3.2. The same is not true of JSON: `MAPLOOKUP(MAPJSONEXTRACTOR(...),'a')` returns NULL for a nested object and NULL for its children, because `MAPJSONEXTRACTOR` flattens one level only. **If you need structure, use ARRAY/ROW; if you need to keep raw JSON, stop expecting it to be queryable.**

---

## 5. Performance and Cost

Everything above, consolidated (same 600,000 pairs, single node):

| Metric | ARRAY | Normalized | Verdict |
|--------|-------|------------|---------|
| Total size (low cardinality) | 1,106,697 B | 2,703,752 B | Array wins 2.44x |
| Total size (high cardinality) | 2,635,830 B | 2,743,239 B | Tie (3.9%) |
| Cost per value (high cardinality) | 4.06 B | 3.57 B | Normalized wins 13.7% |
| Membership query | 20.9 ms | 20.4 ms | Tie |
| Per-element aggregation | 261.5 ms | 37.2 ms | Normalized wins 7.0x |
| Per-element aggregation (rewritten) | 165.6 ms | 37.2 ms | Normalized wins 4.5x |
| Full scan | 18.4 ms | 3.6 ms | Normalized wins 5.1x |
| Whole-list read-back | 12.2 ms | 18.2 ms | **Array wins 1.5x** |
| Write 600,000 values | 79.9 ms | 1,103.7 ms | **Array wins 13.8x** |
| EXPLAIN cost (aggregation) | 65K | 764 | 85x |

One sentence: **ARRAY is a write-heavy, read-whole-list type. The moment you aggregate, join, or sort individual elements, it moves the cost from load time to query time.**

An honest measurement caveat: neither table had `ANALYZE_STATISTICS` run on it, so every `Rows` in the `EXPLAIN` output is the `(NO STATISTICS)` default of 10K. The cost figures (65K / 764 / 128K) are therefore useful as *plan-shape* indicators, not as evidence about cardinality estimation. The 7.0x, 4.5x and 13.8x ratios come from real execution time and are independent of statistics. With statistics collected the cost gap will move with the real cardinality, but the extra `UserDefined` node will not go away.

---

## 6. Common Errors and Anti-patterns

All eight below were produced by this lab, not copied from documentation.

**1. Treating SET as a column type**

```
ERROR 5108:  Type "SET" does not exist
HINT:  Expected user-defined type
```

When you see 5108, stop checking your syntax and check whether the type exists in 25.4. `SET<VARCHAR>` is worse - it fails earlier with `ERROR 4856` at the `<`.

**2. Assuming a native JSON type**

```
ERROR 5108:  Type "JSON" does not exist
```

Neither `JSON` nor `JSONB` exists. Declaring `LONG VARCHAR` is the workable path.

**3. Trying to put MAP in an ordinary table**

```
ERROR 10329:  Only Parquet or Orc External Tables can use map type
```

The `MAP{...}` constructor does not exist either (`ERROR 4856`). MAP only arrives from `MAPJSONEXTRACTOR` or an external table.

**4. Using EXPLODE without OVER**

```
ERROR 5401:  User-defined transform function Explode must have an OVER clause
```

The correct form is `EXPLODE(tags) OVER (PARTITION BY event_id)`. Once that is in place there is another trap: it returns **two** columns (position, value), so you must supply two aliases.

```
ERROR 5386:  User defined transform will return 2 columns, whereas 1 aliases provided
```

The order is `(pos, value)` and positions **start at 0**:

```
=> SELECT pos, tag FROM (SELECT EXPLODE(ARRAY['a','b']) OVER (PARTITION BY 1) AS (pos, tag)) x;
 pos | tag
-----+-----
   0 | a
   1 | b
```

**5. Inserting the result of EXPLODE directly**

```
ERROR 2521:  Cannot specify anything other than user defined transforms and
             partitioning expressions in the SELECT list
```

`INSERT ... SELECT` rejects a UDTF wrapped in a subquery whose columns are then re-selected. Either put the transform in the outermost select list with matching columns, or use the element-access expansion from 4.1.

**6. Element types do not coerce**

```
ERROR 2631:  Column "a" is of type array[int] but expression is of type array[varchar]
HINT:  You will need to rewrite or cast the expression
```

`INSERT INTO t (a) VALUES (ARRAY['1','2'])` into an `ARRAY[INT]` column is wrong; cast per element or the whole array as `::ARRAY[INT]`. Declaring `ARRAY[VARCHAR(3)]` bites even more easily:

```
ERROR 4800:  String of 6 octets is too long for type Varchar(3)
```

**7. Using `contains()` to look for several values**

```
ERROR 9899:  The second argument of type [array[varchar(3), 2]] is not a scalar type
```

`contains(tags, ARRAY['t02','t09'])` has no "any of" semantics. Use `contains(tags,'t02') OR contains(tags,'t09')`, or expand the array first.

**8. Forgetting the difference between a NULL array and an empty one**

```
 id |  tags   | len | has_t07 | no_t07
----+---------+-----+---------+--------
  1 |         |     |         |
  2 | []      |   0 | f       | t
  3 | ["t07"] |   1 | t       | f
  4 | ["t01"] |   1 | f       | t
```

`WHERE NOT contains(tags,'t07')` returns **2 of the 4 rows**, not 3: the `tags IS NULL` row is filtered out by three-valued logic. To include it, write `WHERE tags IS NULL OR NOT contains(tags,'t07')`. Also note `array_length(NULL)` returns NULL while `array_length(ARRAY[]::ARRAY[VARCHAR])` returns 0 - do not substitute `= 0` for `IS NULL`.

---

## 7. Risks and Recommendations

### Do now (read-only, low risk)

- **Inventory the types in use**: `SELECT table_schema, table_name, column_name, data_type FROM v_catalog.columns WHERE data_type ILIKE 'array%' OR data_type ILIKE 'set%' OR data_type ILIKE 'row%';` Know how many tables and columns are involved before proposing anything.
- **Measure the real footprint**: sum `used_bytes` by `anchor_table_name` in `v_monitor.column_storage` and divide by `row_count`. Putting the old and new designs side by side beats reading documentation.
- **Find queries that aggregate array elements**: search `v_monitor.query_events` or `dc_queries` for `EXPLODE` and `array_length`. Those are the statements an array will drag down.
- **Audit JSON for nested lookups**: any `MAPLOOKUP(MAPJSONEXTRACTOR(...), 'key')` whose key points at a nested object always returns NULL. Such queries never fail - they just return nothing. Add a `count(*)` reconciliation.

### Needs change control

- **Normalize the array columns that are aggregated per element**: the 7.0x gap measured here justifies a schema change. Keep the `event_id` relationship to the parent table.
- **Land the hot JSON keys**: promote the 3-5 most-filtered keys to named columns (10.1x storage and 138x query gap here) and keep the raw document as a backup column rather than a query surface.
- **Avoid frequent type changes on `ROW` columns**: adding a field means `ALTER TABLE` on the type, rewriting that column. Fine at 200,000 rows; schedule a window at hundreds of millions.
- **Prefer arrays on the write path**: if the application writes one event at a time and later reads the whole list back, the 13.8x write gap and 1.5x read-back advantage show up directly in ETL time.

---

## 8. References

- Vertica 25.4 documentation: Complex Types - <https://docs.vertica.com/25.4.x/en/sql-reference/data-types/complex-types/>
- Vertica 25.4 documentation: Array Functions - <https://docs.vertica.com/25.4.x/en/sql-reference/functions/array-functions/>
- Vertica 25.4 documentation: JSON Functions (MapJSONExtractor, MapLookup) - <https://docs.vertica.com/25.4.x/en/sql-reference/functions/json-functions/>
- Vertica 25.4 documentation: Map Functions - <https://docs.vertica.com/25.4.x/en/sql-reference/functions/map-functions/>
