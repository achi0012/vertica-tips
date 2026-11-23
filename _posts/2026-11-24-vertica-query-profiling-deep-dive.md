---
layout: post
title: "Vertica 查詢剖析實戰：從 EXPLAIN 到 PROFILE，逐節點找出慢在哪"
date: 2026-11-24 05:00:00 +0800
categories: vertica performance
tags: [vertica, performance, query-plan, explain, profile, v-monitor, projection]
description: "EXPLAIN 給的是計畫與 cost，PROFILE 才是每個 operator 實際產出幾列、花多少 CPU、從 disk 還是 cache 讀。本機 testdb（v25.4.0-0，單節點）實測四條查詢：cost 最低的 4K 查詢跑了 59.6 ms，cost 12K 的查詢只花 38.6 ms，cost 排序不能用來決定先優化哪一條。25.4 的 profile 資料在 query_profiles、query_plan_profiles、execution_engine_profiles 三張表，舊文件寫的 profile_paths 與 profile_projections 並不存在。"
image: /assets/images/og/vertica-query-profiling-deep-dive-zh.png
faq:
  - q: "查詢變慢時，應該先看 EXPLAIN 還是 PROFILE？"
    a: "先看 EXPLAIN 確認計畫形狀，再用 PROFILE 決定優化哪個節點。實測同一台機器上，cost 4K 的 ILIKE 查詢花了 59.6 ms，cost 12K 的 join 查詢只花 38.6 ms：cost 是相對單位、不是毫秒，照 cost 排序會先優化錯的那一條。"
  - q: "PROFILE 的資料存在哪幾張表？"
    a: "v_monitor.query_profiles 每條 statement 一列，永遠都有；v_monitor.query_plan_profiles 與 v_monitor.execution_engine_profiles 只有在 session 內執行 enable_profiling('query')／enable_profiling('ee') 之後才有資料。三張表用 transaction_id + statement_id 關聯，EE 表再加 path_id 與 activity_id。"
  - q: "為什麼 profile 的 execution time 全部加起來比查詢實際耗時還長？"
    a: "因為 execution_engine_profiles 是每個 operator instance 一列，計時會重疊。實測 5M 列聚合查詢 wall clock 164.5 ms，但 15 個 activity 的 execution time 加總 554.4 ms，約 3.4 倍。要看真正的耗時，用 query_profiles.query_duration_us。"
  - q: "clear_profiling('query') 會清掉什麼？"
    a: "會清掉全域的 query profile，不只你這個 session。實測執行前 v_monitor.query_profiles 有 367,371 列，執行後剩 2 列（就是之後兩條查詢自己）。要關掉 profiling 用 disable_profiling('query')，不要用 clear_profiling。"
  - q: "為什麼 EXPLAIN 對一個只回傳 1 列的等值查詢還是估 5M 列？"
    a: "因為那個 Rows 是掃描量，標註 (ROW COUNT ONLY) 表示只用了列數、沒有欄位統計，filter 的選擇性沒有進估算。實測 where pos_transaction_number = 12345 的 Scan 估 4,999,999 列，實際輸出 1 列，但仍然要處理完 5,000,000 列。"
---
<!--
AI Summary: 在本機 testdb（Vertica v25.4.0-0，單節點 Community Edition）實測查詢剖析：enable_profiling('query'/'ee') 是 session 層級，query_profiles 永遠記錄、query_plan_profiles 與 execution_engine_profiles 只在開啟時記錄（以 marker 查詢驗證：1 列 vs 0 列）。四條查詢的 cost 排序與實測時間排序不一致（4K/59.6ms vs 12K/38.6ms）。逐節點讀出估計與實際落差：SIP runtime filter 把 5,000,000 列擋到 98,124 列、等值查詢估 4,999,999 實際輸出 1 列、5M 聚合查詢估算完全準確。另實測 clear_profiling 清掉全域 367,371 列、冷熱 cache 49.9ms 對 11.9ms（disk 8,915,851 bytes 對 0），並整理五個實際踩到的錯誤。
AI Keywords: Vertica, PROFILE, EXPLAIN, query_profiles, execution_engine_profiles, query_plan_profiles, enable_profiling, SIP, cost, 25.4
-->
<!--more-->

---

## 一、先講結論

