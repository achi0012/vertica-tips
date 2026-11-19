---
layout: post_en
title: "EON Connection Endpoints and Load Balancing: Are Three Ports Three Copies of Data?"
date: 2026-11-20 05:00:00 +0800
categories: vertica eon
tags: [vertica, eon, connection, load-balancing, vip, client-driver, port, 25.4]
description: "This eondb cluster (three nodes, K-Safe=0, four shards, v25.4.0-0) exposes three connection endpoints — 1533, 2533, and 3533 — that are routinely mistaken for three copies of the data or three databases. Measured: each port lands on node0001, node0002, or node0003, yet all three report the same database_id, the same start_time, the same shards, and the same five-million-row fact table. With client load balancing off, 30 out of 30 connections pinned to one node; with it on, they split exactly 10/10/10, for roughly 0.8 ms of extra connect time."
image: /assets/images/og/vertica-eon-connection-load-balancing-en.png
faq:
  - q: "Are three ports three copies of the data?"
    a: "No. Ports 1533, 2533, and 3533 land on v_eondb_node0001, node0002, and node0003 respectively, but every one of them reports database_id 45035996273704986, start_time 2026-11-13 23:00:33, three nodes, 5,000,000 rows in online_sales_fact, and sum(sales_quantity) of 27,511,381. They are three TCP entry points into one eondb with a single catalog."
  - q: "How do you enable load balancing on EON?"
    a: "The server side already has an auto-generated rule: DESCRIBE_LOAD_BALANCE_DECISION shows auto_rr_default_subcluster matching auto_lbg_sc_default_subcluster with policy ROUNDROBIN over three addresses. On the client, set ConnectionLoadBalance=true. Measured across 30 connections, the distribution went from 30/30 on node0001 to 10/10/10, at a cost of about 0.8 ms per connection (1.8 to 2.6 ms average over 15 runs)."
  - q: "Why does load balancing still fail to connect?"
    a: "Because the redirect target is not the entry port. The server returned 10.89.10.11 through 10.89.10.13 on port 5433 — the node export_address — not 1533, 2533, or 3533. The client must be able to reach that address directly. It works from this host only because the host has a 10.89.10.0/24 route through podman1. A client that can reach only 1533 will fail on the second connection."
  - q: "Is one entry port faster than another?"
    a: "Not for a single query. The same group-by with a top-5 sort ran five times through each endpoint with medians of 155.3, 150.4, and 162.8 ms, which is inside the noise; every plan says Execute on: All Nodes, so the endpoint only decides who the initiator is. The real difference is where sessions land and how load is spread, not per-query latency."
  - q: "Do the three endpoints give me high availability?"
    a: "No. Each endpoint maps to one node container; the endpoint is not a VIP and does not retry elsewhere. This cluster runs K-Safe=0 and has six subscriptions of which only four are primary, so losing a node removes the only copy of its segment. The endpoints give you three doors, not fault tolerance."
---

<!--more-->

---

## 1. The Short Version

Running three ports that all connect to the same database invites two familiar mistakes: assuming three endpoints mean three copies of the data, or assuming they mean three databases. On this host, `eondb` is reachable at **1533, 2533, and 3533** on `192.168.100.9`. What the measurements show:

1. **They are three TCP entry points, each mapped to one node container.** 1533 reaches `vnode01`, 2533 reaches `vnode02`, 3533 reaches `vnode03`; every container listens on client port **5433**, at `10.89.10.11`, `.12`, and `.13`.
2. **There is one copy of the data.** All three endpoints report `database_id` `45035996273704986`, `start_time` `2026-11-13 23:00:33`, three rows in `v_catalog.nodes`, 5,000,000 rows in `online_sales_fact`, and `sum(sales_quantity)` = 27,511,381.
3. **The endpoint decides where the session lives.** With client load balancing off, 30 connections to 1533 landed **30/30** on `node0001`. With it on, 30 connections split **10/10/10**.
4. **The redirect target is not the entry port.** The server hands back an `export_address` such as `10.89.10.11:5433`, so environments that only open 1533 will break on the second connection.
5. **The endpoint does not change single-query speed.** The same group-by ran five times through each endpoint with medians of 155.3, 150.4, and 162.8 ms — noise. What changes is where sessions land and how load spreads.

The rest of this article separates the question into three layers — the connection layer (which TCP port), the session layer (which node serves you), and the data layer (where the data actually lives) — each backed by output from this run.

---

## 2. Test Environment

