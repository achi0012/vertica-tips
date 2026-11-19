---
layout: post
title: "EON 的連線入口與 load balancing：三個 port 到底是不是三份資料"
date: 2026-11-20 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, connection, load-balancing, vip, client-driver, port, 25.4]
description: "本機 eondb（3 節點、K-Safe=0、4 shard、v25.4.0-0）有三個連線入口：1533、2533、3533，常被誤認成三份資料或三個資料庫。實測三個入口分別落在 node0001／0002／0003，但看到的 database_id、start_time、shard 與 500 萬筆事實表完全相同，session 也互相可見；關閉 client load balancing 時 30 次連線 30/30 全在同一節點，開啟後精準分成 10/10/10，代價是每次連線多約 0.8 ms。"
image: /assets/images/og/vertica-eon-connection-load-balancing-zh.png
faq:
  - q: "三個 port 是不是三份資料？"
    a: "不是。實測 1533／2533／3533 分別落在 v_eondb_node0001／0002／0003，但三個入口回報的 database_id 都是 45035996273704986、start_time 都是 2026-11-13 23:00:33、節點數都是 3、online_sales_fact 都是 5,000,000 筆、sum(sales_quantity) 都是 27,511,381。三個入口只是同一個 eondb 的三個 TCP 落地點，catalog 只有一份。"
  - q: "EON 的 load balancing 要怎麼開？"
    a: "server 端已經有一條自動產生的規則：DESCRIBE_LOAD_BALANCE_DECISION 顯示 auto_rr_default_subcluster 命中 auto_lbg_sc_default_subcluster（ROUNDROBIN、3 個位址）。client 端只要設 ConnectionLoadBalance=true 就會生效：實測 30 次連線從 30/30 全在 node0001，變成 10/10/10 平均分佈。代價是每次連線多約 0.8 ms（1.8 → 2.6 ms，15 次平均）。"
  - q: "開了 load balancing 為什麼還是連不上？"
    a: "因為重導目標不是入口 port。實測 server 回傳的位址是 10.89.10.11–13:5433（節點的 export_address），不是 1533／2533／3533。client 必須能直接連到那個位址；本機 host 之所以成功，是因為它有 podman1 的 10.89.10.0/24 路由。從外部網路連進來的 client 若只開放了 1533，第二次連線就會失敗。"
  - q: "從哪個入口連，查詢會比較快嗎？"
    a: "單一查詢沒有差別。同一條 group by + top-5 查詢從三個入口各跑 5 次，中位數是 155.3、150.4、162.8 ms，差距在雜訊範圍內；因為計畫都寫 Execute on: All Nodes，入口只決定 initiator 是誰。真正的差別在 session 落點與並行承載，不在單查詢延遲。"
  - q: "三個入口可以當成高可用嗎？"
    a: "不行。三個入口各自對應一個節點容器，入口本身不是 VIP，也沒有跨節點的重試；本機 K-Safe=0、6 筆 subscription 中只有 4 筆是 primary，任一個節點掉，它負責的 segment 就沒有第二份。入口提供的只是「三個可以連的門」，不是容錯。"
---
<!--
AI Summary: 在本機 3 節點 EON 叢集（v25.4.0-0、K-Safe=0、4 shard）實測 1533／2533／3533 三個入口：它們分別落在 node0001／0002／0003，但共用同一份 catalog 與 shard，三個入口回報的 database_id、start_time、500 萬筆事實表統計完全一致；client load balancing 關閉時 30 次連線 30/30 集中，開啟後 10/10/10 平均，重導目標是節點 export_address 10.89.10.11–13:5433 而非入口 port。
AI Keywords: Vertica, EON, connection load balancing, ConnectionLoadBalance, BackupServerNode, export_address, port, 25.4
-->
<!--more-->

---

## 一、先講結論