`EXPLAIN` 回答「打算怎麼做」，`PROFILE` 回答「實際做了多少」。這兩件事在慢查詢上經常不一致，而預設情況下你只看得到前者。

本機實測一輪後，有五件事值得先講：

1. **cost 不是毫秒，排序也不等於時間排序**。四條查詢裡 cost 最低的 `4K` 查詢跑了 59.6 ms，cost `12K` 的 join 查詢只花 38.6 ms。照 cost 排優化順序，會先做錯那一條。
2. **25.4 的 profile 是三張表，不是舊文件寫的那兩張**。`v_monitor.profile_paths`、`v_monitor.profile_projections` 在這個版本不存在，`\d` 直接回 `Did not find any relation`；實際要查的是 `query_profiles`、`query_plan_profiles`、`execution_engine_profiles`。
3. **`query_profiles` 一直在記，另外兩張只在開啟 profiling 的 session 才有**。用一條沒開 profiling 的查詢驗證：`query_profiles` 有 1 列，`query_plan_profiles` 與 `execution_engine_profiles` 都是 0 列。
4. **估算落差最大的節點，常常不是統計壞掉，而是 filter 的選擇性沒進估算**。`where pos_transaction_number = 12345` 的 Scan 估 4,999,999 列、實際輸出 1 列，但它仍然要把 5,000,000 列掃完，這才是 20.9 ms CPU 的來源。
5. **`clear_profiling('query')` 是全域破壞性操作**。實測把 `query_profiles` 從 367,371 列清到 2 列，而且連你正在讀的那份 profile 一起清掉。

這篇的每個數字都來自本機 testdb 的實跑。全程只做 `SELECT` 與 `EXPLAIN`，沒有建立任何物件，也沒有改動任何設定值。

## 二、實測環境

| 項目 | 值 |
|---|---|
| 版本 | Vertica Analytic Database v25.4.0-0 |
| 節點 | 單節點 `v_testdb_node0001`，Community Edition |
| 資料庫 | `testdb` |
| 事實表 | `online_sales.online_sales_fact`，5,000,000 列 |
| 維度表 | `public.customer_dimension` 50,000 列、`public.product_dimension` 60,000 列 |
| 測試方式 | 只讀；沒有建立 schema 或表 |
| 連線 | `vsql-testdb`，每個 `-c` 是獨立 session |

單節點環境有一個好處：profile 不會被節點間的網路傳輸干擾，`bytes read from disk` 與 `bytes read from cache` 的對照很乾淨。代價是沒有辦法觀察跨節點的資料重分布，這也是下面所有數字只代表單節點行為的原因。

## 三、三張表的分工

先把資料模型講清楚，後面才不會拿錯表。

| 表 | 一列代表什麼 | 什麼時候有資料 | 主要鍵 |
|---|---|---|---|
| `v_monitor.query_profiles` | 一條 statement：耗時、回傳列數、SQL 全文、session | 永遠（`GlobalQueryProfiling=0` 也照記） | `transaction_id`, `statement_id` |
| `v_monitor.query_plan_profiles` | 執行計畫的一行文字，含 `Cost`、`Rows`、`PATH ID` | 只有開啟 profiling 的 session | `transaction_id`, `statement_id`, `path_id`, `path_line_index` |
| `v_monitor.execution_engine_profiles` | 一個 operator instance 的一個計數器 | 只有開啟 profiling 的 session | `transaction_id`, `statement_id`, `path_id`, `activity_id`, `counter_name` |

相關的還有 `session_profiles`（連線層級）與 `mergeout_profiles`（mergeout），這篇不展開。

**不存在的兩張表**。很多舊文章與外部分析工具會寫 `v_monitor.profile_paths`、`v_monitor.profile_projections`，在 25.4.0-0 直接查會失敗：

```
=> \d v_monitor.profile_paths
Did not find any relation.
=> \d v_monitor.profile_projections
Did not find any relation.
```

對應的資訊在 `query_plan_profiles`（計畫文字與 running_time）與 `execution_engine_profiles`（counter）裡。寫監控腳本前先確認版本，否則排程會每天寄一封「關聯不存在」的信給你。

**保存期限**。三個相關參數的實查值：

