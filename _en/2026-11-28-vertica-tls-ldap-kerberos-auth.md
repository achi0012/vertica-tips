---
layout: post_en
title: "Vertica Secure Connections and Authentication: Choosing and Combining TLS, LDAP, and Kerberos"
date: 2026-11-28 05:00:00 +0800
categories: vertica security
tags: [vertica, security, tls, ssl, ldap, kerberos, authentication]
description: "TLS encrypts the channel, LDAP validates passwords, and Kerberos provides ticket-based single sign-on. They solve different problems and can be layered, but none replaces another. Measured on testdb (v25.4.0-0, single node) and eondb (3 nodes, K-Safe=0, 4 shards): all five TLS configurations are DISABLE, nine users share three HASH authentication records, and vsql -m require fails with 'server does not support SSL, but SSL was required'. The article also explains why port 8443 appears to have TLS, and why LDAP and Kerberos can only be marked untested on a host with no krb5.conf and no ldapsearch."
image: /assets/images/og/vertica-tls-ldap-kerberos-auth-en.png
faq:
  - q: "What is the difference between TLS and LDAP in Vertica?"
    a: "TLS only encrypts the connection and does not identify the user; LDAP only validates the password against a directory and does not encrypt anything. They stack: TLS protects the 5433 channel, then LDAP validates credentials inside it. On this host the dbadmin session reports authentication_method=Hash and ssl_state=None, so neither plane is enabled."
  - q: "Why does port 8443 negotiate TLS while vsql on 5433 says SSL is not supported?"
    a: "They are different services. Port 5433 is the client port controlled by the 'server' TLS configuration, which is DISABLE here. Port 8443 is the HTTPS service, bootstrapped at install time with self-signed certificates in /opt/vertica/config/https_certs; we measured TLSv1.2 with a CN=HttpsService certificate. TLS on 8443 says nothing about database connections."
  - q: "Does vsql encrypt by default when TLS is disabled?"
    a: "No. vsql defaults to sslmode=prefer, which tries TLS and silently falls back to plaintext without a warning. Our -m prefer connection succeeded with ssl_state=None and an empty tls_version. Only an explicit -m require produced 'server does not support SSL, but SSL was required'."
  - q: "Why does -M kerberos connect successfully with no Kerberos ticket?"
    a: "Because -M expresses a preference, not a requirement. This host has no /etc/krb5.conf, no kinit or klist, and no ticket cache, yet vsql -M kerberos connected; the same session reports authentication_method=Hash, proving password authentication was used. Verify Kerberos in v_monitor.sessions, never by whether the connection succeeded."
  - q: "What does it mean that all five TLS configurations are DISABLE?"
    a: "It means TLS is off for all five connection types: server for client-server, data_channel for internode traffic, LDAPAuth for database-to-LDAP authentication, LDAPLink for the LDAP Link service, and https for the 8443 HTTPS service. Both testdb and eondb report all five as DISABLE."
---

<!--more-->

## 1. The short version

These three terms often appear in one sentence, but they answer three different questions:

- **TLS is channel encryption.** It hides packets from a man in the middle, but it does not tell the database who you are.
- **LDAP is password validation.** The database takes the supplied credentials and binds to a directory server; the password never lives in Vertica.
- **Kerberos is ticket-based single sign-on.** The user obtains a ticket from a KDC and proves identity with it, so no password crosses the network.

You can layer them, but you cannot substitute one for another. Treating LDAP as encryption, or TLS as authentication, are the two most common misconceptions I find during security reviews.

I ran every read-only check available on both local clusters. The results:

1. **TLS is completely off on both clusters.** testdb and eondb each have five TLS configurations (`server`, `data_channel`, `LDAPAuth`, `LDAPLink`, `https`), and all 20 field combinations are `DISABLE` with empty certificate fields.
2. **The built-in check says it plainly.** `SECURITY_CONFIG_CHECK('NETWORK')` reports spread encryption off, data channel TLS off, and client-server TLS off — including the line "TLS Configuration 'server' has a certificate unset".
3. **The client does not stop you by default.** `vsql -m require` fails with `vsql: server does not support SSL, but SSL was required`, but with no flag the default `prefer` silently downgrades to plaintext and the session reports `ssl_state=None`.
4. **Authentication is HASH only.** `v_catalog.client_auth` has three rows, all `HASH`; nine users inherit them through `public`, for 27 grants. `v_monitor.sessions` reports `authentication_method=Hash`.
5. **TLS on 8443 is a separate story.** The HTTPS service serves TLSv1.2 with the install-time self-signed certificate (`CN=HttpsService`). It has nothing to do with the plaintext client port on 5433.
6. **LDAP and Kerberos cannot be tested here.** Nothing listens on 389/636, `ldapsearch` is absent, and `/etc/ldap/ldap.conf` contains only comments. There is no `/etc/krb5.conf`, no `kinit`/`klist`, and no ticket cache. This article shows the evidence and marks all setup steps as document-based and untested.

