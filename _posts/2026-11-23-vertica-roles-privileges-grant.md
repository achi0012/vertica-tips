---
layout: post
title: "Vertica 權限與角色實戰：GRANT、角色繼承、schema 層級與稽核"
date: 2026-11-23 05:00:00 +0800
categories: vertica security
tags: [vertica, security, role, grant, privilege, schema, audit]
description: "Vertica 的讀取權限要同時過兩道門：schema 的 USAGE 與表上的 SELECT，缺哪一道的錯誤碼不同（3580 對 4367）。本機 testdb v25.4.0-0 實測三層角色繼承（使用者→角色→角色→角色），確認繼承本身有效，但 GRANT role TO user 不會自動變成 default role：default_roles 是空的，登入後若不 SET ROLE，查詢會回 ERROR 3580，看起來像授權根本沒生效。"
image: /assets/images/og/vertica-roles-privileges-grant-zh.png
faq:
  - q: "為什麼已經 GRANT 角色給使用者，查詢還是回 USAGE on SCHEMA not granted？"
    a: "因為角色被授予不等於被啟用。本機 25.4.0-0 實測，GRANT LAB_ROLE_REPORT TO LAB_USER_REPORT 之後 v_catalog.users 的 all_roles 有值，但 default_roles 是空的；新連線不會自動帶上角色，查詢就回 ERROR 3580。設定 ALTER USER ... DEFAULT ROLE，或在 session 內 SET ROLE，才會通過第一道門。"
  - q: "只有 schema 的 USAGE、沒有表的 SELECT，錯誤訊息是什麼？"
    a: "ERROR 4367: Permission denied for relation lab_orders。反過來，有表的 SELECT 但收回 schema USAGE，會回 ERROR 3580: Insufficient privilege: USAGE on SCHEMA 'article_lab_privileges' not granted for current user。兩個錯誤碼剛好指出缺的是哪一道門。"
  - q: "REVOKE 一定要加 CASCADE 嗎？"
    a: "被撤銷者若曾把權限轉授出去，不帶 CASCADE 會得到 ROLLBACK 3052: Dependent privileges exist，並附 HINT 提示改用 CASCADE。本機實測加上 CASCADE 後，LAB_ROLE_GRANTOR 轉授給 LAB_ROLE_REPORT 的那筆 SELECT 一起消失，該表只剩擁有者 dbadmin 的授權。"
  - q: "Vertica 可以用 RESET ROLE 回到無角色狀態嗎？"
    a: "25.4.0-0 不行，會回 ERROR 2676: Command RESET is not supported。要用 SET ROLE NONE；實測在 DEFAULT ROLE NONE 的使用者上，SET ROLE 後查詢成功（回 5 列），SET ROLE NONE 後同一條查詢又回 ERROR 3580。"
  - q: "怎麼查出某個角色實際繼承了哪些角色？"
    a: "查 v_catalog.roles 的 assigned_roles 欄位，角色授予角色會列在這裡，帶星號代表 WITH ADMIN OPTION（例如 LAB_ROLE_READ*）。使用者層級則查 v_catalog.users 的 all_roles 與 default_roles，兩者可能不一致，這個落差就是很多權限問題的來源。"
---
<!--
AI Summary: 在本機 testdb（Vertica v25.4.0-0，單節點 Community Edition）實測角色與權限：三層角色鏈 REPORT→ANALYST→READ 的繼承有效，但要先啟用角色；GRANT role 後 default_roles 仍為空，未 SET ROLE 時查詢回 ERROR 3580，啟用後才看到真正的 ERROR 4367（缺表權限）。另記錄 schema USAGE 與表 SELECT 的雙向短缺實驗、DEFAULT ROLE NONE、RESET ROLE 不支援（ERROR 2676）、REVOKE 的 ROLLBACK 3052 與 CASCADE 行為、grantor 記為角色的條件，以及 v_catalog.grants / roles / users 三張表的稽核查詢。清理後 roles 13、users 9、grants 2,486 筆，與實驗前一致。
AI Keywords: Vertica, GRANT, REVOKE, role inheritance, default role, schema USAGE, privilege, audit, v_catalog.grants, 25.4
-->
<!--more-->

---

## 一、先講結論

