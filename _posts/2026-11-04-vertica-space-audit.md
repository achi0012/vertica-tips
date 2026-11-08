---
layout: post
title: "Vertica 空間審計實戰 — 別數 ROS 容器，算位元組（25.4 實測）"
date: 2026-11-04 10:00:00 +0800
categories: vertica storage audit delete-vectors
tags: [vertica, storage, ROS, delete-vectors, projection-storage, purge, v_monitor, audit, mergeout]
description: "68.20 GB 的 Vertica 25.4 實測空間審計：1,254 個 ROS 容器只佔 0.03 GB、164 張 0 bytes 表、288,920 列未 purge 的刪除。四組系統表 SQL 與回收動作對照。"
image: /assets/images/og/vertica-space-audit-zh.png
---

磁碟快滿了，第一反應通常是「ROS 容器太多了，該 merge 一下」。這個直覺在早期的 Vertica 還算可靠，但在 25.x 上，我對一台 68.20 GB 的實機做完盤點後發現：**容器數量與佔用位元組幾乎完全脫鉤**。1,524 個容器裡有 1,254 個（82.3%）不到 1 MB，把它們全部清掉只會回收 0.03 GB — 佔全庫 0.04%。真正吃掉 68 GB 的是另外 33 個容器。

這篇不是 mergeout 機制教學（機制請看 [Tuple Mover 最佳實踐]({{ site.baseurl }}/2026/06/vertica-tuple-mover-best-practice/)），而是**審計流程**：用四張系統表把「空間去哪了」拆成三個可對帳的帳戶，再用兩個判準決定哪些該回收、哪些動了也沒用。

<!--
AI Summary: Vertica 25.4 實機空間審計方法。以 v_monitor.projection_storage、storage_containers、delete_vectors 三表對帳 68.20 GB；實測 1,524 個 ROS 容器中 1,254 個 <1 MB 但僅佔 0.03 GB，270 個 ≥1 MB 容器佔 68.16 GB（99.95%）。提供容器大小直方圖、每表 MB/容器 指標、0 bytes 表偵測、delete vector 佔用統計四組 SQL，以及 DROP/TRUNCATE/PURGE/重建投影/partition drop 的影響對照表。
AI Keywords: Vertica, 空間審計, ROS container, storage_containers, projection_storage, delete vectors, PURGE TABLE, mergeout, 磁碟空間, 25.4
-->
<!--more-->

---

## 一、先對帳：三個「空間帳戶」不要混用

Vertica 的空間資訊分散在三個地方，混用就會得到互相矛盾的結論。

| 來源 | 語意 | 適合回答 |
|------|------|----------|
| `v_monitor.projection_storage` | 每個 projection 在每個節點上的 ROS 儲存量（`used_bytes`、`ros_count`） | 哪張表、哪個投影佔最多位元組 |
| `v_monitor.storage_containers` | 逐個 ROS 容器的明細（大小、列數、刪除列數） | 碎片化程度、單一容器健康度 |
| `v_monitor.delete_vectors` | 刪除標記本身（列範圍 + epoch） | 「有多少列被標記刪除」，**不是空間報表** |

實測時三個帳戶的關係是這樣：

| 指標 | 值 |
|------|-----|
| `projection_storage` 合計 `used_bytes` | 68.20 GB |
| `projection_storage` 合計 `disk_size` | 68.32 GB |
| `storage_containers` 合計 `used_bytes` | 68.197 GB |
| `delete_vectors` 合計 `used_bytes` | 0.65 MB |

兩個重點：

1. `used_bytes` 與 `disk_size` 差 0.12 GB，是檔案配置粒度（block）與稀疏檔案的差額，不是資料不一致。做容量規劃要抓 `disk_size`，做資料量分析要抓 `used_bytes`。
2. **`delete_vectors` 的 `used_bytes` 只有幾 MB，不代表刪除只浪費幾 MB。** DV 是「哪些列已刪」的索引，被刪的列本體還躺在 ROS 容器裡，體積算在 `storage_containers`。這是新手最常誤判的一格，第五節會用實測說明。

```sql
-- 一鍵對帳：三個帳戶並排看
SELECT round(SUM(used_bytes)/1024^3, 2) AS used_gb,
       round(SUM(disk_size) /1024^3, 2) AS disk_gb
FROM v_monitor.projection_storage
WHERE projection_schema NOT IN ('v_catalog','v_monitor','v_internal');
```