```
=> select parameter_name, current_value from configuration_parameters
   where parameter_name in ('GlobalQueryProfiling','GlobalEEProfiling','SessionProfilingAgeOut','SaveDCEEProfileThresholdUS');
     parameter_name     | current_value
------------------------+---------------
 GlobalQueryProfiling   | 0
 GlobalEEProfiling      | 0
 SaveDCEEProfileThresholdUS | 60000000
 SessionProfilingAgeOut | 30
```

`SaveDCEEProfileThresholdUS` 是 60,000,000 微秒（60 秒）：超過 60 秒的查詢即使沒開 session profiling 也會被記錄。`SessionProfilingAgeOut` 是 30 天。本機的實際狀況是 367,371 列、最舊一筆 `2026-11-03 11:23:05`、最新一筆 `2026-11-24 05:04:20`，大約 21 天的量，與 30 天的設定相符。

## 四、開啟 profiling：session 層級，而且是函式不是參數

25.4 的開關是函式：

```
=> select enable_profiling('query');
    enable_profiling
-------------------------
 Query Profiling Enabled
(1 row)

=> select enable_profiling('ee');
   enable_profiling
----------------------
 EE Profiling Enabled
(1 row)
```

兩個都要開：`'query'` 寫 `query_profiles` 與 `query_plan_profiles`，`'ee'` 寫 `execution_engine_profiles`。關閉用 `disable_profiling('query')`，回 `Query Profiling Disabled`。

**舊寫法在 25.4 不成立**。網路上常見的 `ProfileExecution` 參數已經沒有了：

```
=> alter session set ProfileExecution = 1;
ROLLBACK 3638:  Invalid configuration parameter profileexecution; aborting configuration change
```

也**不要**用 `clear_profiling('query')` 來「關閉」profiling。它的輸出看起來很無害（`GLOBAL QUERY Profiling Cleared`），但實測會把全域的 `query_profiles` 清空：

```
清空前：select count(*) from v_monitor.query_profiles;  -> 367371
執行：  select clear_profiling('query');               -> GLOBAL QUERY Profiling Cleared
清空後：select count(*) from v_monitor.query_profiles;  -> 2
```

剩下那 2 列是清空後我自己跑的兩條查詢。也就是說，這個指令會把你正在分析的證據一起刪掉，而且沒有確認提示。要關閉請用 `disable_profiling`。

**scope 的驗證**。開一個沒執行 `enable_profiling` 的連線，跑一條帶 marker 的查詢，再回頭查三張表：

```
=> /* marker_no_profiling_1124 */ select count(*) from online_sales.online_sales_fact;
 5000000

=> select count(*) from v_monitor.query_profiles where query like '%marker_no_profiling_1124%';
 1
=> select count(*) from v_monitor.query_plan_profiles
   where transaction_id=45035996285530091 and statement_id=1;
 0
=> select count(*) from v_monitor.execution_engine_profiles
   where transaction_id=45035996285530091 and statement_id=1;
 0
```

結論很清楚：**查詢層級的耗時永遠有，計畫與 operator 計數器只有你開的那個 session 才有**。這也意味著 profiling 不會對其他 session 造成負擔，但反過來說，事后想補救是補不回來的。

## 五、Step by step：四條查詢

四條查詢都在同一個開啟 profiling 的 session 內依序執行，都用 `EXPLAIN` 先取 cost，再從 profile 取實際值。

### 查詢 1：低選擇性維度過濾

```sql
select c.customer_state, count(*) as n, round(sum(f.sales_dollar_amount),2) as rev
from online_sales.online_sales_fact f
join public.customer_dimension c on f.customer_key = c.customer_key
where c.customer_name ilike '%Smith%'
group by 1 order by n desc limit 5;
```

`EXPLAIN` 的形狀與估算：

```
+-SELECT  LIMIT 5 [Cost: 12K, Rows: 5 (ROW COUNT ONLY)] (PATH ID: 0)
 | +---> SORT [TOPK] [Cost: 12K, Rows: 10K (ROW COUNT ONLY)] (PATH ID: 1)
 | | +---> GROUPBY HASH (LOCAL RESEGMENT GROUPS) [Cost: 12K, Rows: 10K (ROW COUNT ONLY)] (PATH ID: 2)
 | | | +---> JOIN HASH [Cost: 10K, Rows: 5M (ROW COUNT ONLY)] (PATH ID: 3)
 | | | | +-- Outer -> STORAGE ACCESS for f [Cost: 4K, Rows: 5M (ROW COUNT ONLY)] (PATH ID: 4)
 | | | | +-- Inner -> STORAGE ACCESS for c [Cost: 748, Rows: 50K (ROW COUNT ONLY)] (PATH ID: 5)
```

