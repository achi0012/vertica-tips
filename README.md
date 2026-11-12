# Vertica Tips 🐧

Vertica 資料庫技術筆記與最佳實踐。

## 關於

這個專案使用 GitHub Pages + Jekyll 建置，發表 Vertica 資料庫相關的技術文章，
涵蓋資料載入、效能調優、監控診斷、SQL 技巧等主題。

## 文章列表

目前共 26 篇中文文章，均有對應英文版。

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
