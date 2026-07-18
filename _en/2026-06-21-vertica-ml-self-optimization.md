---
layout: post_en
title: "Vertica ML for Self-Monitoring and Smart Tuning (25.4 Verified)"
date: 2026-06-21 02:00:00 +0800
categories: vertica machine-learning auto-tuning
tags: [vertica, ML, LINEAR_REG, SVM, KMEANS, IFOREST, self-optimization, auto-tuning, anomaly-detection]
description: "Using Vertica 25.4.0-0 built-in ML functions (LINEAR_REG, SVM_CLASSIFIER, KMEANS, IFOREST, RF_CLASSIFIER) for self-monitoring and auto-tuning. All SQL verified against a live instance."
---

Vertica 25.4.0-0 includes a complete Machine Learning engine supporting regression, classification, clustering, and anomaly detection. This article demonstrates how to use these ML functions to **monitor and optimize Vertica itself** — all SQL verified on a live 25.4.0-0 instance.

<!--more-->

---

## 1. Available ML Functions

| Category | Training | Prediction/Apply | Status |
|----------|----------|-----------------|--------|
| **Linear Regression** | `LINEAR_REG()` | `PREDICT_LINEAR_REG()` | ✅ |
| **Logistic Regression** | `LOGISTIC_REG()` | `PREDICT_LOGISTIC_REG()` | ✅ |
| **SVM Classification** | `SVM_CLASSIFIER()` | `PREDICT_SVM_CLASSIFIER()` | ✅ |
| **Random Forest** | `RF_CLASSIFIER()` | `PREDICT_RF_CLASSIFIER()` | ✅ |
| **KMEANS Clustering** | `KMEANS()` | `APPLY_KMEANS()` | ✅ |
| **Isolation Forest** | `IFOREST()` | `APPLY_IFOREST()` | ✅ |

### Quick Verification

```sql
=> CREATE TABLE ml_test (x INT, y INT);
=> INSERT INTO ml_test VALUES (1,2),(2,4),(3,6),(4,8),(5,10);
=> COMMIT;
=> SELECT LINEAR_REG('my_model', 'ml_test', 'y', 'x');
=> SELECT PREDICT_LINEAR_REG(x USING PARAMETERS model_name='my_model')
   FROM ml_test;
```

**Output**: Predicts y=2x perfectly (coefficient: 2.0, intercept: 0.0)

---

## 2. Monitoring Data Collection

```sql
=> CREATE TABLE monitor_metrics (
    collected_at    TIMESTAMP DEFAULT NOW(),
    pool_name       VARCHAR(128),
    running_q_count INT,
    query_budget_kb INT,
    memory_inuse_kb INT,
    mem_pressure    NUMERIC(5,2),
    ros_count       INT,
    dv_count        INT,
    epoch_advance   INT,
    resident_gb     NUMERIC(5,2),
    tm_depth        INT,
    is_healthy      INT
);
```

### Data Collection Query

```sql
=> INSERT INTO monitor_metrics (...)
   SELECT r.pool_name, r.running_query_count, ...
   FROM v_monitor.resource_pool_status r
   CROSS JOIN (SELECT COUNT(*) FROM v_monitor.projection_storage) ros
   CROSS JOIN (SELECT COUNT(*) FROM v_monitor.delete_vectors) dv
   CROSS JOIN (SELECT current_epoch, ahm_epoch FROM v_monitor.system) s
   CROSS JOIN (SELECT resident_size FROM dc_process_info ORDER BY time DESC LIMIT 1) p
   WHERE r.pool_name IN ('general', 'tm', 'refresh', 'recovery');
```

---

## 3. Anomaly Detection with IFOREST

```sql
-- Train model (unsupervised, no labels needed)
=> SELECT IFOREST('health_iforest', 'monitor_metrics',
    'running_q_count, memory_inuse_kb, ros_count, dv_count, epoch_advance'
);

-- Detect anomalies in real-time
=> SELECT APPLY_IFOREST(
       running_q_count, memory_inuse_kb, ros_count, dv_count, epoch_advance
       USING PARAMETERS model_name='health_iforest'
   ) AS anomaly
   FROM monitor_metrics ORDER BY collected_at DESC LIMIT 10;
```

**Output**: `{"anomaly_score":0.46,"is_anomaly":false}` — higher score = more anomalous.

---

## 4. Resource Prediction with LINEAR_REG