實際輸出（前 5 列）：

```
 customer_state |   n   |   rev
----------------+-------+---------
 CA             | 24182 | 6594827
 TX             | 12992 | 3525134
 MI             |  5948 | 1632859
 FL             |  4192 | 1139807
 IL             |  4032 | 1108211
```

`query_profiles` 記到的是 **38,611 微秒（38.6 ms）**、回傳 5 列。

### 查詢 2：5M 列全量聚合

```sql
select p.category_description, count(*) as n, round(sum(f.sales_dollar_amount),2) as rev
from online_sales.online_sales_fact f
join public.product_dimension p on f.product_key = p.product_key and f.product_version = p.product_version
group by 1 order by rev desc limit 5;
```

`Cost: 21K`，實際 **164,471 微秒（164.5 ms）**，回傳 4 列：

```
       category_description       |    n    |    rev
----------------------------------+---------+-----------
 Food                             | 2694455 | 735048814
 Misc                             |  770954 | 209945651
 Medical                          |  767397 | 209302089
 Non-food                         |  767194 | 209186779
```

### 查詢 3：全表 ILIKE

```sql
select count(*) as n from online_sales.online_sales_fact where transaction_type ilike '%purchase%';
```

`Cost: 4K`，實際 **59,585 微秒（59.6 ms）**，回傳 1 列 `4750347`。

### 查詢 4：等值點查

```sql
select count(*) as n from online_sales.online_sales_fact where pos_transaction_number = 12345;
```

`Cost: 5K`，實際 **7,944 微秒（7.9 ms）**（這是 cache 已熱的第二次執行；同一天第一次執行是 121,215 微秒 = 121.2 ms，原因見第九節），回傳 1 列 `1`。

四條並排：

| # | 查詢 | EXPLAIN cost | PROFILE 實際 | 回傳列數 |
|---|---|---|---|---|
| 1 | Smith join + group by | 12K | 38.6 ms | 5 |
| 2 | 品類聚合 5M 列 | 21K | 164.5 ms | 4 |
| 3 | 全表 ILIKE count | 4K | 59.6 ms | 1 |
| 4 | 等值點查 | 5K | 7.9 ms | 1 |

## 六、逐節點讀 profile

### 6.1 正確的聚合方式

`execution_engine_profiles` 是「一個 operator instance 一列」，同一條查詢會有 15 個 activity。要把它讀成「每個 operator 的實際量」，語法長這樣：

```sql
select statement_id, path_id, operator_name,
  max(case when counter_name='estimated rows produced' then counter_value end) as est_rows,
  sum(case when counter_name='rows produced' then counter_value end) as rows_out,
  sum(case when counter_name='rows processed' then counter_value end) as rows_in,
  sum(case when counter_name='execution time (us)' then counter_value end) as cpu_us,
  count(distinct activity_id) as instances
from v_monitor.execution_engine_profiles
where session_id = 'v_testdb_node0001-3431075:0xead44'
  and statement_id in (1,2,3,4)
  and counter_name in ('estimated rows produced','rows produced','rows processed','execution time (us)')
group by 1,2,3
order by 1, cpu_us desc;
```

三個關鍵字的分工要先分清楚：

- `estimated rows produced`：planner 對這個 operator 輸出量的估算。同一個 path 內每個 instance 都一樣，所以用 `max`。
- `rows produced`：實際往上游送出的列數。同一 path 內可能有多個 operator（Scan → StorageUnion → ExprEval），**加總會重複計算**。
- `rows processed`／`rows to process`：進到這個 operator 的列數，對 Scan 就是掃描量。

### 6.2 查詢 1：估算 5M，實際掃 98,124

```
 statement_id | path_id | operator_name | est_rows | rows_out | rows_in | cpu_us | instances
--------------+---------+---------------+----------+----------+---------+--------+-----------
            1 |       4 | Scan          |  5000000 |    98124 | 5000000 |  76894 |         1
            1 |       5 | Scan          |    49999 |      983 |   50000 |   6960 |         1
            1 |       3 | Join          |  4999801 |    98124 |         |   6578 |         1
            1 |       2 | GroupByPipe   |    10000 |      160 |         |   2606 |         1
            1 |       3 | ExprEval      |  4999801 |    98124 |         |    717 |         1
            1 |       2 | GroupByHash   |    10000 |       32 |      32 |    469 |         2
```

