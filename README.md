# Vertica Tips 🐧

Vertica 資料庫技術筆記與最佳實踐。

## 關於

這個專案使用 GitHub Pages + Jekyll 建置，發表 Vertica 資料庫相關的技術文章，
涵蓋資料載入、效能調優、監控診斷、SQL 技巧等主題。

## 文章列表

目前共 41 篇中文文章，均有對應英文版。

### 資料載入與外部表

- [Vertica Data Load 最佳實踐 — COPY 命令深度解析 (25.x)](/_posts/2026-06-20-vertica-data-load-best-practice.md)
- [Vertica Smart Scanning 加速外部表查詢 — ObjectStoreGlobStrategy 深度解析](/_posts/2026-06-20-vertica-smart-scanning-external-tables.md)
- [Vertica + Apache Iceberg 實戰指南 — 打造高效 Data Lakehouse](/_posts/2026-06-20-vertica-iceberg-lakehouse.md)

### 效能調優與資源池

- [Vertica 資料庫效能變慢怎麼辦？— 13 步驟診斷檢查清單 (25.x 更新版)](/_posts/2026-06-20-vertica-slow-performance-checklist.md)
- [Vertica 資源池管理最佳實踐 — 從分類到串聯的完整調校指南](/_posts/2026-06-21-vertica-resource-pool-best-practice.md)
- [Vertica Tuple Mover 最佳實踐 — Moveout、Mergeout 與 STRATA 演算法完整指南](/_posts/2026-06-21-vertica-tuple-mover-best-practice.md)
- [Vertica v26.2 NUMA Aware 效能架構 — 單伺服器跑出 3 倍吞吐量](/_posts/2026-06-21-vertica-v26.2-numa-performance.md)

### 叢集、高可用與災難復原

- [Vertica 節點故障恢復實戰指南 — Node Down Recovery (25.x)](/_posts/2026-06-20-vertica-node-recovery-best-practice.md)
- [Vertica 集群重新平衡實戰指南 — Cluster Rebalancing (25.x)](/_posts/2026-06-20-vertica-rebalancing-best-practice.md)
- [Vertica 資料庫崩潰後啟動失敗 — ASR 日期異常的完整復原指南](/_posts/2026-06-21-vertica-asr-recovery.md)

### 監控、日誌與自動化

- [Vertica Health Watchdog 深度解析 — 叢集健康監控的自動守護神](/_posts/2026-06-20-vertica-health-watchdog.md)
- [Vertica Log Analysis 實戰 — 從系統表到日誌檔的根因分析指南](/_posts/2026-06-21-vertica-log-analysis-rca.md)
- [Vertica ML 實戰：自我監控與智慧調優 — 25.4.0-0 完全驗證](/_posts/2026-06-21-vertica-ml-self-optimization.md)

### 資料管理與記憶體

- [Vertica 刪除資料最佳實踐 — DELETE、TRUNCATE、Partition 完整指南](/_posts/2026-06-21-vertica-delete-data-best-practice.md)
- [理解 Linux OOM Kill 在 Vertica 叢集中的成因與預防 — 完整指南](/_posts/2026-06-21-vertica-oom-prevention.md)
- [Vertica 空間審計實戰 — 別數 ROS 容器，算位元組（25.4 實測）](/_posts/2026-11-04-vertica-space-audit.md)

### 最新文章

