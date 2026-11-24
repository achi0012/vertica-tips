---
layout: post
title: "Vertica × Kafka 整合：vkafka、scheduler 與微批次載入的實際行為"
date: 2026-11-25 05:00:00 +0800
categories: vertica data-load
tags: [vertica, kafka, vkafka, scheduler, streaming, data-load, micro-batch]
description: "Kafka 整合不是 Vertica 內建功能：KafkaLib 的 12 個函式要先安裝，scheduler 是資料庫外的 Java 程序，而且它的幀預設 300 秒。本機 v25.4.0-0 實測兩條路徑：COPY KafkaSource 讀 5 則訊息花 507 ms，scheduler 以 10 秒幀載入 11 則訊息，端到端延遲 3.0 到 7.0 秒，每幀固定 0.5 秒 COPY 成本佔掉 11.2% 的牆上時間。"
image: /assets/images/og/vertica-kafka-integration-scheduler-zh.png
faq:
  - q: "Vertica 的 Kafka 整合是內建功能嗎？"
    a: "不是。KafkaLib 的 12 個函式（KafkaSource、KafkaParser、KafkaJSONParser、KafkaOffsets 等）要先安裝到資料庫才會出現在 v_catalog.user_functions；scheduler 則是資料庫外的 Java 程序（vertica-kafka-scheduler.jar）。本機 testdb 已安裝 library，但 scheduler 從未設定過，套件檔在 /opt/vertica/packages/kafka，版本與伺服器同為 25.4.0。"
  - q: "scheduler 的微批次延遲實際上有多久？"
    a: "等於幀（frame）的相位，不是固定值。把 frame-duration 設成 10 秒，實測三次端到端延遲分別是 3,049 ms、6,811 ms、7,043 ms，平均約 5.6 秒。預設 frame-duration 是 300 秒，也就是最壞情況要等 5 分鐘才看得到資料。"
  - q: "訊息載入後 kafka_offset、kafka_ts 欄位為什麼是 NULL？"
    a: "25.4 實測兩條路徑都不會自動填。用 COPY KafkaSource 載入時，目標表的 kafka_topic、kafka_partition、kafka_offset、kafka_ts、kafka_key 全是 NULL；改用 scheduler 也一樣。要追 offset 得用 KafkaOffsets() OVER()（COPY 路徑），或查 config schema 的 stream_microbatch_history（scheduler 路徑）。"
  - q: "scheduler 重啟後會不會重複載入或漏資料？"
    a: "不會重複。實測停掉 scheduler 後產生 2 則訊息（offset 9、10），停機期間表仍是 9 列；重新 launch 後變 11 列，重複檢查 0 列。offset 存在 config schema 裡，history 中 start_offset 11 對上 end_offset 10 就代表沒有新資料。"
  - q: "訊息內容是壞的 JSON，COPY 會報錯嗎？"
    a: "不會，這是最危險的地方。實測 10 則訊息中 5 則 payload 不合法，COPY 回報 Rows Loaded = 5、沒有任何錯誤，KafkaOffsets() 卻顯示 msg_count = 10、bytes_read = 834。要抓這種情況只能比對 msg_count 與實際列數。"
---
<!--
AI Summary: 在本機 testdb（Vertica v25.4.0-0，單節點）對本機 redpanda broker（192.168.100.9:9092）實測 Kafka 整合：catalog 沒有 user_libraries 檢視，改用 v_catalog.user_functions 查到 public schema 下 12 個 KafkaLib 函式；COPY KafkaSource 載入 5 則訊息 382 bytes 花 507 ms，KafkaOffsets() 回報 END_OF_STREAM；10 則訊息中 5 則 JSON 不合法時 COPY 仍回報成功但只載 5 列（靜默略過）。vkconfig v25.4.0 建 11 張 config 表，frame-duration 設 10 秒實測端到端延遲 3,049/6,811/7,043 ms，16 幀共 32 個 partition-batch、COPY 總計 18.15 秒佔 161 秒的 11.2%，每幀固定約 0.5 秒；重啟後 offset 續傳不重複（9→9→11）。另實測 kafka_offset/kafka_ts 在兩條路徑都不會被填入。
AI Keywords: Vertica, Kafka, vkafka, KafkaSource, KafkaJSONParser, vkconfig, scheduler, microbatch, frame duration, KafkaOffsets, 25.4
-->
<!--more-->