Vertica 的權限問題很少是「GRANT 沒下」，多數是「下對了，但沒生效」或「只過了一道門」。本機實測一輪之後，有四件事值得先講：

1. **讀一張表要過兩道門**：schema 的 `USAGE`，加上表上的 `SELECT`。缺前者回 `ERROR 3580`，缺後者回 `ERROR 4367`，錯誤碼直接指出你缺哪一道。
2. **`GRANT role TO user` 不會讓角色自動生效**。授予後 `v_catalog.users.all_roles` 有值，但 `default_roles` 是空的；新連線查詢照樣失敗，而且錯誤訊息是「USAGE not granted」，看起來像 schema 授權寫錯，其實是角色沒啟用。
3. **角色繼承本身沒問題**。三層鏈（使用者 → 角色 → 角色 → 角色），權限掛在最底層，實測查得到資料。繼承不是問題，啟用才是。
4. **`REVOKE` 遇到轉授會整筆回滾**，錯誤是 `ROLLBACK 3052: Dependent privileges exist`，必須加 `CASCADE`。

這篇的每個數字都來自本機 testdb 的實跑，測試物件在自建的 `article_lab_privileges` schema 與 `LAB_*` 角色，結束後全部刪除，`v_catalog.roles` 回到 13 個、`users` 9 個、`grants` 2,486 筆，與實驗前一致。

![權限的兩道門：角色鏈只決定身分，schema USAGE 與表權限各自把關]({{ '/assets/images/privileges-role-chain.svg' | relative_url }}){: loading="lazy" }

---

## 二、實測環境

| 項目 | 值 |
|------|-----|
| 資料庫 | `testdb`，單節點，Community Edition |
| 版本 | `Vertica Analytic Database v25.4.0-0` |
| 測試 schema | `article_lab_privileges`（3 張表：`lab_orders` 5 列、`lab_customers` 3 列、`lab_regions` 3 列） |
| 測試角色 | `LAB_ROLE_READ`、`LAB_ROLE_ANALYST`、`LAB_ROLE_REPORT`、`LAB_ROLE_GRANTOR` |
| 測試使用者 | `LAB_USER_REPORT`、`LAB_USER_GRANTOR` |
| 實驗前後基準 | 13 個角色、9 個使用者、2,486 筆 grants |
| 實測時間 | 2026-11-23 05:00 起（Asia/Taipei） |

全程只對自建 schema 與自建角色/使用者做 DDL/DML，沒有碰 `dbadmin`、既有角色或 `public` 的授權。所有 `GRANT`/`REVOKE` 都作用在 `LAB_*` 物件上。

順帶量到這座資料庫的授權規模，作為「稽核查詢在真實環境長什麼樣」的參考：

```sql
=> SELECT count(*) AS total_grants, count(DISTINCT grantee) AS distinct_grantees
   FROM v_catalog.grants;
 total_grants | distinct_grantees
--------------+-------------------
         2486 |                20

=> SELECT object_type, count(*) FROM v_catalog.grants GROUP BY 1 ORDER BY 2 DESC;
 object_type      | count
------------------+-------
 TABLE            |  1365
 PROCEDURE        |   658
 VIEW             |   372
 SCHEMA           |    75
 RESOURCEPOOL     |     8
 ROLE             |     5
 CLIENTAUTHENTICATION | 3
```

「ROLE」只有 5 筆——角色授予角色在整座資料庫裡是少數，這也意味著角色鏈一旦建歪，不容易被日常查詢發現。

---

## 三、原理：三個概念、兩道門

Vertica 的權限分成三層，混在一起講就會亂：

| 層級 | 內容 | 授予對象 | 誰能授予 |
|------|------|----------|----------|
| 系統權限 | `dbadmin`、`dbduser`、`sysmonitor`、`udxdeveloper`、`operator` 等內建角色 | 使用者或角色 | superuser / useradmin |
| 物件權限 | schema 的 `USAGE`/`CREATE`；表的 `SELECT`/`INSERT`/`UPDATE`/`DELETE`/`ALTER`/`REFERENCES`/`TRUNCATE`/`DROP` | 使用者或角色 | 物件擁有者，或被授予 `WITH GRANT OPTION` 者 |
| 角色 | 權限的容器，可授予角色（繼承）或使用者 | 使用者或角色 | 同上 |