---

## 二、看位元組，不要看數量：容器大小直方圖

這是整篇最重要的一張表。同一台機器、同一時刻，把 1,524 個 ROS 容器依大小分桶：

| 容器大小 | 容器數 | 容器數佔比 | 合計 GB | 位元組佔比 |
|----------|-------:|-----------:|--------:|-----------:|
| **< 1 MB** | **1,254** | **82.3%** | **0.03** | **0.04%** |
| 1 – 10 MB | 91 | 6.0% | 0.37 | 0.54% |
| 10 – 100 MB | 132 | 8.7% | 3.77 | 5.5% |
| 0.1 – 1 GB | 14 | 0.9% | 3.09 | 4.5% |
| **≥ 1 GB** | **33** | **2.2%** | **60.94** | **89.4%** |
| 合計 | 1,524 | 100% | 68.20 | 100% |

82.3% 的容器只承載 0.04% 的資料。反過來說，2.2% 的容器裝了 89.4% 的資料。

所以「容器太多」這句話本身沒有資訊量 — 要問的是**容器數與位元組數是否同向**。實測這台機器的合計數字是：

- 容器數 ≥ 1 MB：270 個
- 這 270 個容器合計：**68.16 GB（99.95%）**
- 剩下 1,254 個 < 1 MB 容器合計：0.03 GB（0.05%）

把它們 merge 掉，磁碟不會變空，只是少 1,254 個檔案。**這種碎片值得處理，但理由是「檔案數／mergeout 開銷／查詢要掃更多容器」，不是「省空間」。** 兩者的處置優先序完全不同，混在一起談就會做出錯的決定。

```sql
-- 容器大小直方圖
SELECT CASE WHEN used_bytes <   1048576 THEN 'a  <1MB'
            WHEN used_bytes <  10485760 THEN 'b  1-10MB'
            WHEN used_bytes < 104857600 THEN 'c  10-100MB'
            WHEN used_bytes < 1073741824 THEN 'd  0.1-1GB'
            ELSE                             'e  >=1GB' END AS bucket,
       COUNT(*)                          AS containers,
       round(SUM(used_bytes)/1024^3, 2)  AS gb
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
GROUP  BY 1
ORDER  BY 1;
```

---

## 三、真正的碎片長什麼樣：MB / 容器

把上面的直方圖下推到「每張表」，指標是 **平均每個容器承載多少 MB**。同一個資料庫裡，兩個極端並存：

| 表（已遮蔽） | 佔用 | 容器數 | 投影數 | MB / 容器 | 判讀 |
|--------------|-----:|-------:|-------:|----------:|------|
| `schema_a.t1` | 41.19 GB | 57 | 1 | 739.9 | 健康，大容器為主 |
| `schema_a.t2` | 12.57 GB | 1 | 1 | 12,875 | 健康，單一大容器 |
| `schema_a.t3` | 7.03 GB | 1 | 1 | 7,196 | 健康 |
| `schema_b.t1` | 4.63 GB | 136 | 2 | 34.9 | 觀察，載入批次多 |
| `schema_c.t1` | 0.13 GB | 267 | 1 | **0.49** | **碎片化** |

`schema_b.t1` 4.63 GB 拆成 136 個容器，平均 34.9 MB — 這在頻繁增量載入的表上很正常，mergeout 會慢慢收拾，不需要人工介入。

`schema_c.t1` 只有 131 MB，卻散成 267 個容器，其中 **255 個小於 1 MB**。這種表的查詢要開 267 個容器逐個掃，metadata 與開啟檔案的固定成本就吃掉大半時間，而且已經接近 `ContainersPerProjectionLimit`（預設 1024）的警戒區。

```sql
-- 每表 MB / 容器（找碎片化）
SELECT anchor_table_schema                             AS table_schema,
       anchor_table_name                               AS table_name,
       round(SUM(used_bytes)/1024^3, 2)                AS gb,
       SUM(ros_count)                                  AS ros,
       COUNT(*)                                        AS projections,
       round(SUM(used_bytes)/NULLIF(SUM(ros_count),0)/1048576, 1) AS mb_per_ros
FROM   v_monitor.projection_storage
WHERE  projection_schema NOT IN ('v_catalog','v_monitor','v_internal')
GROUP  BY 1, 2
ORDER  BY 3 DESC
LIMIT  20;
```

