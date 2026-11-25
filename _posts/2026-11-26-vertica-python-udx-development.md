---
layout: post
title: "Vertica Python UDx 實戰：Scalar、Transform 與 Aggregate 三種函數"
date: 2026-11-26 05:00:00 +0800
categories: vertica udx
tags: [vertica, udx, python, scalar-function, transform-function, aggregate-function, c++]
description: "Vertica 25.4 的 Python SDK 只有 Scalar 與 Transform，沒有 AggregateFunction：實測 class 繼承 AggregateFunction 直接 AttributeError。本文用 20 萬列資料把三種函數都跑過一次，Scalar 反而比原生 SQL 快 23.6%（220 ms 對 288 ms），Transform 打平（191 ms 對 206 ms），但單列查詢慢 5.3 倍；Aggregate 只能改用 C++，且只吃一個輸入欄位，兩欄加權平均會回 ERROR 2136。"
image: /assets/images/og/vertica-python-udx-development-zh.png
faq:
  - q: "Vertica 的 Python UDx 可以寫 Aggregate Function 嗎？"
    a: "不行。25.4 的 Python SDK 只匯出 ScalarFunction、TransformFunction、UDFilter、UDParser，沒有 AggregateFunction；實測 class 繼承 vertica_sdk.AggregateFunction 會在建 library 時回 AttributeError: module 'vertica_sdk' has no attribute 'AggregateFunction'。UDAF 只能用 C++ 寫。"
  - q: "Python UDx 會比原生 SQL 慢嗎？"
    a: "看形狀。本機 20 萬列實測：逐列字串處理的 Scalar UDx 花 220 ms，比 SQL 的 288 ms 快 23.6%；一對多的 Transform 花 191 ms，與 SQL 的 206 ms 差不多；但只查一列時 UDx 要跨 fenced process，15 ms 對 2.8 ms，慢 5.3 倍。"
  - q: "UDAF 可以用兩個輸入欄位嗎？"
    a: "不行。Vertica 的 aggregate function 只接受一個輸入欄位，實測以兩個 FLOAT 參數建立會回 ERROR 2136: Aggregate function cannot have 2 input argument(s)。加權平均這種需要兩個值的聚合，只能靠視窗函數、先把兩個值編碼成一個，或改成 C++ analytic function 來做。"
  - q: "Python Transform Function 為什麼會少掉幾列資料？"
    a: "常見原因是把官方範例的 while True / if not input.next(): break 改成 while input.next()。同一份邏輯、同一批 20 萬列資料，官方寫法輸出 800,087 列，改寫後只剩 800,064 列，而且沒有任何錯誤；改成每個 event_id 一個分區時甚至回 0 列。請照官方迴圈結構寫。"
---

<!--
AI Summary: 在 testdb（Vertica v25.4.0-0，單節點）以 20 萬列、80 萬個 tag 實測 Python UDx 三種函數：Scalar 遮罩 Email 花 220 ms（原生 SQL 288 ms，快 23.6%）、Transform 拆 tag 花 191 ms（原生 SQL 206 ms）、單列查詢 15 ms（原生 2.8 ms，慢 5.3 倍）。Python SDK 沒有 AggregateFunction，UDAF 只能 C++ 且只接受單一輸入欄位（兩欄回 ERROR 2136）；C++ harmonic mean 花 31.2 ms，原生 SQL 6.3 ms。另實測 UDTF 迴圈寫法錯誤會靜默漏掉 23 列（800,087→800,064），以及 getPrototype 不能帶長度、多欄輸出必須給欄位索引等 API 細節。
AI Keywords: Vertica, Python UDx, UDSF, UDTF, UDAF, vertica_sdk, CREATE LIBRARY, fenced mode, 25.4
-->
<!--more-->

---

## 一、先講結論

「用 Python 寫 Vertica 的 UDx」聽起來是一件事，實際上在 25.4 上是**兩種半**：