- [Vertica 25.4 VectorOps 實戰：用 ARRAY 做向量相似度搜尋](/_posts/2026-11-05-vertica-vectorops-array-similarity-search.md)
- [Vertica 統計資訊管理：ANALYZE_STATISTICS 與查詢計畫為什麼選錯](/_posts/2026-11-06-vertica-statistics-query-plan.md)
- [Vertica 投影設計實戰：Segmentation、Sort Order、Encoding 與 ROS Pushback](/_posts/2026-11-07-vertica-projection-design-best-practice.md)
- [Vertica 分區表設計與 Partition Pruning 實戰](/_posts/2026-11-08-vertica-partition-design-pruning.md)
- [Vertica 時序分析實戰：TIMESERIES 插值、補洞與事件視窗](/_posts/2026-11-09-vertica-timeseries-analysis.md)
- [Vertica 空間分析實戰：Geospatial 函數與空間索引](/_posts/2026-11-10-vertica-geospatial-analysis.md)
- [Vertica 複雜型別實戰：ARRAY、SET、ROW 與 JSON 半結構化資料](/_posts/2026-11-11-vertica-complex-data-types.md)
- [Vertica vbr 備份還原實戰：Enterprise 模式完整備份演練、還原程序與 EON 差異](/_posts/2026-11-12-vertica-vbr-backup-restore.md)
- [EON 架構總覽：7 個 storage location 與 4 個 shard，看懂 communal storage、depot 與 segment](/_posts/2026-11-13-vertica-eon-architecture-communal-depot-shard.md)
- [EON 的 shard 與 segmentation：為什麼資料分佈不是看 projection 的 segmentation](/_posts/2026-11-14-vertica-eon-shards-segmentation.md)
- [EON 三節點的資源池與記憶體實況：27 筆 resource pool 狀態怎麼讀](/_posts/2026-11-15-vertica-eon-resource-pools-memory.md)
- [EON 的 ROS 與 storage container：1,428 個 container 與十億筆資料的實際分佈](/_posts/2026-11-16-vertica-eon-ros-storage-containers.md)
- [EON 日常巡檢 12 支 SQL：shard 覆蓋、depot 命中率、communal 延遲一次看完](/_posts/2026-11-17-vertica-eon-monitoring-sql.md)
- [十億筆資料在 EON 上的查詢解剖：shard pruning 與 depot 命中如何決定 EXPLAIN 與實際時間](/_posts/2026-11-18-vertica-eon-billion-row-query-anatomy.md)
- [Enterprise 轉 EON 遷移檢查表：14 個上線前必須重新檢視的設定](/_posts/2026-11-19-vertica-eon-migration-checklist.md)
- [EON 的連線入口與 load balancing：三個 port 到底是不是三份資料](/_posts/2026-11-20-vertica-eon-connection-load-balancing.md)
- [EON 資料分佈的盲點：1,435 個 container、221 個 projection，卻只有 6 個有 partition 紀錄](/_posts/2026-11-21-vertica-eon-container-partition-audit.md)
- [EON 的 Tuple Mover 行為：什麼時候搬、搬到哪裡、跟 Enterprise 差在哪](/_posts/2026-11-22-vertica-eon-tuple-mover.md)
- [Vertica 權限與角色實戰：GRANT、角色繼承、schema 層級與稽核](/_posts/2026-11-23-vertica-roles-privileges-grant.md)
- [Vertica 查詢剖析實戰：從 EXPLAIN 到 PROFILE，逐節點找出慢在哪](/_posts/2026-11-24-vertica-query-profiling-deep-dive.md)
- [Vertica × Kafka 整合：vkafka、scheduler 與微批次載入的實際行為](/_posts/2026-11-25-vertica-kafka-integration-scheduler.md)
- [Vertica Python UDx 實戰：Scalar、Transform 與 Aggregate 三種函數](/_posts/2026-11-26-vertica-python-udx-development.md)

### English Versions

