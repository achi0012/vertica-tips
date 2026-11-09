---
layout: post_en
title: "Vertica Geospatial Analysis: Functions and Spatial Indexes in Practice"
date: 2026-11-10 05:00:00 +0800
categories: vertica geospatial analytics
tags: [vertica, geospatial, st-contains, st-distance, spatial-index, wkt, geojson]
description: "In Vertica, GEOMETRY works in planar units and GEOGRAPHY in metres, and the same two points can differ by 10%. On 200,000 points against 8 district polygons the brute-force join took 7,303-8,661 ms while the spatial index took 827.6 ms - but 1,602 boundary points belong to no polygon at all, and the index is a snapshot that still answered 31,251 after a polygon changed."
image: /assets/images/og/vertica-geospatial-analysis-en.png
faq:
  - q: "Does ST_Distance return metres or degrees in Vertica?"
    a: "It depends on the type. GEOMETRY is planar and returns coordinate units, so with longitude and latitude it returns degrees; GEOGRAPHY is spherical and returns metres. The same two points (Taipei 101 to Banqiao) return 0.108083 for GEOMETRY and 10,926.8 for GEOGRAPHY."
  - q: "Does ST_Contains include points that sit exactly on the boundary?"
    a: "No. On a unit square and on 200,000 test points, boundary points return false for ST_Contains, true for ST_Intersects, and false again for ST_Within. 1,602 of the 200,000 points landed exactly on grid lines, so the eight districts summed to 198,398 rows under ST_Contains but 201,201 under ST_Intersects, where 1,201 points belong to two districts at once."
  - q: "Can a spatial index be built on point data?"
    a: "No. On 25.4.0-0 an index over 200,000 POINT values fails outright: There are no valid polygons to index. NOTICE: Some geometries ignored [Non (MULTI)POLYGON: 200000]. Spatial indexes only cover (MULTI)POLYGON."
  - q: "Does the spatial index update itself when the data changes?"
    a: "No, it is a snapshot. After halving one district polygon the index still returned 31,251 rows while the true answer was 15,499; only STV_Refresh_Index moved it to 15,626, and the extra 127 rows are boundary points because the index uses intersects semantics."
  - q: "How much faster is the spatial index than a brute-force join?"
    a: "On the same 8 polygons and 200,000 points: brute-force ST_Intersects took 7,303.5 ms and ST_Contains with GROUP BY took 8,660.7 ms, while the index transform took 827.6 ms (8.8x). The index output must be materialised (CTAS 1,994.0 ms) or aggregated, and aggregating the materialised table takes 15.0 ms."
---

<!--more-->

---

## 1. The Short Version

Vertica's geospatial support is not just a handful of extra functions. It is a subsystem with its own coordinate systems, its own boundary conventions, and its own maintenance cycle. The first things people get wrong are not syntax but **units** and **boundaries** - and neither produces an error, only a plausible wrong answer.

I took a single-node Vertica 25.4.0-0 instance, built 8 simplified district polygons and 200,000 points, and measured `ST_Contains`, `ST_Intersects`, `ST_Distance` and `STV_Create_Index` end to end. Six findings:

1. **`ST_Distance` returns whatever unit the type implies.** Same two points (Taipei 101 to Banqiao): `GEOMETRY` returns **0.108083** (degrees), `GEOGRAPHY` returns **10,926.8** (metres). There is no `ST_Distance(..., 'meters')`; pick the wrong type and you get a number 100,000 times too small without any warning.
2. **Points on a boundary belong to no polygon.** `ST_Contains` returns `false` for a point on the edge, `ST_Intersects` returns `true`, and `ST_Within` returns `false`. **1,602** of the 200,000 points sit exactly on grid lines, so the eight districts sum to **198,398** rows under `ST_Contains` but **201,201** under `ST_Intersects`, where **1,201 points belong to two districts at once**.
3. **A spatial index only indexes polygons.** Building one over 200,000 `POINT` values fails: `There are no valid polygons to index. NOTICE: Some geometries ignored [Non (MULTI)POLYGON: 200000]`. The index answers "which polygons contain this point", not the reverse.
4. **The index works, but the margin is smaller than you would guess.** Brute-force `ST_Intersects` join **7,303.5 ms**, `ST_Contains` with `GROUP BY` **8,660.7 ms**, index transform **827.6 ms** (8.8x). But the transform only emits `(pt_gid, pol_gid)`; you must land it (CTAS **1,994.0 ms**) or aggregate it, and aggregating the landed table takes **15.0 ms**.
5. **The index is a snapshot, not a trigger.** Halve a polygon without refreshing and the index still returns **31,251** while the truth is **15,499**. After `STV_Refresh_Index` it returns **15,626** - the extra 127 rows are boundary points, because the index uses intersects semantics.
6. **A bounding-box prefilter helps - in the right place.** A constant bbox plus `ST_Contains`: **450.4 ms** (16x faster). Put `ST_XMin(d.geom)` inside the join condition: **14,842.7 ms**, twice as slow as doing nothing.

