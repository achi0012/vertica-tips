---
layout: post_en
title: "Python UDx in Practice: Scalar, Transform, and Aggregate Functions"
date: 2026-11-26 05:00:00 +0800
categories: vertica udx
tags: [vertica, udx, python, scalar-function, transform-function, aggregate-function, c++]
description: "Vertica 25.4's Python SDK ships Scalar and Transform but no AggregateFunction: subclassing vertica_sdk.AggregateFunction fails with AttributeError. This article runs all three function types over 200,000 rows. A Python scalar function beat native SQL by 23.6% (220 ms vs 288 ms) and a transform tied it (191 ms vs 206 ms), while a single-row call was 5.3x slower. Aggregates require C++ and accept exactly one input column, so a two-column weighted average is rejected with ERROR 2136."
image: /assets/images/og/vertica-python-udx-development-en.png
faq:
  - q: "Can you write an aggregate function in Python for Vertica?"
    a: "No. The 25.4 Python SDK exports ScalarFunction, TransformFunction, UDFilter, and UDParser, but no AggregateFunction. Subclassing vertica_sdk.AggregateFunction fails at CREATE LIBRARY time with AttributeError: module 'vertica_sdk' has no attribute 'AggregateFunction'. UDAFs are C++ only."
  - q: "Is a Python UDx slower than native SQL?"
    a: "It depends on the shape. Over 200,000 rows a per-row string scalar function took 220 ms versus 288 ms for the SQL equivalent, 23.6% faster. A one-to-many transform took 191 ms against 206 ms, essentially a tie. A single-row call, however, took 15 ms versus 2.8 ms because every call crosses the fenced process boundary, 5.3x slower."
  - q: "Can a UDAF take two input columns?"
    a: "No. A Vertica aggregate function accepts exactly one input column; declaring two FLOAT arguments returns ERROR 2136: Aggregate function cannot have 2 input argument(s). A weighted average has to be expressed as a window function, by encoding both values into one, or as a C++ analytic function."
  - q: "Why does a Python transform function silently drop rows?"
    a: "A common cause is rewriting the official while True / if not input.next(): break loop as while input.next(). With identical logic and the same 200,000 input rows, the official shape produced 800,087 output rows while the rewrite produced 800,064, with no error at all; partitioning by event_id made it return zero rows. Keep the documented loop structure."
---

<!--more-->

---

## 1. The short version

"Write Vertica UDxs in Python" sounds like one thing. On 25.4 it is **two and a half**:

1. **Scalar (UDSF) works, and it was the fastest of the three here.** Masking 200,000 email addresses in Python took 220 ms; the native `substr` plus `position` equivalent took 288 ms, so the UDx was **23.6% faster**. That is the opposite of the usual "Python is slow" intuition, and the reason is worth understanding.
2. **Transform (UDTF) works, and performance is a wash.** Expanding 800,000 tags took 191 ms with the UDx and 206 ms with `split_part` plus a numbers cross join.
3. **Aggregate (UDAF) does not work, not in Python anyway.** The 25.4 `vertica_sdk` has no `AggregateFunction` class at all; subclassing it raises `AttributeError`. UDAFs are C++ only, and they **accept exactly one input column**: my weighted average over two DOUBLE columns was rejected at `CREATE AGGREGATE FUNCTION` with `ERROR 2136`.

Two findings matter more than the timings:

- **Single-row calls are 5.3x slower.** The same masking logic took 15 ms through a UDx and 2.8 ms in SQL. Python UDxs always run in a fenced process, so every call pays a cross-process cost. Scanning many rows amortizes it; looking up one row does not.
- **The UDTF loop shape can silently drop rows.** Written the way the official example does it (`while True` with `if not input.next(): break`), the function produced exactly 800,087 rows. Rewritten with identical logic as `while input.next():`, it produced 800,064 rows, **23 rows short, with no warning or error**. Same data, same function, only the loop shape differed.

Every number here comes from a real run: Vertica v25.4.0-0, a single-node testdb, and a 200,000-row table built for the test. All objects lived in the `article_lab_udx` schema and were dropped with `DROP SCHEMA ... CASCADE` afterwards.

## 2. Test environment

| Item | Value |
|---|---|
| Version | Vertica Analytic Database v25.4.0-0 (Community Edition) |
| Node | Single node `v_testdb_node0001`, state UP |
| Database | `testdb` |
| Test data | `lab_events`, 200,000 rows, 3–5 tags each, 800,087 tags total |
| Bundled Python | `/opt/vertica/oss/python3/bin/python3` → Python 3.13.1 |
| C++ compiler | g++ 15.2.0 (SDK headers under `/opt/vertica/sdk/include`) |
| Python UDx execution | fenced (`v_catalog.user_functions.is_fenced = t`) |
| C++ UDAF execution | unfenced (`is_fenced = f`) |
| Test schema | `article_lab_udx` (dropped) |