## 2. Environment and method

| Item | Value |
|---|---|
| Version | Vertica Analytic Database v25.4.0-0 (Community Edition) |
| Enterprise test database | `testdb`, single node, 127.0.0.1:5433 |
| EON test database | `eondb`, 3 nodes (`v_eondb_node0001`–`0003`, all UP), 4 shards, K-Safe=0 |
| Client | `vsql` 25.04.0000; `vertica_python` helper for eondb |
| Method | Read-only: `SELECT`, `SHOW`, `openssl s_client`, `curl`, `ss` |
| Test objects | None. No DDL was needed, so no `article_lab_*` schema was created and no setting was changed |

Start with the version and node state; this also surfaces the K-Safe warning on its own:

```sql
=> SELECT version();
 Vertica Analytic Database v25.4.0-0

=> SELECT node_name, node_state FROM v_catalog.nodes ORDER BY node_name;
     node_name     | node_state
-------------------+------------
 v_eondb_node0001  | UP
 v_eondb_node0002  | UP
 v_eondb_node0003  | UP
```

Every `eondb` connection emits `WARNING 6046: The database is currently 0-safe`. That is unrelated to TLS, but it is a reminder that this is a shared test cluster where any `ALTER` needs a maintenance window. Everything in this article against eondb is a query.

One clarification up front: **not every port needs TLS.** 5433 is the client port; 5434 is client port +1, which the documentation describes as intra-cluster communication (for example during a plan), not another TLS endpoint. TLS configurations control 5433, the internode data channel, and database-to-LDAP connections. Port 8443 belongs to the HTTPS service and uses its own bootstrap certificate.

## 3. Three planes, kept separate

| Plane | Question it answers | Vertica mechanism | State on this host |
|---|---|---|---|
| Transport | Can anyone read the packets? | TLS configuration plus `ServerTLSConfig`, `InternodeTLSConfig`, `LDAPAuthTLSConfig`, `LDAPLinkTLSConfig`, `HttpsTLSConfig` | All five configurations `DISABLE` |
| Authentication | Who are you, and where is the password checked? | `CREATE AUTHENTICATION` plus `v_catalog.client_auth` | Three records, all `HASH` |
| Authorization | What may you do? | Roles, privileges, schema USAGE | Out of scope here |

There is a fourth layer that is easy to miss: **Spread control-channel encryption**, controlled by `EncryptSpreadComm`, which is separate from TLS configurations. `SECURITY_CONFIG_CHECK` lists it independently and warns you not to change other security parameters before spread is encrypted.

Following the connection sequence makes the separation obvious. The client opens TCP to 5433; if the `server` configuration is not `DISABLE`, TLS handshakes on that socket; only then does the client send its startup packet, and the server picks an authentication record by `auth_priority` to decide between HASH, LDAP, or GSS. TLS happens before authentication and protects the credentials and subsequent SQL, but it does not know who you are. LDAP and Kerberos handle identity but leave the payload readable. That is exactly why `v_monitor.sessions` keeps `ssl_state` and `authentication_method` in separate columns.

Here is what each of the five TLS configurations controls (names and purposes from the 25.4 documentation):

| Configuration | Scope | Who is the client |
|---|---|---|
| `server` | Client-server TLS (5433) | The database is the server |
| `data_channel` | Internode data channel | Nodes are both client and server |
| `LDAPAuth` | Database-to-LDAP bind during user login | The database is the client |
| `LDAPLink` | LDAP Link user and group synchronization | The database is the client |
| `https` | The HTTPS service on 8443 | The database is the server |

## 4. TLS, measured

### 4.1 What the five configurations actually look like

