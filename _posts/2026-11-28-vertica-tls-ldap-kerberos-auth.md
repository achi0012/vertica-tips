---
layout: post
title: "Vertica 安全連線與認證：TLS、LDAP、Kerberos 怎麼選、怎麼組合"
date: 2026-11-28 05:00:00 +0800
categories: vertica security
tags: [vertica, security, tls, ssl, ldap, kerberos, authentication]
description: "TLS 管通道加密、LDAP 管密碼驗證、Kerberos 管單一簽入，三者解決的是不同問題，可以疊加但不會互相取代。本文在 testdb（v25.4.0-0 單節點）與 eondb（3 節點、K-Safe=0、4 shard）實查：5 個 TLS configuration 全部 DISABLE、9 個使用者只有 3 筆 HASH 認證記錄，vsql -m require 直接回 server does not support SSL, but SSL was required。文中也解釋為什麼 8443 看起來有 TLS，以及在沒有 krb5.conf 與 ldapsearch 的機器上，LDAP 與 Kerberos 為什麼只能標「未實測」。"
image: /assets/images/og/vertica-tls-ldap-kerberos-auth-zh.png
faq:
  - q: "TLS 和 LDAP 差在哪裡？"
    a: "TLS 只負責把連線加密，不判斷你是誰；LDAP 負責把密碼送到目錄服務驗證，不負責加密。兩者可以疊加：先用 TLS 保護 5433 的通道，再讓 LDAP 在通道裡驗證密碼。本機實測 dbadmin 的 session 是 authentication_method=Hash、ssl_state=None，兩個平面都沒開。"
  - q: "為什麼 8443 有 TLS，5433 的 vsql 卻說不支援 SSL？"
    a: "因為兩者是不同服務。5433 是 client port，TLS 由 server 這個 TLS configuration 控制，本機是 DISABLE；8443 是 HTTPS service，安裝時就在 /opt/vertica/config/https_certs 產生自簽憑證來啟動，實測拿到 TLSv1.2 與 CN=HttpsService 的憑證。8443 有 TLS 不代表資料庫連線有加密。"
  - q: "沒開 TLS 時，vsql 預設會加密嗎？"
    a: "不會。vsql 預設 sslmode=prefer，會先嘗試 TLS，伺服器不支援就靜默降級成明文，不會警告。實測 -m prefer 連線成功，但 v_monitor.sessions 的 ssl_state=None、tls_version 是空字串；只有明確指定 -m require 才會出現 server does not support SSL, but SSL was required。"
  - q: "指定 -M kerberos 為什麼沒有 ticket 也能連上？"
    a: "因為 -M 是偏好不是強制。本機沒有 /etc/krb5.conf、沒有 kinit/klist、也沒有 ticket cache，但 vsql -M kerberos 仍然連線成功；查同一條 session 的 authentication_method 是 Hash，證明它走的是密碼驗證。要驗證 Kerberos 是否生效，不能看連線成功，要看 sessions 表。"
  - q: "五個 TLS configuration 全部 DISABLE 代表什麼？"
    a: "代表五種連線類型都沒有啟用 TLS：server 管 client-server、data_channel 管節點間、LDAPAuth 管資料庫到 LDAP 的認證連線、LDAPLink 管 LDAP Link 服務、https 管 8443 的 HTTPS service。本機 testdb 與 eondb 都是 5 個全 DISABLE。"
---

<!--
AI Summary: 在 Vertica v25.4.0-0 的 testdb（單節點）與 eondb（3 節點、K-Safe=0、4 shard）實查 TLS、LDAP、Kerberos 三種安全機制：兩套叢集的 5 個 TLS configuration 全為 DISABLE、SECURITY_CONFIG_CHECK('NETWORK') 顯示 spread／data channel／client-server 三層全關、9 個使用者只有 3 筆 HASH 認證記錄（27 筆授予），vsql -m require 回 server does not support SSL, but SSL was required，而預設 -m prefer 靜默降級、session 的 ssl_state=None。另解釋 8443 HTTPS service 以安裝時的自簽憑證提供 TLS（CN=HttpsService，2026-05-01 至 2036-04-28），與 5433 的明文連線無關；LDAP 與 Kerberos 因本機無 LDAP server、無 ldapsearch、無 krb5.conf 與 kinit，全文標為未實測並附判斷用的實際查詢輸出。
AI Keywords: Vertica, TLS, SSL, LDAP, Kerberos, client authentication, GSS, sslmode, SECURITY_CONFIG_CHECK, 25.4
-->
<!--more-->