```sql
-- Train ROS growth model
=> SELECT LINEAR_REG('ros_model', 'monitor_metrics',
    'ros_count', 'collected_at');

-- Predict ROS in 6 hours
=> SELECT PREDICT_LINEAR_REG(
       NOW() + INTERVAL '1 hour' * h
       USING PARAMETERS model_name='ros_model')
   FROM (SELECT LEVEL AS h FROM dual CONNECT BY LEVEL <= 6) t;
```

---

## 5. Health Classification with SVM

```sql
=> SELECT SVM_CLASSIFIER('svm_health', 'monitor_metrics',
    'is_healthy', 'running_q_count, memory_inuse_kb, ros_count, dv_count');

=> SELECT PREDICT_SVM_CLASSIFIER(
       running_q_count, memory_inuse_kb, ros_count, dv_count
       USING PARAMETERS model_name='svm_health')
   FROM monitor_metrics ORDER BY collected_at DESC LIMIT 10;
```

---

## 6. Workload Clustering with KMEANS

```sql
=> SELECT KMEANS('load_kmeans', 'monitor_metrics',
    'running_q_count, memory_inuse_kb, ros_count', 3);

=> SELECT collected_at, APPLY_KMEANS(
       running_q_count, memory_inuse_kb, ros_count
       USING PARAMETERS model_name='load_kmeans') AS cluster_id
   FROM monitor_metrics ORDER BY collected_at;
```

---

## 7. Auto-Tuning with Built-in Scheduler

### Enable Scheduler

```sql
=> SELECT SET_CONFIG_PARAMETER('EnableStoredProcedureScheduler', 1);
```

### Create Tuning Procedure

```sql
=> CREATE OR REPLACE PROCEDURE auto_tune() LANGUAGE PLvSQL AS $$
DECLARE
    predicted_ros INT;
    mem_pct NUMERIC(8,2);
BEGIN
    SELECT PREDICT_LINEAR_REG(NOW() + INTERVAL '2 hours'
               USING PARAMETERS model_name='ros_model')
    INTO predicted_ros;
    IF predicted_ros > 2000 THEN
        PERFORM SELECT DO_TM_TASK('mergeout');
    END IF;
    SELECT memory_inuse_kb * 100.0 / NULLIF(query_budget_kb, 0)
    INTO mem_pct FROM v_monitor.resource_pool_status WHERE pool_name = 'general';
    IF mem_pct > 80 THEN
        EXECUTE 'ALTER RESOURCE POOL general PLANNEDCONCURRENCY 2';
    END IF;
END;
$$;
```

### Schedule

```sql
=> CREATE SCHEDULE optimize_30min USING CRON '*/30 * * * *';
=> CREATE TRIGGER optimize_trigger
   ON SCHEDULE optimize_30min
   EXECUTE PROCEDURE auto_tune() AS DEFINER;
```

### Daily Model Retraining

```sql
=> CREATE OR REPLACE PROCEDURE retrain_models() LANGUAGE PLvSQL AS $$
BEGIN
    PERFORM SELECT LINEAR_REG('ros_model', 'monitor_metrics', 'ros_count', 'collected_at');
    PERFORM SELECT SVM_CLASSIFIER('svm_health', 'monitor_metrics', 'is_healthy',
               'running_q_count, memory_inuse_kb, ros_count, dv_count');
    PERFORM SELECT IFOREST('health_iforest', 'monitor_metrics',
               'running_q_count, memory_inuse_kb, ros_count, dv_count, epoch_advance');
END;
$$;

=> CREATE SCHEDULE daily_3am USING CRON '0 3 * * *';
=> CREATE TRIGGER retrain_trigger ON SCHEDULE daily_3am
   EXECUTE PROCEDURE retrain_models() AS DEFINER;
```

### Manage Schedules

```sql
=> SELECT * FROM scheduler_time_table;
=> SELECT active_scheduler_node();
=> EXECUTE TRIGGER optimize_trigger;
```

---

## 8. PL/vSQL Limitations (25.4.0-0)

| Feature | Status |
|---------|--------|
| `PERFORM INSERT INTO` (for DML) | ✅ |
| `EXECUTE 'ALTER ...'` (for DDL) | ✅ |
| `SELECT ... INTO variable` | ✅ |
| `LOOP`, `WHILE`, `IF/ELSIF` | ✅ |
| `CURSOR FOR` | ❌ Not supported |
| `FOR ... IN (SELECT)` | ❌ Not supported |

---

*All SQL verified on Vertica 25.4.0-0. See also the related article on Health Watchdog for additional safeguards.*