三個可以直接讀出來的結論：

1. **事實表 Scan 估 5,000,000、實際只產出 98,124**。差額不是統計錯誤，是 runtime filter：`rows filtered by SIPs expression = 4,901,876`，也就是 98.0% 的列在掃描階段就被 hash join 建出來的 SIP 擋掉，只留 98,124 列進入 join。這就是為什麼 cost 12K 的查詢只需要 38.6 ms。
2. **維度表 Scan 估 49,999、實際 983 列**（`customer_dimension` 共 50,000 列，其中 983 個 Smith）。估算幾乎沒有過濾，落差 50.9 倍。
3. **`(ROW COUNT ONLY)` 的意義**。計畫文字裡每個 `Rows:` 後面都掛這個標註，代表估算只用了列數、沒有用欄位統計。所以 `ilike '%Smith%'` 這種謂詞，估算端完全看不出選擇性，只有執行後的 profile 看得到。

Join 的 `rows_out` 98,124 與事實表 Scan 相同，代表 983 個客戶平均每人約 100 筆訂單，join 沒有放大也沒有縮小。

### 6.3 查詢 2：估算準確，慢在 join 與聚合

```
 statement_id | path_id | operator_name | est_rows | rows_out | rows_in | cpu_us | instances
--------------+---------+---------------+----------+----------+---------+--------+-----------
            2 |       3 | Join          |  5000000 |  5000000 |         | 291042 |         1
            2 |       2 | GroupByPipe   |    10000 |       20 |         | 125773 |         1
            2 |       4 | Scan          |  5000000 |  5000000 | 5000000 |  90808 |         1
            2 |       3 | ExprEval      |  5000000 |  5000000 |         |  42874 |         1
            2 |       5 | Scan          |    60000 |    60000 |   60000 |   1074 |         1
```

這條查詢的估算是準的：事實表 Scan 估 5,000,000、實際 5,000,000；產品維度估 60,000、實際 60,000。PROFILE 在這裡的價值不是「抓錯」，而是「分配時間」：Join 291.0 ms、GroupByPipe 125.8 ms、Scan 90.8 ms。慢在 hash join 與聚合，不在掃描。

這也是為什麼「估算準不準」不該是讀 profile 的唯一目的。估算準的查詢一樣會慢，你要知道慢在哪個 operator。

### 6.4 查詢 4：估 4,999,999、輸出 1 列

```
 statement_id | path_id | operator_name | est_rows | rows_out | rows_in | cpu_us | instances
--------------+---------+---------------+----------+----------+---------+--------+-----------
            4 |       2 | Scan          |  4999999 |        1 | 5000000 |  20964 |         1
            4 |       2 | ExprEval      |  4999999 |        1 |         |    126 |         1
```

這是整篇落差最大的一個節點：估算 4,999,999，實際輸出 1 列，ratio 約 500 萬倍。但這個數字要正確解讀：

- `rows_in = 5,000,000`，代表這個 Scan 真的處理了 5 百萬列，沒有被 min/max 或 valindex 剪掉（`rows pruned by valindex = 0`）。
- `rows_out = 1`，代表過濾後只剩 1 列。
- 所以這不是「planner 估錯所以選錯計畫」，而是「這個計畫本來就要掃完 5M 列才能回答」。20.9 ms 的 CPU 花在掃描與謂詞求值，不是花在輸出。

要改善只有兩條路：讓 `pos_transaction_number` 成為排序鍵或投影的 segmentation 鍵（需要 DDL），或是接受它。用 cost 看這條查詢只會得到「5K，很便宜」的結論，看不到它其實掃了整張表。

### 6.5 用 SQL 直接找落差最大的節點

把上面的判讀寫成查詢，可以每天跑：

```sql
select statement_id, path_id,
  max(case when counter_name='estimated rows produced' then counter_value end) as est_rows,
  sum(case when counter_name='rows produced' then counter_value end) as rows_out,
  round(max(case when counter_name='estimated rows produced' then counter_value end)::numeric
        / nullif(sum(case when counter_name='rows produced' then counter_value end),0),1) as est_over_act
from v_monitor.execution_engine_profiles
where session_id = current_session()
  and counter_name in ('estimated rows produced','rows produced')
group by 1,2
having sum(case when counter_name='rows produced' then counter_value end) > 0
order by est_over_act desc
limit 10;
```