One number that has nothing to do with speed but a lot to do with budget: **83.5 bytes per point row** (`STV_MemSize` reports 85 bytes for a single point), against 16 bytes for two `float8` columns. At hundreds of millions of points, that 5x gap is the disk bill.

This is for anyone who already has longitude and latitude columns and is about to build "which district is this store in", "nearest warehouse", or "delivery radius" queries. Every number comes from a real run, and the `article_lab_geo` schema was dropped with `DROP SCHEMA ... CASCADE` at the end.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Vertica version | `Vertica Analytic Database v25.4.0-0` |
| Nodes | 1 (Community Edition) |
| Database / user | `testdb` / `dbadmin` |
| Lab schema | `article_lab_geo` (dropped after the lab) |
| Polygon table | `districts`: 8 rows, 4 columns (4 longitude cells x 2 latitude cells) |
| One polygon | 0.05° longitude x 0.10° latitude, `ST_Area` = 0.005 deg² |
| Point table | `poi`: 200,000 rows, 5 columns (`poi_id`, `lon`, `lat`, `geom`, `geog`) |
| Point extent | longitude 121.45-121.6496, latitude 24.95-25.1099992 |
| Spatial index | `districts_idx` on `districts.geom` |
| Measured on | 2026-11-10 |

Three details that affect reproducibility:

- **Longitude is 500 evenly spaced values (0.0004°), latitude is 200,000 very fine values (0.0000008°).** The generator said `(i-1)/500`, intending to step once per 500 rows, but `i` being an integer did not make the division integer - it became numeric, so latitude turned into a continuous fine grid. That accident turned out to be a good boundary test: four longitude lines land exactly on 121.45 / 121.50 / 121.55 / 121.60 (because 125 x 0.0004 = 0.05 is exact in float8), while only two latitude values land exactly on 24.95 and 25.05 (because 0.1 ÷ 0.0000008 = 125,000 divides evenly).
- **Keep `public` in `SET SEARCH_PATH`.** Every geospatial UDF is registered in `public` (visible in `v_catalog.user_functions`). Setting `SET SEARCH_PATH TO article_lab_geo` alone makes `ST_GeomFromText` vanish with the misleading error `Function ST_GeomFromText(unknown) does not exist`.
- **This connection runs with `autocommit` off.** I committed explicitly after `CREATE` and `INSERT`, and every measurement ran inside a single `vsql -f` session.

---

## 3. How It Works

### 3.1 GEOMETRY and GEOGRAPHY: same function names, two unit systems

Vertica has two spatial types: `GEOMETRY` works in planar (Cartesian) coordinates, `GEOGRAPHY` works on the WGS84 spheroid. The function names are almost identical, the semantics are not:

| Behaviour | `GEOMETRY` | `GEOGRAPHY` |
|-----------|-----------|-------------|
| Coordinate meaning | planar x/y | longitude/latitude in degrees |
| `ST_Distance` unit | coordinate units (degrees) | metres |
| `ST_Area` unit | square degrees | square metres |
| `ST_Buffer` radius | degrees | unsupported in 25.4.0-0 (`ERROR 3457`) |
| Constructors | `ST_GeomFromText(wkt [, srid])`, `STV_GeometryPoint(x, y)` | `ST_GeographyFromText(wkt)`, `STV_GeographyPoint(x, y)` |
| Good for | floor plans, already-projected coordinates | distances and areas on the globe |

Measured on the same pair of coordinates:

```
=> SELECT ST_Distance(ST_GeomFromText('POINT(121.5654 25.0330)'),
                      ST_GeomFromText('POINT(121.4590 25.0140)')) AS dist_deg,
          ST_Distance(ST_GeographyFromText('POINT(121.5654 25.0330)'),
                      ST_GeographyFromText('POINT(121.4590 25.0140)')) AS dist_m;
     dist_deg      |      dist_m
-------------------+------------------
 0.108083116165286 | 10926.8093268334
```