---

## 一、先講結論

Vertica 的 Kafka 整合常被講成「內建串流」，但拆開來看是三件不同的事：一個要安裝進資料庫的 C++ library（`KafkaLib`）、一個資料庫外的 Java 程序（scheduler，`vertica-kafka-scheduler.jar`）、以及一個 broker。三者任何一個沒到位，你得到的就不是「即時」。

本機實測一輪後，六件事值得先講：

1. **library 要裝，scheduler 要跑**。套件檔隨伺服器安裝就在 `/opt/vertica/packages/kafka`，但函式必須在 DB 內註冊才看得到；本機 `public` schema 下確實有 12 個 `Kafka*` 函式，scheduler 卻從未被設定過（沒有 config schema、沒有 java 程序）。所以「有沒有裝」要分兩層問。
2. **`v_catalog.user_libraries` 在 25.4 不存在**。查它會得到 `ERROR 4568`，判斷 library 是否安裝要改查 `v_catalog.user_functions`。
3. **scheduler 的「即時」是一個幀（frame）。預設 300 秒**。`vkconfig scheduler --create` 的 `--frame-duration` 預設值就是 `00:05:00`，意思是每 5 分鐘才把 buffer 裡的訊息 COPY 一次。
4. **幀的相位決定延遲**。把幀設成 10 秒，三次端到端延遲量到 3,049 ms、6,811 ms、7,043 ms。同一組設定、同一台機器，差 2.3 倍，差別只在你訊息抵達時距離下一個幀開始還有多久。
5. **每一幀都有固定成本，即使沒有訊息**。實測 16 幀、2 個 partition，共 32 次 COPY、總計 18.15 秒，佔 161 秒觀測窗的 11.2%；沒有資料的 partition 0 每幀照樣跑一次 0.5 秒的 COPY。
6. **`kafka_offset`、`kafka_ts` 不會自動填**。用 `COPY ... KafkaSource` 或 scheduler 載入，目標表那五個 `kafka_*` 欄位都是 NULL。要追 offset 得用 `KafkaOffsets()` 或查 config schema 的 `stream_microbatch_history`。

這篇的每個數字都來自本機實跑：Vertica v25.4.0-0、單節點 testdb、broker 是本機的 redpanda（`192.168.100.9:9092`）。測試物件全部建在 `article_lab_kafka` 與 `article_lab_kafka_cfg` 兩個 schema，寫完已 `DROP SCHEMA ... CASCADE`，broker 上的測試 topic 也已刪除。

## 二、實測環境

| 項目 | 值 |
|---|---|
| 版本 | Vertica Analytic Database v25.4.0-0 |
| 節點 | 單節點，Community Edition |
| 資料庫 | `testdb` |
| Broker | redpanda，1 broker，`192.168.100.9:9092` |
| Broker 起始 topic 數 | 6（含 `__consumer_offsets`） |
| KafkaLib | 已安裝於 `public` schema，12 個函式 |
| scheduler | vkconfig v25.4.0，Configuration Schema Version v11.0 |
| 測試 schema | `article_lab_kafka`（資料表）、`article_lab_kafka_cfg`（scheduler 設定） |
| 連線 | `vsql-testdb`；需要單一 session 時用 `-f` 跑整個腳本 |

單節點環境的好處是延遲數字乾淨：沒有節點間網路、沒有跨節點的 offset 協調。代價是所有結論都只代表單節點行為，多節點叢集的 scheduler 會為每個節點各起一份 COPY。

## 三、兩條載入路徑，先分清楚

Vertica 讀 Kafka 有兩種完全不同的機制，混著講是大部分困惑的來源。

| | 單次 COPY（`KafkaSource`） | scheduler（`vkconfig launch`） |
|---|---|---|
| 誰在跑 | 你的 vsql session | 資料庫外的 Java 程序 |
| 生命週期 | 一條 SQL，跑完就結束 | 常駐，直到 `vkconfig shutdown` |
| 何時載入 | 你下的那一刻 | 每一幀（frame）一次 |
| offset 存哪 | session 變數（`KafkaOffsets()` 可讀） | config schema 的 `stream_sources` |
| 適合 | 回填、一次性搬移、批次 ETL | 持續寫入、近即時儀表板 |
| 失敗重跑 | 你自己指定起始 offset | 程式自己從存下的 offset 續傳 |

