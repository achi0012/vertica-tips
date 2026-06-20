---
layout: post
title: "Vertica Smart Scanning 加速外部表查詢 — ObjectStoreGlobStrategy 深度解析"
date: 2026-06-20 16:00:00 +0800
categories: vertica smart-scanning external-tables
tags: [vertica, smart-scanning, external-tables, object-store, S3, performance, partition-pruning]
description: "Vertica 23.4+ 引入的 ObjectStoreGlobStrategy 如何透過分層掃描（Hierarchical Strategy）將外部表查詢加速 88%？本文深入解析 Flat 與 Hierarchical 兩種策略的運作原理、實測對比與最佳實踐。"
---

隨著資料湖規模不斷增長，尤其在 Amazon S3、Google Cloud Storage 這類物件儲存上，**外部表 (External Table)** 的查詢效率成為效能瓶頸的關鍵。Vertica 從 23.4 版開始引入了一個容易被忽略但威力強大的配置參數 — `ObjectStoreGlobStrategy`，它能大幅改變 Vertica 掃描分割區檔案的方式，讓查詢速度提升近 **9 倍**。

<!--more-->

---

## 一、什麼是 ObjectStoreGlobStrategy？

當 Vertica 查詢外部表時，需要先**列出 (list)** 物件儲存中的檔案清單。`ObjectStoreGlobStrategy`（自 Vertica 23.4 引入）決定了 Vertica 如何在物件儲存中列舉檔案，這對於按 `year`、`month`、`region` 等欄位分割的大量資料尤為關鍵。

Vertica 提供兩種策略：

| 策略 | 說明 | 引入版本 |
|------|------|----------|
| **Flat** (預設) | 一次列出所有檔案，再套用過濾條件 | 原始行為 |
| **Hierarchical** | 逐層列出目錄，邊掃描邊過濾 | Vertica 23.4+ |

---

## 二、Flat Strategy — 簡單但昂貴

### 運作方式

Flat 策略在指定路徑下執行**完整檔案列舉**，然後才套用查詢過濾條件。Vertica 會取得所有物件名稱，無論查詢是否需要。

```
查詢: WHERE region='West' AND year=2023 AND month=01

Flat 策略:
  1. 列出 data/ 下所有檔案 (數萬個)
  2. 逐一比對 year/month/region
  3. 只保留符合條件的檔案
```

### 效能影響

- **高 metadata 開銷**：列出所有檔案的 API 成本極高
- **查詢緩慢**：過濾發生在列舉**之後**
- **擴展性差**：隨著檔案數量增長，效能直線下降

### 適用時機

- 資料集小、分割區少
- 查詢過濾條件不包含分割區欄位

---

## 三、Hierarchical Strategy — 更聰明、更快速

### 運作方式

Hierarchical 策略**逐層**列出物件，每層都套用過濾條件。這讓 Vertica 可以在列舉過程中就**提前修剪**不相關的路徑，直接跳過不需要的目錄。

```
查詢: WHERE region='West' AND year=2023 AND month=01

Hierarchical 策略:
  1. 列出 year=2023/      → 只探索 2023
  2. 在 year=2023/ 下列出 month=01/  → 只探索 January
  3. 在 month=01/ 下列出 region=West/ → 只探索 West
  4. 只掃描這一個目錄下的檔案
```

### 效能影響

- **低 metadata 開銷**：只探索相關目錄
- **查詢更快速**：分割區修剪發生在列舉**期間**
- **優異的擴展性**：適合大型、深度分割的資料集
- **降低成本**：大幅減少物件儲存的 API 呼叫次數

### 適用時機

- 大型資料集、多層分割（如 `year/month/region`）
- 查詢經常過濾分割區欄位

---

## 四、實測對比

### 資料情境

假設資料儲存在 S3 上，按 `year`、`month`、`region` 三層分割：

```
s3://bucket/data/year=2025/month=09/region=South/file.parquet
```

### 建立外部表

```sql
CREATE EXTERNAL TABLE public.records (
    id INT,
    name VARCHAR(50),
    year INT,
    month INT,
    day INT,
    region VARCHAR(50)
)
AS COPY FROM 's3://new-bucket/data/*/*/*/*'
PARTITION COLUMNS year, month, region PARQUET;
```

### 使用預設策略 (Flat) 查詢

```sql
=> SELECT * FROM records
   WHERE region='West' AND year=2023 AND month=01;
```

```
Time: First fetch (1000 rows): 588.460 ms
Time: All rows formatted:     3757.432 ms
```

### 切換為 Hierarchical 策略

```sql
=> ALTER DATABASE default SET ObjectStoreGlobStrategy = 'Hierarchical';
```

### 使用 Hierarchical 策略查詢

```sql
=> SELECT * FROM records
   WHERE region='West' AND year=2023 AND month=01;
```

```
Time: First fetch (1000 rows): 67.682 ms
Time: All rows formatted:     1835.546 ms
```

### 效能對比

| 策略 | First Fetch (1000 rows) | All Rows Formatted | 加速比 |
|------|------------------------|-------------------|--------|
| **Flat** (預設) | 588.460 ms | 3757.432 ms | 基準 |
| **Hierarchical** | **67.682 ms** | **1835.546 ms** | **88.5% 更快** |

> Hierarchical 策略在首次資料擷取（First Fetch）上快了 **88.5%**，整體格式化時間也減少了 **51.1%**。在更大規模的資料集上，差距會更加顯著。

---

## 五、何時選擇哪種策略

| 特性 | Flat 策略 | Hierarchical 策略 |
|------|----------|------------------|
| **列舉方式** | 一次列舉所有檔案 | 逐層掃描目錄 |
| **分割區修剪** | 列舉後過濾 | 列舉期間過濾 |
| **Metadata 開銷** | 高 | 低 |
| **查詢效能** | 較慢 | 較快 |
| **擴展性** | 大資料集不佳 | 大資料集優異 |
| **最佳場景** | 小型、淺層分割 | 大型、深度分割 |

### 選擇指南

- **小資料集 + 少量分割區** → Flat 即可（設定簡單，無需調整）
- **大資料集 + 多層分割區** → **Hierarchical**（強烈建議）
- **查詢經常過濾分割區欄位** → **Hierarchical**（顯著加速）
- **不確定如何選擇** → Hierarchical（在多數情境下表現更佳）

---

## 六、設定方式

```sql
-- 全域修改 (建議)
=> ALTER DATABASE default SET ObjectStoreGlobStrategy = 'Hierarchical';

-- 確認當前設定
=> SELECT parameter_name, current_value
   FROM configuration_parameters
   WHERE parameter_name = 'ObjectStoreGlobStrategy';
```

---

## 結論

`ObjectStoreGlobStrategy` 是 Vertica 23.4+ 中一個**低成本、高回報**的效能調優選項。只需一行 `ALTER DATABASE` 指令，就能讓外部表的查詢速度提升數倍。

對於生產環境中大量使用外部表查詢物件儲存（S3、GCS）的場景，強烈建議採用 **Hierarchical** 策略。特別是結合 Vertica 的分割區修剪 (Partition Pruning) 機制，可以讓查詢引擎以最聰明的方式掃描資料，大幅減少 I/O 開銷與 API 呼叫成本。

---

*本文基於 Vertica 25.4.0-0 與 OpenText Community 文章 "Smart Scanning in Vertica: Faster External Table Queries on Partitioned Object Stores" 撰寫。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