Area diverges even more: the same 0.05° x 0.10° polygon gives `ST_Area` = `0.006` (square degrees) as `GEOMETRY` and `67,218,996.59` (square metres) as `GEOGRAPHY`. Read the first as square kilometres and you get a number that is absurdly small.

There is a practical decision here: **if all you need is "what contains what", `GEOMETRY` is enough and much faster** (4.56 us per pair, measured below). You only need `GEOGRAPHY` when you must print metres or square kilometres, or when you run radius queries.

### 3.2 Boundary semantics: three functions, three answers

The OGC definition requires `ST_Contains` to have an interior intersection and no part of the second geometry on the boundary. Vertica implements exactly that, as a unit square (0-10) probe shows:

```
=> WITH poly AS (SELECT ST_GeomFromText('POLYGON((0 0,10 0,10 10,0 10,0 0))') AS g)
   SELECT ST_AsText(pt) AS point_wkt,
          ST_Contains(g, pt) AS contains, ST_Intersects(g, pt) AS intersects,
          ST_Within(pt, g) AS within
   FROM poly, (SELECT ST_GeomFromText('POINT(5 5)') AS pt
               UNION ALL SELECT ST_GeomFromText('POINT(0 5)')
               UNION ALL SELECT ST_GeomFromText('POINT(10 10)')
               UNION ALL SELECT ST_GeomFromText('POINT(10.0001 5)')) pts;
   point_wkt       | contains | intersects | within
-------------------+----------+------------+--------
 POINT (5 5)       | t        | t          | t
 POINT (0 5)       | f        | t          | f
 POINT (10 10)     | f        | t          | f
 POINT (10.0001 5) | f        | f          | f
```

The rule: **`ST_Contains` means strictly inside, `ST_Intersects` includes the boundary, and `ST_Within` is `ST_Contains` written the other way round, equally boundary-exclusive.**

This hurts in practice because administrative boundaries are shared. A coordinate that lands on the line between two districts belongs to neither under `ST_Contains`. That is not a bug; it is the definition.

### 3.3 What a spatial index actually is: a grid, not a B-tree

`STV_Create_Index(table_key, geom USING PARAMETERS index='name')` cuts the polygon set into grid cells and records which polygons cover each cell. At query time it looks up the cell for a point, narrows the candidate set, then does the exact predicate.

Three properties matter:

1. **Only `(MULTI)POLYGON` can be indexed.** Points and lines cannot.
2. **The index is a separate snapshot object.** It does not follow `UPDATE`/`INSERT` inside a transaction; you refresh it yourself.
3. **Queries go through `STV_Intersect`, not `ST_Contains`.** `STV_Intersect` has intersects semantics (boundary included) and returns index keys, not geometry.

`STV_Intersect` is a UDTF that **returns two columns** (`pt_gid`, `pol_gid`), so `SELECT poi_id, STV_Intersect(...) AS x` fails with `ERROR 5386: User defined transform will return 2 columns, whereas 1 aliases provided`, and mixing other columns in the select list fails with `ERROR 2521`. The standard pattern lands it in a table:

```sql
CREATE TABLE geo_hits AS
SELECT STV_Intersect(poi_id, geom USING PARAMETERS index='districts_idx') OVER (PARTITION BY poi_id)
FROM poi;
```

That is the key mental shift: **the index is not a way to make `ST_Contains` faster. It is a tool that produces a mapping table.** Your query still has to join to it.

---

## 4. Step by Step

### 4.1 Schema and polygons: the first trap is INSERT

```sql
DROP SCHEMA IF EXISTS article_lab_geo CASCADE;
CREATE SCHEMA article_lab_geo;
SET SEARCH_PATH TO article_lab_geo, public;   -- public is not optional

CREATE TABLE districts (
  district_id   INT,
  district_name VARCHAR(16),
  wkt           VARCHAR(200),
  geom          GEOMETRY
);
```

Trying to save time with one multi-row insert hits this:

```
=> INSERT INTO districts (district_id, district_name, geom) VALUES
   (1,'Zhongzheng', ST_GeomFromText('POLYGON((...))')),
   (2,'Da-an',      ST_GeomFromText('POLYGON((...))'));
ERROR 10645:  Cannot use multi-row insert: column 'geom' has unsupported type 'geometry'
HINT:  Try inserting one row at a time
```