兩條路徑共用同一組 library 函式：`KafkaSource` 是 UD Source，負責從 broker 拉資料；`KafkaParser` 只切記錄邊界，`KafkaJSONParser` 額外做 JSON 解析；`KafkaOffsets()` 把這次 COPY 走到哪裡寫回 session。

scheduler 本質上就是「每幀幫你下一條 COPY」，所以你用第二條路徑時，第一條路徑的知識全部適用。

## 四、先查本機到底裝了什麼

這一步比多數人想的複雜，因為 catalog 檢視在 25.4 改名了。

```sql
=> select count(*) from v_catalog.user_libraries;
ERROR 4568:  Relation "v_catalog.user_libraries" does not exist

=> select count(*) from v_catalog.udx_libraries;
ERROR 4568:  Relation "v_catalog.udx_libraries" does not exist
```

網路上的檢查腳本多半還停在舊名稱。實際可用的是 `v_catalog.user_functions`：

```sql
=> select schema_name, function_name, procedure_type
   from v_catalog.user_functions
   where function_name ilike '%kafka%' order by 2;
 schema_name |     function_name     |     procedure_type
-------------+-----------------------+-------------------------
 public      | KafkaAvroParser       | User Defined Parser
 public      | KafkaCheckBrokers     | User Defined Transform
 public      | KafkaExport           | User Defined Transform
 public      | KafkaInsertDelimiters | User Defined Filter
 public      | KafkaInsertLengths    | User Defined Filter
 public      | KafkaJsonParser       | User Defined Parser
 public      | KafkaListManyTopics   | User Defined Transform
 public      | KafkaListTopics       | User Defined Transform
 public      | KafkaOffsets          | User Defined Transform
 public      | KafkaParser           | User Defined Parser
 public      | KafkaSource           | User Defined Source
 public      | KafkaTopicDetails     | User Defined Transform
(12 rows)
```

12 個函式都在 `public`，代表 library 已安裝。接著確認 broker 從資料庫這一端看不看得到：

```sql
=> select KafkaCheckBrokers(USING PARAMETERS brokers='192.168.100.9:9092') over();
 broker_id |   hostname    | port
-----------+---------------+------
         0 | 192.168.100.9 | 9092

=> select KafkaListTopics(USING PARAMETERS brokers='192.168.100.9:9092') over();
       topic        | num_partitions
--------------------+----------------
 __consumer_offsets |              3
 wcl_lowitag        |              1
 vlm0_cpu_usage     |              1
 vlm0_memory_usage  |              1
 wcl_lowidoc        |              1
 a2v-kafka-test     |              1
(6 rows)
```

這裡有兩個會讓人卡住十分鐘的語法細節：

- **這些函式吃的是參數，不是欄位**。寫 `KafkaListTopics('192.168.100.9:9092')` 會得到 `ERROR 3457: Function KafkaListTopics(varchar) does not exist`；寫 `KafkaListTopics(brokers='...')` 會得到 `ERROR 2624: Column "brokers" does not exist`。正確語法是 `USING PARAMETERS`。
- **省略參數時的錯誤訊息很明確**：`ERROR 7109: Parameter 'brokers' is required`。看到 7109 就知道是參數沒帶，不是連線問題。

至於「scheduler 有沒有在跑」，SQL 查不到，要看作業系統：

```
$ ps aux | grep -i kafka | grep -v grep
（無輸出）
$ ls /opt/vertica/packages/kafka/lib/
disruptor-3.4.2.jar  libkafka.so.25.4.0  vertica-kafka-scheduler.jar ...
```

也就是說：**library 已裝、scheduler 沒跑**。這是最常見的半套狀態，資料不會自己進來，但你下 COPY 是會動的。

## 五、單次 COPY：507 毫秒讀完 5 則訊息

先建最小可用的目標表，然後把 offset 歸零重讀。