| Item | Value |
|------|-------|
| Database | `eondb` (EON), three nodes, K-Safe = 0 |
| Version | `Vertica Analytic Database v25.4.0-0` |
| Shards | 4 (`replica` plus `segment0001`–`segment0003`); the three segments cover 2³² |
| Node addresses | `10.89.10.11`/`.12`/`.13`, client port 5433 on all, subcluster `default_subcluster` |
| Entry points | 1533, 2533, 3533 on `192.168.100.9` (host-side podman publishes to container 5433) |
| Another database on the same host | `testdb` (Enterprise, one node), which is what occupies host port **5433** |
| Test table | `online_sales.online_sales_fact`, 5,000,000 rows, 50,000 customers, `sum(sales_quantity)` = 27,511,381 |
| Client | `vertica_python` 1.4.0 on Linux, `ssl=False` |
| Run time | 2026-11-20 from 05:00 (Asia/Taipei) |

**Read-only throughout**: only `SELECT`, `EXPLAIN`, `SHOW`, and `DESCRIBE_LOAD_BALANCE_DECISION`. No `CREATE`, `INSERT`, `ALTER`, `SET_LOAD_BALANCE_POLICY`, and no cluster changes.

Where the host-side entry points come from is visible in the systemd-generated podman units:

```
# /etc/containers/systemd/vnode01.container (systemctl cat vnode01.service)
Network=vertica-eon:ip=10.89.10.11
PublishPort=1533:5433
# vnode02: ip=10.89.10.12, PublishPort=2533:5433
# vnode03: ip=10.89.10.13, PublishPort=3533:5433
```

```bash
$ ss -ltn | grep -E "1533|2533|3533|5433"
LISTEN 0 4096 0.0.0.0:1533 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:2533 0.0.0.0:*
LISTEN 0 4096 0.0.0.0:3533 0.0.0.0:*
LISTEN 0 128  0.0.0.0:5433 0.0.0.0:*
```

Three of those four listeners belong to `eondb`. The fourth, **5433**, is `testdb` — a detail that turns into a real incident pattern in section 6.

![Three entry points, three node containers, and one catalog: 30 connections pinned to node0001 with client load balancing off, split 10/10/10 with it on]({{ '/assets/images/eon-connection-paths.svg' | relative_url }}){: loading="lazy" }

---

## 3. How It Works: One Database, Three Landing Points, One Home Per Session

EON connections break into three layers. Conflating them is how people arrive at "three databases":

| Layer | What it is | Evidence from this run |
|-------|-----------|------------------------|
| Connection | host port to container 5433, pure TCP mapping | Three LISTEN sockets; `PublishPort=1533:5433` |
| Session | a session lives on **one** node, which acts as initiator | `v_monitor.current_session.node_name` and the `session_id` prefix |
| Data | catalog, shards, subscriptions are cluster-wide | Identical `database_id` and `v_catalog.shards` from all three endpoints |

The connection layer is three doors, the session layer is which house you walked into, and the data layer is a single warehouse. So three ports do not mean three copies of the data — and, in the other direction, one endpoint does not mean high availability, because behind it sits exactly one container, and a container restart drops the connection.

Session ownership is visible directly in `session_id`:

```
v_eondb_node0001-91:0xf4fe4
v_eondb_node0002-53:0xf6384
v_eondb_node0003-53:0xf6384
```

The prefix is the node name; the suffix is that node's own session counter. It also explains why a monitoring query run on a node always sees exactly one "own" session: **sessions do not span nodes**. Execution does.

---

## 4. Do All Three Endpoints See the Same Data?

### 4.1 Endpoint to node, one query

One connection per endpoint, asking who is serving:

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

Three endpoints, three nodes, one-to-one, no exceptions. The database identity from any of them:

```sql
=> SELECT database_id, database_name, start_time, export_subnet, load_balance_policy
   FROM v_catalog.databases;
```

```
45035996273704986 | eondb | 2026-11-13 23:00:33.974277+00:00 | 0 | none
```

Identical from all three. `start_time` is when the database started, not the session, so a matching value means the same database instance.

### 4.2 Same data, same shards, same numbers

The same four queries through each endpoint:

```sql
=> SELECT count(*) AS rows FROM online_sales.online_sales_fact;
=> SELECT sum(sales_quantity) AS qty, count(DISTINCT customer_key) AS customers
   FROM online_sales.online_sales_fact;
=> SELECT count(*) AS segments, sum(upper_hash_bound - lower_hash_bound + 1) AS covered
   FROM v_catalog.shards WHERE shard_type = 'Segment';
=> SELECT count(*) FROM v_catalog.nodes;
```