一台機器上開三個 port 連同一個資料庫，最常見的誤解是「三個入口等於三份資料」或「等於三個資料庫」。在這台跑著 `eondb` 的機器上，三個入口是 `192.168.100.9` 的 **1533、2533、3533**，實測結果很明確：

1. **它們是三個 TCP 落地點，對應三個節點容器。** 1533 進 `vnode01`、2533 進 `vnode02`、3533 進 `vnode03`；容器內部的 client port 都是 **5433**，主機位址分別是 `10.89.10.11`、`.12`、`.13`。
2. **資料只有一份。** 三個入口回報的 `database_id` 都是 `45035996273704986`、`start_time` 都是 `2026-11-13 23:00:33`、`v_catalog.nodes` 都是 3 列、`online_sales_fact` 都是 5,000,000 筆、`sum(sales_quantity)` 都是 27,511,381。
3. **入口決定 session 落在哪個節點。** 不開 client load balancing 時，30 次連線到 1533 是 **30/30** 全部落在 `node0001`；開啟後 30 次是 **10/10/10**。
4. **load balancing 的重導目標不是入口 port。** server 回的是 `10.89.10.11:5433` 這類 `export_address`，所以「只開 1533 就以為萬事俱備」的環境會失敗。
5. **入口不影響單查詢速度。** 同一條 group by 從三個入口各跑 5 次，中位數 155.3／150.4／162.8 ms，差距在雜訊內；差別在 session 落點與並行承載。

這篇把「三個 port 的真相」拆成三層：連線層（TCP 到哪）、session 層（誰在服務你）、資料層（資料在哪），每層都有本次實跑的輸出。

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| 資料庫 | `eondb`（EON），3 節點，K-Safe = 0 |
| 版本 | `Vertica Analytic Database v25.4.0-0` |
| shard | 4（`replica` + `segment0001`~`segment0003`），3 個 segment 合計覆蓋 2³² |
| 節點位址 | `10.89.10.11`／`.12`／`.13`，client port 皆 5433，subcluster `default_subcluster` |
| 連線入口 | `192.168.100.9` 的 1533／2533／3533（host 端 podman 發佈，對應容器 5433） |
| 同機另一個 DB | `testdb`（Enterprise，1 節點），也就佔用 host 的 **5433** |
| 測試資料表 | `online_sales.online_sales_fact`，5,000,000 筆、50,000 個 customer、`sum(sales_quantity)` = 27,511,381 |
| 客戶端 | `vertica_python` 1.4.0（Linux），`ssl=False` |
| 實測時間 | 2026-11-20 05:00 起（Asia/Taipei） |

**全程唯讀**：只跑 `SELECT`、`EXPLAIN`、`SHOW` 與 `DESCRIBE_LOAD_BALANCE_DECISION`；沒有 `CREATE`、`INSERT`、`ALTER`、`SET_LOAD_BALANCE_POLICY`，也沒有對叢集做任何變更。

主機端的入口是怎麼來的，可以直接看 systemd 產生的 podman 單元：

```
# /etc/containers/systemd/vnode01.container（systemctl cat vnode01.service）
Network=vertica-eon:ip=10.89.10.11
PublishPort=1533:5433
# vnode02：ip=10.89.10.12、PublishPort=2533:5433
# vnode03：ip=10.89.10.13、PublishPort=3533:5433
```

```bash
$ ss -ltn | grep -E "1533|2533|3533|5433"
LISTEN 0 4096 0.0.0.0:1533 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:2533 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:3533 0.0.0.0:*
LISTEN 0 128  0.0.0.0:5433 0.0.0.0:*
```

四個 LISTEN 裡，前三個是 `eondb` 的三個入口，最後一個 `5433` 是 **`testdb`**——這一點在第六節會變成一個很常見的誤連。

![三個入口、三個節點容器與同一份 catalog：關閉 client load balancing 時 30 次連線全在 node0001，開啟後分成 10/10/10]({{ '/assets/images/eon-connection-paths.svg' | relative_url }}){: loading="lazy" }

