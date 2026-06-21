---
layout: post
title: "Vertica + Apache Iceberg 實戰指南 — 打造高效 Data Lakehouse"
date: 2026-06-20 22:00:00 +0800
categories: vertica iceberg lakehouse
tags: [vertica, iceberg, lakehouse, S3, Glue, Nessie, OAuth2, external-tables]
description: "深入解析 Vertica 與 Apache Iceberg 的整合 — 從架構概念、功能演進對照表，到五大實戰範例：AWS Glue、檔案系統表、Lakekeeper REST Catalog、Nessie OAuth2 與 Bearer Token 認證。"
---

想像一個廚房：左邊是巨大的冷藏庫（Data Lake），塞滿食材但雜亂無章；右邊是精緻的備餐檯（Data Warehouse），井然有序但成本高昂。中間的 ETL 輸送帶全年無休地搬運食材，耗費大量成本。

這就是傳統資料架構的困境。而 **Apache Iceberg + Vertica** 的組合，就像一位米其林主廚走進你的廚房，直接使用你現有的食材，不必搬運、不必複製。

<!--
AI Summary: Vertica + Apache Iceberg Data Lakehouse 實戰指南。涵蓋 Iceberg 核心特性 (ACID/Schema Evolution/Time Travel)、Vertica Iceberg 功能演進對照 (12.0.4→25.4.0)、五大實戰範例 (AWS Glue/File System Tables/Lakekeeper REST/Nessie OAuth2/Nessie Bearer Token)、Metastore URL 結構速查、效能取捨建議 (ROS vs Iceberg)。
AI Keywords: Vertica, Apache Iceberg, data lakehouse, AWS Glue, Nessie, Lakekeeper, REST catalog, OAuth2, Parquet
-->
<!--more-->

---

## 一、Apache Iceberg 是什麼？

Apache Iceberg 是一個專為超大分析資料集設計的**開放表格格式 (Open Table Format)**。與傳統表格格式不同，Iceberg 提供了：

| 特性 | 說明 |
|------|------|
| **ACID 交易** | 確保並發寫入時的資料一致性 |
| **Schema Evolution** | 新增、刪除、更新欄位不需重寫資料 |
| **Hidden Partitioning** | 自動分割區管理，使用者無需手動指定 |
| **Time Travel** | 查詢資料的歷史版本 |
| **Metadata 管理** | 高效追蹤檔案與統計資訊 |

Iceberg 將表格格式與儲存層分離，實現真正的**運算與儲存分離**。

---

## 二、為什麼選擇 Vertica + Iceberg？

### 核心優勢

| 優勢 | 說明 |
|------|------|
| **直接查詢資料湖** | 無需搬移資料，直接在 S3 上查詢 Iceberg 格式 |
| **跨引擎互通** | Spark 寫入 → Vertica 查詢，或 Trino/Flink 任意組合 |
| **零 Lock-in** | 開放格式，資料永遠屬於你 |
| **Time Travel** | 查詢歷史版本的資料快照 |
| **無資料重複** | 一份資料，多引擎共享 |

### 效能 Reality Check

| 場景 | 建議儲存格式 |
|------|-------------|
| 高頻、關鍵任務查詢 | Vertica **原生 ROS**（效能最佳） |
| 生產儀表板（次秒級響應） | 原生 ROS |
| **探索式分析**（冷/溫資料） | **Iceberg 外部表** |
| 跨引擎工作負載 | Iceberg |
| 成本優化優先於效能 | Iceberg |

> **關鍵原則**：Iceberg 提供的是架構靈活性，而非原生儲存的替代品。ROS 用於效能關鍵的工作負載，Iceberg 用於跨引擎互通與成本優化。

---

## 三、功能演進對照表 (12.0.4 → 25.4.0)

Vertica 對 Iceberg 的支援從 12.0.4 開始，持續強化至今。以下是完整的功能對照：

| 功能類別 | 12.0.4 | 23.3+ | 24.2+ | 24.4+ | 25.1+ | 25.4+ |
|---------|--------|-------|-------|-------|-------|-------|
| **表格類型** | | | | | | |
| 檔案系統表 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Base Location 支援 | 部分* | ✅ | ✅ | ✅ | ✅ | ✅ |
| Metadata File Path | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Metastore** | | | | | | |
| AWS Glue | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Hive Metastore (HMS) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| REST Catalog API | | ✅ | ✅ | ✅ | ✅ | ✅ |
| Project Nessie | | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lakekeeper | | ✅ | ✅ | ✅ | ✅ | ✅ |
| **認證方式** | | | | | | |
| Bearer Token | | ✅ | ✅ | ✅ | ✅ | ✅ |
| OAuth2 | | | ✅ | ✅ | ✅ | ✅ |
| **中繼資料** | | | | | | |
| Iceberg v1 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Iceberg v2 | | ✅ | ✅ | ✅ | ✅ | ✅ |
| Fallback Name Mapping | | | ✅ | ✅ | ✅ | ✅ |
| **資料格式** | | | | | | |
| Parquet | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Field IDs Optional | 部分** | 部分** | ✅ | ✅ | ✅ | ✅ |
| **資料類型** | | | | | | |
| 基本類型 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Struct (ROW) | 部分*** | ✅ | ✅ | ✅ | ✅ | ✅ |
| List (ARRAY) | | | ✅ | ✅ | ✅ | ✅ |
| Map | | | ✅ | ✅ | ✅ | ✅ |
| UUID | | | ✅ | ✅ | ✅ | ✅ |
| **分割區表** | | | | | | |
| 讀取分割區表 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Partition Pruning | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **監控** | | | | | | |
| QUERY_EVENTS | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| EXTERNAL_DATA_FILES_PRUNED | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