注意兩件事：這個查詢自己也會被 profile（因為 profiling 還開著），所以 `session_id = current_session()` 會把它自己也算進去；另外同一 path 內多個 operator 的 `rows_out` 相加會重複計算（見第十節），所以這個排序是「找嫌疑人」的清單，不是判決書，還是要回去看 operator 名稱。

## 七、三種「估算 vs 實際」落差

| 型態 | 實測例子 | 估算 | 實際 | 原因 |
|---|---|---|---|---|
| runtime filter 讓實際遠小於估算 | 查詢 1 事實表 Scan | 5,000,000 | 98,124 | SIP 擋掉 4,901,876 列（98.0%） |
| filter 選擇性沒進估算 | 查詢 4 Scan | 4,999,999 | 1 | 只用了列數，沒有欄位統計 |
| join 放大 | 查詢 1 Join | 4,999,801 | 98,124 | 983 客戶 × 平均約 100 筆訂單 |
| 估算準確 | 查詢 2 事實表 Scan | 5,000,000 | 5,000,000 | 全表掃描，沒有謂詞 |

第一種落差通常是好消息（執行期比預期快），第二種是誤導（看起來便宜但掃全表），第三種要看你 join 的鍵是否重複。把這四種分開，才不會看到 ratio 大就開始重建統計。

## 八、cost 與時間的排序不一致

把四條查詢按 cost 與實際時間各排一次：

| 排名 | 按 cost | 按實測時間 |
|---|---|---|
| 1 | 查詢 3（4K，59.6 ms） | 查詢 4（7.9 ms） |
| 2 | 查詢 4（5K，7.9 ms） | 查詢 1（38.6 ms） |
| 3 | 查詢 1（12K，38.6 ms） | 查詢 3（59.6 ms） |
| 4 | 查詢 2（21K，164.5 ms） | 查詢 2（164.5 ms） |

只有最後一名一致。cost 最低的查詢 3 花了 59.6 ms，是查詢 1 的 1.5 倍；cost 排名第 3 的查詢 1 反而比它快。原因不難理解：cost 是 planner 用統計推出來的相對單位，包含 CPU、I/O、網路與記憶體的加權；它不反映 cache 命中、SIP 的執行期效果，也不反映你機器上實際的並行度。

實務上的用法：

- **用 cost 比較「同一個查詢的不同寫法」**，例如改寫 join 順序、拿掉 function call，看計畫形狀有沒有變。
- **用 `query_duration_us` 排「哪條查詢該優化」**，因為那是使用者感受到的時間。
- **用 profile 的 operator 計時決定「從哪裡下手」**。

三者順序錯了，就會出現「優化了 cost 最低但那條根本不慢」的情況。

## 九、冷熱 cache：同一條 SQL 49.9 ms 對 11.9 ms

為了確認量到的是 I/O 還是 CPU，用另一張 5M 列的表 `store.store_sales_fact` 連跑兩次同樣的查詢：

```
select count(*) from store.store_sales_fact where sales_dollar_amount > 0;
```

| 執行 | wall clock | bytes read from disk | bytes read from cache | Scan CPU |
|---|---|---|---|---|
| 第一次 | 49.9 ms | 8,915,851 | 262,144 | 87.7 ms |
| 第二次 | 11.9 ms | 0 | 40,452,776 | 29.7 ms |

第二次完全命中 cache，時間掉到 24%。這也是為什麼查詢 4 的第一次是 121.2 ms、第二次只有 7.9 ms：第二次的 profile 顯示 `bytes read from disk = 0`、`bytes read from cache = 40,452,776`。

兩個實務結論：

1. **比較兩條 SQL 之前，先各跑一次暖機**，否則你量到的是 page cache 的狀態，不是查詢的效率。
2. **`bytes read from disk` 是判斷「這條查詢是不是 I/O bound」最快的信號**。Scan 的 CPU 高、disk 讀 0，問題在謂詞求值；CPU 不高但 disk 讀滿，問題在投影設計或 partition 剪枝。