---

## 三、核心原理：一個 DB、三個落地點、每個 session 一個家

EON 的連線可以拆成三層，混在一起講就會得出「三個資料庫」的錯誤結論：

| 層 | 是什麼 | 本機實測依據 |
|----|--------|--------------|
| 連線層 | host port → 容器 5433，純 TCP 對應 | `ss -ltn` 三個 LISTEN；`PublishPort=1533:5433` |
| session 層 | session 只活在**一個**節點，由該節點當 initiator | `v_monitor.current_session.node_name` 與 `session_id` 前綴 |
| 資料層 | catalog、shard、subscription 全叢集共用 | 三個入口的 `database_id`、`v_catalog.shards` 完全一致 |

連線層是三個門，session 層是「你進了哪一戶」，資料層是「倉庫只有一個」。所以「三個 port」不代表三份資料，只代表三個入口；反過來說，**一個入口也不代表一個節點的高可用**，因為入口後面就是那一個容器，容器重啟就是斷線。

session 的歸屬可以從 `session_id` 直接看出來：

```
v_eondb_node0001-91:0xf4fe4
v_eondb_node0002-53:0xf6384
v_eondb_node0003-53:0xf6384
```

`session_id` 的前綴是節點名稱，後面是該節點自己的 session 序號。這也是為什麼同一支監控 SQL 在不同節點上看到的「自己的 session」永遠只有一筆——**session 不會跨節點**，跨節點的是查詢執行。

---

## 四、實測：三個入口看到的是不是同一份資料

### 4.1 入口 → 節點，一次問清楚

每個入口各開一條連線，問「你是誰」：

```sql
=> SELECT node_name, session_id FROM v_monitor.current_session;
```

```
-- 192.168.100.9:1533
v_eondb_node0001 | v_eondb_node0001-91:0xf4fe4
-- 192.168.100.9:2533
v_eondb_node0002 | v_eondb_node0002-53:0xf6384
-- 192.168.100.9:3533
v_eondb_node0003 | v_eondb_node0003-53:0xf6384
```

三個入口、三個節點，一對一，沒有例外。再把三個入口的資料庫身分攤開：

```sql
=> SELECT database_id, database_name, start_time, export_subnet, load_balance_policy
   FROM v_catalog.databases;
```

```
45035996273704986 | eondb | 2026-11-13 23:00:33.974277+00:00 | 0 | none
```

三個入口印出來的是同一列。`start_time` 是資料庫啟動時間，不是 session 時間，這一欄相同就代表是同一個資料庫實例。

### 4.2 同一份資料、同一組 shard、同一批數字

同一組查詢在三個入口各跑一次：

```sql
=> SELECT count(*) AS rows FROM online_sales.online_sales_fact;
=> SELECT sum(sales_quantity) AS qty, count(DISTINCT customer_key) AS customers
   FROM online_sales.online_sales_fact;
=> SELECT count(*) AS segments, sum(upper_hash_bound - lower_hash_bound + 1) AS covered
   FROM v_catalog.shards WHERE shard_type = 'Segment';
=> SELECT count(*) FROM v_catalog.nodes;
```

| 查詢 | 1533 | 2533 | 3533 |
|------|------|------|------|
| `online_sales_fact` 筆數 | 5,000,000 | 5,000,000 | 5,000,000 |
| `sum(sales_quantity)` | 27,511,381 | 27,511,381 | 27,511,381 |
| `count(DISTINCT customer_key)` | 50,000 | 50,000 | 50,000 |
| segment shard 數／覆蓋 hash 值 | 3／4,294,967,296 | 3／4,294,967,296 | 3／4,294,967,296 |
| `v_catalog.nodes` 列數 | 3 | 3 | 3 |

`3 × segment` 的 hash 區間加起來剛好 4,294,967,296 = 2³²，沒有缺口。如果三個入口是三個獨立資料庫，`v_catalog.nodes` 不可能都回 3 列，`shards` 也不可能都看到同一組邊界。

