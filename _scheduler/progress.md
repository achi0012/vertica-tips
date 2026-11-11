# 文章產線進度

> 自動產生，請勿手改。來源：`_scheduler/vertica-series.json`、`_scheduler/vertica-eon-series.json`。
> 更新時間：2026-11-11 21:12（Asia/Taipei）· 產生指令：`/usr/bin/python3 tools/build_progress.py`

排程：每天 05:00 產下一篇（中英文各一），完成後 commit + push；週日 20:00 另檢查官方公告。

**總進度：7/18**，剩 11 篇。

下一篇：**Vertica vbr 備份還原實戰：Enterprise 模式完整演練，與 EON 模式的關鍵差異**（vertica-vbr-backup-restore，預計 2026-11-12）


## vertica-tips-8-articles（7/8）

| # | 中文標題 | English | 日期 | 狀態 |
|---|---|---|---|---|
| 1 | [Vertica 25.4 VectorOps 實戰：用 ARRAY 做向量相似度搜尋](https://achi0012.github.io/vertica-tips/2026/11/vertica-vectorops-array-similarity-search/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-vectorops-array-similarity-search/) | 2026-11-05 | ✅ |
| 2 | [Vertica 統計資訊管理：ANALYZE_STATISTICS 與查詢計畫為什麼選錯](https://achi0012.github.io/vertica-tips/2026/11/vertica-statistics-query-plan/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-statistics-query-plan/) | 2026-11-06 | ✅ |
| 3 | [Vertica 投影設計實戰：Segmentation、Sort Order、Encoding 與 ROS Pushback](https://achi0012.github.io/vertica-tips/2026/11/vertica-projection-design-best-practice/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-projection-design-best-practice/) | 2026-11-07 | ✅ |
| 4 | [Vertica 分區表設計與 Partition Pruning 實戰](https://achi0012.github.io/vertica-tips/2026/11/vertica-partition-design-pruning/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-partition-design-pruning/) | 2026-11-08 | ✅ |
| 5 | [Vertica 時序分析實戰：TIMESERIES 插值、補洞與事件視窗](https://achi0012.github.io/vertica-tips/2026/11/vertica-timeseries-analysis/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-timeseries-analysis/) | 2026-11-09 | ✅ |
| 6 | [Vertica 空間分析實戰：Geospatial 函數與空間索引](https://achi0012.github.io/vertica-tips/2026/11/vertica-geospatial-analysis/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-geospatial-analysis/) | 2026-11-10 | ✅ |
| 7 | [Vertica 複雜型別實戰：ARRAY、SET、ROW 與 JSON 半結構化資料](https://achi0012.github.io/vertica-tips/2026/11/vertica-complex-data-types/) | [EN](https://achi0012.github.io/vertica-tips/en/2026/11/vertica-complex-data-types/) | 2026-11-11 | ✅ |
| 8 | Vertica vbr 備份還原實戰：Enterprise 模式完整演練，與 EON 模式的關鍵差異 | — | 預計 11-12 | ⬜ |

## vertica-eon-10-articles（0/10）

環境：`eondb` · 3 nodes · K-Safe=0 · v25.4.0-0 · **唯讀，不動叢集**

| # | 中文標題 | English | 日期 | 狀態 |
|---|---|---|---|---|
| 1 | EON 架構總覽：7 個 storage location 與 4 個 shard，看懂 communal storage、depot 與 segment | — | 預計 11-13 | ⬜ |
| 2 | EON 的 shard 與 segmentation：為什麼資料分佈不是看 projection 的 segmentation | — | 預計 11-14 | ⬜ |
| 3 | EON depot 解密：auto-data-depot 存了什麼、什麼時候 miss、miss 一次要付多少時間 | — | 預計 11-15 | ⬜ |
| 4 | EON 的 ROS 與 storage container：1,424 個 container 與十億筆資料的實際分佈 | — | 預計 11-16 | ⬜ |
| 5 | EON 日常巡檢 12 支 SQL：shard 覆蓋、depot 命中率、communal 延遲一次看完 | — | 預計 11-17 | ⬜ |
| 6 | 十億筆資料在 EON 上的查詢解剖：shard pruning 與 depot 命中如何決定 EXPLAIN 與實際時間 | — | 預計 11-18 | ⬜ |
| 7 | Enterprise 轉 EON 遷移檢查表：14 個上線前必須重新檢視的設定 | — | 預計 11-19 | ⬜ |
| 8 | EON 的連線入口與 load balancing：三個 port 到底是不是三份資料 | — | 預計 11-20 | ⬜ |
| 9 | EON communal storage 效能解剖：S3 相容儲存上的延遲從哪裡來 | — | 預計 11-21 | ⬜ |
| 10 | EON 的 Tuple Mover 行為：什麼時候搬、搬到哪裡、跟 Enterprise 差在哪 | — | 預計 11-22 | ⬜ |