本機 `v_catalog.roles` 的內建角色關係可以直接看出來：

```sql
=> SELECT name, assigned_roles FROM v_catalog.roles
   WHERE name IN ('dbadmin','dbduser','pseudosuperuser','sysmonitor','udxdeveloper','public')
   ORDER BY 1;
       name       | assigned_roles
------------------+----------------
 dbadmin          | dbduser*
 dbduser          |
 pseudosuperuser  | dbadmin*
 public           |
 sysmonitor       |
 udxdeveloper     |
```

`dbadmin` 繼承 `dbduser`，星號代表 `WITH ADMIN OPTION`——它可以再把這個角色授予別人。`public` 是一個「所有使用者都隱含加入」的角色，它的授權對全體生效，這也是為什麼本機 `v_monitor` 的 165 個 view 與 `v_catalog`/`v_internal` 的 356 個 table 一般使用者都讀得到，不需要 `sysmonitor`。

### 為什麼 `USAGE` 要獨立成一道門

`schema` 的 `USAGE` 只代表「你進得了這個 namespace」——可以解析 `schema.table` 這個名字，但不代表你能讀裡面的任何一張表。反過來，只給了表的 `SELECT` 而沒有 `USAGE`，查詢會在解析物件名稱那一步就被擋下。兩道門是 AND 關係，不是 OR。

實務上這個設計很好用：給 `USAGE` 讓使用者看得到 schema 的存在，但每張表要不要開放，可以逐表決定。代價是錯誤訊息很容易被誤讀——`3580` 與 `4367` 看起來都像「沒授權」，但修的對象完全不同。

---

## 四、Step-by-step 實測

### 4.1 建一條三層角色鏈

```sql
=> CREATE ROLE LAB_ROLE_READ;
=> CREATE ROLE LAB_ROLE_ANALYST;
=> CREATE ROLE LAB_ROLE_REPORT;
=> GRANT LAB_ROLE_READ TO LAB_ROLE_ANALYST;
=> GRANT LAB_ROLE_ANALYST TO LAB_ROLE_REPORT;
=> CREATE USER LAB_USER_REPORT IDENTIFIED BY '******';
=> GRANT LAB_ROLE_REPORT TO LAB_USER_REPORT;
=> GRANT USAGE ON SCHEMA article_lab_privileges TO LAB_ROLE_READ;
```

目錄裡的樣子：

```sql
=> SELECT name, assigned_roles FROM v_catalog.roles
   WHERE name LIKE 'LAB_%' ORDER BY name;
       name       |  assigned_roles
------------------+------------------
 LAB_ROLE_ANALYST | LAB_ROLE_READ
 LAB_ROLE_READ    |
 LAB_ROLE_REPORT  | LAB_ROLE_ANALYST

=> SELECT grantor, grantee, object_type, object_name, privileges_description
   FROM v_catalog.grants
   WHERE grantee LIKE 'LAB_%' OR grantor LIKE 'LAB_%'
   ORDER BY object_type, grantee, object_name;
 grantor |     grantee      | object_type  |      object_name       | privileges_description
---------+------------------+--------------+------------------------+------------------------
 dbadmin | LAB_USER_REPORT  | RESOURCEPOOL | general                | USAGE
 dbadmin | LAB_ROLE_ANALYST | ROLE         | LAB_ROLE_READ          |
 dbadmin | LAB_ROLE_REPORT  | ROLE         | LAB_ROLE_ANALYST       |
 dbadmin | LAB_USER_REPORT  | ROLE         | LAB_ROLE_REPORT        |
 dbadmin | LAB_ROLE_READ    | SCHEMA       | article_lab_privileges | USAGE
```

兩個細節：角色授予角色在 `v_catalog.grants` 是 `object_type = 'ROLE'`、`privileges_description` 空白；`CREATE USER` 會順帶給該使用者 resource pool `general` 的 `USAGE`，那是自動的，不用自己下。

然後看使用者的角色狀態，這裡出現本文最重要的一個坑：

```sql
=> SELECT user_name, all_roles, default_roles
   FROM v_catalog.users WHERE user_name = 'LAB_USER_REPORT';
    user_name     |    all_roles    | default_roles
------------------+-----------------+---------------
 LAB_USER_REPORT  | LAB_ROLE_REPORT |
```

