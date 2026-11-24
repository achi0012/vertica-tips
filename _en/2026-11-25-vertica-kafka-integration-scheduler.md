---
layout: post_en
title: "Vertica and Kafka Integration: vKafka, the Scheduler, and Micro-batch Loading"
date: 2026-11-25 05:00:00 +0800
categories: vertica data-load
tags: [vertica, kafka, vkafka, scheduler, streaming, data-load, micro-batch]
description: "Kafka support is not built into Vertica: the 12 KafkaLib functions must be installed, the scheduler is a Java process outside the database, and its frame defaults to 300 seconds. Two measured paths on a local v25.4.0-0 node: a COPY via KafkaSource read 5 messages in 507 ms, while the scheduler at a 10-second frame loaded 11 messages with end-to-end latency between 3.0 and 7.0 seconds, spending 11.2% of wall time on a fixed 0.5-second COPY per frame."
image: /assets/images/og/vertica-kafka-integration-scheduler-en.png
faq:
  - q: "Is Kafka integration a built-in Vertica feature?"
    a: "No. The 12 KafkaLib functions (KafkaSource, KafkaParser, KafkaJSONParser, KafkaOffsets and others) only appear in v_catalog.user_functions after the library is installed into the database, and the scheduler is a Java process outside Vertica (vertica-kafka-scheduler.jar). On this testdb the library was installed but the scheduler had never been configured; the package files live in /opt/vertica/packages/kafka at version 25.4.0, matching the server."
  - q: "How much latency does the scheduler's micro-batching actually add?"
    a: "It depends on the frame phase, not a fixed number. With frame-duration set to 10 seconds, three end-to-end measurements came out at 3,049 ms, 6,811 ms, and 7,043 ms, roughly 5.6 seconds on average. The default frame duration is 300 seconds, so the worst case is five minutes before data becomes visible."
  - q: "Why are kafka_offset and kafka_ts null after loading?"
    a: "In 25.4 neither path fills them automatically. Loading with COPY KafkaSource leaves kafka_topic, kafka_partition, kafka_offset, kafka_ts, and kafka_key all null, and the scheduler does the same. To track offsets you read KafkaOffsets() OVER() for the COPY path, or stream_microbatch_history in the config schema for the scheduler path."
  - q: "Does restarting the scheduler duplicate or lose data?"
    a: "It does not duplicate. After shutting the scheduler down I produced two messages at offsets 9 and 10; the table stayed at 9 rows while it was down, then reached 11 rows after relaunch with zero duplicate rows. Offsets are stored in the config schema, and start_offset 11 against end_offset 10 in the history is what no-new-data looks like."
  - q: "Does a malformed JSON payload make the COPY fail?"
    a: "No, and that is the dangerous part. With 10 messages where 5 payloads were invalid, COPY reported Rows Loaded = 5 with no error at all, while KafkaOffsets() showed msg_count = 10 and bytes_read = 834. The only way to catch this is to compare msg_count against the rows actually inserted."
---
<!--more-->

---

## 1. The short version

Kafka integration is usually described as "built-in streaming", but it is really three separate things: a C++ library that must be installed into the database (`KafkaLib`), a Java process outside the database (the scheduler, `vertica-kafka-scheduler.jar`), and a broker. If any one of them is missing, what you have is not real time.

Six findings from a measured run on a local instance:

1. **The library must be installed and the scheduler must run.** The package ships with the server in `/opt/vertica/packages/kafka`, but the functions only become visible once registered in the database. On this host, `public` holds 12 `Kafka*` functions while the scheduler had never been configured: no config schema, no Java process. "Is it installed" is two questions, not one.
2. **`v_catalog.user_libraries` does not exist in 25.4.** Querying it returns `ERROR 4568`; the working check is `v_catalog.user_functions`.
3. **The scheduler's "real time" is one frame, and the frame defaults to 300 seconds.** The `--frame-duration` default for `vkconfig scheduler --create` is `00:05:00`, meaning buffered messages are copied once every five minutes.
4. **Frame phase determines latency.** At a 10-second frame, three end-to-end measurements came out at 3,049 ms, 6,811 ms, and 7,043 ms. Same settings, same machine, a 2.3x spread, decided entirely by how close the message arrived to the next frame.
5. **Every frame costs something even when there is no data.** Over 16 frames and 2 partitions, 32 COPY statements took 18.15 seconds in total, 11.2% of a 161-second observation window. Partition 0 had no messages and still ran a 0.5-second COPY every frame.
6. **`kafka_offset` and `kafka_ts` are not populated for you.** Whether you use `COPY ... KafkaSource` or the scheduler, those five `kafka_*` columns stay null. Offsets come from `KafkaOffsets()` or from `stream_microbatch_history` in the config schema.

