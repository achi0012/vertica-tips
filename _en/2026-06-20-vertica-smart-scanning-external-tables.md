---
layout: post_en
title: "Vertica Smart Scanning — Faster External Table Queries with ObjectStoreGlobStrategy"
date: 2026-06-20 16:00:00 +0800
categories: vertica smart-scanning external-tables
tags: [vertica, smart-scanning, ObjectStoreGlobStrategy, external-tables, S3, partition-pruning, performance]
description: "How Vertica 23.4+ ObjectStoreGlobStrategy accelerates external table queries by 88.5% using hierarchical scanning. Deep dive into Flat vs Hierarchical strategies with benchmark comparisons."
---

As data lakes grow on object stores like S3 and GCS, external table query performance becomes critical. Vertica 23.4+ introduced a powerful yet often overlooked parameter — `ObjectStoreGlobStrategy` — that can improve query speed by **up to 9x** through smarter partition-aware scanning.

<!--more-->

---

## 1. What is ObjectStoreGlobStrategy?

Controls how Vertica lists files on object stores when querying external tables with partitioned data.

| Strategy | Description | Introduced |
|----------|-------------|------------|
| **Flat** (default) | Lists ALL files first, then filters | Legacy |
| **Hierarchical** | Lists one directory level at a time, filters as it goes | Vertica 23.4+ |

---

## 2. Flat Strategy — Simple but Costly

Lists **all files** in the path before applying filters. Vertica retrieves every object name regardless of query needs.

```
Query: WHERE region='West' AND year=2023 AND month=01

Flat:
  1. List ALL files under data/ (thousands)
  2. Match year/month/region on each
  3. Keep only matching files
```

**Impact**: High metadata overhead, slow execution, poor scalability.

**Best for**: Small datasets, few partitions, queries without partition filters.

---

## 3. Hierarchical Strategy — Smarter, Faster

Lists objects **one directory level at a time**, pruning irrelevant paths early.

```
Query: WHERE region='West' AND year=2023 AND month=01

Hierarchical:
  1. List year=2023/          → only explore 2023
  2. Under year=2023/, list month=01/  → only January
  3. Under month=01/, list region=West/ → only West
  4. Scan only files in that directory
```

**Impact**: Low metadata overhead, fast execution, excellent scalability, reduced API calls.

**Best for**: Large datasets, deep partitioning, queries filtering on partition columns.

---

## 4. Benchmark Results

### Test Setup

Data stored on S3 partitioned by `year/month/region`:

```
s3://bucket/data/year=2025/month=09/region=South/file.parquet
```

### Default Strategy (Flat)

```sql
=> SELECT * FROM records WHERE region='West' AND year=2023 AND month=01;
```

```
Time: First fetch (1000 rows): 588.460 ms
Time: All rows formatted:     3757.432 ms
```

### Switch to Hierarchical

```sql
=> ALTER DATABASE default SET ObjectStoreGlobStrategy = 'Hierarchical';
```

### Hierarchical Strategy

```sql
=> SELECT * FROM records WHERE region='West' AND year=2023 AND month=01;
```

```
Time: First fetch (1000 rows): 67.682 ms
Time: All rows formatted:     1835.546 ms
```

### Comparison

| Strategy | First Fetch | All Rows Formatted | Improvement |
|----------|-------------|-------------------|-------------|
| **Flat** | 588.460 ms | 3757.432 ms | Baseline |
| **Hierarchical** | **67.682 ms** | **1835.546 ms** | **88.5% faster** |

> Hierarchical is **88.5% faster** for first fetch and **51.1% faster** for total formatting. The gap widens with larger datasets.

---

## 5. Choosing the Right Strategy

| Feature | Flat | Hierarchical |
|---------|------|--------------|
| Listing method | All at once | One level at a time |
| Partition pruning | After listing | During listing |
| Metadata overhead | High | Low |
| Query performance | Slower | Faster |
| Scalability | Poor for big data | Excellent |

### Guidelines

- **Small datasets + few partitions** → Flat is fine
- **Large datasets + deep partitioning** → **Hierarchical** (strongly recommended)
- **Queries frequently filter on partitions** → **Hierarchical**
- **Not sure which** → Hierarchical (better in most scenarios)

---

## 6. Configuration

```sql
-- Global setting
=> ALTER DATABASE default SET ObjectStoreGlobStrategy = 'Hierarchical';

-- Verify
=> SELECT parameter_name, current_value
   FROM configuration_parameters
   WHERE parameter_name = 'ObjectStoreGlobStrategy';
```

---

*Based on Vertica 25.4.0-0 testing and OpenText Community article on Smart Scanning.*