1. **Scalar（UDSF）可以，而且是這次三種裡效能最好的一種。** 20 萬列的 Email 遮罩，Python UDx 花 220 ms，原生 SQL 的 `substr` + `position` 組合花 288 ms，**UDx 快 23.6%**。這跟「Python 一定慢」的直覺相反，原因後面會拆。
2. **Transform（UDTF）可以，效能也打平。** 80 萬個 tag 的展開，UDx 花 191 ms，SQL 的 `split_part` + 數字表 cross join 花 206 ms。
3. **Aggregate（UDAF）不行——至少 Python 不行。** 25.4 的 `vertica_sdk` 根本沒有 `AggregateFunction` 這個類別，繼承它會直接 `AttributeError`。UDAF 只能寫 C++，而且**只接受一個輸入欄位**：我原本想做的加權平均（兩個 DOUBLE）在 `CREATE AGGREGATE FUNCTION` 就被拒絕，回 `ERROR 2136`。

另外兩個比效能更值得記住的發現：

- **單列查詢慢 5.3 倍。** 同一條遮罩邏輯，查一列時 UDx 要 15 ms，SQL 只要 2.8 ms。Python UDx 一律跑在 fenced process，每次呼叫都有跨程序成本；掃大量列時這個成本被攤掉，查單列時它是全部。
- **UDTF 的迴圈寫法會靜默漏列。** 我照官方範例的 `while True` / `if not input.next(): break` 寫，輸出 800,087 列完全正確；把同樣邏輯改寫成 `while input.next():`，輸出變成 800,064 列，**少了 23 列，沒有任何警告或錯誤**。同一份資料、同一個函式、只差迴圈形狀。

這篇的每個數字都來自本機實跑：Vertica v25.4.0-0、單節點 testdb、20 萬列自建事件表。測試物件全部在 `article_lab_udx` schema，寫完已 `DROP SCHEMA ... CASCADE`。

## 二、實測環境

| 項目 | 值 |
|---|---|
| 版本 | Vertica Analytic Database v25.4.0-0（Community Edition） |
| 節點 | 單節點 `v_testdb_node0001`，狀態 UP |
| 資料庫 | `testdb` |
| 測試資料 | `lab_events`，200,000 列，tags 每列 3–5 個，共 800,087 個 tag |
| 內建 Python | `/opt/vertica/oss/python3/bin/python3` → Python 3.13.1 |
| C++ 編譯器 | g++ 15.2.0（SDK 標頭在 `/opt/vertica/sdk/include`） |
| Python UDx 執行模式 | fenced（`v_catalog.user_functions.is_fenced = t`） |
| C++ UDAF 執行模式 | unfenced（`is_fenced = f`） |
| 測試 schema | `article_lab_udx`（已清除） |

先查版本與節點，這一步不能省，因為 UDx 的 API 與 Python 版本跟著伺服器走：

```sql
=> SELECT version();
 Vertica Analytic Database v25.4.0-0

=> SELECT node_name, is_primary, node_state FROM v_catalog.nodes;
     node_name     | is_primary | node_state
-------------------+------------+------------
 v_testdb_node0001 | t          | UP
```

節點上的 Python 版本可以直接問：

```bash
$ /opt/vertica/oss/python3/bin/python3 --version
Python 3.13.1
```

## 三、三種函數的形狀，先分清楚

| | Scalar（UDSF） | Transform（UDTF） | Aggregate（UDAF） |
|---|---|---|---|
| 輸入 → 輸出 | 一列進、一個值出 | 一個 partition 進、零到多列出 | 一整欄進、一個值出 |
| 呼叫方式 | `SELECT f(col)` | `SELECT f(col) OVER (PARTITION ...)` | `SELECT f(col)`，和 `sum()` 同位置 |
| 25.4 可用語言 | C++、Python、Java、R | C++、Python、Java、R | **只有 C++** |
| 本機 Python 實測 | 成功 | 成功（有漏列陷阱） | `AttributeError`，不存在的類別 |
| 執行位置 | fenced | fenced | C++ 預設 unfenced |