**`geometry` does not accept multi-row `VALUES`.** Either one `INSERT` per row, or store WKT first and `UPDATE ... SET geom = ST_GeomFromText(wkt)` (I chose the latter: all 8 rows in one statement). This is a UDx type limitation, not a version bug.

### 4.2 Generating 200,000 points

`v_catalog.columns` (4,911 rows) crossed with `v_catalog.types` (90 rows) gives the numbers, no external file needed:

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

Two type details:

- `STV_GeometryPoint` accepts only `FLOAT`; pass numeric and you get `Function STV_GeometryPoint(numeric, numeric) does not exist`, so cast with `::FLOAT`.
- **There is no `ST_GeographyPoint`** (`ERROR 3457`); spherical points come from `STV_GeographyPoint`. Likewise `ST_GeogFromText` does not exist - use `ST_GeographyFromText`.

Loading 200,000 rows took **1,883.7 ms**.

### 4.3 Brute force: 8.6 seconds and 198,398 rows

```sql
SELECT d.district_name, count(*) AS poi_cnt
FROM poi p JOIN districts d ON ST_Contains(d.geom, p.geom)
GROUP BY d.district_name ORDER BY poi_cnt DESC;
```

```
 district_name | poi_cnt
---------------+---------
 Zhongzheng    |   31000
 Da-an         |   31000
 Xinyi         |   31000
 Neihu         |   30999
 Shilin        |   18600
 Beitou        |   18600
 Zhongshan     |   18600
 Songshan      |   18599
Time: First fetch (8 rows): 8660.659 ms
```

The plan turns `ST_Contains` into a join filter over two full scans:

```
+-GROUPBY HASH (LOCAL RESEGMENT GROUPS)
 | +---> JOIN HASH
 | |      Join Filter: public.ST_Contains(d.geom, p.geom)
 | | +-- Outer -> STORAGE ACCESS for d
 | | +-- Inner -> STORAGE ACCESS for p
```

In other words: **8 polygons x 200,000 points = 1,600,000 UDF calls**, or **4.56 us per pair**. The optimizer cannot hoist a bbox condition out of an opaque UDF - which is precisely why you need the spatial index or a manual bbox.

### 4.4 Boundary points: 1,602 missing rows

The eight districts sum to 198,398, which is 1,602 short of 200,000. A `LEFT JOIN ... WHERE d.district_id IS NULL` finds them:

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

**1,600 points sit exactly on the four vertical edges** (400 each), and 2 more sit exactly on the latitude lines 24.95 / 25.05. `ST_Contains` gives those points to nobody, while `ST_Intersects` gives each of them to both neighbours:

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

**1,201 points belong to two districts** (0.6%). Sum population per district with `ST_Intersects` and you double count 1,201 rows; with `ST_Contains` you lose 1,602. **Both are wrong, in opposite directions.** The fix is to define an assignment rule (nearest centroid, for example) or to accept dropped boundary points and document it in the report definition.

![A 4 by 2 grid of districts showing ST_Contains point counts and the 1,600 points that sit on the dashed grid lines and are excluded]({{ '/assets/images/geospatial-boundary-rings.svg' | relative_url }}){: loading="lazy" }

One related limitation: writing "belongs to no district" as `WHERE NOT EXISTS (SELECT 1 FROM districts d WHERE ST_Contains(...))` fails on 25.4.0-0:

```
ERROR 2784:  Correlated subquery could not be flattened as a join
```

`ST_Contains` prevents the optimizer from rewriting the correlated subquery as a join. Use `LEFT JOIN ... IS NULL` instead; it costs **7,489.8 ms**.

### 4.5 Creating the index: the `OVER` clause is mandatory

```sql
SELECT STV_Create_Index(district_id, geom USING PARAMETERS index='districts_idx', overwrite=true)
OVER () FROM districts;
```

```
   type   | polygons | SRID | min_x  | min_y | max_x  | max_y | info
----------+----------+------+--------+-------+--------+-------+------
 GEOMETRY |        8 |    0 | 121.45 | 24.95 | 121.65 | 25.15 |
```

Four syntax points I hit for real:

- Without `OVER`: `ERROR 5401: User-defined transform function STV_Create_Index must have an OVER clause`.
- With `OVER (PARTITION BEST)`: `ERROR 6379: PARTITION BEST can only be used with single-phase user defined transform functions`. On one node, `OVER ()` is what you want.
- With `OVER (PARTITION BY district_id)`: `The OVER() clause cannot contain any column`.
- `STV_Describe_Index` must stand alone in the select list: `SELECT * FROM STV_Describe_Index() OVER ()` gives `ERROR 5383`, and on 25.4.0-0 it returns a single column (the index name) with no cell or memory statistics.

### 4.6 Querying through the index: 827.6 ms

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

Against **7,303.5 ms** for brute-force `ST_Intersects` and **8,660.7 ms** for `ST_Contains` with `GROUP BY`, that is **8.8x**. Per point: brute force compares each point against all 8 polygons (7,303.5 ms ÷ 200,000 = **36.5 us/point**), the index transform costs **4.14 us/point**. **What the index saves is candidate polygon comparison, not scanning** - with only 8 polygons the candidate set was small to begin with, so the ceiling is low. The gap widens when you have tens of thousands of polygons.

The landed result looks like this:

```
 pt_gid | pol_gid
--------+---------
      1 |       4
      2 |       3
      3 |       3
```

`pt_gid` is the first argument you passed (`poi_id`), `pol_gid` is the index key (`district_id`). Landing it as `geo_hits`: **1,994.0 ms**, 201,201 rows, **402,728 bytes** (2.0 bytes per row - two integers compress very well). Aggregating afterwards:

```
SELECT d.district_name, count(*) FROM geo_hits h
JOIN districts d ON d.district_id = h.pol_gid GROUP BY 1;
Time: First fetch (8 rows): 15.045 ms
```

**15 ms against 8,660 ms.** That is the index's real working model: **compute the mapping once offline, serve relational aggregations online.** If your point table grows by millions of rows a day, that pattern beats recomputing the predicate every time.

### 4.7 The index is a snapshot: 31,251 versus 15,499

Shrink the Neihu polygon from 0.05° x 0.10° to 0.05° x 0.05°, without refreshing:

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

**The index says 31,251; the truth is 15,499** - off by a factor of two, with no error and no warning. After refreshing:

```
=> SELECT STV_Refresh_Index(district_id, geom USING PARAMETERS index='districts_idx') OVER () FROM districts;
 type     | polygons | indexed | appended | updated | deleted
----------+----------+---------+----------+---------+---------
 GEOMETRY |        8 |       1 |        0 |       1 |       0
Time: 71.463 ms

=> SELECT count(*) FROM geo_hits2 WHERE pol_gid = 4;   -- re-running STV_Intersect into a new table
 15626
```

**15,626, which is 127 more than `ST_Contains`'s 15,499.** Again the boundary: the index uses intersects semantics, so perimeter points get counted. Those 127 rows are the perimeter of the shrunk polygon, not a broken index.

Practical advice: **make `STV_Refresh_Index` the last step of the ETL**, after `COMMIT` and before serving queries. Boundaries that change once a month (administrative districts, trade areas) are easy; delivery zones that change daily mean the maintenance cost has to be part of the design.

### 4.8 Distance and area: degrees, metres, projections

```sql
-- Taipei 101 -> Banqiao
SELECT ST_Distance(ST_GeomFromText('POINT(121.5654 25.0330)'),
                   ST_GeomFromText('POINT(121.4590 25.0140)'));
-- 0.108083116165286  (degrees)

SELECT round(ST_Distance(ST_Transform(ST_GeomFromText('POINT(121.5654 25.0330)', 4326), 3857),
                         ST_Transform(ST_GeomFromText('POINT(121.4590 25.0140)', 4326), 3857))::NUMERIC, 1);
-- 12072.2   (metres, Web Mercator)

SELECT round(ST_Distance(ST_GeographyFromText('POINT(121.5654 25.0330)'),
                         ST_GeographyFromText('POINT(121.4590 25.0140)'))::NUMERIC, 1);
-- 10926.8   (metres, spherical)
```

Three different numbers: **10,926.8 / 12,072.2 / 0.108083**. Web Mercator inflates distance away from the equator, and at 25°N the error is already **+10.5%**; use it for delivery distances and you overstate cost by a tenth.

`ST_Transform` has a mandatory first step: geometry without an SRID cannot be projected.