Every number below comes from an actual run: Vertica v25.4.0-0, single-node testdb, against a local redpanda broker at `192.168.100.9:9092`. All test objects lived in two schemas, `article_lab_kafka` and `article_lab_kafka_cfg`, both dropped with `DROP SCHEMA ... CASCADE` afterwards, and the test topics were deleted from the broker.

## 2. Test environment

| Item | Value |
|---|---|
| Version | Vertica Analytic Database v25.4.0-0 |
| Nodes | Single node, Community Edition |
| Database | `testdb` |
| Broker | redpanda, 1 broker, `192.168.100.9:9092` |
| Topics before the test | 6, including `__consumer_offsets` |
| KafkaLib | Installed in `public`, 12 functions |
| Scheduler | vkconfig v25.4.0, Configuration Schema Version v11.0 |
| Test schemas | `article_lab_kafka` (tables), `article_lab_kafka_cfg` (scheduler config) |
| Client | `vsql-testdb`; `-f` for anything that must stay in one session |

A single node keeps the latency numbers clean: no inter-node traffic, no cross-node offset coordination. The price is that none of this says anything about a multi-node cluster, where the scheduler starts one COPY per node.

## 3. Two load paths, kept separate

There are two entirely different mechanisms for reading Kafka from Vertica, and conflating them causes most of the confusion.

| | One-off COPY (`KafkaSource`) | Scheduler (`vkconfig launch`) |
|---|---|---|
| Who runs it | Your vsql session | A Java process outside the database |
| Lifetime | One statement, then done | Long-running until `vkconfig shutdown` |
| When data loads | The moment you run it | Once per frame |
| Offset storage | Session variable, readable via `KafkaOffsets()` | `stream_sources` in the config schema |
| Good for | Backfill, one-time migration, batch ETL | Continuous feeds, near-real-time dashboards |
| Retry behaviour | You choose the starting offset | The process resumes from the stored offset |

Both paths share the same library functions: `KafkaSource` is a UD Source that pulls from the broker, `KafkaParser` only finds record boundaries, `KafkaJSONParser` additionally parses JSON, and `KafkaOffsets()` writes back where this COPY stopped.

The scheduler is fundamentally "run a COPY for you every frame", so everything you learn on the first path applies to the second.

## 4. First, find out what is actually installed

This step is more involved than most people expect, because the catalog view was renamed in 25.4.

```sql
=> select count(*) from v_catalog.user_libraries;
ERROR 4568:  Relation "v_catalog.user_libraries" does not exist

=> select count(*) from v_catalog.udx_libraries;
ERROR 4568:  Relation "v_catalog.udx_libraries" does not exist
```

Most community check scripts still use the old names. What works is `v_catalog.user_functions`:

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

All 12 functions sit in `public`, so the library is installed. Next, confirm the database side can reach the broker:

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

Two syntax details that can cost you ten minutes each:

- **These functions take parameters, not columns.** `KafkaListTopics('192.168.100.9:9092')` returns `ERROR 3457: Function KafkaListTopics(varchar) does not exist`, while `KafkaListTopics(brokers='...')` returns `ERROR 2624: Column "brokers" does not exist`. The correct form is `USING PARAMETERS`.
- **Leaving the parameter out gives a clear message**: `ERROR 7109: Parameter 'brokers' is required`. If you see 7109, the parameter is missing; it is not a connectivity problem.

Whether the scheduler is running cannot be answered from SQL. You have to look at the operating system:

```
$ ps aux | grep -i kafka | grep -v grep
(no output)
$ ls /opt/vertica/packages/kafka/lib/
disruptor-3.4.2.jar  libkafka.so.25.4.0  vertica-kafka-scheduler.jar ...
```

In other words: **library installed, scheduler not running.** That half-configured state is common. Data does not arrive by itself, but your COPY statements do work.

## 5. A one-off COPY: 5 messages in 507 ms

Create a minimal target table, then read from the beginning of the topic.

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

The `stream` format is `topic|partition|offset`: `-2` means the beginning, `-3` the end, and a non-negative integer is an explicit offset. Then read back where the COPY stopped:

```sql
=> select KafkaOffsets() over();
       ktopic       | kpartition | start_offset | end_offset | msg_count | bytes_read |    duration     |    ending     |            end_msg
--------------------+------------+--------------+------------+-----------+------------+-----------------+---------------+--------------------------------
 article_lab_events |          0 |            0 |          4 |         5 |        382 | 00:00:00.507329 | END_OF_STREAM | Brokers signaled partition EOF
```

Three columns deserve attention:

- `end_offset = 4` is the **next offset to read**, not the last one consumed. Offsets 0 through 4 is 5 messages, so the next read starts at 5.
- `duration` of 507 ms is almost entirely waiting for the broker to say there is nothing more, not parsing. The same table, 5 messages, 382 bytes, lands between 503 and 510 ms on every run: that is the fixed cost of EOF detection.
- `ending = END_OF_STREAM` means a clean finish. A timeout shows up as `DURATION`, meaning your `duration` expired before the data ran out.

### Controlling the replay range with offsets

Change the starting offset to 5:

```sql
COPY article_lab_kafka.events
SOURCE KafkaSource(stream='article_lab_events|0|5', brokers='192.168.100.9:9092',
                   stop_on_eof=true, duration=interval '10 seconds')
PARSER KafkaJsonParser() DIRECT;

 ktopic | kpartition | start_offset | end_offset | msg_count | bytes_read |    duration
--------+------------+--------------+------------+-----------+------------+-----------------
 ...    |          0 |            5 |          9 |         5 |        452 | 00:00:00.507467
```

The two batches differ in size (382 versus 452 bytes in `bytes_read`) because that column reflects the payload actually pulled. It also makes a related point: **offset is the only precise coordinate you have**. Timestamps are not, since several messages can be written in the same millisecond.

## 6. The most dangerous behaviour: COPY reports success on broken JSON

While testing I produced messages with `rpk topic produce -f '%k:%v'`. That format string makes rpk parse each input line as `key:value`, so the first five payloads were split at the first colon and what actually landed in the broker was `"2026-11-25 05:10:01","event_type":"login",...` with the leading `{"event_time":` missing.

Reading them back with `KafkaParser`, which only finds boundaries and does not parse, shows exactly that:

```
                                      msg
--------------------------------------------------------------------------------
 "2026-11-25 05:10:01","event_type":"login","user_id":"u001","amount":1.100}
```

Then I read the same topic from offset 0 with `KafkaJSONParser`:

```
 Rows Loaded
-------------
           5

=> select KafkaOffsets() over();
 ktopic | kpartition | start_offset | end_offset | msg_count | bytes_read |    duration     |    ending
--------+------------+--------------+------------+-----------+------------+-----------------+---------------
 ...    |          0 |            0 |          9 |        10 |        834 | 00:00:00.508592 | END_OF_STREAM
```

**`msg_count = 10`, `Rows Loaded = 5`, and not a single error or rejected record.** Five invalid JSON payloads were dropped by the parser and COPY reported success.

This is the one thing to remember from the whole article: `KafkaJSONParser` silently skips records it cannot parse rather than failing. If your monitoring only watches for COPY failures, this data loss is invisible forever. The only reliable signals are **comparing `msg_count` with the rows actually inserted**, or attaching a rejection table to the load spec.

Producing correctly (no format string) yields intact JSON:

```
$ rpk topic consume article_lab_events -o 5 --num 1 -f '%v\n'
{"event_time":"2026-11-25 05:10:01","event_type":"login","user_id":"u001","amount":1.100}
```