「fenced」不是形容詞，是安全邊界：Python UDx 跑在資料庫外的獨立程序，崩潰只會讓那一條查詢失敗；C++ UDx 預設跑在伺服器行程內，一個 segfault 就是整個 database 下線。

Python SDK 到底給了什麼，可以直接在 UDx 裡把 `dir()` 印出來。這是我在節點上實際拿到的清單（節錄）：

```
ArrayReader, ArrayWriter, BlockReader, BlockWriter, ColumnTypes, FilterFactory,
InputBuffer, OutputBuffer, ParamReader, ParamWriter, PartitionReader,
PartitionWriter, ParserFactory, ScalarFunction, ScalarFunctionFactory,
ServerInterface, TransformFunction, TransformFunctionFactory, UDFilter,
UDParser, UDXFactory, UDXObject, VerticaType, Volatility, ...
```

沒有 `AggregateFunction`、沒有 `AnalyticFunction`。這就是為什麼下面第三步一定要換語言。

![三種 UDx 的形狀與語言支援：Scalar 與 Transform 可用 Python 且為 fenced，Aggregate 只有 C++ 且預設 unfenced；20 萬列實測 Scalar 220 毫秒對 SQL 288 毫秒]({{ '/assets/images/udx-three-function-types.svg' | relative_url }}){: loading="lazy" }

## 四、Step 1：Scalar，把 Email 遮罩

需求很現實：給非工程師看的報表要遮掉 Email 的 local part。SQL 寫得出來，但要三個函數疊在一起；用 UDx 則是一個函式、一次掃描。

### 4.1 程式

```python
import vertica_sdk

class mask_email(vertica_sdk.ScalarFunction):
    def processBlock(self, server_interface, arg_reader, res_writer):
        while True:
            if arg_reader.isNull(0):
                res_writer.setNull()
            else:
                s = arg_reader.getString(0)
                at = s.find('@')
                if at <= 0:
                    res_writer.setString(s)
                else:
                    res_writer.setString(s[0] + '***' + s[at:])
            res_writer.next()
            if not arg_reader.next():
                break

class mask_email_factory(vertica_sdk.ScalarFunctionFactory):
    volatility = vertica_sdk.Volatility.IMMUTABLE
    strictness = vertica_sdk.Strictness.RETURN_NULL_ON_NULL_INPUT

    def getPrototype(self, srv_interface, arg_types, return_type):
        arg_types.addVarchar()
        return_type.addVarchar()

    def getReturnType(self, srv_interface, arg_types, return_type):
        return_type.addVarchar(128)

    def createScalarFunction(self, srv):
        return mask_email()
```

三個地方值得停下來看：

- **`getPrototype` 與 `getReturnType` 是兩套不同的型別物件。** `getPrototype` 收到的是 `ColumnTypes`，`addVarchar()` **不接受長度參數**；長度要在 `getReturnType` 的 `SizedColumnTypes` 上給。我一開始在 `getPrototype` 寫 `return_type.addVarchar(128)`，`CREATE FUNCTION` 直接失敗：

  ```
  TypeError: addVarchar() takes exactly 0 positional arguments (1 given)
  ```

- **`volatility = IMMUTABLE`** 讓優化器可以常數折疊、快取結果；有副作用或依時間變動的函式不能這樣標。
- **`RETURN_NULL_ON_NULL_INPUT`** 讓引擎在輸入是 NULL 時直接回 NULL，不必進 Python。但 `processBlock` 裡我還是留了 `isNull` 判斷，因為這個 strictness 只作用在「所有輸入皆 NULL」的情況，寫防禦性程式碼比較安全。

### 4.2 安裝與執行

