---
layout: post_en
title: "Vertica Log Analysis for Root Cause Investigation — A Practical Guide"
date: 2026-06-21 04:00:00 +0800
categories: vertica log-analysis troubleshooting
tags: [vertica, log-analysis, Data-Collector, query-events, vertica-log, RCA, troubleshooting, performance]
description: "Systematic Vertica log analysis workflow — from Data Collector retention management, monitoring query optimization, QUERY_EVENTS comparison, to vertica.log severity analysis with cross-validation."
---

When Vertica performance issues occur during specific time windows with unknown causes, systematic log analysis is key to finding the root cause. Based on Moshe Goldberg's LinkedIn article on Vertica Log Analysis, this guide provides actionable steps updated for **Vertica 25.x**.

<!--more-->

---

## Analysis Workflow

```
1. Data Collector Retention Check
   └── Does history cover the problem window?
         ↓
2. Monitoring Query Noise Elimination
   └── Are heavy monitoring queries adding background pressure?
         ↓
3. QUERY_EVENTS Comparison
   └── What event types differ between healthy and degraded periods?
         ↓
4. vertica.log Severity Analysis
   └── Layer by severity, pinpoint the exact time window
         ↓
5. Cross-Validation
   └── Verify against raw log files
```

---

## 1. Data Collector Retention

### Check if Enabled

```sql
=> SELECT parameter_name, current_value
   FROM configuration_parameters WHERE parameter_name = 'EnableDataCollector';
```

### Check Disk Space

```sql
=> SELECT storage_path, disk_space_free_percent
   FROM v_monitor.disk_storage WHERE storage_path ILIKE '%Catalog%';
```

### Check Retention Policy

```sql
=> SELECT DISTINCT table_name, component, description
   FROM data_collector WHERE table_name ILIKE '%projections_used%';

=> SELECT get_data_collector_policy('ProjectionsUsed');
```

### Increase Retention

```sql
=> SELECT set_data_collector_policy('ProjectionsUsed', 2000, 150000);
=> SELECT set_data_collector_time_policy('ProjectionsUsed', '6 days'::INTERVAL);
```

### Estimate DC Storage

```sql
=> SELECT node_name,
          SUM(disk_size_kb) / (1024^2) AS disk_size_gb,
          SUM(current_disk_bytes) / (1024^3) AS current_disk_gb
   FROM v_monitor.data_collector GROUP BY node_name ORDER BY node_name;
```

### Global Time Policy

```sql
=> SELECT set_data_collector_time_policy('3 days'::INTERVAL);
```

> Always check this first. If history is too short, subsequent analysis will be incomplete.

---

## 2. Monitoring Query Noise

Identify repeated monitoring-style queries hitting system tables:

```sql
=> SELECT /*+ LABEL('monitoring_query_finder') */
       qr.user_name,
       COUNT(*) AS query_count,
       MAX(qr.request_duration_ms) AS max_duration_ms,
       SUM(qr.request_duration_ms) AS total_duration_ms
   FROM v_monitor.query_requests qr
   WHERE qr.start_timestamp >= NOW() - INTERVAL '7 days'
   GROUP BY 1 ORDER BY total_duration_ms DESC;
```

---

## 3. QUERY_EVENTS Comparison

Compare event types between healthy and degraded periods:

```sql
=> SELECT event_type, event_description,
          'HEALTHY' AS period, COUNT(*) AS event_count
   FROM v_monitor.query_events
   WHERE event_timestamp BETWEEN '2026-06-01' AND '2026-06-15'
   GROUP BY 1, 2
   UNION ALL
   SELECT event_type, event_description,
          'DEGRADED' AS period, COUNT(*) AS event_count
   FROM v_monitor.query_events
   WHERE event_timestamp BETWEEN '2026-06-15' AND '2026-06-20'
   GROUP BY 1, 2;
```

**Key observations**:
- New event types appearing in degraded period?
- More spill-related events?
- Lock-related events concentrated?
- Plan conversion changes?

---

## 4. vertica.log Analysis

### Log Location

```bash
$ admintools -t list_db -d testdb
```

### Severity Levels

```
<LOG>, <INFO>, <NOTICE>, <WARNING>, <ERROR>, <ROLLBACK>, <FATAL>, <PANIC>
```

### Load Log into Table

```sql
=> CREATE TABLE public.vertica_log (
    dtext VARCHAR(4000), thread_id VARCHAR(32), thread_name VARCHAR(64),
    dtime TIMESTAMP, component VARCHAR(64), level VARCHAR(16)
);
```

### Compare Good Day vs Bad Day

```sql
=> SELECT level, COUNT(*) AS cnt
   FROM public.vertica_log WHERE dtime::DATE = '2026-06-15'
   GROUP BY level;
```

### Hourly Breakdown (Most Powerful)

```sql
=> SELECT DATE_TRUNC('hour', dtime) AS hour, level, COUNT(*) AS cnt
   FROM public.vertica_log WHERE dtime::DATE = '2026-06-15'
   GROUP BY 1, 2 ORDER BY hour, level;
```

> Transforms "performance was bad that afternoon" into concrete "ERROR spike at 17:00-18:00."

### Raw Log Validation

```bash
#!/bin/bash
LOG_DIR=$(admintools -t list_db -d testdb | grep "Catalog" | awk '{print $2}')
LOG_FILE="$LOG_DIR/v_testdb_node0001_catalog/vertica.log"
grep "2026-06-15 17:" "$LOG_FILE" | grep '<ERROR>'
```

---

## 5. Cross-Validation Workflow

| Step | Method | Purpose |
|------|--------|---------|
| 1 | Data Collector policy check | Ensure sufficient history |
| 2 | Monitoring query check | Eliminate monitoring noise |
| 3 | QUERY_EVENTS comparison | Find optimizer/execution changes |
| 4 | vertica.log severity analysis | Pinpoint exact time windows |
| 5 | Raw log validation | Confirm no parser errors |

---

## System Tables (25.x)

| Table | 25.x Location | Status |
|-------|---------------|--------|
| `data_collector` | Bare table | ✅ |
| `disk_storage` | `v_monitor.disk_storage` | ✅ |
| `query_events` | `v_monitor.query_events` | ✅ |
| `query_requests` | `v_monitor.query_requests` | ✅ |

---

*Based on Moshe Goldberg's LinkedIn article "Vertica Log Analysis." SQL updated to Vertica 25.4.0-0.*