## 7. The scheduler: a Java process that runs a COPY every frame

All scheduler configuration lives in the database, so you gain a schema. The sequence is: create a pool, create the config schema, define a cluster, a source, a load spec, a target, a microbatch, then launch.

![Kafka to Vertica micro-batch data flow: 11 messages and 998 bytes through the scheduler, one COPY per partition per frame at 0.567 seconds on average, into events_mb for 11 rows]({{ '/assets/images/kafka-flow-frame.svg' | relative_url }}){: loading="lazy" }

### 7.1 Give it a dedicated resource pool

```sql
create resource pool article_lab_kafka_pool
  MEMORYSIZE '10%' PLANNEDCONCURRENCY 1 QUEUETIMEOUT 0;

=> select pool_name, memory_size_kb, planned_concurrency, queue_timeout
   from v_monitor.resource_pool_status where pool_name like 'article_lab%';
       pool_name        | memory_size_kb | planned_concurrency | queue_timeout
------------------------+----------------+---------------------+---------------
 article_lab_kafka_pool |        2859483 |                   1 |             0
```

Each setting has a reason rather than being copied from an example: `QUEUETIMEOUT 0` means a COPY never waits in a queue (waiting only accumulates latency), `PLANNEDCONCURRENCY 1` caps concurrent micro-batches, and `MEMORYSIZE` puts a hard ceiling on how much memory streaming can take from ordinary queries.

### 7.2 Configure it, and hit the first trap

```bash
$ vkconfig scheduler --create --config-schema article_lab_kafka \
    --resource-pool article_lab_kafka_pool --frame-duration 00:00:10
Exception in thread "main" com.vertica.solutions.kafka.exception.ConfigurationException:
  Configuration already exists for given schema name.
```

**The config schema must not already exist.** I had created `article_lab_kafka` for the target tables and then tried to reuse it as the config schema, which is rejected. Use a fresh name and the scheduler creates its 11 tables itself:

```
stream_clusters          stream_load_specs        stream_microbatches
stream_events            stream_lock              stream_microbatch_history
stream_scheduler         stream_microbatch_source_map
stream_scheduler_history stream_sources           stream_targets
```

The settings land where you can read them immediately:

```sql
=> select version, frame_duration, resource_pool, config_refresh,
          new_source_policy, pushback_policy, pushback_max_count, auto_sync
   from article_lab_kafka_cfg.stream_scheduler;
 version | frame_duration |     resource_pool      | config_refresh | new_source_policy | pushback_policy | pushback_max_count | auto_sync
---------+----------------+------------------------+----------------+-------------------+-----------------+--------------------+-----------
 v11.0   | 00:00:10       | article_lab_kafka_pool | 00:01          | FAIR              | LINEAR          |                  5 | t
```

`frame_duration` is the 10 seconds just set. **Without the flag this row reads `00:05:00`**, the 5-minute default, and that single row answers most "why is Kafka loading so slow" questions.

### 7.3 Four objects and a mapping

```bash
vkconfig cluster    --create --cluster lab_broker --hosts 192.168.100.9:9092
vkconfig source     --create --source article_lab_mb --cluster lab_broker --partitions 2
vkconfig load-spec  --create --load-spec lab_json --parser KafkaJsonParser --load-method DIRECT
vkconfig target     --create --target-schema article_lab_kafka --target-table events_mb
vkconfig microbatch --create --microbatch mb_lab --target-schema article_lab_kafka \
    --target-table events_mb --load-spec lab_json \
    --add-source article_lab_mb --add-source-cluster lab_broker --max-parallelism 2
```

`--partitions 2` must match the topic's real partition count; `--max-parallelism 2` caps concurrent COPY statements within a frame. Both numbers drive how many COPY statements run per frame, and therefore the fixed cost measured next.

### 7.4 Measure latency after launch

Once the scheduler was up, I produced two bursts of three messages two seconds apart; 25 seconds later the target table held 6 rows. Then the latency test: produce one message, poll the target table every 250 ms until the row appears.