```sql
=> SELECT name, owner, certificate, ca_certificate, cipher_suites, mode
   FROM v_monitor.tls_configurations ORDER BY name;
     name     |  owner  | certificate | ca_certificate | cipher_suites | cipher_list |  mode
--------------+---------+-------------+----------------+---------------+-------------+---------
 LDAPAuth     | dbadmin |             |                |               |             | DISABLE
 LDAPLink     | dbadmin |             |                |               |             | DISABLE
 data_channel | dbadmin |             |                |               |             | DISABLE
 https        | dbadmin |             |                |               |             | DISABLE
 server       | dbadmin |             |                |               |             | DISABLE
(5 rows)
```

All five are `DISABLE`, and both `certificate` and `ca_certificate` are empty. This is not a half-finished configuration; no certificate has ever been attached.

### 4.2 The built-in check covers all three layers

```sql
=> SELECT security_config_check('NETWORK');
 Spread security details:
* EncryptSpreadComm = []
Spread encryption is disabled
It is NOT safe to set/change other security config parameters while spread is not encrypted!
Please set EncryptSpreadComm to enable spread encryption first

Data Channel security details:
 TLS Configuration 'data_channel' TLSMODE is DISABLE
TLS on the data channel is disabled
Please set EncryptSpreadComm and configure TLS Configuration 'data_channel' to enable TLS on the data channel

Client-Server network security details:
* TLS Configuration 'server' TLSMODE is DISABLE
* TLS Configuration 'server' has a certificate unset
Client-Server TLS is disabled
To enable Client-Server TLS set a certificate on TLS Configuration 'server' and/or set the tlsmode to 'ENABLE' or higher
```

This output is more trustworthy than any checklist you write yourself, because the database computes it. Three facts: spread is unencrypted, internode traffic has no TLS, and client-server TLS is off with no certificate configured.

### 4.3 Which configuration each parameter points to

```sql
=> SHOW CURRENT ServerTLSConfig;
  level  |      name       | setting
---------+-----------------+---------
 DEFAULT | ServerTLSConfig | server

=> SHOW CURRENT InternodeTLSConfig;
  level  |        name        | setting
---------+--------------------+--------------
 DEFAULT | InternodeTLSConfig | data_channel

=> SHOW CURRENT HttpsTLSConfig;
  level  |      name      | setting
---------+----------------+---------
 DEFAULT | HttpsTLSConfig | https

=> SHOW CURRENT MinTLSVersion;
  level  |     name      | setting
---------+---------------+---------
 DEFAULT | MinTLSVersion | 2
```

The parameters point at the predefined configurations, which is normal. The problem is that the configurations themselves are `DISABLE`. `MinTLSVersion=2` means that once TLS is enabled, the floor is TLS 1.2.

### 4.4 Client behaviour with no TLS

Running the same `SELECT 1` with different `sslmode` values:

| Client flag | Result | Actual message |
|---|---|---|
| `-m require` | Fails | `vsql: server does not support SSL, but SSL was required` |
| `-m verify-ca` | Fails | Same |
| `-m verify-full` | Fails | Same |
| `-m allow` | Succeeds | Plaintext |
| `-m disable` | Succeeds | Plaintext |
| No flag (default `prefer`) | Succeeds | Plaintext, **with no warning** |

The failure, verbatim:

```text
$ vsql -m require -c "select 1 as ok;"
vsql: server does not support SSL, but SSL was required
```

For the successful ones, the session state is:

```sql
=> SELECT ssl_state, tls_version
   FROM v_monitor.sessions WHERE session_id = current_session();
 ssl_state | tls_version
-----------+-------------
 None      |
(1 row)
```

`ssl_state` is `None` and `tls_version` is empty. In other words, the default `prefer` silently turns "I want encryption" into "plaintext is fine" when the server has no TLS. If compliance requires encryption, hard-code `require` or stronger in the client; never rely on the default.

Confirm it at the network layer by sending a TLS ClientHello to 5433:

```text
$ openssl s_client -connect 127.0.0.1:5433 -brief
Connecting to 127.0.0.1
40270995567A0000:error:0A00010B:SSL routines:tls_validate_record_header:wrong version number:../ssl/record/methods/tlsany_meth.c:77:
```