```sql
-- 碎片排行：容器數多、且多數 < 1 MB 的投影
SELECT schema_name, projection_name,
       COUNT(*)                                                  AS ros,
       SUM(CASE WHEN used_bytes < 1048576 THEN 1 ELSE 0 END)      AS tiny,
       round(SUM(used_bytes)/1048576, 1)                          AS mb
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
GROUP  BY 1, 2
HAVING COUNT(*) > 20
ORDER  BY 4 DESC
LIMIT  20;
```

實測前三名（`tiny` = 小於 1 MB 的容器數）：

| 投影 | 容器數 | 其中 < 1 MB | 合計 |
|------|-------:|------------:|-----:|
| `schema_c.t1_super` | 267 | 255 | 131 MB |
| `schema_d.t1_super` | 46 | 46 | 近 0（空投影） |
| `schema_a.t4_super` | 42 | 42 | 3 MB |

第二、三名值得單獨說：**46 個容器、42 個容器，合計幾乎 0 位元組**。這是空投影（建了但沒資料，或資料已被刪光），mergeout 沒有東西可合併，但它們仍然進 `storage_containers` 統計。所以光看「容器數排行」會被這種投影污染 — 這正是必須同時看位元組的原因。

---

## 四、164 張 0 bytes 的表：審計裡最容易拿分的一段

409 張使用者表裡，**164 張（40.1%）佔用 0 bytes**。它們不佔空間，但會：

- 增加 catalog 負擔與 `v_catalog` 查詢時間
- 讓「這張表還有沒有在用」變得不確定
- 在 `DROP SCHEMA ... CASCADE` 或遷移時變成地雷

查詢方式（`NOT EXISTS` 版本比 `SUM(used_bytes)=0` 可靠，因為完全沒有投影的表不會出現在 `projection_storage`）：

```sql
SELECT t.table_schema, t.table_name
FROM   v_catalog.tables t
WHERE  t.table_schema NOT IN ('v_catalog','v_monitor','v_internal','v_func','v_txtindex')
  AND  NOT EXISTS (
         SELECT 1 FROM v_monitor.projection_storage ps
         WHERE  ps.anchor_table_schema = t.table_schema
           AND  ps.anchor_table_name   = t.table_name
           AND  ps.used_bytes > 0)
ORDER  BY 1, 2;
```

**但「0 bytes」不等於「可以 drop」。** 空表可能是：

1. 當期還沒載入資料的分割表（每月一張的那種）
2. 應用程式寫入失敗後留下的殼
3. 有人故意保留的 schema 樣板
4. 有 view／外部程序引用，只是暫時無資料

我不用 `query_requests` 的文字比對當唯一證據 — 它只保留最近的請求，且 `request` 欄位不保證含表名，容易誤殺。建議流程是：

| 步驟 | 動作 | 目的 |
|------|------|------|
| 1 | 匯出 `CREATE TABLE` DDL 與相關 view 定義 | 保留重建能力 |
| 2 | `ALTER TABLE ... RENAME TO ..._deprecated_YYYYMM` | 不刪資料，觀察應用是否報錯 |
| 3 | 觀察 2–4 週（含月結、季結） | 抓到低頻使用者 |
| 4 | 無異常才 `DROP TABLE` | 這一步不可逆 |

第 2 步是關鍵：**rename 是可逆的，drop 不是。** 這台機器上那 164 張我一張都沒動 — 審計的產出是清單與建議，不是執行。

---

## 五、288,920 列標記刪除：DV 存在不等於空間浪費

實測 `v_monitor.delete_vectors`：**310 筆 DV、涵蓋 19 個投影、合計 288,920 列**。

分布極度集中 — 兩張表的 super projection 就佔 94.5%：

| 投影（已遮蔽） | DV 筆數 | 標記刪除列數 | DV 自身大小 |
|----------------|--------:|-------------:|------------:|
| `schema_a.t1_super` | 111 | 143,151 | 0.4 MB |
| `schema_a.t5_super` | 4 | 129,733 | 0.3 MB |
| `schema_a.t6_super` | 3 | 6,079 | < 0.1 MB |
| `schema_a.t7_super` | 1 | 3,042 | < 0.1 MB |
| `schema_c.t1_super` | 156 | 1,864 | < 0.1 MB |