| Message | Produce time | End-to-end visibility |
|---|---|---|
| u201 | 43 ms | **3,049 ms** |
| u202 | 46 ms | **6,811 ms** |
| u203 | 109 ms | **7,043 ms** |

A 5.6-second average, with the spread coming from phase: with a 10-second frame, a message arriving just as a frame starts is fast and one that just misses waits for the next. That is the definition of micro-batching, not a tuning problem. **The only thing you can tune is the frame length; the jitter is structural.**

### 7.5 What a frame looks like

`stream_microbatch_history` is the scheduler's ledger, one row per frame per partition:

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

Four things read straight off this:

1. **Every frame runs one COPY per partition.** Two partitions means two statements every 10 seconds.
2. **Empty partitions still run.** `partition_messages = 0` and `end_offset` below `start_offset` (11 versus 10 means nothing to read), yet `last_batch_duration` is still 0.5 seconds.
3. **That 0.5 seconds is COPY's fixed cost**, matching the 507 ms one-off COPY in section 5 exactly. It is the Kafka client waiting for EOF, not a coincidence.
4. `end_reason = END_OF_STREAM` is normal. `TIME` means the frame ran out before the data was consumed, which is the signal to raise `--max-parallelism` or shorten batches.

Summing the whole window:

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

16 frames, 32 COPY statements, 11 messages, 18.15 seconds of COPY time. The observation window was 161 seconds, so **11.2% of wall time went into COPY**, the vast majority of it idle. The 1.504-second maximum is the first frame after relaunch, which initialises both partitions at once.

## 8. Cost model: frame length decides everything

Project the measured numbers onto three frame lengths:

| Frame duration | End-to-end latency | COPYs per minute (2 partitions) | COPY share |
|---|---|---|---|
| 10 s (measured) | 3.0–7.0 s | 12 | 11.2% |
| 60 s (projected) | 0–60 s | 2 | 1.9% |
| 300 s (default, projected) | 0–300 s | 0.4 | 0.4% |

The arithmetic is simple: each COPY costs a fixed 0.5 seconds, the count is 60/frame × partitions, and the share is count × 0.5 / 60. Under a 300-second frame those 11 messages would take up to five minutes to appear, which is why a dashboard labelled "near real time" often is not.

Shortening the frame is not free either: every frame produces a new batch of ROS containers, so a 10-second frame means 6 batches a minute. For a table receiving a few dozen rows a second, ROS count grows faster than the data and mergeout pressure rises. **Frame length trades latency against small files; shorter is not automatically better.**

A practical starting point: decide how stale the data may be, divide by five for jitter headroom, and use `partition_bytes` from `stream_microbatch_history` to check that each frame carries at least 10 MB. Below that order of magnitude, lengthen the frame or switch to batch COPY.

## 9. Offset resumption: restart without duplication

The scheduler stores offsets in the config schema, so a restart resumes rather than re-reads. The measured sequence:

```
1. shutdown            -> 9 rows
2. produce 2 messages while down (offsets 9, 10)
3. query target        -> still 9 rows (nobody is loading)
4. relaunch, wait 25 s
5. query target        -> 11 rows, the new ones exactly u301 and u302
6. duplicate check     -> 0 rows
```

The new rows:

```
 user_id |     event_time
---------+---------------------
 u301    | 2026-11-25 05:40:01
 u302    | 2026-11-25 05:40:02
```

Two consequences. First, **the scheduler does not re-read old data after a restart, but it also does not skip what arrived while it was down** — offsets stayed put, so those messages were picked up. That is what makes it more reliable than a cron job that copies from the tail every minute.

Second, this is also where "at least once" comes from: offsets are written back to the config schema **after** the batch completes. If the process is killed after a successful COPY but before the offset is stored, the frame is re-read on restart. Design the target table to tolerate duplicates (primary key or post-load dedupe) instead of assuming exactly-once.

## 10. Common mistakes and anti-patterns

### Mistake 1: using an existing schema as the config schema

```
Exception in thread "main" com.vertica.solutions.kafka.exception.ConfigurationException:
  Configuration already exists for given schema name.
```