```sql
=> CREATE LIBRARY article_lab_udx.udx_lab
   AS '/tmp/opencode/udx_lab/udx_lab.py' LANGUAGE 'Python';
CREATE LIBRARY

=> CREATE FUNCTION article_lab_udx.lab_mask_email
   AS NAME 'mask_email_factory' LIBRARY article_lab_udx.udx_lab;
CREATE FUNCTION

=> SELECT lab_mask_email('alice.wang@example.com') AS masked,
          lab_mask_email(NULL) AS masked_null;
      masked      | masked_null
------------------+-------------
 a***@example.com |
```

`CREATE LIBRARY` 不是「登記一個路徑」，而是**把檔案複製進 catalog**。從錯誤訊息裡可以看到副本的實際位置：

```
/opt/data/testdb/v_testdb_node0001_catalog/Libraries/02b94901.../udx_lab_02b94901....py
```

所以改完 `.py` 檔一定要 `CREATE OR REPLACE LIBRARY`（或 `ALTER LIBRARY ... AS`），否則跑的還是舊版。這一點在開發階段很容易搞混：檔案改了、函式沒變。

## 五、Step 2：Transform，一列拆成多列

`tags` 欄位是逗號分隔的字串，報表要的是「一個 tag 一列」。這是 UDTF 的標準工作。

### 5.1 程式（注意迴圈結構）

```python
class split_tags(vertica_sdk.TransformFunction):
    def processPartition(self, server_interface, input, output):
        while True:
            for pos, tag in enumerate(input.getString(0).split(',')):
                tag = tag.strip()
                if not tag:
                    continue
                output.setString(0, tag)
                output.setInt(1, pos + 1)
                output.next()
            if not input.next():
                break
```

`output.setString(0, tag)` 的第一個參數是**輸出欄位索引**，不是值。Scalar 只有一欄，所以 `setString(value)` 可以；Transform 有兩欄，只給一個參數會得到：

```
TypeError: setString() takes exactly 2 positional arguments (1 given)
```

### 5.2 安裝與查詢

```sql
=> CREATE TRANSFORM FUNCTION article_lab_udx.lab_split_tags
   AS NAME 'split_tags_factory' LIBRARY article_lab_udx.udx_lab;

=> SELECT tag, pos FROM (
     SELECT lab_split_tags(tags) OVER (PARTITION BEST) AS (tag, pos)
     FROM article_lab_udx.lab_events WHERE event_id <= 2
   ) t ORDER BY 1, 2;
   tag    | pos
----------+-----
 backup   |   5
 security |   2
 sql      |   4
 tuning   |   3
 udx      |   1
```

`AS (tag, pos)` 不是裝飾。沒有它，UDTF 不知道兩欄叫什麼名字：

```
ERROR 5384:  User defined transform must provide names or aliases for return columns
```

### 5.3 這裡有個會靜默漏列的坑

我第一版把迴圈寫成 `while input.next():`，邏輯與上面完全一樣，只是把「先讀再判斷」改成「判斷後才讀」。結果：

| 寫法 | 輸出列數 | `sum(length(tag))` |
|---|---|---|
| 官方 `while True` / `if not input.next(): break` | 800,087 | 4,241,485 |
| `while input.next():` | 800,064 | 4,241,351 |

少 23 列，**沒有任何錯誤、警告或 log**。用官方 SDK 內附的 `StringTokenizer`（同樣的資料、同樣的 `PARTITION BEST`）跑，結果是 800,087，證明不是資料問題。把分區改成 `PARTITION BY event_id`（20 萬個單列分區）時，錯的寫法甚至回 **0 列**，官方寫法仍是 800,087。

更麻煩的是，漏掉的比例只有 0.003%，抽樣檢查很可能看不出來。我把兩邊的 tag 計數並排，才看到每一種 tag 都少一點：