---

## 一、先講結論

這三個名詞很常被放在同一句話裡，但它們解的是三個不同的問題：

- **TLS 是通道加密。** 它讓中間人看不到封包內容，但不告訴資料庫「你是誰」。
- **LDAP 是密碼驗證。** 資料庫把使用者送的帳號密碼拿去目錄服務 bind，成功就放行；密碼不存在 Vertica 裡。
- **Kerberos 是票證式單一簽入。** 使用者先向 KDC 拿 ticket，再用 ticket 證明身分，整個過程沒有密碼經過網路。

可以疊加，但不能互相取代。把 LDAP 當成加密手段、或把 TLS 當成認證手段，是我在健檢時最常看到的兩個誤解。

我在本機兩套叢集上把能唯讀查的全部查了一輪，結論如下：

1. **兩套叢集的 TLS 都是全關的。** testdb 與 eondb 各有 5 個 TLS configuration（`server`、`data_channel`、`LDAPAuth`、`LDAPLink`、`https`），20 個欄位組合全部是 `DISABLE`，憑證欄位全空。
2. **內建檢查函式說得很直接。** `SECURITY_CONFIG_CHECK('NETWORK')` 回報 spread 加密關閉、data channel TLS 關閉、client-server TLS 關閉，並且明白寫出「server 的 certificate 是 unset」。
3. **客戶端預設不會幫你踩煞車。** `vsql -m require` 會失敗並印出 `vsql: server does not support SSL, but SSL was required`；但不加參數時，預設的 `prefer` 會安靜地降級成明文，session 的 `ssl_state` 就是 `None`。
4. **認證只有 HASH。** `v_catalog.client_auth` 只有 3 筆記錄，全部是 `HASH`；9 個使用者透過 `public` 繼承這 3 筆，總共 27 筆授予。`v_monitor.sessions` 的 `authentication_method` 是 `Hash`。
5. **8443 的 TLS 是另一回事。** HTTPS service 用安裝時產生的自簽憑證（`CN=HttpsService`）提供 TLSv1.2，和 5433 的明文 client port 無關。看到 8443 能握手就推論「資料庫有開 TLS」是錯的。
6. **LDAP 與 Kerberos 在這台無法實測。** 沒有 LDAP server 在聽 389/636、沒有 `ldapsearch`、`/etc/ldap/ldap.conf` 只有註解；沒有 `/etc/krb5.conf`、沒有 `kinit`/`klist`、沒有任何 ticket cache。本文會把判斷用的查詢輸出貼出來，設定步驟則明確標成「依官方文件，未實測」。

## 二、實測環境與方法

| 項目 | 值 |
|---|---|
| 版本 | Vertica Analytic Database v25.4.0-0（Community Edition） |
| Enterprise 測試庫 | `testdb`，單節點，127.0.0.1:5433 |
| EON 測試庫 | `eondb`，3 節點（`v_eondb_node0001`～`0003` 全部 UP），4 個 shard，K-Safe=0 |
| 連線工具 | `vsql` 25.04.0000；eondb 用 `vertica_python` helper |
| 測試方式 | 全部唯讀：`SELECT`、`SHOW`、`openssl s_client`、`curl`、`ss` |
| 測試物件 | 無。本篇不需要 DDL，沒有建立 `article_lab_*` schema，也沒有改任何設定 |

先確認版本與節點，順便讓 K-Safe 的警告自己現形：

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

`eondb` 每次連線都會送 `WARNING 6046: The database is currently 0-safe`。這和 TLS 無關，但它提醒你：這是一個共用測試叢集，任何 `ALTER` 都該有窗口，所以本篇對它只做查詢。