```sql
create table article_lab_kafka.events (
  event_time timestamp, event_type varchar(20),
  user_id varchar(20), amount numeric(12,3));

ALTER SESSION CLEAR UDPARAMETER ALL FOR KafkaLib;

COPY article_lab_kafka.events
SOURCE KafkaSource(stream='article_lab_events|0|-2',
                   brokers='192.168.100.9:9092',
                   stop_on_eof=true,
                   duration=interval '10 seconds')
PARSER KafkaJsonParser() DIRECT;

 Rows Loaded
-------------
           5
```

`stream` 的格式是 `topic|partition|offset`：`-2` 代表從頭、`-3` 代表從尾、非負整數就是明確的 offset。接著把這次 COPY 走到哪裡讀回來：

```sql
=> select KafkaOffsets() over();
       ktopic       | kpartition | start_offset | end_offset | msg_count | bytes_read |    duration     |    ending     |            end_msg
--------------------+------------+--------------+------------+-----------+------------+-----------------+---------------+--------------------------------
 article_lab_events |          0 |            0 |          4 |         5 |        382 | 00:00:00.507329 | END_OF_STREAM | Brokers signaled partition EOF
```

三個欄位值得注意：

- `end_offset = 4` 是**下一條要讀的 offset**（不是最後一條的 offset）。讀了 0 到 4 共 5 則，下一條是 5。
- `duration` 507 ms 幾乎全部是「等 broker 告訴我沒有更多資料」的時間，不是解析時間。同一張表、5 則訊息、382 bytes，怎麼跑都落在 503 到 510 ms，這是 EOF 偵測的固定成本。
- `ending = END_OF_STREAM` 代表正常結束；若逾時會是 `DURATION`，代表你設的 `duration` 到了但資料還沒讀完。

### 用 offset 控制重讀範圍

把起始 offset 改成 5：

```sql
COPY article_lab_kafka.events
SOURCE KafkaSource(stream='article_lab_events|0|5', brokers='192.168.100.9:9092',
                   stop_on_eof=true, duration=interval '10 seconds')
PARSER KafkaJsonParser() DIRECT;

 ktopic | kpartition | start_offset | end_offset | msg_count | bytes_read |    duration
--------+------------+--------------+------------+-----------+------------+-----------------
 ...    |          0 |            5 |          9 |         5 |        452 | 00:00:00.507467
```

同一個 topic 裡前後兩批訊息的大小不同（382 對 452 bytes），因為 `bytes_read` 算的是實際拉下來的 payload。這也順便說明一件事：**offset 是你唯一能精確定位的座標**，時間戳不行，因為多則訊息可能是同一個毫秒寫入的。

## 六、最危險的行為：JSON 壞掉時 COPY 說成功

測試時我用 `rpk topic produce -f '%k:%v'` 送訊息，這個格式字串會把輸入行當成 `key:value` 解析，結果前 5 則訊息的 payload 被從第一個冒號切開，實際存進 broker 的內容是 `"2026-11-25 05:10:01","event_type":"login",...`——開頭的 `{"event_time":` 不見了。

用 `KafkaParser`（只切邊界、不解析）讀出來長這樣：

```
                                      msg
--------------------------------------------------------------------------------
 "2026-11-25 05:10:01","event_type":"login","user_id":"u001","amount":1.100}
```

然後我對同一個 topic 用 `KafkaJSONParser` 從 offset 0 讀：

```
 Rows Loaded
-------------
           5

=> select KafkaOffsets() over();
 ktopic | kpartition | start_offset | end_offset | msg_count | bytes_read |    duration     |    ending
--------+------------+--------------+------------+-----------+------------+-----------------+---------------
 ...    |          0 |            0 |          9 |        10 |        834 | 00:00:00.508592 | END_OF_STREAM
```

**`msg_count = 10`，`Rows Loaded = 5`，沒有任何錯誤、沒有任何 rejected 訊息。** 五則壞掉的 JSON 被解析器直接丟掉，COPY 回報成功。

這是整篇最該記住的一條：`KafkaJSONParser` 對無法解析的記錄是靜默略過，不是報錯。你的監控如果只看「COPY 有沒有失敗」，這種資料遺失永遠不會被發現。唯一可靠的訊號是**比對 `msg_count` 與目標表實際列數**，或是在 load spec 上掛 rejection table。