`wrong version number` means the server replied with the Vertica protocol, not a TLS record. It corroborates the vsql error from the other direction.

There is one more ordering trap: trying to inspect ciphers before enabling TLS is rejected.

```sql
=> SELECT list_enabled_ciphers();
ERROR 6960:  SSL is not enabled
```

That is not a bug. A cipher list only makes sense after TLS is on.

### 4.5 So why does 8443 have TLS?

This is the easiest thing in the article to misread. The same `vertica` process listens on 8443:

```text
$ ss -ltnp | grep 8443
LISTEN 0  4096  0.0.0.0:8443  0.0.0.0:*  users:(("vertica",pid=3431075,fd=12))
```

A handshake succeeds:

```text
$ openssl s_client -connect 127.0.0.1:8443 -brief
Can't use SSL_get_servername
depth=1 C=US, ST=Massachusetts, L=Cambridge, O=OpenText, OU=Vertica, CN=rootca
verify error:num=19:self-signed certificate in certificate chain
CONNECTION ESTABLISHED
Protocol version: TLSv1.2
Ciphersuite: ECDHE-RSA-AES256-GCM-SHA384
```

The certificate:

```text
subject=C=US, ST=Massachusetts, L=Cambridge, O=OpenText, OU=Vertica, CN=HttpsService
issuer=C=US, ST=Massachusetts, L=Cambridge, O=OpenText, OU=Vertica, CN=rootca
notBefore=May  1 17:44:01 2026 GMT
notAfter=Apr 28 17:44:01 2036 GMT
```

Subject, issuer, and dates match `/opt/vertica/config/https_certs/1000.pem` exactly:

```text
$ ls -l /opt/vertica/config/https_certs/
-rw-r--r-- 1 dbadmin verticadba 2029 May  2  2026 1000.pem
-rw-r--r-- 1 dbadmin verticadba 1838 May  2  2026 1001.pem
-rw------- 1 dbadmin verticadba 1017 May  2  2026 dbadmin.csr
-rw------- 1 dbadmin verticadba 1704 May  2  2026 dbadmin.key
-rw------- 1 dbadmin verticadba 3122 May  2  2026 dbadmin.p12
-rw------- 1 dbadmin verticadba 1838 May  2  2026 dbadmin.pem
-rw------- 1 dbadmin verticadba 6322 May  2  2026 httpstls.json
```

The documentation is explicit: `install_vertica` generates self-signed certificates in this directory to **bootstrap** the HTTPS service on a new cluster and they are not suitable for production; certificates in the TLS configuration supersede them. The correct reading is:

- TLS on 8443 is HTTPS-service bootstrap behaviour, not something the `server` configuration enabled.
- The `https` configuration is `DISABLE` with an empty `certificate`, meaning no catalog certificate was attached, so the service falls back to the install-time pair.
- None of this implies anything about TLS on 5433.

Without `-k`, a client hits hostname validation immediately:

```text
$ curl -sS -o /dev/null https://127.0.0.1:8443/
curl: (60) SSL: certificate subject name 'HttpsService' does not match target hostname '127.0.0.1'
```

With `-k`, the service itself answers:

```text
$ curl -k -sS -D - -o /dev/null https://127.0.0.1:8443/
HTTP/1.1 401 Unauthorized
Content-Length: 215
Connection: keep-alive
Server: oatpp/1.3.0
Content-Type: application/problem+json
```

`CN=HttpsService` has no SAN, so hostname verification must fail. That is expected for a bootstrap certificate, not a broken service — but production needs a certificate with a SAN signed by an internal CA.

## 5. Authentication, measured

### 5.1 HASH only — no LDAP, GSS, TLS, or OAuth

```sql
=> SELECT auth_name, auth_method, is_auth_enabled, auth_priority,
          method_priority, is_fallthrough_enabled
   FROM v_catalog.client_auth ORDER BY auth_priority;
         auth_name         | auth_method | is_auth_enabled | auth_priority | method_priority | is_fallthrough_enabled
---------------------------+-------------+-----------------+---------------+-----------------+------------------------
 default_hash_network_ipv4 | HASH        | True            |            -1 |               2 | False
 default_hash_network_ipv6 | HASH        | True            |            -1 |               2 | False
 default_hash_local        | HASH        | True            |            -1 |               2 | False
(3 rows)
```