Check the version and the nodes first; the UDx API and the Python runtime follow the server, not your laptop.

```sql
=> SELECT version();
 Vertica Analytic Database v25.4.0-0

=> SELECT node_name, is_primary, node_state FROM v_catalog.nodes;
     node_name     | is_primary | node_state
-------------------+------------+------------
 v_testdb_node0001 | t          | UP
```

The Python version on the node can be asked directly:

```bash
$ /opt/vertica/oss/python3/bin/python3 --version
Python 3.13.1
```

## 3. The three shapes, side by side

| | Scalar (UDSF) | Transform (UDTF) | Aggregate (UDAF) |
|---|---|---|---|
| Input to output | One row in, one value out | One partition in, zero or more rows out | One column in, one value out |
| Call site | `SELECT f(col)` | `SELECT f(col) OVER (PARTITION ...)` | `SELECT f(col)`, same place as `sum()` |
| Languages in 25.4 | C++, Python, Java, R | C++, Python, Java, R | **C++ only** |
| Measured here | Works | Works (with a trap) | `AttributeError`, class does not exist |
| Execution | fenced | fenced | C++ defaults to unfenced |

"Fenced" is not an adjective, it is a safety boundary. Python UDxs run in a separate process outside the database, so a crash fails one query. C++ UDxs run inside the server by default, so one segfault takes the database down.

What the Python SDK actually ships can be printed from inside a UDx. This is the list I got back on the node (abridged):

```
ArrayReader, ArrayWriter, BlockReader, BlockWriter, ColumnTypes, FilterFactory,
InputBuffer, OutputBuffer, ParamReader, ParamWriter, PartitionReader,
PartitionWriter, ParserFactory, ScalarFunction, ScalarFunctionFactory,
ServerInterface, TransformFunction, TransformFunctionFactory, UDFilter,
UDParser, UDXFactory, UDXObject, VerticaType, Volatility, ...
```

No `AggregateFunction`, no `AnalyticFunction`. That is why step three below has to change languages.

![The three UDx shapes and their language support: Scalar and Transform work in Python and run fenced, while Aggregate is C++ only and unfenced by default; over 200,000 rows Scalar took 220 ms against 288 ms in SQL]({{ '/assets/images/udx-three-function-types.svg' | relative_url }}){: loading="lazy" }

## 4. Step 1: a scalar function that masks email addresses

The requirement is mundane: a report for non-engineers must hide the local part of every email address. SQL can do it, but only by stacking three functions; a UDx does it in one pass.

### 4.1 The code

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

Three things deserve a pause:

- **`getPrototype` and `getReturnType` receive different type objects.** `getPrototype` gets a `ColumnTypes`, whose `addVarchar()` **takes no length**; the length belongs in `getReturnType`, which receives a `SizedColumnTypes`. I first wrote `return_type.addVarchar(128)` inside `getPrototype` and `CREATE FUNCTION` failed:

  ```
  TypeError: addVarchar() takes exactly 0 positional arguments (1 given)
  ```

- **`volatility = IMMUTABLE`** lets the optimizer constant-fold and cache results. A function with side effects or a time dependency must not claim this.
- **`RETURN_NULL_ON_NULL_INPUT`** lets the engine return NULL without entering Python, but only when every input is NULL, so the explicit `isNull` check stays for safety.

### 4.2 Install and run

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

`CREATE LIBRARY` does not register a path; it **copies the file into the catalog**. The error messages reveal where the copy lives:

```
/opt/data/testdb/v_testdb_node0001_catalog/Libraries/02b94901.../udx_lab_02b94901....py
```

So after editing the `.py` file you must run `CREATE OR REPLACE LIBRARY` (or `ALTER LIBRARY ... AS`), otherwise the old code keeps running. This is easy to get wrong during development: the file changed, the function did not.

## 5. Step 2: a transform that turns one row into many

`tags` is a comma-separated string; the report wants one row per tag. That is textbook UDTF work.

### 5.1 The code (mind the loop shape)

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

The first argument of `output.setString(0, tag)` is the **output column index**, not the value. A scalar has one column, so `setString(value)` works there; a transform with two columns needs both:

```
TypeError: setString() takes exactly 2 positional arguments (1 given)
```

### 5.2 Install and query

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

`AS (tag, pos)` is not decoration. Without it the UDTF has no column names:

```
ERROR 5384:  User defined transform must provide names or aliases for return columns
```

### 5.3 The trap: rows disappear without an error

My first version used `while input.next():` with identical logic, just "check then read" instead of "read then check". The result:

| Loop shape | Rows out | `sum(length(tag))` |
|---|---|---|
| Official `while True` / `if not input.next(): break` | 800,087 | 4,241,485 |
| `while input.next():` | 800,064 | 4,241,351 |

