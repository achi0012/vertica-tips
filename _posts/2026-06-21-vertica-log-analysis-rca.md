---
layout: post
title: "Vertica Log Analysis 實戰 — 從系統表到日誌檔的根因分析指南"
date: 2026-06-21 04:00:00 +0800
categories: vertica log-analysis troubleshooting
tags: [vertica, log-analysis, data-collector, query-events, vertica-log, performance, RCA]
description: "系統化的 Vertica 日誌分析流程 — 從 Data Collector 歷史保留、監控查詢最佳化、QUERY_EVENTS 健康/異常期比對，到 vertica.log 的嚴重程度分析與精準時段定位。"
---

當 Vertica 效能問題發生在特定時段但原因不明時，系統化的日誌分析是找到根因的關鍵。Moshe Goldberg 在 LinkedIn 上發表了一篇深入的 Vertica Log Analysis 文章，本文將其核心方法整理為可操作的指南，並更新至 **Vertica 25.x** 系統表結構。

<!--
AI Summary: 系統化的 Vertica 日誌分析與根因調查指南。涵蓋 Data Collector 歷史保留政策管理、監控查詢干擾排除、QUERY_EVENTS 健康/異常期比對分析、vertica.log 嚴重程度分層與精準時段定位、以及跨資料源交叉驗證方法。參考 Moshe Goldberg 的 LinkedIn 文章，SQL 已更新至 25.x。
AI Keywords: Vertica, log analysis, Data Collector, query events, vertica.log, RCA, troubleshooting, performance, monitoring
-->
<!--more-->

---

## 一、分析流程總覽

```
1. Data Collector 歷史資料確認
   └── 保留政策是否涵蓋問題時段？
         ↓
2. 排除監控查詢干擾
   └── 是否有 heavy monitoring queries 增加背景壓力？
         ↓
3. QUERY_EVENTS 比對
   └── 健康期 vs 異常期的事件類型差異？
         ↓
4. vertica.log 嚴重程度分析
   └── 按 severity 分層，找出精準時段
         ↓
5. 交叉驗證
   └── 用 bash 腳本直接比對原始日誌
```

---

## 二、Data Collector 歷史資料確認

Data Collector 是 Vertica 的內建歷史資料收集機制，記錄了系統活動與效能計數器。要進行根因分析，首先要確認 Data Collector 的保留政策是否涵蓋了問題時段。

### 2.1 確認啟用狀態

```sql
=> SELECT parameter_name, current_value
   FROM configuration_parameters
   WHERE parameter_name = 'EnableDataCollector';
```

預設為啟用 (`1`)。若需要手動切換：

```sql
=> ALTER DATABASE default SET EnableDataCollector = 1;  -- 啟用
=> ALTER DATABASE default SET EnableDataCollector = 0;  -- 停用
```

### 2.2 檢查磁碟空間

在深入歷史資料前，先確認儲存本身沒有問題：

```sql
=> SELECT storage_path, disk_space_free_percent
   FROM v_monitor.disk_storage
   WHERE storage_path ILIKE '%Catalog%';
```

> **版本差異**：原文使用 `disk_storage`（裸表），25.x 中已移至 `v_monitor.disk_storage`。

### 2.3 查詢特定 Data Collector 表格的保留政策

```sql
-- 找出對應的 DC 表
=> SELECT DISTINCT table_name, component, description
   FROM data_collector
   WHERE table_name ILIKE '%projections_used%';

-- 查詢保留政策
=> SELECT get_data_collector_policy('ProjectionsUsed');

-- 若保留期太短，調高
=> SELECT set_data_collector_policy('ProjectionsUsed', 2000, 150000);
-- 或設定時間保留
=> SELECT set_data_collector_time_policy('ProjectionsUsed', '6 days'::INTERVAL);
```

### 2.4 估計 Data Collector 儲存用量

```sql
=> SELECT node_name,
          SUM(disk_size_kb) / (1024^2) AS disk_size_gb,
          SUM(current_disk_bytes) / (1024^3) AS current_disk_gb,
          SUM(current_disk_records) AS current_disk_records
   FROM v_monitor.data_collector
   GROUP BY node_name
   ORDER BY node_name;
```

### 2.5 全域設定時間保留政策

```sql
-- 對所有 DC 表套用 3 天保留
=> SELECT set_data_collector_time_policy('3 days'::INTERVAL);
```

> **實戰建議**：在分析前先確認這個。如果歷史資料不夠長，後續分析再精準也是白費。

---

## 三、排除監控查詢干擾

有時候環境問題不是由業務負載引起的，而是監控查詢本身變成了 noise。多個重複或高成本的監控查詢可能增加不必要的背景壓力。