```sql
-- 未 purge 的刪除：先看集中在哪裡
SELECT schema_name, projection_name,
       COUNT(*)                          AS dv_entries,
       SUM(deleted_row_count)            AS deleted_rows,
       round(SUM(used_bytes)/1048576, 1) AS dv_mb
FROM   v_monitor.delete_vectors
GROUP  BY 1, 2
ORDER  BY 4 DESC
LIMIT  20;
```

接著是最容易被誤讀的一個數字：

```sql
-- 帶有刪除標記的容器，總共佔多少位元組
SELECT COUNT(*)                             AS containers_with_dv,
       round(SUM(used_bytes)/1024^3, 2)     AS gb
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
  AND  deleted_row_count > 0;
```

實測結果：**243 個容器、43.38 GB**。佔全庫 63.6%。

這個數字**不能**解讀成「有 43 GB 可以回收」。它的意思是「這 43.38 GB 的容器裡含有刪除標記」，容器內絕大多數列還是活的。真正的可回收量 = purge 後容器重寫前後的 `used_bytes` 差值，而那個差值**只能在執行 purge 時量測** — 本次審計沒有執行（屬變更動作），所以這裡標「未量測」。

那什麼時候會清？

| 觸發 | 行為 | 可控性 |
|------|------|--------|
| Mergeout | 重寫容器時順便套用 DV、丟棄已刪列 | 背景，時間不可控 |
| `PURGE TABLE` | 主動重寫受影響容器，強制清掉刪除列 | 你決定何時跑 |
| 刪除整個 partition | 直接丟掉該 partition 的容器 | 最快、最乾淨 |

判斷是否需要主動 purge，看的是**刪除列佔比**而不是 DV 筆數。`schema_a.t1_super` 有 111 筆 DV，但容器數 57、合計 41.19 GB，`deleted_row_count / total_row_count` 只有 0.63% — 這種量 mergeout 會自己收掉，不需要人工介入。全庫佔比最高的其實是另一張：`schema_a.t5_super` 的 182.8 萬列裡標記刪除 12.97 萬列，**7.10%** — 這才是該排 purge 的對象，而且它是 4 筆 DV 而非 111 筆。**用 DV 筆數排序會挑錯人。** 反之若某投影刪除列佔比長期 > 10%、或磁碟吃緊需要立刻回收，才排 `PURGE TABLE`（IO 密集，放離峰）。

---

## 六、回收動作對照表

審計完的下一步是「要不要動」。把五個選項攤開比較，注意最後一欄 — 只有重建投影是可回滾的：

| 動作 | 回收什麼 | 影響／鎖 | 可回滾 | 適用時機 |
|------|----------|----------|--------|----------|
| `DROP TABLE` | 資料 + 所有投影 | 短 DDL 鎖；依賴的 view 失效 | ❌ 只能靠備份 | 確認無用後 |
| `TRUNCATE TABLE` | 全部資料列 | 快，直接丟容器，保留表結構 | ❌ | 保留殼、清空資料 |
| `PURGE TABLE` | 已刪列空間 + DV | 重寫容器，IO 重、時間與資料量成正比 | 資料本來就已刪，無資料風險 | 離峰 |
| 重建投影 | 碎片、排序錯亂 | `CREATE PROJECTION` + 背景 refresh，需額外磁碟 | ✅ 舊投影可保留 | 碎片化且無其他解法 |
| Drop partition | 整個分割的容器 | 最快 | ❌ | 有 partition 設計的表 |

三條實務原則：

1. **能 drop partition 就不要 purge。** partition drop 是丟容器，成本 O(1)；purge 是重寫，成本與資料量成正比。
2. **碎片化的表優先重建投影，而不是狂跑 mergeout。** mergeout 有吞吐上限，267 個碎容器靠它收要很久；重建投影是一次性重排。
3. **併發視角**：purge 與重建投影都會跟線上查詢搶 IO。單節點環境沒有其他地方可以逃，務必排離峰並觀察 `v_monitor.resource_acquisitions`。

---

## 七、把審計變成一頁 SQL