| tag | SQL 計數 | UDx 計數 | 差 |
|---|---|---|---|
| monitoring | 79,877 | 79,872 | 5 |
| eon | 79,872 | 79,868 | 4 |
| backup | 80,158 | 80,155 | 3 |
| python | 80,128 | 80,125 | 3 |
| sql | 79,658 | 79,656 | 2 |
| udx | 79,933 | 79,933 | 0 |

結論很簡單：**UDTF 的 `processPartition` 請照官方範例的形狀寫，不要自己重構成看起來更「Pythonic」的迴圈。** 這種錯誤不會有任何訊號，只會讓下游報表少幾筆。

## 六、Step 3：Aggregate，只能換 C++

### 6.1 先證明 Python 不行

```python
class py_agg(vertica_sdk.AggregateFunction):
    ...
```

```sql
=> CREATE LIBRARY article_lab_udx.agg_fail
   AS '/tmp/opencode/udx_lab/agg_fail.py' LANGUAGE 'Python';
ROLLBACK 3399:  Failure in UDx RPC call InvokeGetLibraryInfo():
(Python error type [<class 'AttributeError'>])
AttributeError: module 'vertica_sdk' has no attribute 'AggregateFunction'
```

注意錯誤發生在 `CREATE LIBRARY`，不是執行時：Python 在 import 階段就炸了。

### 6.2 換 C++，還要面對第二個限制

UDAF 在 C++ 端要實作 `initAggregate`、`aggregate`、`combine`、`terminate`，並用 `getIntermediateTypes` 宣告中間態。我寫了一個 harmonic mean（調和平均，`n / Σ(1/x)`），核心如下：

```cpp
class HarmonicMean : public AggregateFunction
{
public:
    virtual void initAggregate(ServerInterface &srv, IntermediateAggs &aggs)
    {
        aggs.getFloatRef(0) = 0.0;   // Σ(1/x)
        aggs.getIntRef(1) = 0;       // count
    }

    virtual void aggregate(ServerInterface &srv, BlockReader &argReader,
                           IntermediateAggs &aggs)
    {
        vfloat &recip = aggs.getFloatRef(0);
        vint &cnt = aggs.getIntRef(1);
        do {
            if (argReader.isNull(0)) continue;
            vfloat x = argReader.getFloatRef(0);
            if (x == 0.0) continue;
            recip += 1.0 / x;
            cnt += 1;
        } while (argReader.next());
    }

    virtual void terminate(ServerInterface &srv, BlockWriter &resWriter,
                           IntermediateAggs &aggs)
    {
        vfloat recip = aggs.getFloatRef(0);
        vint cnt = aggs.getIntRef(1);
        if (cnt == 0 || recip == 0.0) resWriter.setNull();
        else resWriter.setFloat(static_cast<vfloat>(cnt) / recip);
    }

    InlineAggregate()
};
```

`getFloatRef` 的名字容易誤導：`vfloat` 在 SDK 裡是 `typedef double`，不是 float32。中間態的索引必須與 `getIntermediateTypes` 的順序一致；我第一次把 count 寫成 `getIntRef(0)`，結果 harmonic mean 回 `Infinity`——讀到了 double 欄位的位元組當 int。這種錯誤不會報錯，只會回一個看起來很像數字的錯答案。

編譯時有兩個實測踩到的點：

```bash
g++ -I /opt/vertica/sdk/include -shared -fPIC -std=c++11 \
    -D_GLIBCXX_USE_CXX11_ABI=1 \
    -o HarmonicMean.so HarmonicMean.cpp /opt/vertica/sdk/include/Vertica.cpp
```

1. **必須一起編 `Vertica.cpp`**。只編自己的 `.cpp` 會得到 `undefined symbol: vt_throw_exception`，`CREATE LIBRARY` 時才爆。
2. **SDK 對 GCC 14 以上沒有測試過**。本機 g++ 15.2.0，編譯會印 `#warning "Vertica builds using GCC v14.0 or above were not tested."`；能編、能跑，但這是別人的免責聲明，升級前要重編。