```
=> SELECT ST_Transform(ST_GeomFromText('POINT(121.5654 25.0330)'), 3857);
ERROR 5861:  Error calling processBlock() in User Function ST_Transform ...:
             Geometry has undefined SRID [0]
```

Use `ST_GeomFromText(wkt, 4326)` to declare the source SRID (`ST_SRID` then returns 4326).

### 4.9 Radius queries and nearest neighbours

"Find points within 500 metres" has two spellings that differ by 11.7%:

```sql
-- planar: 0.005 degrees
SELECT count(*) FROM poi
WHERE STV_DWithin(geom, STV_GeometryPoint(121.5654::FLOAT, 25.0330::FLOAT), 0.005::FLOAT);
-- 487, 1,023.7 ms

-- spherical: 500 metres
SELECT count(*) FROM poi
WHERE STV_DWithin(geog, STV_GeographyPoint(121.5654::FLOAT, 25.0330::FLOAT), 500::FLOAT);
-- 436, 1,116.7 ms
```

At Taipei's latitude, 0.005° is 556 m north-south but 505 m east-west, so "a 0.005 degree circle" is an ellipse that sweeps in 51 extra points (**487 vs 436, +11.7%**).

Nearest neighbours:

```sql
-- STV_NN: full scan, but no sort of the whole table
SELECT ST_AsText(NN) AS nearest_geom, round(dist::NUMERIC, 6)
FROM (SELECT STV_NN(geom, STV_GeometryPoint(121.5654::FLOAT, 25.0330::FLOAT), 3) OVER () FROM poi) x;
-- POINT (121.5656 25.032984) | 0.000201
-- POINT (121.5652 25.0328408)| 0.000256
-- POINT (121.5652 25.0332408)| 0.000313
Time: 613.197 ms

-- sort it yourself
SELECT poi_id, round(ST_Distance(geog, STV_GeographyPoint(121.5654::FLOAT, 25.0330::FLOAT))::NUMERIC, 1)
FROM poi ORDER BY 2 LIMIT 5;
-- 20.2 / 26.8 / 33.5 / 47.2 / 50.5 metres
Time: 1,432.421 ms
```

Both point the same way (nearest neighbours in the 20-22 m range, with the planar-versus-spherical conversion accounting for roughly 10%, exactly the unit problem from 3.1), but `STV_NN` is **2.3x faster** because it uses an internal distance ordering instead of computing and sorting 200,000 distances.

Note that `STV_NN` returns distance in **coordinate units (degrees)** and does not use the spatial index at all (an index cannot be built on points), so it is a full scan every time.

### 4.10 GeoHash: another prefilter

```sql
SELECT ST_GeoHash(STV_GeometryPoint(121.5654::FLOAT, 25.0330::FLOAT)) AS gh;
-- wsqqqm15z1kjt9f9yprs (20 characters)

SELECT count(*) FROM poi
WHERE LEFT(ST_GeoHash(geom)::VARCHAR, 5) = LEFT(ST_GeoHash(STV_GeometryPoint(...))::VARCHAR, 5);
-- 12,085, 1,191.1 ms
```

`ST_GeoHash` always returns 20 characters (no precision argument), so truncation is on you: `LEFT(ST_GeoHash(geom), 5)` fails with `Function LEFT(long varchar, int) does not exist` because the return type is `long varchar`, not `varchar`.

Scanning 200,000 rows takes 1.19 s to narrow to 12,085 candidates. **The value of GeoHash is not the CPU it saves; it is that a string prefix can go into a B-tree or a partition key.** On a table partitioned by geohash prefix, this query becomes a range scan.

`ST_GeomFromGeoHash` deserves a warning: it returns the cell's bounding box as a **POLYGON**, not the original point.

```
=> SELECT ST_AsText(ST_GeomFromGeoHash('wsqqqm15z1kjt9f9yprs'));
 POLYGON ((121.5654 25.033, 121.5654 25.033, 121.5654 25.033, 121.5654 25.033, 121.5654 25.033))
```

A 20-character cell is far smaller than what `float8` can print, so all five vertices render at the same coordinate. If you need a point back, call `ST_Centroid`.

---

## 5. Performance and Cost

Same data, same session, consecutive runs (warm). The "relative" column is against the slowest brute-force `ST_Contains` join (8,660.7 ms); above 1 means faster:

![Decision flow for point-in-polygon, distance and radius, and nearest neighbour, with measured timings]({{ '/assets/images/geospatial-query-choice.svg' | relative_url }}){: loading="lazy" }