每次磁碟告警都重寫查詢很浪費。這支 SQL 一次輸出五個指標，可以直接貼進 Grafana 或 cron 化的檢查腳本（純唯讀）：

```sql
WITH sz AS (
  SELECT t.table_schema, t.table_name,
         COALESCE(SUM(ps.used_bytes), 0) AS bytes
  FROM   v_catalog.tables t
  LEFT   JOIN v_monitor.projection_storage ps
         ON ps.anchor_table_schema = t.table_schema
        AND ps.anchor_table_name   = t.table_name
  WHERE  t.table_schema NOT IN ('v_catalog','v_monitor','v_internal','v_func','v_txtindex')
  GROUP  BY 1, 2
)
SELECT 'total_gb'            AS metric,
       round(SUM(bytes)/1024^3, 2)::varchar AS value
FROM   sz
UNION ALL
SELECT 'empty_tables', COUNT(*)::varchar FROM sz WHERE bytes = 0
UNION ALL
SELECT 'containers_total', COUNT(*)::varchar
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
UNION ALL
SELECT 'containers_lt_1mb', COUNT(*)::varchar
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
  AND  used_bytes < 1048576
UNION ALL
SELECT 'gb_in_containers_with_dv', round(SUM(used_bytes)/1024^3, 2)::varchar
FROM   v_monitor.storage_containers
WHERE  schema_name NOT IN ('v_catalog','v_monitor','v_internal')
  AND  deleted_row_count > 0;
```

門檻建議（依本次實測值校準，非通用標準）：

| 指標 | 本次實測 | 告警門檻 | 判讀 |
|------|---------:|----------|------|
| `total_gb` | 68.20 | 磁碟使用率 > 80% | 容量問題，走第六節 |
| `empty_tables` | 164（40%） | > 30% | 清理候選，走第四節 |
| `containers_lt_1mb` | 1,254 | > 2,000 或單投影 > 500 | 碎片／pushback 風險 |
| 單表 MB / 容器 | 最低 0.49 | < 5 且該表 > 1 GB | 真碎片，考慮重建 |
| `gb_in_containers_with_dv` | 43.38 | 成長斜率異常 | 刪除流程設計問題 |

最後一列是斜率而非絕對值：這台機器 63.6% 的位元組躺在帶 DV 的容器裡，數字嚇人但可能完全正常（大表長期有零星刪除）。**看它一週內漲多快，比看它現在多大有意義。**

---

## 八、實測環境與可重現性

| 項目 | 值 |
|------|-----|
| Vertica 版本 | 25.4.0-0 |
| 節點數 | 1（單節點） |
| 資料庫 | 測試庫（表名已遮蔽） |
| 使用者 schema / 表 / 投影 | 27 / 409 / 394 |
| `used_bytes` / `disk_size` | 68.20 GB / 68.32 GB |
| ROS 容器數 | 1,524（平均 45.8 MB） |
| 量測時間 | 2026-11-04 |

三點說明：

1. **所有數字都是單節點實測**，多節點環境的 `storage_containers` 會按 `node_name` 展開，總量與碎片分布會不同，但判讀方法不變。
2. **表名一律改寫**（`schema_a.t1` 這種形式），數值保留原樣。讀者照 SQL 跑自己的庫即可得到自己的數字。
3. **本次未執行任何變更**：沒有 PURGE、沒有 DROP、沒有重建投影。因此「可回收多少 GB」這個問題，本文的答案是「未量測」，量測方法在第五節。

---

*本文所有數字取自 Vertica 25.4.0-0 單節點實測（2026-11-04），SQL 均為唯讀查詢，可直接在生產環境執行。Mergeout／STRATA 的內部機制請參考 [Tuple Mover 最佳實踐]({{ site.baseurl }}/2026/06/vertica-tuple-mover-best-practice/)；刪除語法的選擇請參考 [刪除資料最佳實踐]({{ site.baseurl }}/2026/06/vertica-delete-data-best-practice/)。*

**參考資料**（Vertica 25.4.x 官方文件）：`PURGE` 陳述式、`PURGE_TABLE()` 函式、`TRUNCATE` 陳述式、Working with Native Tables → Removing Table Data、系統表 `v_monitor.storage_containers` / `v_monitor.projection_storage` / `v_monitor.delete_vectors`。