`all_roles` 有 `LAB_ROLE_REPORT`，`default_roles` 是空的。也就是說：**角色已經授予，但新連線不會自動啟用它。**

### 4.2 只給 USAGE、角色又沒啟用：ERROR 3580

用這個使用者連線（不同 session）：

```sql
=> SELECT all_roles, default_roles FROM v_catalog.users WHERE user_name = current_user;
    all_roles    | default_roles
-----------------+---------------
 LAB_ROLE_REPORT |

=> SELECT count(*) FROM article_lab_privileges.lab_orders;
ERROR 3580:  Insufficient privilege: USAGE on SCHEMA 'article_lab_privileges' not granted for current user
```

訊息說 USAGE 沒給，但 USAGE 明明已經授予 `LAB_ROLE_READ`，而使用者的角色鏈一路繼承到它。真正的原因是角色沒啟用，所以整個角色鏈的權限在這次 session 都看不到。**如果只照錯誤訊息去補 schema 授權，會補在錯的地方。**

### 4.3 SET ROLE 之後，才看到真正的錯誤

```sql
=> SET ROLE LAB_ROLE_REPORT;
SET
=> SELECT count(*) FROM article_lab_privileges.lab_orders;
ERROR 4367:  Permission denied for relation lab_orders
```

過了第一道門，錯誤換成缺表權限。這一對錯誤碼就是診斷順序：先 `3580` 再 `4367`，一次只會出現一個。

### 4.4 設定 default role，並把 SELECT 授到鏈的最底層

```sql
=> ALTER USER LAB_USER_REPORT DEFAULT ROLE LAB_ROLE_REPORT;
=> SELECT user_name, all_roles, default_roles FROM v_catalog.users WHERE user_name='LAB_USER_REPORT';
    user_name    |    all_roles    |  default_roles
-----------------+-----------------+-----------------
 LAB_USER_REPORT | LAB_ROLE_REPORT | LAB_ROLE_REPORT
```

再把 `SELECT` 授予**最底層**的 `LAB_ROLE_READ`，用新連線（不 `SET ROLE`）查詢：

```sql
=> SELECT region, count(*) AS orders, sum(amount) AS total
   FROM article_lab_privileges.lab_orders
   GROUP BY region ORDER BY region;
 region | orders | total
--------+--------+--------
 east   |      1 |  15.25
 north  |      2 | 200.50
 south  |      2 | 299.99
```

三層繼承確實生效：權限掛在 `LAB_ROLE_READ`，使用者只被授予 `LAB_ROLE_REPORT`，中間隔了兩層。**繼承沒有問題，啟用才是問題。**

### 4.5 反向實驗：有 SELECT、收回 USAGE

```sql
=> REVOKE USAGE ON SCHEMA article_lab_privileges FROM LAB_ROLE_READ;
=> SELECT count(*) FROM article_lab_privileges.lab_orders;   -- 以 LAB_USER_REPORT 連線
ERROR 3580:  Insufficient privilege: USAGE on SCHEMA 'article_lab_privileges' not granted for current user
```

同一張表、同一個 `SELECT` 授權，只把 schema 的 `USAGE` 拿掉就讀不到。兩道門缺一不可，這組對照就是證明。

### 4.6 DEFAULT ROLE NONE、SET ROLE、SET ROLE NONE

```sql
=> ALTER USER LAB_USER_REPORT DEFAULT ROLE NONE;
=> SELECT all_roles, default_roles FROM v_catalog.users WHERE user_name='LAB_USER_REPORT';
    all_roles    | default_roles
-----------------+---------------
 LAB_ROLE_REPORT |
```

以該使用者重新連線：

```sql
=> SELECT count(*) FROM article_lab_privileges.lab_orders;
ERROR 3580:  Insufficient privilege: USAGE on SCHEMA 'article_lab_privileges' not granted for current user
=> SET ROLE LAB_ROLE_REPORT;
SET
=> SELECT count(*) FROM article_lab_privileges.lab_orders;
 count
-------
     5
=> SET ROLE NONE;
SET
=> SELECT count(*) FROM article_lab_privileges.lab_orders;
ERROR 3580:  Insufficient privilege: USAGE on SCHEMA 'article_lab_privileges' not granted for current user
```