### 4.3 session 只在一個節點，但大家都看得見

同時從三個入口各留一條連線，再從第四條連線（走 1533）查全叢集的 session：

```sql
=> SELECT node_name, count(*) AS sessions FROM v_monitor.sessions
   WHERE user_name = 'dbadmin' GROUP BY node_name ORDER BY node_name;
```

```
v_eondb_node0001 | 2
v_eondb_node0002 | 1
v_eondb_node0003 | 1
```

（多出來的那一筆是查詢自己的 session。）從 `node0001` 這條連線，看得到 `node0002`、`node0003` 上的 session，而且 `client_hostname` 都是 `192.168.100.9:埠號`：

```sql
=> SELECT node_name, client_hostname, session_id FROM v_monitor.sessions
   WHERE user_name = 'dbadmin' ORDER BY login_timestamp DESC LIMIT 4;
```

```
v_eondb_node0001 | 192.168.100.9:42814 | v_eondb_node0001-91:0xf4fe7
v_eondb_node0003 | 192.168.100.9:33206 | v_eondb_node0003-53:0xf6384
v_eondb_node0002 | 192.168.100.9:32842 | v_eondb_node0002-53:0xf6384
v_eondb_node0001 | 192.168.100.9:42810 | v_eondb_node0001-91:0xf4fe4
```

**這裡有一個實務上的副作用**：三個入口的來源 IP 都是同一個 `192.168.100.9`，只是來源 port 不同。所以「依 client IP 做 routing rule」在這種 single-host 入口的環境裡，分不出你走的是哪個 port；要分流只能靠帳號、role、workload 或不同的入口 IP（VIP／多網卡）。

### 4.4 EXPLAIN：initiator 不同，計畫一字不差

同一條查詢，從 1533 與 2533 各跑一次 `EXPLAIN`：

```sql
=> EXPLAIN SELECT count(*) FROM online_sales.online_sales_fact;
```

```
+-GROUPBY NOTHING [Cost: 2, Rows: 1 (ROW COUNT ONLY)] (PATH ID: 1)
|  Aggregates: count(*)
|  Execute on: All Nodes
| +---> STORAGE ACCESS for online_sales_fact [Cost: 1, Rows: 5M (1 RLE) (ROW COUNT ONLY)] (PATH ID: 2)
| |      Projection: default_namespace.online_sales.online_sales_fact_super
| |      Execute on: All Nodes
```

計畫完全相同，差別在 GraphViz 標頭的 `All Nodes Vector`：

```
-- 從 1533 連
node[0]=v_eondb_node0001 (initiator) Up
node[1]=v_eondb_node0002 (executor) Up
node[2]=v_eondb_node0003 (executor) Up

-- 從 2533 連
node[0]=v_eondb_node0002 (initiator) Up
node[1]=v_eondb_node0001 (executor) Up
node[2]=v_eondb_node0003 (executor) Up
```

`node[0]` 就是回答你連線的那個節點，它負責彙總與回傳，其他節點是 executor。同一份計畫裡還有一段值得記住的資訊：

```
Participating subscriptions:
  replica: [<0, 1, 2>]
  segment0001: [<0>]
  segment0002: [<1>]
  segment0003: [<2>]
```

這段就是「哪個節點持有哪些 shard」在計畫裡的形式。三條 `Execute on: All Nodes` 的節點，加上 `StorageUnionStep`，代表即使你只連一個入口，查詢仍然讀三個 segment。

### 4.5 入口不影響查詢時間

同一條 `group by + top 5` 查詢，每個入口各跑 5 次（每次重新建連線）：

```sql
=> SELECT customer_key, sum(sales_quantity) AS q
   FROM online_sales.online_sales_fact
   GROUP BY customer_key ORDER BY q DESC LIMIT 5;
```