Twenty-three rows vanished, **with no error, warning, or log entry**. Running the SDK's own `StringTokenizer` over the same data with the same `PARTITION BEST` returned 800,087, so the data was fine. Partitioning by `event_id` (200,000 single-row partitions) made the broken version return **zero rows**, while the official shape still returned 800,087.

Worse, the loss is 0.003%, so spot checks will not catch it. I only saw it by comparing per-tag counts:

| Tag | SQL count | UDx count | Diff |
|---|---|---|---|
| monitoring | 79,877 | 79,872 | 5 |
| eon | 79,872 | 79,868 | 4 |
| backup | 80,158 | 80,155 | 3 |
| python | 80,128 | 80,125 | 3 |
| sql | 79,658 | 79,656 | 2 |
| udx | 79,933 | 79,933 | 0 |

The takeaway is blunt: **write `processPartition` in the documented shape; do not refactor it into something that looks more Pythonic.** Nothing signals the mistake, and downstream reports simply come up short.

## 6. Step 3: aggregates, which means C++

### 6.1 Proving Python cannot do it

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

Note that this fails at `CREATE LIBRARY`, not at execution time: Python raises during import.

### 6.2 C++, and a second limitation

A C++ UDAF implements `initAggregate`, `aggregate`, `combine`, and `terminate`, and declares its intermediate state in `getIntermediateTypes`. I wrote a harmonic mean (`n / Σ(1/x)`):

```cpp
class HarmonicMean : public AggregateFunction
{
public:
    virtual void initAggregate(ServerInterface &srv, IntermediateAggs &aggs)
    {
        aggs.getFloatRef(0) = 0.0;   // sum of 1/x
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

`getFloatRef` is misleadingly named: `vfloat` is `typedef double` in the SDK, not float32. Intermediate indexes must match the order declared in `getIntermediateTypes`; my first attempt used `getIntRef(0)` for the count and the function returned `Infinity`, because it read the bit pattern of a double as an integer. That kind of bug does not raise an error, it just returns a number that looks plausible.

Two things bit me at compile time:

```bash
g++ -I /opt/vertica/sdk/include -shared -fPIC -std=c++11 \
    -D_GLIBCXX_USE_CXX11_ABI=1 \
    -o HarmonicMean.so HarmonicMean.cpp /opt/vertica/sdk/include/Vertica.cpp
```

1. **You must compile `Vertica.cpp` along with your own file.** Compiling only your `.cpp` builds fine but fails at `CREATE LIBRARY` with `undefined symbol: vt_throw_exception`.
2. **The SDK has not been tested with GCC 14 or newer.** On g++ 15.2.0 the build prints `#warning "Vertica builds using GCC v14.0 or above were not tested."` It compiles and runs, but it is someone else's disclaimer, and an upgrade means rebuilding.

### 6.3 Two input columns? Rejected outright

What I originally wanted was a weighted average, `sum(score*weight)/sum(weight)`. With a factory prototype declaring two FLOAT arguments:

```sql
=> CREATE AGGREGATE FUNCTION article_lab_udx.lab_weighted_avg
   AS NAME 'WeightedAvgFactory' LIBRARY article_lab_udx.udx_wavg;
ROLLBACK 2136:  Aggregate function cannot have 2 input argument(s)
```

In other words, **a UDAF's signature is one column in, one value out**. A weighted average belongs in a window function, or in an encoding of both values into one (a ROW will not work either, since aggregates do not accept complex types), or in a C++ analytic function. That is a design-time constraint, not something to discover halfway through.

### 6.4 Comparing results

```sql
=> SELECT lab_harmonic_mean(score) AS udx_hm, ... AS sql_hm
   FROM lab_events WHERE score > 0;
      udx_hm      |      sql_hm
------------------+------------------
 10.0703745450042 | 10.0703745450033
```

The gap is 9×10⁻¹³, caused by floating-point addition order: the UDAF accumulates per block and combines, SQL does one `sum`. For a harmonic mean, which is sensitive to small values, that is fine; for money, use `VNumeric` rather than `vfloat`.

## 7. Performance: "Python is slow" is not the rule

Everything ran over 200,000 rows in one session on the same data; each variant ran three times and the median is reported.

| Work | Native SQL | Python / C++ UDx | Difference |
|---|---|---|---|
| Scalar: mask 200,000 emails | 288.5 ms | **220.4 ms** | UDx 23.6% faster |
| Transform: expand to 800,000 tags | 205.6 ms | **190.8 ms** | UDx 7.2% faster |
| Aggregate: harmonic mean | **6.3 ms** | 31.2 ms | UDx 5.0x slower |
| Single row: mask one string | **2.8 ms** | 15.0 ms | UDx 5.3x slower |

How to read it:

- **Scan-heavy scalar work can be faster in Python.** The SQL version calls `position('@' in email)` and then `substr`, scanning and materializing the column twice. The Python version finds the `@` once and slices. At 200,000 rows, saving one scan more than pays for the interpreter.
- **Transforms tie.** Both sides expand 200,000 rows into 800,000; the difference is noise. The value of a UDTF is expressing something SQL cannot, not speed.
- **Single-row queries pay for startup, not computation.** Most of the 15 ms is the round trip to the fenced process. Scanning 200,000 rows amortizes the same process across every call, which is why the overhead disappears. UDxs are a poor fit for OLTP-style point lookups.
- **The UDAF's 5x is framework overhead.** Native `sum` is built-in C++ and vectorized; my UDAF makes a virtual call and updates intermediate state per block. In absolute terms both are under 0.05 seconds for 200,000 rows.

`EXPLAIN` shows where the UDx lands: an ordinary `ExprEval`, with no special node.

```
5[label = "ExprEval:
  length(article_lab_udx.lab_mask_email(lab_events.user_email))"]
6[label = "ScanStep: lab_events_super
  user_email"]
```

Which means **a UDx does not change the plan shape**. Projections and statistics still matter exactly as much as before.

## 8. Common mistakes and anti-patterns

1. **Assuming Python can write UDAFs.** It fails at `CREATE LIBRARY` with `AttributeError: module 'vertica_sdk' has no attribute 'AggregateFunction'`. The documentation is clear that UDAFs are C++ only, but "Python UDx" invites the assumption.
2. **Passing a length to `addVarchar` in `getPrototype`.** `TypeError: addVarchar() takes exactly 0 positional arguments (1 given)`. Lengths belong in `getReturnType`.
3. **Forgetting the column index on multi-column output.** `setString(tag)` works in a scalar and fails in a transform with `setString() takes exactly 2 positional arguments`.
4. **Rewriting the UDTF loop.** Section 5.3: 800,087 rows became 800,064 with no error. Only a row-count comparison against the SQL version catches it.
5. **Editing the `.py` file without recreating the library.** `CREATE LIBRARY` copied the file into the catalog; use `CREATE OR REPLACE LIBRARY` while developing.
6. **Installing UDxs in `public`.** They become visible database-wide and eventually collide. Everything here lived in `article_lab_udx` and went away with one `DROP SCHEMA ... CASCADE`.
7. **Assuming a C++ UDAF is safe.** It defaults to unfenced (`is_fenced = f` here), so one out-of-bounds access can take the database down. Set fenced explicitly in production and accept the cost.

## 9. Risks and recommendations

**Do now (read-only, low risk)**

- Write the SQL version first and time it on realistic row counts before reaching for a UDx. The result here: only per-row string work and awkward expansions justified one.
- Install UDxs in a dedicated schema with a naming prefix (`lab_`), so `v_catalog.user_functions.function_definition` tells you exactly which library backs which function.
- Confirm with `EXPLAIN` that the plan did not change, and compare total row counts and checksums against the SQL version instead of eyeballing the first few rows.

**Needs change control**

- Python packages on the nodes: `import pandas` returns `ModuleNotFoundError: No module named 'pandas'`. Third-party packages must be installed into Vertica's Python environment on **every node** and declared with `CREATE LIBRARY ... DEPENDS`.
- Upgrades: a C++ `.so` is tied to the SDK ABI and must be rebuilt and retested on every major version; Python UDxs need their SDK classes rechecked.
- Compiler version: the SDK marks GCC 14 and newer as untested, so production builds should use a compiler matching Vertica's own build.

## 10. Cleanup

```sql
=> DROP SCHEMA article_lab_udx CASCADE;
DROP SCHEMA

=> SELECT count(*) FROM v_catalog.user_functions WHERE schema_name = 'article_lab_udx';
 count
-------
     0
```

`DROP SCHEMA ... CASCADE` removes the libraries and functions together; no separate `DROP LIBRARY` is needed. The `.py`, `.cpp`, `.so`, and CSV files left in `/tmp/opencode/udx_lab/` are lab material and were never inside the database.

## 11. References

- [Developing user-defined extensions (UDxs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/) — the five UDx types and their languages
- [Scalar functions (UDSFs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/scalar-functions-udsfs/) — `ScalarFunction` and `ScalarFunctionFactory`
- [Transform functions (UDTFs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/transform-functions/) — `processPartition` and one-to-many output
- [Aggregate functions (UDAFs)](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/aggregate-functions/) — C++ only, single input column
- [UDx restrictions](https://docs.vertica.com/25.4.x/en/extending/developing-udxs/udx-restrictions/) — aggregate and transform limits
- [Python UDx SDK documentation](https://docs.vertica.com/25.4.x/sdkdocs/PythonSDK/sdk_documentation.html) — the Python SDK class list