- [Vertica Data Load Best Practices — COPY Command Deep Dive (25.x)](/_en/2026-06-20-vertica-data-load-best-practice.md)
- [Vertica Health Watchdog Deep Dive — Automatic Cluster Monitoring (24.4+)](/_en/2026-06-20-vertica-health-watchdog.md)
- [Vertica + Apache Iceberg: Building a High-Performance Data Lakehouse](/_en/2026-06-20-vertica-iceberg-lakehouse.md)
- [Vertica Node Down Recovery Field Guide — Practical Guide (25.x)](/_en/2026-06-20-vertica-node-recovery-best-practice.md)
- [Vertica Cluster Rebalancing Field Guide (25.x)](/_en/2026-06-20-vertica-rebalancing-best-practice.md)
- [Vertica Performance Slow? — 13-Step Diagnostic Checklist (25.x Updated)](/_en/2026-06-20-vertica-slow-performance-checklist.md)
- [Vertica Smart Scanning — Faster External Table Queries with ObjectStoreGlobStrategy](/_en/2026-06-20-vertica-smart-scanning-external-tables.md)
- [Vertica Database Fails to Start After Crash — ASR Invalid Date Recovery Guide](/_en/2026-06-21-vertica-asr-recovery.md)
- [Best Practices for Deleting Data in Vertica — DELETE, TRUNCATE, Partition Guide (25.x)](/_en/2026-06-21-vertica-delete-data-best-practice.md)
- [Vertica Log Analysis for Root Cause Investigation — A Practical Guide](/_en/2026-06-21-vertica-log-analysis-rca.md)
- [Vertica ML for Self-Monitoring and Smart Tuning (25.4 Verified)](/_en/2026-06-21-vertica-ml-self-optimization.md)
- [Understanding Linux OOM Kills in Vertica Clusters — Prevention Guide](/_en/2026-06-21-vertica-oom-prevention.md)
- [Vertica Resource Pool Best Practices — From Custom Pools to Cascading (25.x)](/_en/2026-06-21-vertica-resource-pool-best-practice.md)
- [Vertica Tuple Mover Best Practices — Mergeout, STRATA & TM Pool Tuning (25.x)](/_en/2026-06-21-vertica-tuple-mover-best-practice.md)
- [Vertica v26.2 NUMA-Aware Performance — 3x Throughput on a Single Server](/_en/2026-06-21-vertica-v26.2-numa-performance.md)
- [Auditing Vertica Disk Space: Don't Count ROS Containers, Count Bytes (25.4 Measured)](/_en/2026-11-04-vertica-space-audit.md)
- [Vertica 25.4 VectorOps in Practice: Vector Similarity Search with ARRAY](/_en/2026-11-05-vertica-vectorops-array-similarity-search.md)
- [Vertica Statistics Management: Why the Optimizer Chooses the Wrong Plan](/_en/2026-11-06-vertica-statistics-query-plan.md)
- [Vertica Projection Design in Practice: Segmentation, Sort Order, Encoding, and ROS Pushback](/_en/2026-11-07-vertica-projection-design-best-practice.md)
- [Vertica Partition Table Design and Partition Pruning in Practice](/_en/2026-11-08-vertica-partition-design-pruning.md)
- [Vertica Time Series Analysis: TIMESERIES Interpolation, Gap Filling, and Event Windows](/_en/2026-11-09-vertica-timeseries-analysis.md)
- [Vertica Geospatial Analysis: Functions and Spatial Indexes in Practice](/_en/2026-11-10-vertica-geospatial-analysis.md)
- [Vertica Complex Data Types: ARRAY, SET, ROW, and JSON](/_en/2026-11-11-vertica-complex-data-types.md)
- [Vertica vbr Backup and Restore in Practice: A Full Enterprise Backup Walkthrough, the Restore Procedure, and How EON Differs](/_en/2026-11-12-vertica-vbr-backup-restore.md)
- [EON Architecture Overview: Communal Storage, Depot, and Segments Through 7 Storage Locations and 4 Shards](/_en/2026-11-13-vertica-eon-architecture-communal-depot-shard.md)
- [Shards and Segmentation in EON: Why Data Distribution Ignores Projection Segmentation](/_en/2026-11-14-vertica-eon-shards-segmentation.md)
- [Resource Pools and Memory Across Three EON Nodes: Reading 27 Rows of Pool Status](/_en/2026-11-15-vertica-eon-resource-pools-memory.md)
- [ROS and Storage Containers in EON: 1,428 Containers Across a Billion Rows](/_en/2026-11-16-vertica-eon-ros-storage-containers.md)
- [Twelve SQL Queries That Keep an EON Cluster Honest: Shard Coverage, Depot Hit Rate, and Communal Latency](/_en/2026-11-17-vertica-eon-monitoring-sql.md)
- [Anatomy of a Billion-Row Query on EON: How Shard Pruning and Depot Hits Shape EXPLAIN and Wall-Clock Time](/_en/2026-11-18-vertica-eon-billion-row-query-anatomy.md)
- [Enterprise to EON Migration Checklist: Fourteen Settings to Re-examine Before Go-Live](/_en/2026-11-19-vertica-eon-migration-checklist.md)
- [EON Connection Endpoints and Load Balancing: Are Three Ports Three Copies of Data?](/_en/2026-11-20-vertica-eon-connection-load-balancing.md)
- [A Blind Spot in EON Data Distribution: 1,435 Containers, 221 Projections, and Only Six With Partition Records](/_en/2026-11-21-vertica-eon-container-partition-audit.md)
- [Tuple Mover Behaviour in EON: When It Moves Data, Where It Moves It, and How It Differs from Enterprise](/_en/2026-11-22-vertica-eon-tuple-mover.md)
- [Vertica Privileges and Roles in Practice: GRANT, Role Inheritance, and Schema-Level Rights](/_en/2026-11-23-vertica-roles-privileges-grant.md)
- [Vertica Query Profiling in Depth: From EXPLAIN to PROFILE, Node by Node](/_en/2026-11-24-vertica-query-profiling-deep-dive.md)
- [Vertica and Kafka Integration: vKafka, the Scheduler, and Micro-batch Loading](/_en/2026-11-25-vertica-kafka-integration-scheduler.md)
- [Python UDx in Practice: Scalar, Transform, and Aggregate Functions](/_en/2026-11-26-vertica-python-udx-development.md)

## 本機開發

```bash
# 安裝相依套件
gem install bundler jekyll
bundle install

# 啟動本機預覽
bundle exec jekyll serve --livereload
```

## License

MIT