| 入口 | 5 次耗時（ms） | 中位數 |
|------|----------------|--------|
| 1533（node0001） | 147.0、148.8、158.3、155.3、247.0 | 155.3 |
| 2533（node0002） | 144.4、201.6、158.1、149.4、150.4 | 150.4 |
| 3533（node0003） | 149.4、207.5、143.7、217.1、162.8 | 162.8 |

中位數差 12 ms，但每一次都有 40–100 ms 的抖动，這個差距不能當成「哪個入口比較快」的證據。真正決定時間的是資料量與 depot 命中，不是入口。

---

## 五、Load balancing：從 30/30 到 10/10/10

### 5.1 預設：入口把你釘在一個節點

每個入口各連 30 次（每次重新建線），不開 client load balancing：

| 入口 | node0001 | node0002 | node0003 |
|------|----------|----------|----------|
| 1533 | **30** | 0 | 0 |
| 2533 | 0 | **30** | 0 |
| 3533 | 0 | 0 | **30** |

「30/30」不是巧合，是因為 `PublishPort=1533:5433` 只指向 `vnode01`。應用程式若把連線字串寫死成一個 port，等於把所有 session 壓在同一個節點——這就是文件講的「連線集中在單一 host 會吃掉它的 CPU 與記憶體」。

### 5.2 開啟 client load balancing：精準的 10/10/10

同一批測試，加上 `connection_load_balance=True`（等同 JDBC 的 `ConnectionLoadBalance=true`）：

| 入口 | node0001 | node0002 | node0003 |
|------|----------|----------|----------|
| 1533 | 10 | 10 | 10 |
| 2533 | 10 | 10 | 10 |
| 3533 | 10 | 10 | 10 |

三個入口各自都均分，因為**決定權在 server**：client 只是舉手說「我願意被重導」，實際選哪個節點是收到請求的那個節點依 ROUNDROBIN 指標決定的。就算只給一個備援位址（`backup_server_node=[2533]`），9 次連線照樣 3/3/3——備援清單只影響「連不上時去哪裡」，不影響「連上之後被導去哪裡」。

### 5.3 機制：一個請求、一次重導、一個位址

`vertica_python` 的實作是把 PostgreSQL 的 SSLRequest 換成 load balance 請求：連上入口後送一個 `LoadBalanceRequest`，server 回 `Y` 加上 host/port，client 斷線重連到那個位址。把重導後的 peer 印出來：

```
PEER AFTER LB: ('10.89.10.12', 5433)  ORIG: 192.168.100.9 1533
PEER AFTER LB: ('10.89.10.13', 5433)  ORIG: 192.168.100.9 1533
PEER AFTER LB: ('10.89.10.11', 5433)  ORIG: 192.168.100.9 2533
```

**重導目標是 `10.89.10.11–13:5433`，不是 1533／2533／3533。** 這個位址就是 `v_catalog.nodes.export_address`：

```sql
=> SELECT node_name, node_address, export_address, export_address_family FROM v_catalog.nodes;
```

```
v_eondb_node0001 | 10.89.10.11 | 10.89.10.11 | ipv4
v_eondb_node0002 | 10.89.10.12 | 10.89.10.12 | ipv4
v_eondb_node0003 | 10.89.10.13 | 10.89.10.13 | ipv4
```

決策過程也可以直接問出來：

```sql
=> SELECT DESCRIBE_LOAD_BALANCE_DECISION('192.168.100.9');
```

```
Describing load balance decision for address [192.168.100.9] on subcluster [default_subcluster]
Load balance cache internal version id (node-local): [1]
Considered rule [auto_rr_default_subcluster] subcluster interior filter  [default_subcluster]... current subcluster matches this rule
Matched to load balance group [auto_lbg_sc_default_subcluster] the group has policy [ROUNDROBIN] number of addresses [3]
(0) LB Address: [10.89.10.11]:5433
(1) LB Address: [10.89.10.12]:5433
(2) LB Address: [10.89.10.13]:5433
Chose address at position [0]
Routing table decision: Success. Load balance redirect to: [10.89.10.11] port [5433]
```