另外先講清楚：**傳輸埠不是全部都需要 TLS。** 5433 是 client port；5434 是 client port +1，官方文件寫明是叢集內部通訊用（例如 plan 期間），不是另一個 TLS 入口。真正由 TLS configuration 控制的是 5433、節點之間的 data channel、以及資料庫往 LDAP 的連線。8443 則是 HTTPS service，走的是自己的啟動憑證。

## 三、三個平面，不要混在一起

| 平面 | 回答的問題 | Vertica 機制 | 本機狀態 |
|---|---|---|---|
| 傳輸 | 封包在路上能不能被看懂 | TLS configuration + `ServerTLSConfig`／`InternodeTLSConfig`／`LDAPAuthTLSConfig`／`LDAPLinkTLSConfig`／`HttpsTLSConfig` | 5 個 configuration 全 `DISABLE` |
| 認證 | 你是誰、密碼去哪裡驗 | `CREATE AUTHENTICATION` + `v_catalog.client_auth` | 3 筆，全部 `HASH` |
| 授權 | 你能做什麼 | role、privilege、schema USAGE | 不是本篇範圍 |

還有一個常被漏掉的第四層：**Spread 控制通道加密**。它由 `EncryptSpreadComm` 控制，和 TLS configuration 是兩套東西；`SECURITY_CONFIG_CHECK` 會把它獨立列出來，而且警告「在 spread 未加密前不要去改其他安全參數」。

把連線的順序拆開，會更清楚為什麼這三層不能互相取代。客戶端先建 TCP（5433），如果 `server` 這個 configuration 不是 `DISABLE`，就在同一個 socket 上做 TLS 握手；握手完成後才送 startup packet，伺服器再依 `client_auth` 的 `auth_priority` 挑一筆記錄，決定用 HASH、LDAP 或 GSS 驗證身分。也就是說，TLS 在認證之前發生，它保護的是「認證過程與後續 SQL」，但它本身不知道你是誰；反過來說，LDAP 或 Kerberos 只處理身分，封包內容仍然可以被旁邊的人看光。這也是為什麼 `v_monitor.sessions` 要把 `ssl_state` 與 `authentication_method` 分成兩個欄位——它們本來就是兩件不同的事。

五個 TLS configuration 各自管哪一種連線，整理如下（名稱與用途取自 25.4 文件）：

| Configuration | 管什麼 | 誰是 client |
|---|---|---|
| `server` | client-server TLS（5433） | 資料庫是 server |
| `data_channel` | 節點間資料通道 | 節點互為 client/server |
| `LDAPAuth` | 使用者登入時，資料庫到 LDAP server 的 bind | 資料庫是 client |
| `LDAPLink` | LDAP Link 服務同步使用者與群組 | 資料庫是 client |
| `https` | 8443 的 HTTPS service | 資料庫是 server |

## 四、TLS 實測

### 4.1 五個 configuration 的實際長相

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

五個都是 `DISABLE`，`certificate` 與 `ca_certificate` 都是空字串。這不是「還沒調」而已，是「連憑證都沒掛」。

### 4.2 內建檢查函式：一句話講完三層

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

這段輸出比任何自製檢查表都可靠，因為它是資料庫自己算的。三個重點：spread 沒加密、節點間沒 TLS、client-server 沒 TLS 而且連憑證都沒設。

### 4.3 參數指向哪個 configuration

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

參數都指向預設的 configuration，這是正常的；問題在 configuration 本身是 `DISABLE`。`MinTLSVersion=2` 代表一旦啟用，最低只接受 TLS 1.2。

### 4.4 沒有 TLS 時，客戶端會怎樣

把同一條 `SELECT 1` 用不同 `sslmode` 丟出去：

| 客戶端參數 | 結果 | 實際訊息 |
|---|---|---|
| `-m require` | 失敗 | `vsql: server does not support SSL, but SSL was required` |
| `-m verify-ca` | 失敗 | 同上 |
| `-m verify-full` | 失敗 | 同上 |
| `-m allow` | 成功 | 明文 |
| `-m disable` | 成功 | 明文 |
| 不加參數（預設 `prefer`） | 成功 | 明文，**沒有任何警告** |

失敗時的原文：

```text
$ vsql -m require -c "select 1 as ok;"
vsql: server does not support SSL, but SSL was required
```