*註：S3 在 v12.0.x 需要完整 metadata 檔案路徑，不支援 base location*
**註：v24.2+ 可透過 fallback name mapping 從 metadata 讀取 field IDs*
***註：v12.0.x 中缺少 struct 欄位會報錯，v23.3+ 則視為 NULL*

---

## 四、五大實戰範例

### 範例 1：AWS Glue Catalog 整合

**情境**：Iceberg 表格儲存在 S3，metadata 由 AWS Glue Data Catalog 管理。

**Step 1：用 PySpark 建立 Iceberg 表格並載入資料**

```python
from pyspark.sql import SparkSession

spark = (SparkSession.builder
    .appName("iceberg_commit_store_orders")
    .config("spark.sql.extensions",
        "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.glue_catalog",
        "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.glue_catalog.warehouse",
        "s3://your-bucket/partitioned_parquet/")
    .config("spark.sql.catalog.glue_catalog.catalog-impl",
        "org.apache.iceberg.aws.glue.GlueCatalog")
    .config("spark.sql.catalog.glue_catalog.io-impl",
        "org.apache.iceberg.aws.s3.S3FileIO")
    .getOrCreate()
)

spark.sql("""
CREATE TABLE IF NOT EXISTS glue_catalog.sruthiglue.store_orders (
  id BIGINT, name STRING, year INT, month INT, day INT, region STRING
) USING ICEBERG
""")
```

**Step 2：在 Vertica 建立 Iceberg 外部表**

```sql
=> CREATE EXTERNAL TABLE store_orders_glue
   STORED BY ICEBERG
   LOCATION 's3://your-bucket/partitioned_parquet'
   GLUE_DB 'sruthiglue'
   GLUE_TABLE 'store_orders';
```

**Step 3：設定認證並查詢**

```sql
=> ALTER SESSION SET GlueEndpoint ='glue.us-east-1.amazonaws.com';
=> ALTER SESSION SET AWSAuth = '<ACCESS_KEY>:<SECRET_KEY>';
=> ALTER SESSION SET AWSEndpoint = 's3.amazonaws.com';
=> ALTER SESSION SET AWSEnableHTTPS = 1;
=> ALTER SESSION SET AWSRegion = 'us-east-1';

=> SELECT * FROM store_orders_glue;
 id | name  | year | month | day | region
----+-------+------+-------+-----+--------
  3 | Carol | 2025 |    11 |  25 | APAC
  4 | Dave  | 2025 |    11 |  25 | US
  2 | Bob   | 2025 |    11 |  25 | EU
...
```

**幕後運作原理**：
1. Vertica 連接 AWS Glue Catalog 取得 Iceberg metadata
2. 從 S3 讀取 Iceberg metadata 檔案（schema、分割區）
3. 透過 manifest 檔案識別要讀取的資料檔案
4. 套用 partition pruning 與 predicate pushdown
5. 只從 S3 讀取必要的 Parquet 資料檔案

---

### 範例 2：檔案系統表 (File System Tables)

**情境**：Iceberg 表格以標準目錄結構儲存在 S3 上。

**方法 A：指定特定 metadata 檔案（固定版本查詢）**

```sql
-- 查詢 v1 版本的資料（5 筆）
=> CREATE EXTERNAL TABLE orders_v1
   STORED BY ICEBERG
   LOCATION 's3://bucket/iceberg_demo/orders/metadata/v1.metadata.json';

=> SELECT * FROM orders_v1;
```

**方法 B：使用 Base Location（自動取最新版本）**

```sql
-- 自動讀取最新版本（7 筆，含 append 的資料）
=> CREATE EXTERNAL TABLE orders_latest
   STORED BY ICEBERG
   LOCATION 's3://bucket/iceberg_demo/orders/';

=> SELECT * FROM orders_latest;
```

---

### 範例 3：REST Catalog 整合 (Lakekeeper)

**情境**：Iceberg 表格透過 Lakekeeper 的 REST API 進行 catalog 管理。