三個重點：

1. 規則是 **自動產生的**（`auto_rr_default_subcluster`、`auto_lbg_sc_default_subcluster`），所以在 `load_balance_groups` 裡查不到——本機這張表是 0 列。
2. 群組政策是 `ROUNDROBIN`、成員 3 個位址，所以 30 次連線會是 10/10/10 而不是隨機分佈。
3. 重導只做一次（文件明講 "This redirection only takes place once"），不會被踢來踢去。

### 5.4 成本：每次連線多約 0.8 ms

連線本身（不含查詢）各量 15 次：

| 設定 | 最短 | 最長 | 平均 |
|------|------|------|------|
| 不開 CLB | 1.6 ms | 2.3 ms | **1.8 ms** |
| 開 CLB | 2.5 ms | 2.7 ms | **2.6 ms** |

多出來的就是「連上入口 → 問 → 斷線 → 重連」這一段。對長連線、連線池的應用幾乎無感；對「每次請求都新建連線」的短連線服務，等於多 44% 的連線延遲——這種情況該做的是連線池，不是關掉 CLB。

### 5.5 failover：入口掛掉時誰接手

`BackupServerNode` 是另一個機制，跟 CLB 不同：CLB 是「連上之後被重導」，備援清單是「連不上時換一個」。實測三種情況：

| 情境 | 結果 | 耗時 |
|------|------|------|
| 連 4533（沒人聽）且無備援 | `ConnectionError: Failed to establish a connection to the primary server or any backup address.` | 0.2 ms |
| 連 4533，備援 1533／2533／3533 | 成功，落在 `node0001` | 6.0 ms |
| 連 4533，備援 4633／4733（都不存在） | 同上錯誤訊息 | 5.4 ms |

所以 client 端該設的是**兩個一起**：`ConnectionLoadBalance=true` 負責分散，`BackupServerNode`（或連線字串的 host 清單）負責在入口不可用時換門。只設前者，入口一掛就沒戲；只設後者，連線永遠集中在第一個活著的節點。

### 5.6 為什麼 `load_balance_policy` 寫的是 none

`v_catalog.databases` 的 `load_balance_policy` 是 `none`，但 LB 明明有效。這一欄描述的是「有沒有一條 policy-based 的路由規則」（依來源 IP 分流的那些），不是「classic 的 ROUNDROBIN 有沒有開」。真正的答案在 `DESCRIBE_LOAD_BALANCE_DECISION`：它列出實際命中的規則與群組。

用欄位判斷「LB 沒開」而把 `ConnectionLoadBalance` 關掉，是這台機器上很容易犯的錯。**要驗證，就跑決策函式，不要讀欄位。**

---

## 六、常見錯誤與反模式

**錯誤 1：把 1533／2533／3533 當三個資料庫，各寫各的。**
三個入口的 `database_id` 相同、`start_time` 相同、資料表筆數相同——它們是同一個 `eondb`。當成三個 DB 去跑 ETL，等於對同一份資料重複寫入；本機是唯讀環境，我沒有實測寫入衝突，但從 catalog 共用這點就能推論：三個入口看到的是同一份 catalog，不存在「各自獨立的寫入」。

**錯誤 2：把入口當 VIP／HA。**
入口後面就是一個容器。`PublishPort=1533:5433` 對應 `vnode01`，容器重啟，這條入口就斷。再加上本機 K-Safe=0（6 筆 subscription 只有 4 筆 primary），節點掉了連資料都沒有第二份。入口是「三個門」，不是「三道鎖」。

**錯誤 3：開了 CLB，但防火牆只放行入口 port。**
重導目標是 `10.89.10.11–13:5433`。如果 client 只能到 1533，第一次連線成功、第二次被導去 5433 就失敗。本機能通是因為 host 有 `10.89.10.0/24 dev podman1` 的路由；跨網段的環境要嘛開放 5433，要嘛把 `export_address` 設成 client 可達的位址。