正確的產生方式（不指定 format）讀進來就是完整的 JSON：

```
$ rpk topic consume article_lab_events -o 5 --num 1 -f '%v\n'
{"event_time":"2026-11-25 05:10:01","event_type":"login","user_id":"u001","amount":1.100}
```

## 七、scheduler：一個 Java 程序，每幀幫你下 COPY

scheduler 的設定全部存在資料庫裡，所以你會多一個 schema。流程是：建 pool → 建 config schema → 建 cluster → 建 source → 建 load spec → 建 target → 建 microbatch → launch。

![Kafka 到 Vertica 的微批次資料流：11 則訊息、998 bytes 經 scheduler 每幀每 partition 一次 COPY，平均 0.567 秒，寫入 events_mb 11 列]({{ '/assets/images/kafka-flow-frame.svg' | relative_url }}){: loading="lazy" }

### 7.1 先給它一個專屬資源池

```sql
create resource pool article_lab_kafka_pool
  MEMORYSIZE '10%' PLANNEDCONCURRENCY 1 QUEUETIMEOUT 0;

=> select pool_name, memory_size_kb, planned_concurrency, queue_timeout
   from v_monitor.resource_pool_status where pool_name like 'article_lab%';
       pool_name        | memory_size_kb | planned_concurrency | queue_timeout
------------------------+----------------+---------------------+---------------
 article_lab_kafka_pool |        2859483 |                   1 |             0
```

三個參數各有理由，不是照抄範例：`QUEUETIMEOUT 0` 讓 COPY 永遠不等佇列（等下去只會累積延遲），`PLANNEDCONCURRENCY 1` 限制同時執行的 micro-batch 數，`MEMORYSIZE` 給一個明確上限避免串流吃掉一般查詢的記憶體。

### 7.2 建設定，然後踩第一個坑

```bash
$ vkconfig scheduler --create --config-schema article_lab_kafka \
    --resource-pool article_lab_kafka_pool --frame-duration 00:00:10
Exception in thread "main" com.vertica.solutions.kafka.exception.ConfigurationException:
  Configuration already exists for given schema name.
```

**config schema 必須不存在**。我先建了 `article_lab_kafka` 放目標表，再拿它當 config schema，就被拒絕。換一個沒用過的 schema 名稱即可，scheduler 會自己建 11 張表：

```
stream_clusters          stream_load_specs        stream_microbatches
stream_events            stream_lock              stream_microbatch_history
stream_scheduler         stream_microbatch_source_map
stream_scheduler_history stream_sources           stream_targets
```

建完馬上看得到設定的實際落點：

```sql
=> select version, frame_duration, resource_pool, config_refresh,
          new_source_policy, pushback_policy, pushback_max_count, auto_sync
   from article_lab_kafka_cfg.stream_scheduler;
 version | frame_duration |     resource_pool      | config_refresh | new_source_policy | pushback_policy | pushback_max_count | auto_sync
---------+----------------+------------------------+----------------+-------------------+-----------------+--------------------+-----------
 v11.0   | 00:00:10       | article_lab_kafka_pool | 00:01          | FAIR              | LINEAR          |                  5 | t
```

`frame_duration` 就是你剛剛設的 10 秒。**沒設的話這裡會是 `00:05:00`**，也就是預設 5 分鐘——很多「Kafka 載入怎麼這麼慢」的問題，答案就在這一行。

### 7.3 四個物件加一個映射

```bash
vkconfig cluster    --create --cluster lab_broker --hosts 192.168.100.9:9092
vkconfig source     --create --source article_lab_mb --cluster lab_broker --partitions 2
vkconfig load-spec  --create --load-spec lab_json --parser KafkaJsonParser --load-method DIRECT
vkconfig target     --create --target-schema article_lab_kafka --target-table events_mb
vkconfig microbatch --create --microbatch mb_lab --target-schema article_lab_kafka \
    --target-table events_mb --load-spec lab_json \
    --add-source article_lab_mb --add-source-cluster lab_broker --max-parallelism 2
```