```sql
-- 在 Vertica 建立外部表，指向 Lakekeeper REST endpoint
=> CREATE EXTERNAL TABLE lakekeeper_orders
   STORED BY ICEBERG
   LOCATION 'http://10.xx.xx.94:8181/catalog/v1/{warehouse-id}/namespaces/lakekeeper/tables/orders';

-- 設定 MinIO 存取
=> ALTER SESSION SET AWSAuth = 'user:password';
=> ALTER SESSION SET AWSEndpoint = '10.xx.xx.94:9000';
=> ALTER SESSION SET AWSEnableHttps = 0;

-- 查詢
=> SELECT * FROM lakekeeper_orders;
```

---

### 範例 4：REST Catalog + OAuth2 認證 (Nessie)

**情境**：Iceberg 表格透過 Nessie REST API 管理，使用 OAuth2 認證。

```sql
=> CREATE EXTERNAL TABLE nessie_orders_oauth2
   STORED BY ICEBERG
   LOCATION 'http://10.xx.xx.91:19120/iceberg/v1/main/namespaces/nessie/tables/orders'
   REST_AUTH '{
     "oauthTokenUri": "http://10.xx.xx.91:8080/realms/iceberg/protocol/openid-connect/token",
     "oauthClientId": "client1",
     "oauthClientSecret": "sxxxx"
   }';
```

---

### 範例 5：REST Catalog + Bearer Token 認證 (Nessie)

**情境**：使用 Bearer Token 替代 OAuth2 進行認證。

**先從 Keycloak 取得 token：**

```bash
$ curl -X POST http://10.xx.xx.91:8080/realms/iceberg/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=client1" \
  -d "client_secret=sxxxx"

→ 回傳 access_token
```

**在 Vertica 建立外部表：**

```sql
=> CREATE EXTERNAL TABLE nessie_orders_bearer
   STORED BY ICEBERG
   LOCATION 'http://10.xx.xx.91:19120/iceberg/v1/main/namespaces/nessie/tables/orders'
   REST_AUTH '{"bearerToken": "eyJhbGciOi...}"}';

=> SELECT * FROM nessie_orders_bearer;
```

---

## 五、Metastore LOCATION URL 結構速查

| Metastore | URL 結構範例 |
|-----------|-------------|
| **Lakekeeper** | `http://{host}:8181/catalog/v1/{warehouse-id}/namespaces/{ns}/tables/{table}` |
| **Nessie** | `http://{host}:19120/iceberg/v1/{branch}/namespaces/{ns}/tables/{table}` |

### REST_AUTH 認證設定

| 認證方式 | REST_AUTH 內容 |
|---------|---------------|
| **OAuth2** | `{"oauthTokenUri": "...", "oauthClientId": "...", "oauthClientSecret": "..."}` |
| **Bearer Token** | `{"bearerToken": "eyJ..."}` |

---

## 六、最佳實踐建議

### 何時使用 Iceberg 外部表

- ✅ 探索式分析 on 冷/溫資料
- ✅ 跨引擎工作負載（Spark 寫入、Vertica 查詢）
- ✅ 成本優化優先於原始效能
- ✅ 資料已存在 Iceberg 格式

### 何時使用 Vertica 原生 ROS

- ✅ 高頻、關鍵任務查詢
- ✅ 生產儀表板（次秒級響應）
- ✅ 熱資料（Hot data）持續被查詢

### 設定檢查清單

```sql
-- 1. 確認 Vertica 版本 >= 24.4.0
=> SELECT version();

-- 2. 確認 S3/物件儲存連線
=> ALTER SESSION SET AWSAuth = '<key>:<secret>';
=> ALTER SESSION SET AWSEndpoint = 's3.amazonaws.com';
=> ALTER SESSION SET AWSEnableHTTPS = 1;

-- 3. 確認 Metastore 連線
-- Glue: GLUE_DB / GLUE_TABLE 參數
-- REST: LOCATION URL + REST_AUTH

-- 4. 驗證查詢
=> SELECT COUNT(*) FROM your_iceberg_table;
```

---

## 結論

Vertica + Apache Iceberg 的組合，為資料架構提供了前所未有的靈活性。你可以在同一個 Vertica 叢集中同時擁有：

- **原生 ROS 表格**：承載高頻、關鍵任務的查詢
- **Iceberg 外部表**：直接查詢資料湖中的 Iceberg 格式資料

兩者之間不需要 ETL、不需要搬移資料、不需要複製。這是真正的 **Data Lakehouse** 架構 — 將 Data Lake 的靈活性與 Data Warehouse 的分析能力合而為一。

---

*本文基於 Vertica 25.4.0-0 與 OpenText Community 文章 "Vertica and Apache Iceberg: Your Complete Guide to the Ultimate Data Lakehouse Power Couple" 撰寫。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