**錯誤 4：連 `192.168.100.9:5433` 以為連到 eondb。**
host 的 5433 是 **`testdb`**（Enterprise，1 節點，同樣 v25.4.0-0）。實測 `select current_database()` 回 `testdb`。這個錯誤不會報錯，只會讓你對著另一個資料庫查半天，還會覺得「怎麼 shard 是 0 列」。

**錯誤 5：以為「入口都通」就代表資料都一樣。**
入口通只證明 TCP 通。要證明是同一份資料，至少要比對 `database_id`、`start_time`、`v_catalog.nodes` 列數、`v_catalog.shards` 邊界，以及一張事實表的 `count(*)` 與 `sum()`。少比一項，就可能把「連到備援叢集」當成「連到同一個叢集」。

---

## 七、風險與建議

**現在就能做（唯讀、低風險）**

1. 用 `SELECT node_name, session_id FROM v_monitor.current_session` 建立「入口 → 節點」對照表，放進 runbook。
2. 用 `DESCRIBE_LOAD_BALANCE_DECISION('<client_ip>')` 驗證每個網段實際會走到哪個群組、什麼政策；這比讀 `load_balance_policy` 可靠。
3. 比對三個入口的 `database_id`、`start_time`、`v_catalog.shards`、一張事實表的 `count(*)`，寫成每日巡檢的一支 SQL。
4. 監控 `v_monitor.sessions` 的節點分佈；三個節點長期 30/0/0 就代表 client 沒有開 CLB 或連線字串寫死。
5. 量一次「連線成本」：CLB 開了多 0.8 ms，用這個數字說服應用端開連線池，而不是關 CLB。

**需要變更（要窗口、要核准）**

1. 開放 client 到節點 `5433` 的網路路徑，或把 `export_address` 設成 client 可達的位址——這是 CLB 生效的前提。
2. 在 client 端設定 `ConnectionLoadBalance=true` 與 `BackupServerNode`／host 清單；這是應用設定變更，要跟應用團隊排程。
3. 設定 policy-based 的 routing rule（`CREATE NETWORK ADDRESS`／`CREATE LOAD BALANCE GROUP`／`CREATE ROUTING RULE`）——本機沒有建立，屬文件整理。
4. 提高 K-Safe 或增加節點：本機 CE 授權節點上限 3 且已用滿，這不是改設定能解決的。

**不建議**

把三個入口當成三個資料庫、用入口 port 當成 HA、或只看 `load_balance_policy` 欄位就判定 LB 狀態。這篇的所有數字都是唯讀查出來的；routing rule 與 drain 行為沒有在本機建立或演練，屬於文件整理。

---

## 八、參考資料

- About native connection load balancing（25.4）：<https://docs.vertica.com/25.4.x/en/admin/managing-client-connections/connection-load-balancing/about-native-connection-load-balancing/>
- JDBC connection properties（`ConnectionLoadBalance`、`BackupServerNode`）：<https://docs.vertica.com/25.4.x/en/connecting-to/client-libraries/accessing/java/creating-and-configuring-connection/jdbc-connection-properties>
- Load balancing（client 端設定）：<https://docs.vertica.com/25.4.x/en/connecting-to/client-libraries/accessing/ccpp/load-balancing>
- Managing client connections（drain、`MaxClientSessions`）：<https://docs.vertica.com/25.4.x/en/admin/managing-client-connections/>
- v_catalog.nodes（`export_address`）：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/nodes/>
- v_monitor.current_session：<https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/current-session/>

---

*本文數字的來源：2026-11-20 在本機 `eondb`（EON，3 節點、K-Safe=0、4 shard，v25.4.0-0）的唯讀查詢與連線實驗。沒有對叢集做任何變更，沒有建立 routing rule，也沒有修改任何 load balancing 設定。*