### 6.3 兩個輸入欄位？直接拒絕

我原本要寫的是加權平均 `sum(score*weight)/sum(weight)`。把工廠的 prototype 宣告成兩個 FLOAT 後：

```sql
=> CREATE AGGREGATE FUNCTION article_lab_udx.lab_weighted_avg
   AS NAME 'WeightedAvgFactory' LIBRARY article_lab_udx.udx_wavg;
ROLLBACK 2136:  Aggregate function cannot have 2 input argument(s)
```

也就是說，**UDAF 的簽名是「一欄進、一值出」**。加權平均要嘛用視窗函數在 SQL 裡做，要嘛把兩個值編碼成一個（例如 ROW → 但 aggregate 不支援 complex type），要嘛改成 C++ analytic function。這是選型時就該知道的事，不是寫到一半才發現。

### 6.4 結果對照

```sql
=> SELECT lab_harmonic_mean(score) AS udx_hm,
          (SELECT ...) AS sql_hm FROM lab_events WHERE score > 0;
      udx_hm      |      sql_hm
------------------+------------------
 10.0703745450042 | 10.0703745450033
```

差距 9×10⁻¹³，來自浮點加法順序（UDAF 的中間態是逐區塊累加後 combine，SQL 是單一 `sum`）。以 harmonic mean 這種對小值敏感的聚合來說，這個量級完全可以接受；但如果你的聚合是金額，請用 `VNumeric` 而不是 `vfloat`。

## 七、效能：不是「Python 一定慢」

全部在 20 萬列上跑，同一 session、同一份資料，每種跑三次取中位數：

| 工作 | 原生 SQL | Python / C++ UDx | 差距 |
|---|---|---|---|
| Scalar：Email 遮罩（20 萬列） | 288.5 ms | **220.4 ms** | UDx 快 23.6% |
| Transform：拆 tag（80 萬列輸出） | 205.6 ms | **190.8 ms** | UDx 快 7.2% |
| Aggregate：harmonic mean | **6.3 ms** | 31.2 ms | UDx 慢 5.0 倍 |
| 單列查詢：遮罩一個字串 | **2.8 ms** | 15.0 ms | UDx 慢 5.3 倍 |

怎麼讀這張表：

- **掃描型的 Scalar 反而快。** SQL 版本要 `position('@' in email)` 再一次 `substr` 拼接，等於同一欄掃兩次、建兩次中間字串；Python 版本一次 `find` 就切完。20 萬列時，Python 的解譯成本被「少掃一次欄位」抵掉還有剩。
- **Transform 打平。** 兩邊都要把 20 萬列展開成 80 萬列，差距在誤差範圍內。UDTF 的價值在「SQL 寫不出來或寫起來很痛苦」，不是速度。
- **單列查詢貴在啟動，不在運算。** 15 ms 裡絕大部分是 fenced process 的往返；查 20 萬列時同一個 process 攤掉 20 萬次呼叫，成本就看不見了。所以 UDx 不適合 OLTP 式的點查詢。
- **UDAF 慢 5 倍是框架成本。** 原生 `sum` 是 C++ 內建、可向量化；我的 UDAF 每個 block 都要走一次虛擬呼叫與中間態更新。31 ms 對 6 ms 看起來很多，但兩者都是「掃 20 萬列」，絕對值都不到 0.05 秒。

`EXPLAIN` 看得出來 UDx 被放在哪裡——它就是一個普通的 `ExprEval`，沒有神奇的節點：

```
5[label = "ExprEval:
  length(article_lab_udx.lab_mask_email(lab_events.user_email))"]
6[label = "ScanStep: lab_events_super
  user_email"]
```

也就是說，**UDx 不會幫你改變計畫形狀**。該做的 projection、該建的統計資訊，一個都不能少。

## 八、常見錯誤與反模式

