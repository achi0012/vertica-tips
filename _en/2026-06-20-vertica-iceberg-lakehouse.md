---
layout: post_en
title: "Vertica + Apache Iceberg: Building a High-Performance Data Lakehouse"
date: 2026-06-20 22:00:00 +0800
categories: vertica iceberg lakehouse
tags: [vertica, iceberg, lakehouse, S3, Glue, Nessie, OAuth2, external-tables, Parquet]
description: "Complete guide to Vertica + Apache Iceberg integration — architecture, feature evolution matrix (12.0.4→25.4.0), five hands-on examples (AWS Glue, File System Tables, Lakekeeper, Nessie OAuth2, Bearer Token)."
---

Imagine a kitchen with a massive walk-in refrigerator (Data Lake) and a gleaming prep station (Data Warehouse), connected by an expensive ETL conveyor belt. **Apache Iceberg + Vertica** eliminates that conveyor belt entirely.

This guide explores how Vertica and Iceberg work together to create a flexible, high-performance data lakehouse.

<!--more-->

---

## 1. What is Apache Iceberg?

Apache Iceberg is an open table format designed for huge analytic datasets:

| Feature | Description |
|---------|-------------|
| **ACID Transactions** | Consistent concurrent writes |
| **Schema Evolution** | Add/drop/rename columns without rewriting |
| **Hidden Partitioning** | Automatic partition management |
| **Time Travel** | Query historical data versions |
| **Metadata Management** | Efficient file and statistics tracking |

---

## 2. Why Vertica + Iceberg?

| Benefit | Description |
|---------|-------------|
| **Query data lakes directly** | No ETL, query Iceberg on S3 directly |
| **Multi-engine interoperability** | Spark writes, Vertica queries |
| **Zero lock-in** | Open format, your data stays yours |
| **No data duplication** | One copy, shared across engines |

### Performance Reality

| Scenario | Recommended Storage |
|----------|-------------------|
| High-frequency critical queries | Vertica **native ROS** (best performance) |
| Production dashboards (sub-second) | Native ROS |
| **Exploratory analytics** (cold/warm) | **Iceberg external tables** |
| Multi-engine workloads | Iceberg |
| Cost optimization | Iceberg |

---

## 3. Feature Evolution (12.0.4 → 25.4.0)

| Feature | 12.0.4 | 23.3+ | 24.2+ | 24.4+ | 25.4+ |
|---------|--------|-------|-------|-------|-------|
| File System Tables | ✅ | ✅ | ✅ | ✅ | ✅ |
| Base Location Support | Partial | ✅ | ✅ | ✅ | ✅ |
| **Metastores** | | | | | |
| AWS Glue | ✅ | ✅ | ✅ | ✅ | ✅ |
| Hive Metastore | ✅ | ✅ | ✅ | ✅ | ✅ |
| REST Catalog API | | ✅ | ✅ | ✅ | ✅ |
| Nessie | | ✅ | ✅ | ✅ | ✅ |
| Lakekeeper | | ✅ | ✅ | ✅ | ✅ |
| **Auth** | | | | | |
| Bearer Token | | ✅ | ✅ | ✅ | ✅ |
| OAuth2 | | | ✅ | ✅ | ✅ |
| **Data Types** | | | | | |
| Iceberg v2 | | ✅ | ✅ | ✅ | ✅ |
| Struct (ROW) | Partial | ✅ | ✅ | ✅ | ✅ |
| List (ARRAY) | | | ✅ | ✅ | ✅ |
| UUID | | | ✅ | ✅ | ✅ |

---

## 4. Five Practical Examples

### Example 1: AWS Glue Catalog

```sql
=> CREATE EXTERNAL TABLE store_orders_glue
   STORED BY ICEBERG
   LOCATION 's3://your-bucket/partitioned_parquet'
   GLUE_DB 'sruthiglue'
   GLUE_TABLE 'store_orders';
```

### Example 2: File System Table (Version Pinning)

```sql
-- Specific version
=> CREATE EXTERNAL TABLE orders_v1 STORED BY ICEBERG
   LOCATION 's3://bucket/iceberg_demo/orders/metadata/v1.metadata.json';

-- Latest version (base location)
=> CREATE EXTERNAL TABLE orders_latest STORED BY ICEBERG
   LOCATION 's3://bucket/iceberg_demo/orders/';
```

### Example 3: REST Catalog (Lakekeeper)

```sql
=> CREATE EXTERNAL TABLE lakekeeper_orders STORED BY ICEBERG
   LOCATION 'http://host:8181/catalog/v1/{warehouse-id}/namespaces/ns/tables/orders';
```

### Example 4: REST Catalog + OAuth2 (Nessie)

```sql
=> CREATE EXTERNAL TABLE nessie_orders_oauth2 STORED BY ICEBERG
   LOCATION 'http://host:19120/iceberg/v1/main/namespaces/ns/tables/orders'
   REST_AUTH '{"oauthTokenUri": "...", "oauthClientId": "client1", "oauthClientSecret": "sxxx"}';
```

### Example 5: REST Catalog + Bearer Token (Nessie)

```sql
=> CREATE EXTERNAL TABLE nessie_orders_bearer STORED BY ICEBERG
   LOCATION 'http://host:19120/iceberg/v1/main/namespaces/ns/tables/orders'
   REST_AUTH '{"bearerToken": "eyJhbGciOi..."}';
```

---

## 5. Metastore URL Reference

| Metastore | URL Pattern |
|-----------|-------------|
| **Lakekeeper** | `http://{host}:8181/catalog/v1/{warehouse-id}/namespaces/{ns}/tables/{table}` |
| **Nessie** | `http://{host}:19120/iceberg/v1/{branch}/namespaces/{ns}/tables/{table}` |

### REST_AUTH Formats

| Auth | JSON |
|------|------|
| **OAuth2** | `{"oauthTokenUri":"...","oauthClientId":"...","oauthClientSecret":"..."}` |
| **Bearer Token** | `{"bearerToken":"eyJ..."}` |

---

## 6. Best Practices

### Use Iceberg External Tables for:
- ✅ Exploratory analytics on cold/warm data
- ✅ Multi-engine workloads (Spark writes, Vertica reads)
- ✅ Cost optimization over raw performance
- ✅ Data already in Iceberg format

### Use Native ROS for:
- ✅ High-frequency, mission-critical queries
- ✅ Production dashboards (sub-second response)
- ✅ Hot data queried constantly

---

*Based on Vertica 25.4.0-0 and OpenText Community article on Vertica + Iceberg.*