成功的那幾種，查 session 狀態會看到：

```sql
=> SELECT ssl_state, tls_version
   FROM v_monitor.sessions WHERE session_id = current_session();
 ssl_state | tls_version
-----------+-------------
 None      |
(1 row)
```

`ssl_state` 是 `None`，`tls_version` 是空字串。也就是說，`prefer` 這個預設值在伺服器不支援 TLS 時，會安靜地把你的「我想要加密」變成「好吧，明文也可以」。如果你的合規要求是「連線必須加密」，客戶端一定要寫死 `require` 以上，不能靠預設。

從網路層再驗一次。對 5433 直接發 TLS ClientHello：

```text
$ openssl s_client -connect 127.0.0.1:5433 -brief
Connecting to 127.0.0.1
40270995567A0000:error:0A00010B:SSL routines:tls_validate_record_header:wrong version number:../ssl/record/methods/tlsany_meth.c:77:
```

`wrong version number` 的意思是：伺服器回的不是 TLS record，而是 Vertica 自己的協定。這和 vsql 的錯誤訊息互相印證。

還有一個小陷阱：想在沒開 TLS 時先看有哪些 cipher 可用，會直接被打回來。

```sql
=> SELECT list_enabled_ciphers();
ERROR 6960:  SSL is not enabled
```

這不是 bug，是順序問題：cipher 清單要等 TLS 開了才有意義。

### 4.5 那 8443 為什麼有 TLS？

這是本文最容易被誤判的一段。同一個 `vertica` process 在聽 8443：

```text
$ ss -ltnp | grep 8443
LISTEN 0  4096  0.0.0.0:8443  0.0.0.0:*  users:(("vertica",pid=3431075,fd=12))
```

對它握手會成功：

```text
$ openssl s_client -connect 127.0.0.1:8443 -brief
Can't use SSL_get_servername
depth=1 C=US, ST=Massachusetts, L=Cambridge, O=OpenText, OU=Vertica, CN=rootca
verify error:num=19:self-signed certificate in certificate chain
CONNECTION ESTABLISHED
Protocol version: TLSv1.2
Ciphersuite: ECDHE-RSA-AES256-GCM-SHA384
```

憑證本身：

```text
subject=C=US, ST=Massachusetts, L=Cambridge, O=OpenText, OU=Vertica, CN=HttpsService
issuer=C=US, ST=Massachusetts, L=Cambridge, O=OpenText, OU=Vertica, CN=rootca
notBefore=May  1 17:44:01 2026 GMT
notAfter=Apr 28 17:44:01 2036 GMT
```

這張憑證和 `/opt/vertica/config/https_certs/1000.pem` 的 subject、issuer、日期完全一致。檔案目錄長這樣：

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

官方文件寫得很清楚：`install_vertica` 會在這個目錄產生自簽憑證，用來**bootstrap** 新叢集的 HTTPS service，不適合生產；TLS configuration 裡的憑證會取代它們。所以正確的解讀是：

- 8443 的 TLS 是 HTTPS service 的啟動行為，不是 `server` 這個 configuration 開的。
- `https` configuration 是 `DISABLE`、`certificate` 是空的，代表沒有另外掛上 catalog 內的憑證，服務就用安裝時的那一組。
- 5433 的 client port 完全沒有 TLS，這兩件事不能互相推論。

客戶端如果不帶 `-k`，會直接撞上名稱驗證：

```text
$ curl -sS -o /dev/null https://127.0.0.1:8443/
curl: (60) SSL: certificate subject name 'HttpsService' does not match target hostname '127.0.0.1'
```

帶 `-k` 才看得到服務本體：

```text
$ curl -k -sS -D - -o /dev/null https://127.0.0.1:8443/
HTTP/1.1 401 Unauthorized
Content-Length: 215
Connection: keep-alive
Server: oatpp/1.3.0
Content-Type: application/problem+json
```

`CN=HttpsService` 沒有 SAN，所以主機名稱驗證一定失敗。這是 bootstrap 憑證的預期行為，不是壞掉；但要上生產，就得換成有 SAN、由內部 CA 簽的憑證。

## 五、認證實測

### 5.1 只有 HASH，沒有 LDAP／GSS／TLS／OAuth

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