## 十、常見錯誤與反模式

### 錯誤 1：用 `clear_profiling` 關閉 profiling

```
=> select clear_profiling('query');
 GLOBAL QUERY Profiling Cleared
```

它清的是全域 `query_profiles`，不是你的 session。實測 367,371 列變 2 列，而且沒有確認、沒有備份。要關閉用 `disable_profiling('query')`。

### 錯誤 2：把同一 path 的 `rows produced` 全部相加

查詢 1 的維度表掃描 path 5 有兩個 operator：`Scan` 產出 983 列、`StorageUnion` 也產出 983 列（它只是把資料往上送）。相加會得到 1,966，剛好是實際的兩倍。同理，查詢 3 的 `Scan` 與 `ExprEval` 都報 4,750,347，相加變 9,500,694。

正確做法是看該 path 的末端 operator，或按 `operator_name` 分開列，不要跨 operator 相加。

### 錯誤 3：把 `execution time` 加總當成查詢耗時

查詢 2 的實測：wall clock 164.5 ms，但 15 個 activity 的 `execution time (us)` 加總 554.4 ms，是 3.4 倍。原因是 pipeline 內的 operator 同時在跑，計時重疊；單節點上還有多個 segment 平行掃描。

要看真正的查詢耗時，用 `query_profiles.query_duration_us`；operator 計時只用來比較「誰佔比高」。

### 錯誤 4：用 `max(statement_id)` 找剛跑完的查詢

profiling 開了之後，**你的診斷查詢自己也會被記錄**。實測在開啟 profiling 的 session 裡查 `query_plan_profiles`，`max(statement_id)` 指向的是這條查詢本身，不是你想看的那條。正確做法是先把 `transaction_id`、`statement_id` 印出來，或用 SQL 文字比對：

```sql
select transaction_id, statement_id, query_duration_us
from v_monitor.query_profiles
where session_id = current_session()
  and query like 'select c.customer_state%';
```

### 錯誤 5：查不存在的表

```
=> \d v_monitor.profile_paths
Did not find any relation.
```

舊版文件、社群腳本、外部 APM 整合常常假設這兩張表存在。25.4 要改查 `query_plan_profiles` 與 `execution_engine_profiles`，欄位名稱也不同（`path_line`、`counter_name`、`counter_value`）。

### 錯誤 6：以為 `ProfileExecution` 還能用

```
=> alter session set ProfileExecution = 1;
ROLLBACK 3638:  Invalid configuration parameter profileexecution; aborting configuration change
```

25.4 只有 `enable_profiling('query'|'ee')` 這個入口。

## 十一、風險與建議

### 現在就能做（唯讀、低風險）

- 在你要分析的 session 開 `enable_profiling('query')` 與 `enable_profiling('ee')`，只影響自己。
- 用 `query_profiles.query_duration_us` 排序慢查詢，抓 top 20 再逐條看。
- 用 `execution_engine_profiles` 的 `bytes read from disk`／`bytes read from cache` 判斷 I/O 或 CPU。
- 用 `estimated rows produced` 對 `rows produced` 找落差，但記得先確認 operator 名稱，避免重複計算。
- 把常用查詢存成檢視或腳本，但**不要**把 `clear_profiling` 放進去。

### 需要變更、要窗口或核准

- `GlobalQueryProfiling=1`：對整個叢集開啟，會增加 DC 表的寫入量。本機 `query_profiles` 21 天累積 367,371 列，全叢集開啟前要先估算磁碟。
- 調低 `SaveDCEEProfileThresholdUS`（預設 60 秒）：會讓更多查詢自動留下 profile，同樣是容量問題。
- `clear_profiling`：破壞性、無確認，任何自動化流程都不該呼叫。
- 為了幾何剪枝改投影設計（排序鍵、segmentation）：要 DDL 與維護窗口，而且要先確認查詢樣態，不要只為了單一查詢改。

## 十二、參考資料

- Vertica 25.4.x 文件，Profiling Queries：<https://docs.vertica.com/25.4.x/en/user/profiling/>
- `ENABLE_PROFILING`：<https://docs.vertica.com/25.4.x/en/sql-reference/functions/enable_profiling/>
- `v_monitor.query_profiles`：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor/query-profiles/>
- `v_monitor.execution_engine_profiles`：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor/execution-engine-profiles/>