| Query | 1533 | 2533 | 3533 |
|-------|------|------|------|
| `online_sales_fact` rows | 5,000,000 | 5,000,000 | 5,000,000 |
| `sum(sales_quantity)` | 27,511,381 | 27,511,381 | 27,511,381 |
| `count(DISTINCT customer_key)` | 50,000 | 50,000 | 50,000 |
| Segment shards / covered hash values | 3 / 4,294,967,296 | 3 / 4,294,967,296 | 3 / 4,294,967,296 |
| `v_catalog.nodes` rows | 3 | 3 | 3 |

The three segment ranges add up to exactly 4,294,967,296 = 2³², with no gap. If the endpoints were separate databases, `v_catalog.nodes` could not return three rows from each, and the shard boundaries would not agree.

### 4.3 Sessions live on one node but are visible everywhere

Hold one connection open per endpoint, then query the cluster-wide session list through a fourth connection to 1533:

```sql
=> SELECT node_name, count(*) AS sessions FROM v_monitor.sessions
   WHERE user_name = 'dbadmin' GROUP BY node_name ORDER BY node_name;
```

```
v_eondb_node0001 | 2
v_eondb_node0002 | 1
v_eondb_node0003 | 1
```

(The extra one is the querying session itself.) From `node0001`, sessions on `node0002` and `node0003` are visible, and every `client_hostname` is `192.168.100.9:<port>`:

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

**This has a practical side effect**: all three endpoints come from the same source IP, `192.168.100.9`, differing only in source port. Routing rules keyed on client IP therefore cannot distinguish which port you used. To spread connections by entry point you need different entry IPs (VIP, multiple NICs) or routing on user, role, or workload instead.

### 4.4 Same plan, different initiator

The same query explained through 1533 and through 2533:

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

The plan is identical; the difference is the `All Nodes Vector` header in the GraphViz output:

```
-- connected through 1533
node[0]=v_eondb_node0001 (initiator) Up
node[1]=v_eondb_node0002 (executor) Up
node[2]=v_eondb_node0003 (executor) Up

-- connected through 2533
node[0]=v_eondb_node0002 (initiator) Up
node[1]=v_eondb_node0001 (executor) Up
node[2]=v_eondb_node0003 (executor) Up
```

`node[0]` is the node answering the connection; it aggregates and returns results while the others execute. The plan also carries the shard map:

```
Participating subscriptions:
  replica: [<0, 1, 2>]
  segment0001: [<0>]
  segment0002: [<1>]
  segment0003: [<2>]
```

That block is "which node holds which shard" in plan form. Three `Execute on: All Nodes` steps plus `StorageUnionStep` mean a single entry point still reads all three segments.

### 4.5 The endpoint does not change query time

The same `group by` with a `top 5` sort, five runs per endpoint with a fresh connection each time:

```sql
=> SELECT customer_key, sum(sales_quantity) AS q
   FROM online_sales.online_sales_fact
   GROUP BY customer_key ORDER BY q DESC LIMIT 5;
```

| Endpoint | Five runs (ms) | Median |
|----------|----------------|--------|
| 1533 (node0001) | 147.0, 148.8, 158.3, 155.3, 247.0 | 155.3 |
| 2533 (node0002) | 144.4, 201.6, 158.1, 149.4, 150.4 | 150.4 |
| 3533 (node0003) | 149.4, 207.5, 143.7, 217.1, 162.8 | 162.8 |

A 12 ms spread in medians, against 40–100 ms of run-to-run jitter, is not evidence that one endpoint is faster. What determines the time is data volume and depot hits, not the entry point.

---

## 5. Load Balancing: From 30/30 to 10/10/10

### 5.1 Default: the endpoint pins you to one node

Thirty connections per endpoint, each a fresh connection, client load balancing off:

| Endpoint | node0001 | node0002 | node0003 |
|----------|----------|----------|----------|
| 1533 | **30** | 0 | 0 |
| 2533 | 0 | **30** | 0 |
| 3533 | 0 | 0 | **30** |

30/30 is not a coincidence; `PublishPort=1533:5433` points only at `vnode01`. An application that hardcodes one port puts every session on one node, which is exactly the overhead concentration the documentation warns about.

### 5.2 With client load balancing: a clean 10/10/10