三筆都是預設的 HASH 記錄，`auth_priority=-1`、`method_priority=2`、fallthrough 關閉。沒有任何 `ldap`、`gss`、`tls`、`oauth` 記錄，也沒有人自訂優先權。

`auth_priority` 決定「同一個使用者有多筆記錄時先試哪一筆」，數字大的先試；三筆預設值都是 -1，所以沒有排序問題。`method_priority=2` 是 HASH 方法本身的權重。`is_fallthrough_enabled=False` 的意思是：一旦這筆記錄被選中而驗證失敗，不會自動退到下一筆，直接拒絕。這個設計在只有 HASH 時看不出差別，但一旦你加入 LDAP 又忘了開 fallthrough，目錄服務短暫不可用就會變成全員登不進來——這也是多數人第一次導入 LDAP 時踩的坑。

再看主機範圍：

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

本機與所有 IPv4／IPv6 來源都走 HASH。這也是為什麼任何來源只要帳號密碼對就能連——沒有來源限制、沒有第二因素。

### 5.2 誰拿到這些記錄

```sql
=> SELECT count(*) AS grants, count(DISTINCT user_name) AS users,
          count(DISTINCT auth_name) AS auth_methods
   FROM v_catalog.user_client_auth;
 grants | users | auth_methods
--------+-------+--------------
     27 |     9 |            3
```

27 = 9 個使用者 × 3 筆記錄，`granted_to` 全部是 `public`。9 個使用者是 `a2v_app`、`a2v_dev_ro`、`a2v_dev_rw`、`a2v_tester1`、`a2v_tester2`、`dbadmin`、`lowi_rap`、`ro_reader`、`test`。

確認沒有任何人是 LDAP 納管的：

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

`ldap_dn` 全空、`managed_by_auth_id` 全是 0、`is_orphaned_from_ldap` 全是 false。

### 5.3 這條 session 實際用了什麼

```sql
=> SELECT user_name, authentication_method, client_authentication_name,
          ssl_state, tls_version
   FROM v_monitor.sessions WHERE session_id = current_session();
 user_name | authentication_method | client_authentication_name | ssl_state | tls_version
-----------+-----------------------+----------------------------+-----------+-------------
 dbadmin   | Hash                  | default_hash_local         | None      |
(1 row)
```

這四欄一起看，就是本文最有用的一條查詢：`Hash` 代表密碼驗證、`default_hash_local` 代表命中的是那一筆預設記錄、`None` 代表沒有 TLS。三個平面一次看完。

### 5.4 LDAP 為什麼只能標「未實測」

先看 LDAP Link 的參數，全部是空的：

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

`LDAPLinkOn=0`、URL 與 search base 都空，代表 LDAP Link 服務根本沒啟動。事件表也是 0 筆：

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

主機層面同樣沒有條件：

```text
$ ss -ltn | grep -E ':(389|636)\b'
（無輸出）

$ which ldapsearch
（無輸出）

$ wc -c /etc/ldap/ldap.conf
247 /etc/ldap/ldap.conf
```

`/etc/ldap/ldap.conf` 的 247 bytes 全部是註解，沒有一行生效設定；`ldapsearch` 沒裝（官方文件把 `open-ldap-tools` 列為 LDAP 認證的前置條件）；本機也沒有任何 LDAP server 在聽 389 或 636。沒有目錄服務，就沒有東西可以 bind，任何「LDAP 認證成功」的輸出都只能是編的。所以本篇對 LDAP 只寫文件步驟，不寫實測結果。

### 5.5 Kerberos 也一樣

參數層面：

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

只有 service name 是預設的 `vertica`，realm、hostname、keytab 全空。

主機層面：

```text
$ ls /etc/krb5.conf
ls: cannot access '/etc/krb5.conf': No such file or directory

$ which kinit klist
（無輸出）

$ env | grep -i krb5
（無輸出）

$ ls /tmp/krb5cc*
ls: cannot access '/tmp/krb5cc*': No such file or directory
```

沒有 client 套件、沒有 realm 設定、沒有 ticket cache。

這裡有一個很容易誤判的實驗。直接指定 Kerberos 連線：