順帶驗到一個方言差異：`RESET ROLE` 在 25.4.0-0 不支援。

```sql
=> RESET ROLE;
ERROR 2676:  Command RESET is not supported
```

要用 `SET ROLE NONE`。

### 4.7 系統權限與 DDL 邊界：一般使用者做不了什麼

把角色拿掉之後，用一般使用者嘗試 DDL，四個錯誤訊息各有意義：

```sql
=> CREATE ROLE LAB_ROLE_HACK;
ROLLBACK 11644:  Only superuser or user admin can create roles

=> CREATE TABLE article_lab_privileges.lab_hack (x int);
ROLLBACK 4367:  Permission denied for schema article_lab_privileges

=> GRANT SELECT ON article_lab_privileges.lab_orders TO LAB_ROLE_READ;
ROLLBACK 2061:  "SELECT" privilege(s) for relation "lab_orders" could not be granted to "LAB_ROLE_READ"
HINT:  Check grantor's granting options

=> ALTER TABLE article_lab_privileges.lab_orders ADD COLUMN note varchar(8);
ROLLBACK 8958:  Insufficient privileges on lab_orders, alter privileges needed
```

四句話分別對應：建角色是系統權限（`11644`）、建表要 schema 的 `CREATE`（`4367`，注意這裡的對象是 schema 不是表）、轉授要有 `WITH GRANT OPTION`（`2061`）、改表要 `ALTER`（`8958`）。這些都不是「多授一個 SELECT」能解決的。

### 4.8 REVOKE 與 CASCADE：grantor 會是角色，不是登入的人

先讓一個非 superuser 透過角色轉授，觀察 grantor 記成誰。建立 `LAB_USER_GRANTOR`，授予 `LAB_ROLE_GRANTOR` 並設為 default role，再用它連線：

```sql
-- 以 LAB_USER_GRANTOR 連線（default role = LAB_ROLE_GRANTOR）
=> GRANT SELECT ON article_lab_privileges.lab_regions TO LAB_ROLE_REPORT;
```

```sql
=> SELECT grantor, grantee, object_type, object_name, privileges_description
   FROM v_catalog.grants WHERE object_name='lab_regions' ORDER BY grantor, grantee;
      grantor      |     grantee      | object_type | object_name | privileges_description
-------------------+------------------+-------------+-------------+------------------------
 LAB_ROLE_GRANTOR  | LAB_ROLE_REPORT  | TABLE       | lab_regions | SELECT
 dbadmin           | LAB_ROLE_GRANTOR | TABLE       | lab_regions | SELECT*
 dbadmin           | dbadmin          | TABLE       | lab_regions | INSERT*, SELECT*, ...
```

`grantor` 是**角色** `LAB_ROLE_GRANTOR`，不是登入的使用者。這點很重要：要驗證「角色轉授」的行為，必須用非 superuser 連線；如果用 `dbadmin` 先 `SET ROLE` 再 `GRANT`，實測 grantor 仍會記成 `dbadmin`，因為 superuser 的 `SET ROLE` 不會改變授權身分。我就先踩了這個坑，目錄裡多出一筆 `dbadmin → LAB_ROLE_REPORT`，得先 `REVOKE` 掉才能做乾淨的 CASCADE 實驗。

接著撤銷，先不帶 `CASCADE`：

```sql
=> REVOKE SELECT ON article_lab_privileges.lab_regions FROM LAB_ROLE_GRANTOR;
ROLLBACK 3052:  Dependent privileges exist
HINT:  Use CASCADE to revoke them too
```

整筆回滾，連 `LAB_ROLE_GRANTOR` 自己那筆都沒動。加上 `CASCADE`：

```sql
=> REVOKE SELECT ON article_lab_privileges.lab_regions FROM LAB_ROLE_GRANTOR CASCADE;
REVOKE PRIVILEGE

=> SELECT grantor, grantee, privileges_description
   FROM v_catalog.grants WHERE object_name='lab_regions' ORDER BY grantor, grantee;
 grantor | grantee |                          privileges_description
---------+---------+-------------------------------------------------------------------------
 dbadmin | dbadmin | INSERT*, SELECT*, UPDATE*, DELETE*, ALTER*, REFERENCES*, DROP*, TRUNCATE*
```