| Query | Time | Output | Relative |
|-------|------|--------|----------|
| `ST_Contains` join + `GROUP BY` | 8,660.7 ms | 8 rows / 198,398 | 1.00x |
| `ST_Contains` join (count only) | 7,694.8 ms | 198,398 | 1.13x |
| `ST_Intersects` join (count only) | 7,303.5 ms | 201,201 | 1.19x |
| `STV_Intersect` index transform | **827.6 ms** | 201,201 | **10.5x** |
| index + CTAS | 1,994.0 ms | 201,201 rows / 402,728 B | 4.34x |
| `GROUP BY` over the landed table | **15.0 ms** | 8 rows | 577x |
| bbox (constant polygon) + `ST_Contains` | 450.4 ms | 31,000 | 19.2x |
| bbox (`ST_XMin` in the join) + `ST_Contains` | 14,842.7 ms | 198,398 | **0.58x** |
| `LEFT JOIN ... IS NULL` (unmatched points) | 7,489.8 ms | 1,602 | 1.16x |
| `STV_DWithin` 0.005° | 1,023.7 ms | 487 | 8.46x |
| `STV_DWithin` 500 m (geography) | 1,116.7 ms | 436 | 7.76x |
| `STV_NN` k=5 | 613.2 ms | 5 rows | 14.1x |
| `ORDER BY ST_Distance(geog)` LIMIT 5 | 1,432.4 ms | 5 rows | 6.05x |
| GeoHash prefix filter (5 chars) | 1,191.1 ms | 12,085 | 7.27x |

Three ratios worth remembering:

1. **The index ceiling here is about 10x**, because it only removes polygon comparisons - scanning 200,000 rows is still there. At 8,000 polygons instead of 8, brute force becomes 1,600,000 x 1,000 comparisons and the index wins by orders of magnitude.
2. **Aggregating the landed table is 577x faster**, which is the value of precomputation and the reason a spatial index behaves more like an ETL tool than a query accelerator in Vertica.
3. **A misplaced bbox filter is worse than none** (0.58x). `ST_XMin(d.geom)` is a UDF, and in a join condition it is recomputed for every row pair: 1,600,000 x 4 UDF calls. Only a constant (`WHERE lon BETWEEN 121.50 AND 121.55`) turns into an ordinary comparison.

Storage:

| Object | Rows | `used_bytes` | Per row |
|--------|------|--------------|---------|
| `poi` | 200,000 | 16,696,257 | **83.5 B** |
| `geo_hits` | 201,201 | 402,728 | 2.0 B |
| `districts` | 8 | 1,315 | 164 B |

`STV_MemSize` reports **85 bytes** for a single point and **157 bytes** for a 5-vertex polygon. A point geometry costs more than 5x two `float8` columns (16 bytes) because it carries a type tag, an SRID and a bbox.

**Design advice: keep numeric `lon`/`lat` columns alongside the geometry.** Numeric columns are cheap (encodable, bbox-filterable, sortable in an ordinary projection) and the `geometry` column is only needed when a spatial function is actually called. With 500 million store coordinates, that decision is worth tens of gigabytes.

---

## 6. Common Mistakes and Anti-Patterns

### 1. Treating degrees as metres

```sql
-- meant 500 metres, wrote 0.005 "degrees"
WHERE STV_DWithin(geom, point, 0.005)
```