`--partitions 2` 必須跟 topic 的實際 partition 數一致；`--max-parallelism 2` 決定同一幀內最多同時起幾條 COPY。這兩個數字直接影響每幀的 COPY 次數，也就影響下面量到的固定成本。

### 7.4 launch 之後量延遲

scheduler 啟動後，連續送兩批訊息（各 3 則，間隔 2 秒），25 秒後目標表有 6 列。接著做延遲量測：送 1 則、每 250 ms 查一次目標表，直到看得到為止。

| 訊息 | produce 耗時 | 端到端可見延遲 |
|---|---|---|
| u201 | 43 ms | **3,049 ms** |
| u202 | 46 ms | **6,811 ms** |
| u203 | 109 ms | **7,043 ms** |

平均 5.6 秒，變異來自相位：10 秒一幀，訊息如果在幀剛開始時抵達就快，剛錯過就要等下一次。這是微批次的定義，不是調校問題——**你能調的只有幀長度，延遲的抖動是結構性的**。

### 7.5 每一幀長什麼樣子

`stream_microbatch_history` 是 scheduler 的帳本，每幀、每個 partition 一列：

```sql
=> select source_partition, start_offset, end_offset, end_reason, partition_messages,
          partition_bytes, batch_start::time, batch_end::time, last_batch_duration
   from article_lab_kafka_cfg.stream_microbatch_history order by batch_start desc limit 4;

 source_partition | start_offset | end_offset |  end_reason   | partition_messages | partition_bytes |   batch_start   |    batch_end    | last_batch_duration
------------------+--------------+------------+---------------+--------------------+-----------------+-----------------+-----------------+---------------------
                1 |           11 |         10 | END_OF_STREAM |                  0 |               0 | 05:07:24.843   | 05:07:25.347  | 00:00:00.503
                0 |            0 |         -1 | END_OF_STREAM |                  0 |               0 | 05:07:24.843   | 05:07:25.347  | 00:00:00.503
                0 |            0 |         -1 | END_OF_STREAM |                  0 |               0 | 05:07:14.823   | 05:07:15.352  | 00:00:00.509
                1 |           11 |         10 | END_OF_STREAM |                  0 |               0 | 05:07:14.823   | 05:07:15.352  | 00:00:00.509
```

四件事可以直接讀出來：

1. **每幀對每個 partition 各下一條 COPY**。2 個 partition 就是每 10 秒 2 條。
2. **沒有資料也照跑**。`partition_messages = 0`、`end_offset` 小於 `start_offset`（11 → 10 表示沒東西可讀），但 `last_batch_duration` 仍是 0.5 秒。
3. **0.5 秒是 COPY 的固定成本**，與第五節單次 COPY 的 507 ms 完全對得上。這不是巧合，是 Kafka client 等 EOF 的行為。
4. `end_reason = END_OF_STREAM` 表示正常；若看到 `TIME` 代表幀時間用完還沒讀完（訊息量超過一幀能處理的量），那是該調高 `--max-parallelism` 或縮小批次的訊號。

把整個觀測窗加總：

```sql
=> select count(distinct frame_start) as frames, count(*) as partition_batches,
          sum(partition_messages) as msgs,
          round(sum(extract(epoch from last_batch_duration))::numeric,2) as total_copy_s,
          round(avg(extract(epoch from last_batch_duration))::numeric,3) as avg_copy_s,
          round(min(extract(epoch from last_batch_duration))::numeric,3) as min_copy_s,
          round(max(extract(epoch from last_batch_duration))::numeric,3) as max_copy_s
   from article_lab_kafka_cfg.stream_microbatch_history;

 frames | partition_batches | msgs | total_copy_s | avg_copy_s | min_copy_s | max_copy_s
--------+-------------------+------+--------------+------------+------------+------------
     16 |                32 |   11 |        18.15 |      0.567 |      0.503 |      1.504
```

16 幀、32 次 COPY、11 則訊息、COPY 總時間 18.15 秒。觀測窗是 161 秒，所以 **11.2% 的牆上時間花在 COPY 上**，其中絕大多數是空轉。最大那筆 1.504 秒出現在重新 launch 後的第一幀，因為它同時處理兩個 partition 的初始化。

## 八、成本模型：幀長度決定一切

把上面的數字拉成三種幀長度：

