# error_analyzer.py 第一性原理拆解

> 保存自会话分析。思维导图：[`docs/03-项目思维导图.md`](03-项目思维导图.md) 第 4 节；交互版 `docs/canvases/chatbi-mind-map.canvas.tsx`。

## 在系统中的位置

仓库内 **没有任何文件 import 本模块**。它是第 7 课「评测 → 分类 → 改 Prompt」闭环的下半截，上半截 `evaluator` 判定失败后没有接过来。

- 设计意图：失败 → 分类 → 改 RULES / Few-shot / ERROR_GUARDS → 再评测
- 代码事实：启发式分类器，输入 `question + sql + 可选 error_msg`，**不看 expected_sql，不看执行结果集**

## 错误的三层真值

| 层 | 判定对象 | 谁能判定 | 本模块 |
|---|---|---|---|
| L1 可执行 | 语法 / 对象是否存在 | 数据库报错 | 有，且最高优先级 |
| L2 结果正确 | 结果集是否等于金标 | `evaluator._results_equivalent` | 没有 |
| L3 口径正确 | 字段、过滤、JOIN、时间是否符合财务定义 | 业务规则 | 用关键词猜 |

ChatBI 真正的对错是 L2。本模块绕开 L2，做的是规范检验：

> 从「问题文本」推出应尽义务，再看「生成 SQL」是否表面上履行。未履行 → 贴标签。

## 方法论：义务模型，不是 token 黑名单

`__init__` 里的 `error_patterns` 是死代码。若按「SQL 里出现某正则就是错」会极性反转：`exchange_rates`、`GROUP BY` 在正确 SQL 里本应出现。

真正干活的是 `_check_*`：

`问题蕴含义务 ∧ SQL 未履行 → True`

分类器必须是 `(问题, SQL)` 二元函数。

## 错误类型 = Prompt 可操作轴

`field / join / time / filter / aggregation / syntax / unknown`

`_get_suggestion` 一律指向改 Prompt（RULES、负例、Few-shot），不建议换模型或做 SQL 后处理。`_get_root_cause` 签名收了 sql/question，函数体不用，根因是标签的同义反复。

## categorize_error 状态机

1. 有 `error_msg` 且像语法 → 立刻 `SYNTAX`（截断）
2. `_detect_error_types` 固定顺序跑 5 个检查，取 `list[0]`
3. 空列表 → `UNKNOWN` + 建议加 Few-shot / Schema 注释

优先级写死：**字段 > JOIN > 时间 > 过滤 > 聚合**。多义务同时破坏时只看见第一种。

语法关键词含 `invalid` / `near` / `expected`，过宽：`Invalid use of group function` 本质是聚合错误，会被收成 SYNTAX。

## 五条检测器

### field

收入 + 有 `gross_amount` 且无 `net_amount`；成本 + 有 `standard_cost` 且无 `material_cost`。

漏：用 `unit_price * quantity`、两字段同时出现、未乘汇率。「含税销售额」仍会命中收入词。

### join

收入且 SQL 已有 `currency` 却无 `exchange_rates`。最常见失败（只 `SUM(net_amount)`）被放过。客户或产品线：两张维表都没有才报，错 JOIN 另一张维表会漏。

### time（实现 bug）

触发词：最近 / 上个 / 本月。查找 `DATE_SUB` / `CURDATE`（大写），但传入的是 `sql.lower()` → **相对时间题几乎恒为 True**。即便修好，`NOW()`、`DATE_FORMAT` 月初写法仍会误判。真义务是「谓词随运行日变化」。

### filter

订单/收入/销售额 且无 `order_status` 标识符。真义务是 `= 'completed'`。`pending` 也会通过。「订单」触发词过宽。

### aggregation

有聚合、无 `GROUP BY`、SELECT 逗号切分后项数 > 1。`SUM(a), SUM(b)` 合法会误报；GROUP BY 列与 SELECT 不一致（真 1055）漏报。`select_part` 死变量。未复用 evaluator 的括号深度解析。

## 与 RULES / ERROR_GUARDS 的镜像扭曲

| 防护层 | 检测器 | 扭曲 |
|---|---|---|
| 收入用 net，禁 gross | 有 gross 且无 net | 同时出现则漏 |
| 成本 = material+labor | 有 standard 且无 material | 漏 labor |
| 收入必须汇率 JOIN | 无汇率表且有 currency | 主失败模式漏掉 |
| 必须 completed | 出现 order_status 即可 | 等号右边不查 |
| 最近 N 月 DATE_SUB | 大写 vs lower | 恒真 |
| GROUP BY ≡ SELECT 非聚合 | 无 GB 且逗号数>1 | 真 1055 漏 |
| 费用父子项 / 毛利 / UNION | 无 checker | 规则集 ⊃ 检测集 |

## 最小充分设计（若要接上）

1. 只分析 evaluator 判定失败的样本
2. 义务来自同一份 `indicators.json`
3. SQL 侧对金标做差分，而不是 token 有无
4. 多标签保留，报告用共现
5. 语法用 MySQL 错误码
6. 时间检查改为对 lower 后的 `date_sub` / `curdate`

## 主线记忆

```
错误类型常量     →  Prompt 可操作的失败轴
error_patterns   →  废弃的「token 即错误」
categorize_error →  L1 短路 + 取第一条 L3
_check_*         →  问句义务 vs SQL 表面履行
root_cause/suggestion →  类型到文案的字典
analyze_batch    →  为分布服务，不校验样本是否真失败
```