1. **以為 Python 能寫 UDAF。** 錯誤在 `CREATE LIBRARY` 就出現：`AttributeError: module 'vertica_sdk' has no attribute 'AggregateFunction'`。文件寫得很清楚：UDAF 只支援 C++，但很多人是看到「Python UDx」就往下寫。
2. **在 `getPrototype` 給 VARCHAR 長度。** `TypeError: addVarchar() takes exactly 0 positional arguments (1 given)`。長度只在 `getReturnType` 給。
3. **多欄輸出忘記欄位索引。** `setString(tag)` 在 Scalar 可以，在 Transform 會回 `setString() takes exactly 2 positional arguments`。
4. **自己重寫 UDTF 的迴圈。** 就是第五節那個：800,087 → 800,064，零錯誤。這種 bug 不會被測試抓到，除非你拿 SQL 版本對總數。
5. **改了 `.py` 卻沒重建 library。** `CREATE LIBRARY` 已把檔案複製進 catalog，改檔案不會生效。開發時請固定用 `CREATE OR REPLACE LIBRARY`。
6. **把 UDx 裝在 `public`。** 全庫可見、遲早衝突。這次所有物件都在 `article_lab_udx`，結束時一個 `DROP SCHEMA ... CASCADE` 全部帶走。
7. **以為 C++ UDAF 是安全的。** 它預設 unfenced（本機 `is_fenced = f`），一個越界存取就能把整個 database 帶下線。要上生產請明確設定 fenced，並接受效能損失。

## 九、風險與建議

**現在就能做（唯讀、低風險）**

- 先用 SQL 寫一版，量 20 萬列以上的真實耗時，再決定要不要 UDx。這次的結果是：只有「逐列字串處理」與「SQL 很難表達的展開」值得寫 UDx。
- 把 UDx 裝在專屬 schema，函式名稱帶前綴（`lab_`），並在 `v_catalog.user_functions` 留下可查的 `function_definition`。
- 用 `EXPLAIN` 確認 UDx 沒有讓計畫變形；用總列數與 checksum 對照 SQL 版本，不要只看前幾列。

**需要變更（要窗口、要核准）**

- 節點上的 Python 套件：`import pandas` 會回 `ModuleNotFoundError: No module named 'pandas'`。要用第三方套件就得在**每個節點**的 Vertica Python 環境安裝，並在 `CREATE LIBRARY ... DEPENDS` 裡宣告。
- 升級：C++ UDx 的 `.so` 綁 SDK ABI，每次大版本升級都要重編、重測；Python UDx 也要確認新版本的 SDK 類別沒有增刪。
- GCC 版本：SDK 對 GCC 14 以上標示未測試，正式環境請用與 Vertica 建置相符的編譯器。

## 十、清理

```sql
=> DROP SCHEMA article_lab_udx CASCADE;
DROP SCHEMA

=> SELECT count(*) FROM v_catalog.user_functions WHERE schema_name = 'article_lab_udx';
 count
-------
     0
```

`DROP SCHEMA ... CASCADE` 會把 library 與函式一起帶走，不需要另外 `DROP LIBRARY`。留在 `/tmp/opencode/udx_lab/` 的 `.py`、`.cpp`、`.so` 與 CSV 是本次實驗素材，不在資料庫內。

## 十一、參考資料

- [Developing user-defined extensions (UDxs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/) — 五種 UDx 型別與可用語言
- [Scalar functions (UDSFs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/scalar-functions-udsfs/) — `ScalarFunction` 與 `ScalarFunctionFactory`
- [Transform functions (UDTFs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/transform-functions/) — `processPartition` 與一對多輸出
- [Aggregate functions (UDAFs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/aggregate-functions/) — 只支援 C++、單一輸入欄位
- [UDx restrictions](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/udx-restrictions/) — aggregate 與 transform 的使用限制
- [Python UDx SDK documentation](https://docs.vertica.com/25.4.x/sdkdocs/PythonSDK/sdk_documentation.html) — Python SDK 類別清單