轉授出去的那筆跟著消失，只剩擁有者自己的完整權限。以 `LAB_USER_REPORT` 重新查 `lab_regions`，也如預期被擋：

```sql
=> SELECT count(*) FROM article_lab_privileges.lab_regions;
ERROR 4367:  Permission denied for relation lab_regions
```

### 4.9 批次授權與 ADMIN OPTION 的星號

25.4 支援 `GRANT ... ON ALL TABLES IN SCHEMA`，一次把整個 schema 的表開放：

```sql
=> GRANT SELECT ON ALL TABLES IN SCHEMA article_lab_privileges TO LAB_ROLE_READ;
GRANT PRIVILEGE
=> SELECT grantee, object_name, privileges_description FROM v_catalog.grants
   WHERE object_schema='article_lab_privileges' AND grantee='LAB_ROLE_READ' ORDER BY object_name;
    grantee    |  object_name  | privileges_description
---------------+---------------+------------------------
 LAB_ROLE_READ | lab_customers | SELECT
 LAB_ROLE_READ | lab_orders    | SELECT
 LAB_ROLE_READ | lab_regions   | SELECT
```

角色層級的 `WITH ADMIN OPTION` 則是這樣記的：

```sql
=> GRANT LAB_ROLE_READ TO LAB_ROLE_ANALYST WITH ADMIN OPTION;
NOTICE 4617:  Role "LAB_ROLE_READ" was already granted to role "LAB_ROLE_ANALYST"
GRANT ROLE
=> SELECT name, assigned_roles FROM v_catalog.roles WHERE name='LAB_ROLE_ANALYST';
       name       |  assigned_roles
------------------+------------------
 LAB_ROLE_ANALYST | LAB_ROLE_READ*
```

即使角色早就授予，再下一次 `WITH ADMIN OPTION` 會出現 `NOTICE 4617` 但仍把 admin option 補上，星號就是痕跡。表的 `WITH GRANT OPTION` 同理，記在 `privileges_description` 的 `SELECT*`。

---

## 五、稽核：三個可以直接貼進 runbook 的查詢

**1. 角色鏈（誰繼承誰、有沒有 admin option）**

```sql
SELECT name, assigned_roles
FROM v_catalog.roles
WHERE name NOT IN ('public')
ORDER BY name;
```

**2. 某個 schema 的完整授權面**

```sql
SELECT grantee, object_type, object_name, privileges_description
FROM v_catalog.grants
WHERE object_schema = 'myschema'
ORDER BY object_type, object_name, grantee;
```

星號 `SELECT*` 代表對方可以把 `SELECT` 再轉授出去——稽核時這比 `SELECT` 更該注意。

**3. `public` 的授權盤點（最常被忽略的擴大面）**

```sql
SELECT object_schema, object_type, count(*) AS grants
FROM v_catalog.grants
WHERE grantee = 'public'
GROUP BY 1, 2 ORDER BY 3 DESC;
```

本機結果顯示 `public` 在 `v_catalog`/`v_monitor`/`v_internal` 上有 356 筆 TABLE 與 165 筆 VIEW 授權，這是內建的，不是誰偷開的；但如果 `public` 出現在你自己的業務 schema 上，那就要問來源。

**4. 使用者實際拿到的角色**

```sql
SELECT user_name, is_super_user, all_roles, default_roles
FROM v_catalog.users
ORDER BY user_name;
```

`all_roles` 與 `default_roles` 的差集，就是「有授權但預設不生效」的清單，也是本文那個 `3580` 的來源。

---

## 六、常見錯誤與反模式

1. **以為 `GRANT role TO user` 就生效。** 實測 `default_roles` 是空的，必須 `ALTER USER ... DEFAULT ROLE` 或每次 `SET ROLE`。這是本文最花時間的一個誤判點。
2. **照著 `3580` 去補 schema USAGE。** 當角色沒啟用時，任何角色鏈上的權限都看不到，錯誤一律報 USAGE；先確認角色狀態，再談授權。
3. **只給 `USAGE` 就以為能讀表。** 缺表權限是 `4367`，兩個錯誤碼要分開看。
4. **用 `dbadmin` 加 `SET ROLE` 去測「角色轉授」。** grantor 會被記成 `dbadmin`，`REVOKE ... CASCADE` 的依賴關係也不成立；要用非 superuser 成員連線。
5. **`REVOKE` 不帶 `CASCADE` 卻以為已經撤銷。** 實際是 `ROLLBACK 3052` 整筆回滾，權限還在，只是沒看到錯誤就以為成功。
6. **把權限授予角色後又逐一授權給使用者。** 兩條路徑同時存在時，撤銷其中一條不會讓使用者失去權限，稽核時很容易誤判「已收回」。