| frame duration | 端到端延遲 | 每分鐘 COPY 次數（2 partition） | COPY 佔比 |
|---|---|---|---|
| 10 秒（實測） | 3.0–7.0 秒 | 12 | 11.2% |
| 60 秒（推算） | 0–60 秒 | 2 | 1.9% |
| 300 秒（預設，推算） | 0–300 秒 | 0.4 | 0.4% |

推算的算法很簡單：每條 COPY 固定 0.5 秒，次數 = 60/幀長 × partition 數，佔比 = 次數 × 0.5 / 60。11 則訊息在 300 秒幀下最多要等 5 分鐘才進表——如果你的儀表板說「近即時」，這就是它為什麼看起來不即時。

反過來，縮短幀長不是免費的：每幀都會產生一批新的 ROS container，10 秒一幀就是每分鐘 6 批。對一張每秒只有幾十筆的小表，這會讓 ROS 數量成長得比資料還快，mergeout 壓力上升。**幀長度是在「延遲」與「小檔案」之間取捨，不是越短越好。**

實務上的起點：先問「資料落後多久可以接受」，把它除以 5 當作幀長度（留 5 倍抖動空間），再用 `stream_microbatch_history` 的 `partition_bytes` 檢查每幀大小是否落在 10 MB 以上。低於這個量級就該拉長幀，或改用單次 COPY 批次載入。

## 九、offset 續傳：重啟不重複

scheduler 的 offset 存在 config schema，所以重啟是「續傳」而不是「重讀」。實測流程：

```
1. shutdown            -> 目前 9 列
2. 停機期間送 2 則（offset 9、10）
3. 查目標表            -> 仍是 9 列（沒有 scheduler 就沒有人載入）
4. 重新 launch，等 25 秒
5. 查目標表            -> 11 列，新增的正好是 u301、u302
6. 重複檢查            -> 0 列
```

第 5 步的新增資料：

```
 user_id |     event_time
---------+---------------------
 u301    | 2026-11-25 05:40:01
 u302    | 2026-11-25 05:40:02
```

這件事的意義：**scheduler 不會因為重啟而重讀舊資料，但也不會幫你補停機期間以外的東西**。停機期間產生的訊息在重啟後補上（因為 offset 還停在那裡），這就是它比「每分鐘跑一次 COPY 從尾端讀」可靠的地方。

它也解釋了為什麼「至少一次」會發生在別的地方：offset 是**批次結束後**才寫回 config schema 的。如果 COPY 成功、offset 還沒寫回時程式被殺，重啟後會重讀那一幀——所以目標表要能承受重複（用 primary key 或載入後 dedupe），而不是假設 exactly-once。

## 十、常見錯誤與反模式

### 錯誤 1：拿已經存在的 schema 當 config schema

```
Exception in thread "main" com.vertica.solutions.kafka.exception.ConfigurationException:
  Configuration already exists for given schema name.
```

`--config-schema` 給一個**不存在**的 schema 名稱，讓 scheduler 自己建。它會建 11 張表，全部歸那個 schema 管；事後要清掉就是 `DROP SCHEMA ... CASCADE`（或 `vkconfig scheduler --drop`）。

### 錯誤 2：被 log4j 的錯誤洗版，以為指令失敗

每次執行 `vkconfig` 都會印出：

```
ERROR Unable to create file /opt/vertica/log/vkafka-sched.log java.io.IOException: Permission denied
ERROR Could not create plugin of type class org.apache.logging.log4j.core.appender.RollingFileAppender
ERROR Unable to locate appender "RollingFile" for logger config "com.vertica.solutions.kafka"
```

原因是 log 設定寫死在 `/opt/vertica/log/`，非 dbadmin 使用者沒有寫入權。指令本身可能完全成功（`exit=0`），設定也確實建好了。判斷成功與否要看**資料庫裡有沒有那 11 張表**，不是看 stderr。用 dbadmin 執行，或改 log 設定路徑，才不會每次都被這段嚇到。

### 錯誤 3：以為 `kafka_offset` 會自動填