```text
$ vsql -M kerberos -c "select 1 as ok;"
 ok
----
  1
(1 row)
```

看起來「Kerberos 可用」，其實不是。同一條 session 查下去：

```sql
=> SELECT authentication_method, client_authentication_name, ssl_state
   FROM v_monitor.sessions WHERE session_id = current_session();
 authentication_method | client_authentication_name | ssl_state
-----------------------+----------------------------+-----------
 Hash                  | default_hash_local         | None
(1 row)
```

`-M kerberos` 只是「我希望用 Kerberos」，伺服器沒有 GSS 記錄時，客戶端仍然用密碼完成連線，而且不會告訴你它降級了。**要驗證認證方法，永遠看伺服器端的 `v_monitor.sessions`，不要看連線成功與否。**

順便試 OAuth，拿到的是另一個「沒設定」的訊號：

```text
$ vsql -M oauth -c "select 1;"
Failed to open private key file: No such file or directory
Failed to load private key
```

## 六、怎麼選、怎麼組合

| 你要解決的問題 | 該用什麼 | 理由 |
|---|---|---|
| 怕封包被側錄（同機房、跨機房、雲端） | client-server TLS（`server` + `ServerTLSConfig`） | 只加密通道，不改認證方式，改動最小 |
| 合規要求雙向憑證、服務對服務免密碼 | mutual TLS（`TRY_VERIFY`／`VERIFY_CA`）+ `tls` 認證 | 憑證 CN 就是使用者名稱 |
| 人員帳號由企業目錄管理、離職要立刻停權 | LDAP 認證 + `LDAPAuth` TLS | 密碼不落地，停權在 AD 端做 |
| AD 網域內要單一簽入、不想再打密碼 | Kerberos（`gss`） | ticket 制，密碼不經過網路 |
| 節點間資料不能被看見 | `data_channel` TLS + `EncryptSpreadComm` | 兩者都要，缺一不可 |
| 8443 的 REST API 要對外 | `https` configuration + 有 SAN 的憑證 | bootstrap 自簽憑證只適合內部啟動 |

組合順序建議照 `SECURITY_CONFIG_CHECK` 的警告走：

1. 先 `EncryptSpreadComm`（需要重啟）。
2. 再 `data_channel`（節點間）。
3. 再 `server`（client-server）。
4. 最後才是 LDAP／LDAPLink 的 TLS，以及 8443 的憑證替換。

原因是 spread 是控制通道，文件明確說「spread 未加密時不應改其他安全參數」。反過來做，你會在中途得到一個「資料通道加密了、控制通道還沒」的混合狀態，排查起來很痛苦。

三個實際情境，套上去大概是這樣：

- **單一團隊、內網、沒有 AD。** 先開 client-server TLS（`ENABLE`），憑證用內部 CA 簽，客戶端改 `verify-ca`。LDAP 與 Kerberos 都不用碰；9 個本機使用者繼續用 HASH，成本最低。
- **公司有 AD，DBA 只有三個人。** LDAP 認證 + `LDAPAuth` TLS，dbadmin 保留 `default_hash_local` 當後路。這是最常見的組合，難點在帳號對應（`search_attribute` 要用 `sAMAccountName` 還是 `uid`）與離職停權的同步。
- **金融或醫療，稽核要求雙向憑證與節點間加密。** `server` 用 `VERIFY_CA`、`data_channel` 開 TLS、`EncryptSpreadComm=vertica`，再加上 `tls` 認證給服務帳號。這個組合要動的層最多，建議先後順序照上面的 1→4 走。

## 七、設定步驟（依官方文件整理，未實測）

以下三段都沒有在本機執行。原因分別是：TLS 變更需要重啟與客戶端相容性確認；LDAP 沒有目錄服務；Kerberos 沒有 KDC。指令語法取自 25.4 文件，實際執行前請在測試叢集跑過。

### 7.1 啟用 client-server TLS