---

## 七、風險與建議

**現在就能做（唯讀、低風險）**

- 用第五節的四個查詢做一次授權盤點，特別看 `SELECT*`/`INSERT*` 這類帶 grant option 的授權，以及 `public` 在業務 schema 上的授權。
- 比較 `all_roles` 與 `default_roles`，找出「已授予但預設不啟用」的角色；這通常是刻意設計（要求明確 `SET ROLE`）也可能是設定漏了，值得跟負責人確認。
- 把 `v_catalog.grants` 的結果定期快照，作為變更前的基準。

**需要變更（要窗口、要核准）**

- `GRANT`/`REVOKE` 與 `ALTER USER ... DEFAULT ROLE` 都會即時改變連線行為。先在測試庫演練撤銷順序，特別是含 `CASCADE` 的操作——它會連帶移除別人的轉授，且沒有「預覽」功能。
- 批次授權 `ALL TABLES IN SCHEMA` 很方便，但它不會涵蓋之後新建的表；新表要嘛再跑一次，要嘛用統一的上線流程處理，不要假設它會自動套用。
- 角色鏈建議不要超過三層。本機只有 5 筆角色授予角色就已經不容易肉眼追蹤，四層以上請直接寫查詢而不是看圖。

---

## 八、清理與驗證

```sql
=> REVOKE LAB_ROLE_REPORT FROM LAB_USER_REPORT;
=> REVOKE LAB_ROLE_GRANTOR FROM LAB_USER_GRANTOR;
=> DROP USER LAB_USER_REPORT;
=> DROP USER LAB_USER_GRANTOR;
=> DROP SCHEMA article_lab_privileges CASCADE;
=> DROP ROLE LAB_ROLE_REPORT CASCADE;
=> DROP ROLE LAB_ROLE_ANALYST CASCADE;
=> DROP ROLE LAB_ROLE_READ CASCADE;
=> DROP ROLE LAB_ROLE_GRANTOR CASCADE;

=> SELECT count(*) AS lab_roles FROM v_catalog.roles WHERE name LIKE 'LAB_%';
 lab_roles
-----------
         0
=> SELECT count(*) AS lab_users FROM v_catalog.users WHERE user_name LIKE 'LAB_%';
 lab_users
-----------
         0
=> SELECT count(*) AS lab_grants FROM v_catalog.grants
   WHERE grantee LIKE 'LAB_%' OR grantor LIKE 'LAB_%' OR object_schema='article_lab_privileges';
 lab_grants
------------
          0
=> SELECT (SELECT count(*) FROM v_catalog.roles) AS roles,
          (SELECT count(*) FROM v_catalog.users) AS users,
          (SELECT count(*) FROM v_catalog.grants) AS grants;
 roles | users | grants
-------+-------+--------
    13 |     9 |   2486
```

`DROP ROLE ... CASCADE` 會一併清掉角色之間的授予關係，不需要先逐一 `REVOKE` 角色。清理後的 13 / 9 / 2,486 與實驗開始前的基準完全相同。

---

## 九、參考資料

- [GRANT PRIVILEGE](https://docs.vertica.com/25.4.x/en/sql-reference/statements/grant-privileges/)
- [GRANT ROLE](https://docs.vertica.com/25.4.x/en/sql-reference/statements/grant-role/)
- [REVOKE PRIVILEGE](https://docs.vertica.com/25.4.x/en/sql-reference/statements/revoke-privileges/)
- [SET ROLE](https://docs.vertica.com/25.4.x/en/sql-reference/statements/set-role/)
- [CREATE ROLE](https://docs.vertica.com/25.4.x/en/sql-reference/statements/create-role/)
- [Security and authentication](https://docs.vertica.com/25.4.x/en/admin/security-and-authentication/)