Pass a schema name that **does not exist** and let the scheduler create it. It creates 11 tables owned by that schema; cleaning up later is `DROP SCHEMA ... CASCADE` (or `vkconfig scheduler --drop`).

### Mistake 2: reading the log4j errors as a failed command

Every `vkconfig` invocation prints:

```
ERROR Unable to create file /opt/vertica/log/vkafka-sched.log java.io.IOException: Permission denied
ERROR Could not create plugin of type class org.apache.logging.log4j.core.appender.RollingFileAppender
ERROR Unable to locate appender "RollingFile" for logger config "com.vertica.solutions.kafka"
```

The log configuration hardcodes a path under `/opt/vertica/log/`, which non-dbadmin users cannot write to. The command itself may have succeeded completely (`exit=0`) and the configuration is genuinely in place. Judge success by **whether those 11 tables exist in the database**, not by stderr. Run as dbadmin or repoint the log path, and this wall of text stops appearing.

### Mistake 3: expecting `kafka_offset` to fill itself

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

All five `kafka_*` columns are null, and the scheduler path leaves them null too. Offsets have to be captured explicitly: `KafkaOffsets()` for the COPY path, `start_offset`/`end_offset` in `stream_microbatch_history` for the scheduler path.

### Mistake 4: wrong parameter syntax, misread as "function missing"

```
=> select KafkaListTopics('192.168.100.9:9092') over();
ERROR 3457:  Function KafkaListTopics(varchar) does not exist, or permission is denied

=> select KafkaListTopics(brokers='192.168.100.9:9092') over();
ERROR 2624:  Column "brokers" does not exist
```

`3457` reads like "reinstall the library", but the actual problem is that parameters go through `USING PARAMETERS`. The same applies to `KafkaOffsets()`, which must appear in the select list:

```
=> select * from KafkaOffsets() over();
ERROR 5383:  User Defined Transform Functions are allowed only in a SELECT list
```

### Mistake 5: watching COPY success instead of `msg_count`

The case from section 6: 10 messages, 5 rows loaded, zero errors. **JSON loading without a rejection table fails silently.** Monitoring must check two things at minimum: whether `msg_count` from `KafkaOffsets()` matches the rows inserted, and whether `ending` is `END_OF_STREAM`.

## 11. Risks and recommendations

### Safe to do now (read-only, low risk)

- Confirm the library with `v_catalog.user_functions` and the scheduler with `ps`/`pgrep`. Checking them separately prevents mistaking "the functions exist" for "streaming works".
- Use `KafkaCheckBrokers`, `KafkaListTopics`, and `KafkaTopicDetails` to see what the broker side exposes. All are read-only.
- Measure latency once in production using the polling method from section 7: produce one message, poll every 250 ms. Write your SLA from the measurement, not from the frame length.
- Watch `end_reason` in `stream_microbatch_history`. `TIME` means frames cannot keep up and latency will accumulate.

### Requires change, a window, or approval

- **Creating a scheduler configuration** adds a schema and a long-running Java process that consumes connections and memory, plus someone to own `launch`/`shutdown` and boot-time startup. It is a service, not a one-off DDL.
- **Changing `frame_duration`** alters both latency and ROS production rate; review it together with ROS counts and mergeout time.
- **The dedicated pool**: 10% memory (2,859,483 KB) was plenty for a 1-message test, but size it from the per-frame `partition_bytes`; too small and COPY statements queue.
- **Altering the target table** (adding a primary key to resist duplicates) requires stopping the microbatch first, otherwise COPY fails.

## 12. References

- Vertica 25.4.x documentation, Integrating with Kafka: <https://docs.vertica.com/25.4.x/en/admin/integrating-kafka/>
- `KafkaSource` and the Kafka functions: <https://docs.vertica.com/25.4.x/en/sql-reference/functions/kafka-source/>
- Streaming Load Scheduler (vkconfig): <https://docs.vertica.com/25.4.x/en/admin/streaming-load/>
- Shipped documentation in the package: `/opt/vertica/packages/kafka/README.txt`, `examples/kafka_copy_load.sql`, `examples/scheduler_quickstart.sql`