```sql
-- 1. 建 key、CA、server 憑證
=> CREATE KEY k_ca TYPE 'RSA' LENGTH 4096;
=> CREATE CA CERTIFICATE ca
   SUBJECT '/C=TW/ST=Taipei/O=Example/OU=DBA/CN=Example Root CA'
   VALID FOR 3650 KEY k_ca;

=> CREATE KEY k_server TYPE 'RSA' LENGTH 2048;
=> CREATE CERTIFICATE server_cert
   SUBJECT '/C=TW/ST=Taipei/O=Example/OU=DBA/CN=vertica.example.com'
   SIGNED BY ca KEY k_server;

-- 2. 掛到 server 這個 configuration
=> ALTER TLS CONFIGURATION server CERTIFICATE server_cert;
=> ALTER TLS CONFIGURATION server TLSMODE 'ENABLE';   -- 雙向憑證改用 TRY_VERIFY 或 VERIFY_CA
-- 若要走 mutual TLS，再加：
=> ALTER TLS CONFIGURATION server ADD CA CERTIFICATES ca;

-- 3. 確認
=> SELECT name, certificate, ca_certificate, mode FROM tls_configurations WHERE name='server';
=> SELECT security_config_check('NETWORK');
```

客戶端驗證：

```bash
vsql -h vertica.example.com -m verify-full -c "select ssl_state, tls_version from v_monitor.sessions where session_id=current_session();"
```

預期 `ssl_state` 不再是 `None`，`tls_version` 有值。`VERIFY_FULL` 在 client-server 情境等同 `VERIFY_CA`，主機名稱驗證要靠客戶端自己做。

### 7.2 LDAP 認證

前置：起碼一台節點裝 `open-ldap-tools`（含 `ldapsearch`），先用它確認 bind 與搜尋條件正確，再設 Vertica。

```sql
=> CREATE AUTHENTICATION ldap_auth METHOD 'ldap' HOST TLS '10.0.0.0/8';
=> ALTER AUTHENTICATION ldap_auth SET
     host='ldaps://ldap.example.com',
     basedn='ou=people,dc=example,dc=com',
     binddn='cn=svc-vertica,ou=svc,dc=example,dc=com',
     bind_password='<密碼>',
     search_attribute='uid';
=> GRANT AUTHENTICATION ldap_auth TO public;   -- 或只給特定角色
```

重點：

- `HOST TLS` 只匹配 TLS 連線；若客戶端還會發明文，要另建 `HOST NO TLS` 的記錄或直接拒絕。
- `host='ldaps://...'` 走 LDAPS；`host='ldap://...'` 搭配 `starttls='hard'` 走 StartTLS。兩條路都要把 CA 掛進 `LDAPAuth` configuration，否則伺服器憑證無法驗。
- `GRANT ... TO public` 之前先想清楚 fallthrough：關閉 fallthrough 時，LDAP bind 失敗就是拒絕，不會退回 HASH。

### 7.3 Kerberos

前置：每一台資料庫節點與客戶端都要裝 Kerberos 5 client、有 `/etc/krb5.conf`、能連到 KDC；每個節點建 `vertica/<fqdn>@REALM` 的 service principal 與 keytab。

```sql
=> ALTER DATABASE DEFAULT SET KerberosRealm = 'EXAMPLE.COM';
=> ALTER DATABASE DEFAULT SET KerberosKeytabFile = '/etc/vertica/vertica.keytab';
=> ALTER DATABASE DEFAULT SET KerberosServiceName = 'vertica';

=> CREATE AUTHENTICATION krb_auth METHOD 'gss' HOST '0.0.0.0/0';
=> GRANT AUTHENTICATION krb_auth TO public;
```

客戶端：

```bash
kinit alice@EXAMPLE.COM
klist
vsql -h vertica.example.com -M kerberos -d testdb \
  -c "select authentication_method from v_monitor.sessions where session_id=current_session();"
```

最後那條查詢是關鍵。`-M kerberos` 本身不證明任何事（見 5.5），`authentication_method` 顯示 `GSS` 或 `Kerberos` 才算成功。

## 八、常見錯誤與反模式