The same test with `connection_load_balance=True` (the equivalent of JDBC's `ConnectionLoadBalance=true`):

| Endpoint | node0001 | node0002 | node0003 |
|----------|----------|----------|----------|
| 1533 | 10 | 10 | 10 |
| 2533 | 10 | 10 | 10 |
| 3533 | 10 | 10 | 10 |

All three endpoints spread evenly, because **the server decides**: the client only signals willingness, and the node receiving the request picks the target with its ROUNDROBIN pointer. Even with a single backup address (`backup_server_node=[2533]`), nine connections still split 3/3/3 — the backup list governs where you go when a connection fails, not where you get redirected after one succeeds.

### 5.3 The mechanism: one request, one redirect, one address

`vertica_python` replaces the PostgreSQL SSLRequest with a load balance request: after connecting to the entry point it sends `LoadBalanceRequest`, the server answers `Y` plus a host and port, and the client disconnects and reconnects there. Printing the peer address after the redirect:

```
PEER AFTER LB: ('10.89.10.12', 5433)  ORIG: 192.168.100.9 1533
PEER AFTER LB: ('10.89.10.13', 5433)  ORIG: 192.168.100.9 1533
PEER AFTER LB: ('10.89.10.11', 5433)  ORIG: 192.168.100.9 2533
```

**The redirect target is `10.89.10.11`–`.13` on 5433, not 1533, 2533, or 3533.** That address is `v_catalog.nodes.export_address`:

```sql
=> SELECT node_name, node_address, export_address, export_address_family FROM v_catalog.nodes;
```

```
v_eondb_node0001 | 10.89.10.11 | 10.89.10.11 | ipv4
v_eondb_node0002 | 10.89.10.12 | 10.89.10.12 | ipv4
v_eondb_node0003 | 10.89.10.13 | 10.89.10.13 | ipv4
```

The decision itself can be interrogated:

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

Three things worth noting:

1. The rule is **auto-generated** (`auto_rr_default_subcluster`, `auto_lbg_sc_default_subcluster`), which is why `load_balance_groups` is empty on this cluster — zero rows.
2. The group policy is `ROUNDROBIN` over three addresses, which is why 30 connections split 10/10/10 rather than randomly.
3. Redirection happens once; the documentation states plainly that "this redirection only takes place once".

### 5.4 The cost: about 0.8 ms per connection

Connect time alone (no query), 15 samples each:

| Setting | Minimum | Maximum | Average |
|---------|---------|---------|---------|
| CLB off | 1.6 ms | 2.3 ms | **1.8 ms** |
| CLB on | 2.5 ms | 2.7 ms | **2.6 ms** |

The difference is the "connect, ask, disconnect, reconnect" round. For long-lived connections and pools this is invisible; for a service that opens a connection per request it is 44% more connect latency, and the fix is a connection pool, not disabling CLB.

### 5.5 Failover: who takes over when an endpoint dies

`BackupServerNode` is a different mechanism from CLB: CLB redirects after you connect, while the backup list is used when you cannot connect. Three measured cases:

| Scenario | Result | Time |
|----------|--------|------|
| Connect to 4533 (nothing listening), no backups | `ConnectionError: Failed to establish a connection to the primary server or any backup address.` | 0.2 ms |
| Connect to 4533 with backups 1533/2533/3533 | Success, session on `node0001` | 6.0 ms |
| Connect to 4533 with backups 4633/4733 (neither exists) | Same error | 5.4 ms |

So clients should set **both**: `ConnectionLoadBalance=true` to spread sessions, and `BackupServerNode` (or a host list in the connection string) to survive an endpoint being down. Only the first means an endpoint failure is fatal; only the second means every connection piles onto the first reachable node.

### 5.6 Why `load_balance_policy` says none

`v_catalog.databases.load_balance_policy` is `none`, yet load balancing demonstrably works. That column describes whether a policy-based routing rule exists — the kind that routes by source IP — not whether classic ROUNDROBIN is active. The authoritative answer comes from `DESCRIBE_LOAD_BALANCE_DECISION`, which prints the rule and group actually matched.

Reading that column as "load balancing is off" and disabling `ConnectionLoadBalance` is an easy mistake here. **Verify with the decision function, not the column.**

---

## 6. Common Mistakes and Anti-Patterns

**Mistake 1: treating 1533, 2533, and 3533 as three databases and writing to each.**
The three endpoints share `database_id`, `start_time`, and row counts — they are one `eondb`. Running ETL against them as if they were separate databases means writing the same data repeatedly. This environment is read-only, so I did not measure write conflicts, but the shared catalog makes the conclusion unavoidable: there is no "independent write path" behind a different port.

**Mistake 2: treating an endpoint as a VIP or as HA.**
Behind the endpoint is one container. `PublishPort=1533:5433` maps to `vnode01`; restart the container and that endpoint is gone. Add K-Safe=0 (six subscriptions, four primary) and a lost node means the only copy of its segment is gone too. Endpoints are three doors, not three locks.

**Mistake 3: enabling CLB but only opening the entry port in the firewall.**
The redirect target is `10.89.10.11`–`.13` on 5433. A client that can reach 1533 succeeds on the first connection and fails on the redirected second one. It works from this host only because of the `10.89.10.0/24 dev podman1` route; across network segments you either open 5433 or set `export_address` to something the client can reach.

**Mistake 4: connecting to `192.168.100.9:5433` and assuming it is eondb.**
Host port 5433 is **`testdb`** (Enterprise, one node, also v25.4.0-0). `select current_database()` returns `testdb`. Nothing errors; you just spend an afternoon querying the wrong database and wondering why `v_catalog.shards` is empty.

**Mistake 5: assuming "all endpoints respond" proves "all endpoints see the same data".**
Reachability only proves TCP. To prove identity, compare `database_id`, `start_time`, the `v_catalog.nodes` row count, the `v_catalog.shards` boundaries, and a fact table's `count(*)` and `sum()`. Skip one and you can mistake a standby cluster for the cluster you meant.

---

## 7. Risks and Recommendations

**Safe now (read-only, low risk)**

1. Build an endpoint-to-node map with `SELECT node_name, session_id FROM v_monitor.current_session` and put it in the runbook.
2. Validate each client subnet with `DESCRIBE_LOAD_BALANCE_DECISION('<client_ip>')`; it is far more reliable than reading `load_balance_policy`.
3. Add a daily check that compares `database_id`, `start_time`, `v_catalog.shards`, and a fact table's `count(*)` across all endpoints.
4. Monitor the per-node session distribution in `v_monitor.sessions`; a long-standing 30/0/0 pattern means clients never enabled CLB or hardcoded one port.
5. Measure connect cost once (0.8 ms with CLB on) and use that number to argue for a connection pool rather than disabling CLB.

**Requires change (window and approval)**

1. Open the network path from clients to node port 5433, or set `export_address` to an address clients can reach — the prerequisite for CLB to work at all.
2. Set `ConnectionLoadBalance=true` and `BackupServerNode` (or a host list) in the application; this is an application configuration change and needs the app team.
3. Configure policy-based routing (`CREATE NETWORK ADDRESS`, `CREATE LOAD BALANCE GROUP`, `CREATE ROUTING RULE`) — not created here; treat as documentation.
4. Raise K-Safe or add nodes: the Community Edition licence allows three nodes and this cluster already uses all three, so no configuration change solves it.

**Not recommended**

Treating the endpoints as three databases, treating an endpoint as HA, or judging load balancing state from the `load_balance_policy` column alone. Every number in this article came from read-only queries; routing rules and drain behaviour were not created or exercised here and are documented as such.

---

## 8. References

- About native connection load balancing (25.4): <https://docs.vertica.com/25.4.x/en/admin/managing-client-connections/connection-load-balancing/about-native-connection-load-balancing/>
- JDBC connection properties (`ConnectionLoadBalance`, `BackupServerNode`): <https://docs.vertica.com/25.4.x/en/connecting-to/client-libraries/accessing/java/creating-and-configuring-connection/jdbc-connection-properties>
- Load balancing (client configuration): <https://docs.vertica.com/25.4.x/en/connecting-to/client-libraries/accessing/ccpp/load-balancing>
- Managing client connections (drain, `MaxClientSessions`): <https://docs.vertica.com/25.4.x/en/admin/managing-client-connections/>
- v_catalog.nodes (`export_address`): <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-catalog-schema/nodes/>
- v_monitor.current_session: <https://docs.vertica.com/25.4.x/en/sql-reference/system-tables/v-monitor-schema/current-session/>

---

*All figures come from read-only queries and connection experiments against `eondb` (EON, three nodes, K-Safe=0, four shards, v25.4.0-0) on 2026-11-20. No cluster changes were made, no routing rules were created, and no load balancing settings were modified.*