Measured: **51 extra points (487 vs 436, +11.7%)**, and the error varies with latitude (about +4% at Kaohsiung's 22.6°N). Always use `GEOGRAPHY` with metres for radius queries.

### 2. Swapping longitude and latitude

```sql
-- wrong: STV_GeometryPoint(lat, lon)
SELECT count(*) FROM poi
WHERE ST_Contains((SELECT geom FROM districts WHERE district_name='Zhongzheng'),
                  STV_GeometryPoint(lat, lon));
-- 0
Time: 2,014.2 ms
```

**It returns 0 and raises nothing**, because (25.03, 121.56) is a perfectly valid coordinate - in the Pacific. Validate with `ST_X()` in 119-122 and `ST_Y()` in 21-26 for Taiwan.

### 3. Assuming boundary points get counted

`ST_Contains` returns `false` for 1,602 grid points. Address-geocoded coordinates rarely land on a boundary; grid sampling can put 1% of the data there. **When report totals do not match the population, check this first.**

### 4. Changing polygons without refreshing the index

The index returned **31,251** against a true **15,499**. The dangerous part is that the query is fast and looks healthy. Put `STV_Refresh_Index` at the end of the ETL and add a monitor that compares index counts against a sampled `ST_Contains` count.

### 5. Building a spatial index on points

```
ERROR 3399: Failure in UDx RPC call InvokeProcessPartition() ...
  Error creating index: There are no valid polygons to index.
  NOTICE: Some geometries ignored [Non (MULTI)POLYGON: 200000]
```

A spatial index is a polygon grid; it answers "which polygons contain this point" and nothing else. To speed up nearest-neighbour work you have `STV_NN` (full scan, cheaper sort) or your own geohash / grid bucketing.

### 6. Putting the bbox condition on a UDF in the join

```sql
-- slow: 14,842.7 ms
ON p.lon BETWEEN ST_XMin(d.geom) AND ST_XMax(d.geom)
```

`ST_XMin` is recomputed for every row pair. Compute the bbox as a constant (subquery or application), or store numeric columns and let an ordinary projection handle it.

### 7. Multi-row INSERT of geometry, or dropping `public` from search_path

```
ERROR 10645: Cannot use multi-row insert: column 'geom' has unsupported type 'geometry'
ERROR 3457: Function ST_GeomFromText(unknown) does not exist
```

The second message makes it look as though the function is missing, when in fact `public` is simply not in the search path. `v_catalog.user_functions` shows that every geospatial UDF lives in `public`.

### 8. Invalid polygons returning false quietly

```sql
=> SELECT ST_IsValid(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))')) AS is_valid,
          STV_IsValidReason(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))')) AS reason;
 is_valid |                  reason
----------+------------------------------------------
 f        | Self-intersection at or near POINT (1 1)

=> SELECT ST_Contains(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))'), ST_GeomFromText('POINT(1 1)'));
 f
```

**A bow-tie polygon does not error; it just makes `ST_Contains` return false.** Run `ST_IsValid` over every polygon before go-live and log `STV_IsValidReason` output.

---

## 7. Risks and Recommendations

### Safe today (read-only, low risk)

1. **Audit types and units.** Query `v_catalog.columns` for every `geometry` / `geography` column and confirm that each `ST_Distance` / `ST_Area` output unit matches the report label.
2. **Run `ST_IsValid` and `STV_IsValidReason` over all polygons** and fix the invalid ones. This is read-only.
3. **Sum once with `ST_Intersects` and once with `ST_Contains`.** The difference is the boundary population; write that number into the report definition.
4. **Measure your bbox selectivity.** `SELECT count(*) FROM t WHERE lon BETWEEN ... AND lat BETWEEN ...` tells you what fraction of the table survives; above 10%, consider geohash or grid bucketing.
5. **Confirm the application sends `(lon, lat)` and not `(lat, lon)`.** Sample 100 rows and check the `ST_X` / `ST_Y` ranges.

### Needs a change window or approval

1. **Build the spatial index.** `STV_Create_Index` creates a new object and consumes memory; run it off-peak and verify the name and extent with `STV_Describe_Index`.
2. **Add the refresh step to the ETL.** `STV_Refresh_Index` is a DDL-class operation; agree on transaction boundaries and retry behaviour.
3. **Split point `geometry` into numeric `lon` / `lat` columns.** Requires table and application changes, but saves 5x storage and a lot of UDF cost over time.
4. **Move real-time `ST_Contains` to an offline mapping table.** This is an architecture change; confirm the freshness requirement first (store-to-district can be nightly, delivery zones may not).

---

## 8. References

- [Geospatial functions](https://docs.vertica.com/25.4.x/en/sql-reference/functions/geospatial-functions/) - full list and argument types for `ST_Contains`, `ST_Intersects`, `ST_Distance`, `ST_Buffer`
- [Geography functions](https://docs.vertica.com/25.4.x/en/sql-reference/functions/geography-functions/) - spherical distance and area semantics
- [Spatial index functions](https://docs.vertica.com/25.4.x/en/sql-reference/functions/spatial-index-functions/) - `STV_Create_Index`, `STV_Refresh_Index`, `STV_Intersect`, `STV_NN` and their `OVER` clause usage
- [GEOMETRY data type](https://docs.vertica.com/25.4.x/en/sql-reference/data-types/geometry/) - maximum length, SRID, WKT/WKB conversion