All three are the default HASH records with `auth_priority=-1` and fallthrough disabled. There is no `ldap`, `gss`, `tls`, or `oauth` record, and nobody has customized priorities.

`auth_priority` decides which record is tried first when a user has several; greater values win. All three defaults are -1, so there is no ordering question. `method_priority=2` is the weight of HASH itself. `is_fallthrough_enabled=False` means that once this record is selected and authentication fails, the server does not try the next one — it rejects. With only HASH this is invisible, but add LDAP and forget fallthrough, and a brief directory outage locks everyone out. That is the usual first-LDAP mistake.

Host scope:

```sql
=> SELECT auth_name, auth_host_type, auth_host_address, auth_method
   FROM v_catalog.client_auth ORDER BY auth_name;
         auth_name         | auth_host_type | auth_host_address | auth_method
---------------------------+----------------+-------------------+-------------
 default_hash_local        | LOCAL          |                   | HASH
 default_hash_network_ipv4 | HOST           | 0.0.0.0/0         | HASH
 default_hash_network_ipv6 | HOST           | ::/0              | HASH
(3 rows)
```

Local connections and every IPv4/IPv6 source use HASH. There is no source restriction and no second factor: correct credentials are enough from anywhere.

### 5.2 Who holds these records

```sql
=> SELECT count(*) AS grants, count(DISTINCT user_name) AS users,
          count(DISTINCT auth_name) AS auth_methods
   FROM v_catalog.user_client_auth;
 grants | users | auth_methods
--------+-------+--------------
     27 |     9 |            3
```

27 = 9 users × 3 records, all with `granted_to=public`. The nine are `a2v_app`, `a2v_dev_ro`, `a2v_dev_rw`, `a2v_tester1`, `a2v_tester2`, `dbadmin`, `lowi_rap`, `ro_reader`, and `test`.

Confirm nobody is LDAP-managed:

```sql
=> SELECT user_name, coalesce(ldap_dn,'(null)') AS ldap_dn,
          managed_by_auth_id, is_orphaned_from_ldap
   FROM v_catalog.users ORDER BY 1;
  user_name  | ldap_dn | managed_by_auth_id | is_orphaned_from_ldap
-------------+---------+--------------------+-----------------------
 a2v_app     |         |                  0 | f
 a2v_dev_ro  |         |                  0 | f
 a2v_dev_rw  |         |                  0 | f
 a2v_tester1 |         |                  0 | f
 a2v_tester2 |         |                  0 | f
 dbadmin     |         |                  0 | f
 lowi_rap    |         |                  0 | f
 ro_reader   |         |                  0 | f
 test        |         |                  0 | f
(9 rows)
```

`ldap_dn` is empty, `managed_by_auth_id` is 0, and `is_orphaned_from_ldap` is false for all nine.

### 5.3 What this session actually used

```sql
=> SELECT user_name, authentication_method, client_authentication_name,
          ssl_state, tls_version
   FROM v_monitor.sessions WHERE session_id = current_session();
 user_name | authentication_method | client_authentication_name | ssl_state | tls_version
-----------+-----------------------+----------------------------+-----------+-------------
 dbadmin   | Hash                  | default_hash_local         | None      |
(1 row)
```

Read together, these four columns are the most useful query in the article: `Hash` means password authentication, `default_hash_local` identifies the record that matched, and `None` means no TLS. All three planes in one row.

### 5.4 Why LDAP can only be marked untested

LDAP Link parameters are all empty:

```sql
=> SELECT parameter_name, current_value
   FROM v_monitor.configuration_parameters
   WHERE parameter_name IN ('LDAPLinkOn','LDAPLinkURL','LDAPLinkSearchBase',
         'LDAPLinkBindDN','LDAPLinkCron','LDAPLinkInterval')
   ORDER BY parameter_name;
     parameter_name     | current_value
------------------------+---------------
 LDAPLinkBindDN         |
 LDAPLinkCron           |
 LDAPLinkInterval       | 86400
 LDAPLinkOn             | 0
 LDAPLinkSearchBase     |
 LDAPLinkURL            |
(6 rows)
```

`LDAPLinkOn=0`, and both the URL and search base are empty, so the LDAP Link service never starts. The event tables are empty too:

```sql
=> SELECT count(*) AS link_events FROM v_monitor.ldap_link_events;
 link_events
-------------
           0

=> SELECT count(*) AS dryrun_events FROM v_monitor.ldap_link_dryrun_events;
 dryrun_events
---------------
             0
```

The host has no prerequisites either:

```text
$ ss -ltn | grep -E ':(389|636)\b'
(no output)

$ which ldapsearch
(no output)

$ wc -c /etc/ldap/ldap.conf
247 /etc/ldap/ldap.conf
```

The 247 bytes of `/etc/ldap/ldap.conf` are all comments, with no active directive; `ldapsearch` is not installed (the documentation lists `open-ldap-tools` as an LDAP prerequisite); and nothing listens on 389 or 636. With no directory service there is nothing to bind against, so any "LDAP authentication succeeded" output could only be fabricated. LDAP appears here as documentation steps, not measurements.

### 5.5 Kerberos is the same

Parameters:

```sql
=> SELECT parameter_name, current_value
   FROM v_monitor.configuration_parameters
   WHERE parameter_name LIKE 'Kerberos%' ORDER BY parameter_name;
    parameter_name     | current_value
-----------------------+---------------
 KerberosHostname      |
 KerberosKeytabFile    |
 KerberosRealm         |
 KerberosServiceName   | vertica
 KerberosTicketDuration| 0
(5 rows)
```

Only the service name holds its default `vertica`; realm, hostname, and keytab are empty.

Host:

```text
$ ls /etc/krb5.conf
ls: cannot access '/etc/krb5.conf': No such file or directory

$ which kinit klist
(no output)

$ env | grep -i krb5
(no output)

$ ls /tmp/krb5cc*
ls: cannot access '/tmp/krb5cc*': No such file or directory
```

No client package, no realm configuration, no ticket cache.

There is an easy false positive here. Ask for Kerberos explicitly:

```text
$ vsql -M kerberos -c "select 1 as ok;"
 ok
----
  1
(1 row)
```

It looks like Kerberos works. It does not. Query the same session:

```sql
=> SELECT authentication_method, client_authentication_name, ssl_state
   FROM v_monitor.sessions WHERE session_id = current_session();
 authentication_method | client_authentication_name | ssl_state
-----------------------+----------------------------+-----------
 Hash                  | default_hash_local         | None
(1 row)
```

`-M kerberos` means "I would prefer Kerberos"; when the server has no GSS record, the client still completes with a password and never mentions the downgrade. **Verify authentication methods on the server side in `v_monitor.sessions`, never by whether the connection succeeded.**

OAuth gives a different "not configured" signal:

```text
$ vsql -M oauth -c "select 1;"
Failed to open private key file: No such file or directory
Failed to load private key
```

## 6. Choosing and combining

| Problem | Use | Why |
|---|---|---|
| Packets can be sniffed (same datacenter, cross-region, cloud) | Client-server TLS (`server` + `ServerTLSConfig`) | Encrypts the channel without changing authentication; smallest change |
| Compliance requires mutual certificates, or service-to-service without passwords | Mutual TLS (`TRY_VERIFY`/`VERIFY_CA`) + `tls` authentication | The certificate CN is the database user name |
| People are managed in a corporate directory and offboarding must be immediate | LDAP authentication + `LDAPAuth` TLS | Passwords stay in AD; revocation happens there |
| Single sign-on inside an AD domain | Kerberos (`gss`) | Ticket-based; no password on the wire |
| Internode traffic must be opaque | `data_channel` TLS + `EncryptSpreadComm` | You need both, not one |
| The 8443 REST API is exposed | `https` configuration + a certificate with a SAN | Bootstrap self-signed certificates are for startup only |

Follow the order implied by the `SECURITY_CONFIG_CHECK` warning:

1. `EncryptSpreadComm` (requires a restart).
2. `data_channel` (internode).
3. `server` (client-server).
4. LDAP/LDAPLink TLS and the 8443 certificate replacement last.

Spread is the control channel, and the documentation states plainly that other security parameters should not be changed while it is unencrypted. Do it backwards and you end up in a mixed state — encrypted data channel, unencrypted control channel — that is painful to debug.

Three concrete scenarios:

- **One team, internal network, no AD.** Enable client-server TLS (`ENABLE`) with an internal-CA certificate and move clients to `verify-ca`. Leave LDAP and Kerberos alone; the nine local users keep using HASH. Lowest cost.
- **The company has AD and there are three DBAs.** LDAP authentication plus `LDAPAuth` TLS, keeping `default_hash_local` for dbadmin as a fallback. This is the most common combination; the hard parts are attribute mapping (`sAMAccountName` versus `uid`) and offboarding synchronization.
- **Finance or healthcare, where audit demands mutual certificates and internode encryption.** `server` at `VERIFY_CA`, `data_channel` TLS, `EncryptSpreadComm=vertica`, plus `tls` authentication for service accounts. This touches the most layers; follow the 1→4 order above.

## 7. Setup steps (from documentation, not executed)

None of the following ran on this host: TLS changes need a restart and client compatibility work, there is no directory service for LDAP, and no KDC for Kerberos. The syntax comes from the 25.4 documentation; test it on a lab cluster first.

### 7.1 Enable client-server TLS

```sql
-- 1. Keys, CA, and server certificate
=> CREATE KEY k_ca TYPE 'RSA' LENGTH 4096;
=> CREATE CA CERTIFICATE ca
   SUBJECT '/C=TW/ST=Taipei/O=Example/OU=DBA/CN=Example Root CA'
   VALID FOR 3650 KEY k_ca;

=> CREATE KEY k_server TYPE 'RSA' LENGTH 2048;
=> CREATE CERTIFICATE server_cert
   SUBJECT '/C=TW/ST=Taipei/O=Example/OU=DBA/CN=vertica.example.com'
   SIGNED BY ca KEY k_server;

-- 2. Attach to the server configuration
=> ALTER TLS CONFIGURATION server CERTIFICATE server_cert;
=> ALTER TLS CONFIGURATION server TLSMODE 'ENABLE';   -- use TRY_VERIFY or VERIFY_CA for mutual TLS
-- For mutual TLS, also:
=> ALTER TLS CONFIGURATION server ADD CA CERTIFICATES ca;

-- 3. Verify
=> SELECT name, certificate, ca_certificate, mode FROM tls_configurations WHERE name='server';
=> SELECT security_config_check('NETWORK');
```

Client verification:

```bash
vsql -h vertica.example.com -m verify-full -c "select ssl_state, tls_version from v_monitor.sessions where session_id=current_session();"
```

`ssl_state` should no longer be `None` and `tls_version` should have a value. In the client-server context, `VERIFY_FULL` behaves like `VERIFY_CA`; hostname validation is the client's job.

### 7.2 LDAP authentication

Prerequisite: install `open-ldap-tools` (which includes `ldapsearch`) on at least one node, and use it to confirm the bind and search filters before touching Vertica.

```sql
=> CREATE AUTHENTICATION ldap_auth METHOD 'ldap' HOST TLS '10.0.0.0/8';
=> ALTER AUTHENTICATION ldap_auth SET
     host='ldaps://ldap.example.com',
     basedn='ou=people,dc=example,dc=com',
     binddn='cn=svc-vertica,ou=svc,dc=example,dc=com',
     bind_password='<password>',
     search_attribute='uid';
=> GRANT AUTHENTICATION ldap_auth TO public;   -- or a narrower role
```

Notes:

- `HOST TLS` matches only TLS connections. If clients still send plaintext, create a separate `HOST NO TLS` record or reject them outright.
- `host='ldaps://...'` uses LDAPS; `host='ldap://...'` with `starttls='hard'` uses StartTLS. Either way the CA must be added to the `LDAPAuth` configuration, or the server certificate cannot be verified.
- Think before granting to `public`: with fallthrough disabled, a failed LDAP bind is a rejection, not a fallback to HASH.

### 7.3 Kerberos

Prerequisites: a Kerberos 5 client and `/etc/krb5.conf` on every database node and client, reachable KDCs, and a `vertica/<fqdn>@REALM` service principal with a keytab on each node.

```sql
=> ALTER DATABASE DEFAULT SET KerberosRealm = 'EXAMPLE.COM';
=> ALTER DATABASE DEFAULT SET KerberosKeytabFile = '/etc/vertica/vertica.keytab';
=> ALTER DATABASE DEFAULT SET KerberosServiceName = 'vertica';

=> CREATE AUTHENTICATION krb_auth METHOD 'gss' HOST '0.0.0.0/0';
=> GRANT AUTHENTICATION krb_auth TO public;
```