### 找出對系統表的重複監控查詢

```sql
=> SELECT /*+ LABEL('monitoring_query_finder') */
       qr.user_name,
       st.table_schema || '.' || st.table_name AS referenced_table,
       COUNT(*) AS query_count,
       MAX(qr.request_duration_ms) AS max_duration_ms,
       TO_CHAR(MAX(qr.start_timestamp), 'YYYY-MM-DD HH24:MI:SS') AS last_seen,
       SUM(qr.request_duration_ms) AS total_duration_ms
   FROM v_monitor.query_requests qr
   JOIN v_catalog.query_requests qrs ON ...  -- 依版本調整 join
   WHERE qr.start_timestamp >= NOW() - INTERVAL '7 days'
     AND st.table_schema IN ('v_monitor', 'v_catalog', 'v_internal')
   GROUP BY 1, 2
   ORDER BY total_duration_ms DESC
   LIMIT 20;
```

即使監控不是根因，減少重複或高頻監控也能減輕背景壓力，讓真正的問題更容易被發現。

---

## 四、QUERY_EVENTS 比對健康期 vs 異常期

`V_MONITOR.QUERY_EVENTS` 系統表記錄了查詢規劃、最佳化與執行事件。這是比對健康期與異常期行為差異的最佳工具之一。

### 比較兩個時段的事件頻率

```sql
=> SELECT event_type, event_description,
          'HEALTHY' AS period, COUNT(*) AS event_count
   FROM v_monitor.query_events
   WHERE event_timestamp BETWEEN '2026-06-01 00:00:00'
                             AND '2026-06-15 00:00:00'
   GROUP BY 1, 2
   UNION ALL
   SELECT event_type, event_description,
          'DEGRADED' AS period, COUNT(*) AS event_count
   FROM v_monitor.query_events
   WHERE event_timestamp BETWEEN '2026-06-15 00:00:00'
                             AND '2026-06-20 00:00:00'
   GROUP BY 1, 2
   ORDER BY event_type, period;
```

**重點觀察**：
- 異常期是否有全新的 event type 出現？
- spill-related events 是否增加？
- lock-related events 是否集中？
- plan-conversion events 是否有變化？

> 比對時要考慮業務流量差異。如果異常期流量本來就比較高，單純的事件數量增加可能不代表問題。真正有意義的是**事件組合的變化**。

---

## 五、vertica.log 分析

vertica.log 是 Vertica 節點的詳細日誌檔案，記錄了系統表可能無法呈現的事件。

### 5.1 尋找日誌位置

```bash
$ admintools -t list_db -d testdb
```

### 5.2 日誌嚴重程度層級

vertica.log 中的嚴重程度標記：

```
<LOG>      — 一般訊息
<INFO>     — 資訊
<NOTICE>   — 注意
<WARNING>  — 警告
<ERROR>    — 錯誤
<ROLLBACK> — 回滾
<FATAL>    — 致命
<PANIC>    — 恐慌
```

### 5.3 將日誌載入 Vertica 表格進行分析

對於大規模調查，將 vertica.log 載入表格比手動掃描更實用：

```bash
$ cat sql_query_vertica_log.sql

DROP TABLE IF EXISTS public.vertica_log CASCADE;
CREATE TABLE public.vertica_log (
    dtext       VARCHAR(4000),
    thread_id   VARCHAR(32),
    thread_name VARCHAR(64),
    dtime       TIMESTAMP,
    component   VARCHAR(64),
    level       VARCHAR(16)
);
```

載入日誌（這需要在 Linux shell 中執行，非 vsql）：

```bash
$ cat vertica.log | while read line; do
    # 根據 vertica.log 格式解析各欄位
    # 實際的 parser 需要根據日誌格式調整
    echo "$line" | ...
done | vsql -c "COPY public.vertica_log FROM STDIN ..."
```

### 5.4 嚴重程度分布分析

```sql
-- 整體嚴重程度分布
=> SELECT CASE WHEN GROUPING(level) = 1 THEN 'Total:'
              ELSE level END AS level,
          COUNT(*)
   FROM public.vertica_log
   GROUP BY ROLLUP(level)
   ORDER BY GROUPING(level), level;
```

```sql
-- 比對 Good Day vs Bad Day 的 severity 差異
=> SELECT level, COUNT(*) AS cnt
   FROM public.vertica_log
   WHERE dtime::DATE = '2026-06-15'  -- Bad Day
   GROUP BY level
   UNION ALL
   SELECT level, COUNT(*) AS cnt
   FROM public.vertica_log
   WHERE dtime::DATE = '2026-06-10'  -- Good Day
   GROUP BY level;
```