1. **看到 8443 能握手，就說「資料庫已經有 TLS」。** 實測 5433 的 `-m require` 直接失敗，8443 卻是 TLSv1.2。兩者是不同服務、不同憑證來源。判斷 client-server TLS 只看 `SECURITY_CONFIG_CHECK` 或 `ServerTLSConfig`。
2. **相信 vsql 的預設值。** 預設 `prefer` 會靜默降級，`ssl_state` 是 `None` 也不會有任何警告。合規環境要在連線字串寫死 `require`／`verify-full`，並在監控裡檢查 `sessions.ssl_state`。
3. **用 `-M kerberos` 當驗收。** 本機沒有 ticket 也能連上，session 卻是 `Hash`。驗收要看 `authentication_method`。
4. **還沒開 TLS 就查 cipher。** `SELECT list_enabled_ciphers();` 回 `ERROR 6960: SSL is not enabled`。先開 TLS 再查，或把這條查詢放在變更後的驗證清單。
5. **用 curl 測 8443 卻不看錯誤。** 不帶 `-k` 一定失敗：`certificate subject name 'HttpsService' does not match target hostname '127.0.0.1'`。這是 bootstrap 憑證沒有 SAN，不是服務壞了。
6. **在 dbadmin 身上直接建 LDAP／GSS 記錄並關掉 fallthrough。** 一旦目錄服務或 KDC 不可達，你就沒有本機密碼後路。實務上保留 `default_hash_local` 給 dbadmin，並在變更前確認還有一條能登入的路徑。

## 九、風險與建議

**現在就能做（唯讀、低風險）**

- 把這三條查詢放進巡檢：`SELECT security_config_check('NETWORK');`、`SELECT name, mode FROM tls_configurations;`、`SELECT authentication_method, ssl_state, count(*) FROM v_monitor.sessions GROUP BY 1,2;`。
- 檢查 8443 憑證到期日：`openssl s_client -connect <host>:8443 </dev/null 2>/dev/null | openssl x509 -noout -dates`。本機是 2036-04-28，但 bootstrap 憑證本來就不該用到那天。
- 確認 dbadmin 仍有一條本機 HASH 後路，再規劃 LDAP／Kerberos。

**需要變更（要窗口、要核准）**

- 開 client-server TLS：驅動程式、連線字串、監控、負載平衡器都要一起改；先用 `ENABLE`（不驗客戶端憑證）確認連得上，再往 `VERIFY_CA` 走。
- `EncryptSpreadComm` 需要重啟資料庫；依文件順序，它要排在其他安全參數之前。
- LDAP／Kerberos 牽涉 AD 或 KDC 團隊，還要處理帳號對應（`ldap_dn`、principal）與離職停權流程。
- 回滾方式：TLS 是把 configuration 改回 `TLSMODE 'DISABLE'`；認證是把自建記錄 `DROP AUTHENTICATION` 或 `ALTER ... DISABLE`。變更前先把現況輸出存檔，否則回滾時沒有對照。

## 十、參考資料

- Vertica 25.4 — TLS configurations：https://docs.vertica.com/25.4.x/en/security-and-authentication/tls-protocol/tls-overview/tls-configs/
- Vertica 25.4 — SECURITY_CONFIG_CHECK：https://docs.vertica.com/25.4.x/en/sql-reference/functions/management-functions/db-functions/security-config-check/
- Vertica 25.4 — Security parameters（`ServerTLSConfig`、`MinTLSVersion`、`EncryptSpreadComm`）：https://docs.vertica.com/25.4.x/en/sql-reference/config-parameters/security-parameters/
- Vertica 25.4 — HTTPS service（8443 與 bootstrap 憑證）：https://docs.vertica.com/25.4.x/en/admin/managing-db/https-service/
- Vertica 25.4 — Client authentication：https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/
- Vertica 25.4 — CREATE AUTHENTICATION：https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-statements/create-authentication/
- Vertica 25.4 — LDAP authentication：https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/ldap-authentication/
- Vertica 25.4 — TLS for LDAP authentication：https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/ldap-authentication/tls-ldap-authentication/
- Vertica 25.4 — Kerberos authentication：https://docs.vertica.com/25.4.x/en/security-and-authentication/client-authentication/kerberos-authentication/
- Vertica 25.4 — ALTER TLS CONFIGURATION（TLSMODE 語意）：https://docs.vertica.com/25.4.x/en/sql-reference/statements/alter-statements/alter-tls-config/