Client:

```bash
kinit alice@EXAMPLE.COM
klist
vsql -h vertica.example.com -M kerberos -d testdb \
  -c "select authentication_method from v_monitor.sessions where session_id=current_session();"
```

That last query is the point. `-M kerberos` proves nothing (see 5.5); `authentication_method` must report `GSS` or `Kerberos`.

## 8. Common mistakes and anti-patterns

1. **"8443 handshakes, so the database has TLS."** Measured: `-m require` on 5433 fails while 8443 speaks TLSv1.2. Different services, different certificate sources. Judge client-server TLS only by `SECURITY_CONFIG_CHECK` or `ServerTLSConfig`.
2. **Trusting the vsql default.** `prefer` silently downgrades, and `ssl_state=None` produces no warning. Compliance environments should hard-code `require`/`verify-full` and alert on `sessions.ssl_state`.
3. **Using `-M kerberos` as acceptance.** It connects with no ticket here, but the session says `Hash`. Check `authentication_method`.
4. **Inspecting ciphers before enabling TLS.** `SELECT list_enabled_ciphers();` returns `ERROR 6960: SSL is not enabled`. Put that query in the post-change validation list instead.
5. **Testing 8443 with curl and ignoring the error.** Without `-k` it always fails: `certificate subject name 'HttpsService' does not match target hostname '127.0.0.1'`. That is a bootstrap certificate with no SAN, not a broken service.
6. **Creating an LDAP/GSS record on dbadmin and disabling fallthrough.** If the directory or KDC becomes unreachable, there is no local password path left. Keep `default_hash_local` for dbadmin and confirm a working login path before every change.

## 9. Risks and recommendations

**Safe now (read-only, low risk)**

- Add three queries to the routine: `SELECT security_config_check('NETWORK');`, `SELECT name, mode FROM tls_configurations;`, and `SELECT authentication_method, ssl_state, count(*) FROM v_monitor.sessions GROUP BY 1,2;`.
- Check the 8443 certificate expiry: `openssl s_client -connect <host>:8443 </dev/null 2>/dev/null | openssl x509 -noout -dates`. Here it is 2036-04-28, but a bootstrap certificate should not live that long.
- Confirm dbadmin still has a local HASH path before planning LDAP or Kerberos.

**Requires change (window and approval)**

- Client-server TLS: drivers, connection strings, monitoring, and load balancers all move together. Start with `ENABLE` (no client certificate verification) to prove connectivity, then move to `VERIFY_CA`.
- `EncryptSpreadComm` requires a database restart and, per the documentation, comes before the other security parameters.
- LDAP/Kerberos involve the AD or KDC team, plus account mapping (`ldap_dn`, principals) and offboarding.
- Rollback: set the TLS configuration back to `TLSMODE 'DISABLE'`; drop or disable the authentication record you created. Save the current output before changing anything, or you will have nothing to compare against.

## 10. References

- Vertica 25.4 — TLS configurations: https://docs.vertica.com/25.4.x/en/security-and-authentication/tls-protocol/tls-overview/tls-configs/
- Vertica 25.4 — SECURITY_CONFIG_CHECK: https://docs.vertica.com/25.4.x/en/sql-reference/functions/management-functions/db-functions/security-config-check/
- Vertica 25.4 — Security parameters (`ServerTLSConfig`, `MinTLSVersion`, `EncryptSpreadComm`): https://docs.vertica.com/25.4.x/en/sql-reference/config-parameters/security-parameters/
- Vertica 25.4 — HTTPS service (8443 and bootstrap certificates): https://docs.vertica.com/25.4.x/en/admin/managing-db/https-service/
- Vertica 25.4 — Client authentication: https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/
- Vertica 25.4 — CREATE AUTHENTICATION: https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-statements/create-authentication/
- Vertica 25.4 — LDAP authentication: https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/ldap-authentication/
- Vertica 25.4 — TLS for LDAP authentication: https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/ldap-authentication/tls-ldap-authentication/
- Vertica 25.4 — Kerberos authentication: https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/kerberos-authentication/
- Vertica 25.4 — ALTER TLS CONFIGURATION (TLSMODE semantics): https://docs.vertica.com/25.4.x/en/sql-reference/statements/alter-statements/alter-tls-config/