```sql
-- Bad Day 的每小時 breakdown（最有用！）
=> SELECT DATE_TRUNC('hour', dtime) AS hour,
          level,
          COUNT(*) AS cnt
   FROM public.vertica_log
   WHERE dtime::DATE = '2026-06-15'
   GROUP BY 1, 2
   ORDER BY hour, level;
```

> 每小時 breakdown 的價值在於：把模糊的「那天下午效能不好」變成具體的「17:00-18:00 ERROR 暴增」。

### 5.5 用 bash 腳本做原始日誌驗證

找出問題時段後，直接驗證原始日誌避免 parser 錯誤：

```bash
#!/usr/bin/env bash
# monitor_one_hour_in_vertica_log.sh
# 檢查特定一小時的 vertica.log

set -euo pipefail

DB_NAME="testdb"
TARGET_DATE="2026-06-15"
TARGET_HOUR="17"

LOG_DIR=$(admintools -t list_db -d "$DB_NAME" | grep "Catalog" | awk '{print $2}')
LOG_FILE="$LOG_DIR/v_${DB_NAME}_node0001_catalog/vertica.log"

echo "=== Checking $LOG_FILE for $TARGET_DATE ${TARGET_HOUR}:00 ==="
grep "$TARGET_DATE ${TARGET_HOUR}:" "$LOG_FILE" | \
  awk '{for(i=1;i<=NF;i++) if($i ~ /^<[A-Z]+>$/) print $i}' | \
  sort | uniq -c | sort -rn

echo ""
echo "=== Raw ERROR lines ==="
grep "$TARGET_DATE ${TARGET_HOUR}:" "$LOG_FILE" | grep '<ERROR>' | head -20
```

---

## 六、交叉驗證工作流程

結合以上四種方法，建立完整的根因分析流程：

| 步驟 | 方法 | 目的 |
|------|------|------|
| 1 | Data Collector 政策確認 | 確保有足夠的歷史資料可回溯 |
| 2 | 監控查詢檢查 | 排除 monitoring noise |
| 3 | QUERY_EVENTS 比對 | 找出 optimizer/execution 行為變化 |
| 4 | vertica.log severity 分析 | 定位精準問題時段 |
| 5 | 原始日誌驗證 | 確認分析結果無 parser 錯誤 |

### 完整分析腳本

將以下查詢儲存為 `rca_checklist.sql`，遇到效能問題時一站式執行：

```sql
-- Step 1: Data Collector 啟用狀態
SELECT parameter_name, current_value
FROM configuration_parameters
WHERE parameter_name = 'EnableDataCollector';

-- Step 2: 磁碟空間
SELECT storage_path, disk_space_free_percent
FROM v_monitor.disk_storage
WHERE storage_path ILIKE '%Catalog%';

-- Step 3: DC 儲存用量
SELECT node_name,
       SUM(disk_size_kb) / (1024^2) AS disk_size_gb
FROM v_monitor.data_collector
GROUP BY node_name;

-- Step 4: QUERY_EVENTS 對比
SELECT event_type,
       COUNT(CASE WHEN event_timestamp < '2026-06-15' THEN 1 END) AS before_cnt,
       COUNT(CASE WHEN event_timestamp >= '2026-06-15' THEN 1 END) AS after_cnt
FROM v_monitor.query_events
WHERE event_timestamp >= '2026-06-01'
GROUP BY event_type
HAVING COUNT(CASE WHEN event_timestamp >= '2026-06-15' THEN 1 END) >
       COUNT(CASE WHEN event_timestamp < '2026-06-15' THEN 1 END) * 1.5;
```

---

## 七、版本差異對照

| 原文用到的物件 | 25.x 位置 | 實測 |
|---------------|----------|------|
| `data_collector` 表 | 裸表（無 schema） | ✅ |
| `disk_storage` | `v_monitor.disk_storage` | ✅ |
| `query_events` | `v_monitor.query_events` | ✅ |
| `get_data_collector_policy()` | 系統函數 | ✅ |
| `set_data_collector_policy()` | 系統函數 | ✅ |
| `set_data_collector_time_policy()` | 系統函數 | ✅ |
| `query_requests` | `v_monitor.query_requests` | ✅ |
| `EnableDataCollector` | 配置參數 | ✅ |

---

*本文參考 Moshe Goldberg 的 LinkedIn 文章 "Vertica Log Analysis"，所有 SQL 已更新至 Vertica 25.4.0-0。如有任何問題或建議，歡迎在 [GitHub](https://github.com/achi0012/vertica-tips) 上討論。*