```sql
create table article_lab_kafka.events_meta (
  event_time timestamp, event_type varchar(20), user_id varchar(20), amount numeric(12,3),
  kafka_topic varchar(80), kafka_partition int, kafka_offset int,
  kafka_ts varchar(40), kafka_key varchar(80));

COPY article_lab_kafka.events_meta SOURCE KafkaSource(stream='article_lab_events|0|5', ...)
PARSER KafkaJsonParser() DIRECT;
-- Rows Loaded: 5

=> select user_id, kafka_topic, kafka_partition, kafka_offset, kafka_ts from article_lab_kafka.events_meta;
 user_id | kafka_topic | kafka_partition | kafka_offset | kafka_ts
---------+-------------+-----------------+--------------+----------
 u001    |             |                 |              |
 u002    |             |                 |              |
 ...
```

五個 `kafka_*` 欄位全是 NULL。用 scheduler 那條路徑也一樣（目標表同樣是 NULL）。要 offset 就得自己抓：COPY 路徑讀 `KafkaOffsets()`，scheduler 路徑查 `stream_microbatch_history` 的 `start_offset`／`end_offset`。

### 錯誤 4：參數語法寫錯，誤判成「函式不存在」

```
=> select KafkaListTopics('192.168.100.9:9092') over();
ERROR 3457:  Function KafkaListTopics(varchar) does not exist, or permission is denied

=> select KafkaListTopics(brokers='192.168.100.9:9092') over();
ERROR 2624:  Column "brokers" does not exist
```

`3457` 說「函式不存在」很容易誤導你去重裝 library。實際上是參數要用 `USING PARAMETERS` 傳。同理，`KafkaOffsets()` 只能放在 select list：

```
=> select * from KafkaOffsets() over();
ERROR 5383:  User Defined Transform Functions are allowed only in a SELECT list
```

### 錯誤 5：只看 COPY 成功，不看 msg_count

第六節那個案例：10 則訊息、5 列載入、零錯誤。**沒有 rejection table 的 JSON 載入，失敗是無聲的。** 監控腳本至少要抓兩件事：`KafkaOffsets()` 的 `msg_count` 與目標表新增列數是否相符，以及 `ending` 欄位是不是 `END_OF_STREAM`。

## 十一、風險與建議

### 現在就能做（唯讀、低風險）

- 用 `v_catalog.user_functions` 確認 library 是否安裝，用 `ps`／`pgrep` 確認 scheduler 是否在跑。這兩件事分開查，才不會把「函式有」誤判成「串流有」。
- 用 `KafkaCheckBrokers`、`KafkaListTopics`、`KafkaTopicDetails` 確認 broker 端看得到什麼，這些都是唯讀。
- 在正式環境先量一次延遲，方法就是第七節那個輪詢：送一則、每 250 ms 查一次。你的 SLA 該用實測值寫，不是用幀長度寫。
- 檢查 `stream_microbatch_history` 的 `end_reason`。出現 `TIME` 就是每幀處理不完，遲早會累積延遲。

### 需要變更、要窗口或核准

- **建立 scheduler 設定**會多一個 schema 與一個常駐 Java 程序，要佔用連線與記憶體，還要有人負責 `launch`／`shutdown` 與開機自動啟動。這是服務，不是一次性的 DDL。
- **調整 `frame_duration`** 同時改變延遲與 ROS 產生速率，要跟 ROS 數量、mergeout 時間一起看。
- **專屬資源池**：本機實測 10% 記憶體（2,859,483 KB）對 1 則訊息的測試綽綽有餘，但正式環境要按每幀的 `partition_bytes` 估算，池太小會出現 COPY 排隊。
- **改動 target table 結構**（加上 primary key 防重複）需要停掉 microbatch，否則 COPY 會失敗。

## 十二、參考資料

- Vertica 25.4.x 文件，Integrating with Kafka：<https://docs.vertica.com/25.4.x/en/admin/integrating-kafka/>
- `KafkaSource` 與 Kafka 函式：<https://docs.vertica.com/25.4.x/en/sql-reference/functions/kafka-source/>
- Streaming Load Scheduler（vkconfig）：<https://docs.vertica.com/25.4.x/en/admin/streaming-load/>
- 本機套件內附說明：`/opt/vertica/packages/kafka/README.txt`、`examples/kafka_copy_load.sql`、`examples/scheduler_quickstart.sql`
