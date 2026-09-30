# 允许使用前向引用注解（dataclass里面引用自身类型）
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

"""
模块定位：ChatBI 的**SQL 安全核心模块**，负责 SQL 危险语句拦截、行级数据权限控制、查询结果列脱敏。
配合前面的 `test_security.py` 单元测试，这个模块就是被测主体。
核心能力清单：

1. 白名单校验：仅允许 SELECT / WITH 开头的查询语句，拦截 DML/DDL、多语句
2. 行级权限：sales 角色自动追加区域过滤条件，只能看自己大区数据
3. 列脱敏：sales/finance 角色返回结果中敏感字段替换为 `***`
4. 角色策略配置：admin /finance/sales 三种角色，各自独立安全策略

# 模块整体架构剖析

## 一、类之间关系

1. **UserContext**：权限载体，保存用户身份信息，作为参数贯穿 `secure_sql` / `mask_result`
2. **SecurityPolicy**：策略模型，**解耦角色和权限规则**，新增角色只需要在这里新增一行配置
3. **QuerySecurityManager**：执行引擎，读取策略，执行 SQL 校验、改写、结果脱敏
4. **SecurityError**：统一异常，database 捕获后交给 ChatBISystem 包装成`error_type="security"`返回前端

## 二、完整调用链路（结合 test_security.py）

```
ChatBISystem.run()
    → DatabaseClient.execute()
        → QuerySecurityManager.secure_sql() 【SQL安全校验+行权限改写】
            → _normalize_sql 清洗SQL
            → _ensure_select_only 拦截危险语句
            → 根据角色策略循环追加行级过滤条件
        → 执行改写后的SQL
        → QuerySecurityManager.mask_result() 【结果脱敏】
        → 返回 columns + masked rows
```

## 三、核心函数逻辑拆解

### 1. secure_sql

> 
> 输入原始 SQL → 输出安全改写 SQL

1. 清洗 SQL，剥离 markdown 标记，去掉末尾分号
2. 校验只能 SELECT/WITH，禁止 DML/DDL、多语句
3. 获取当前角色策略
4. 遍历行级过滤规则：判断 SQL 是否包含目标表 → 构建过滤条件字符串 → 插入到 SQL 合适位置

### 2. mask_result

> 
> 不对 SQL 做修改，**只对查询返回的结果数据做掩码**

1. 获取角色脱敏字段集合
2. 扫描返回字段列表，找到敏感字段下标
3. 遍历每一行，敏感下标值替换为`***`

> 
> ⚠️注意：字段名本身不会隐藏，只是值替换。返回 columns 里依然保留 customer_phone 字段名。

### 3. _append_predicate（最关键的 SQL 改写逻辑）

使用正则定位`GROUP BY / ORDER BY / LIMIT / HAVING`，在它们之前拼接 WHERE 条件。

- 原有 WHERE 存在：`AND`拼接新条件
- 无 WHERE：新增 WHERE 子句

## 四、安全能力优缺点 & 风险点（重点，面试 / 项目复盘可用）

✅ **优点**

1. 策略配置化：新增角色只需要在`role_policies`加 SecurityPolicy，不用改业务逻辑
2. 两层防护：SQL 层行过滤 + 结果层脱敏，双重保障
3. 简单防注入：`_escape_sql_literal`对 region 字符串做单引号转义
4. 支持表别名识别：`_find_table_qualifier`，JOIN 场景也能正确找到表别名拼接条件
5. 支持 CTE（WITH 开头）查询

⚠️ **现存缺陷（重点，对应之前 test_security.py 分析）**

1. **基于字符串正则解析 SQL，不是 AST 语法树解析**
正则很容易被特殊 SQL 写法绕过，复杂嵌套子查询会匹配失败，生产环境高风险。
> 
> 例如子查询内部出现 FROM dim_customers，正则会误匹配，在顶层 SQL 错误追加 region 条件。
2. `_contains_table` 正则匹配简单，子查询、CTE 里的表会误判
3. 行级过滤只支持等值匹配（`=`），不支持多 region（`IN ('华东','华南')`），`UserContext.allowed_region`字段完全没使用，属于预留但未开发
4. `_ensure_select_only` 正则黑名单方式拦截危险关键字，可以通过注释、字符串绕过
5. `_escape_sql_literal` 只是最简单单引号转义，不是参数化查询，只能防御简单注入

## 五、和 test_security.py 的对应关系

表格

| test 用例 | 对应 security.py 函数 |
| --- | --- |
| test_security_manager_rejects_non_select_sql | `_ensure_select_only` |
| test_security_manager_injects_region_filter_for_sales_role | `secure_sql` + `_append_predicate` |
| test_security_manager_masks_sensitive_columns_for_sales_role | `mask_result` |
| test_database_client_applies_security_before_query_execution | secure_sql + mask_result 串联 |
| test_chatbi_system_returns_security_error_type | SecurityError 异常抛出 |
"""

# ---------------------------
# 自定义异常：安全校验失败统一抛出
# ---------------------------
class SecurityError(Exception):
    """权限校验或 SQL安全检查失败。"""
# ---------------------------
# UserContext：用户权限上下文数据类
# 携带当前用户的身份、角色、所属区域，贯穿整个安全校验链路
# slots=True：优化内存，禁止动态新增属性
# ---------------------------
@dataclass(slots=True)
class UserContext:
    """当前请求的最小权限上下文"""

    user_id: str
    role: str = "admin"          # 默认角色admin
    region: str | None = None    # 用户所属大区（销售角色必填）
    allowed_region: list[str] = field(default_factory=list)

    @classmethod
    def demo_admin(cls) -> "UserContext":
        """快速构造一个管理员上下文，用作默认兜底用户"""
        return cls(user_id="demo_admin", role="admin")


# ---------------------------
# SecurityPolicy：安全策略实体
# 每个角色对应一套策略：行级过滤规则、需要脱敏的字段集合
# ---------------------------
@dataclass(slots=True)
class SecurityPolicy:
    # 行级过滤规则列表：(表名, 列名)，代表要给这个表追加该列的权限过滤条件
    row_level_filters: list[tuple[str, str]] = field(default_factory=list)
    # 需要脱敏的字段名集合
    masked_columns: set[str] = field(default_factory=set)


# ---------------------------
# QuerySecurityManager：安全管理器【核心类】
# ---------------------------
class QuerySecurityManager:
    """负责 SQL拦截、行级过滤和结果脱敏"""

    # 正则：匹配危险关键字，忽略大小写
    _DANGEROUS_KEYWORDS = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|REPLACE)\b",
        flags=re.IGNORECASE,
    )
    # 正则：匹配SQL子句边界 GROUP BY / ORDER BY / LIMIT / HAVING
    # 用来定位WHERE插入位置，在这些子句之前拼接权限条件
    _CLAUSE_BOUNDARY = re.compile(
        r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING)\b",
        flags=re.IGNORECASE,
    )

    def __init__(self):
        # 角色 -> 安全策略映射表
        self.role_policies = {
            # admin管理员：无行过滤，无字段脱敏
            "admin": SecurityPolicy(),
            # finance财务：只脱敏手机号、邮箱，不做行级区域过滤
            "finance": SecurityPolicy(
                masked_columns={"customer_phone", "customer_email"},
            ),
            # sales销售：行级过滤dim_customers表region字段；大量金额、联系方式脱敏
            "sales": SecurityPolicy(
                row_level_filters=[("dim_customers", "region")],
                masked_columns={
                    "customer_phone",
                    "customer_email",
                    "gross_amount",
                    "net_amount",
                    "material_cost",
                    "standard_cost",
                    "profit",
                },
            ),
        }

    def secure_sql(self, sql: str, user: UserContext | None = None) -> str:
        """
        对外主入口：对原始SQL做安全处理
        1. SQL标准化清洗
        2. 危险语句拦截校验
        3. 根据角色策略，自动追加行级权限过滤条件
        :param sql: LLM生成原始SQL
        :param user: 用户权限上下文，不传默认admin
        :return: 改写之后安全的SQL
        """
        user_context = user or UserContext.demo_admin()
        normalized_sql = self._normalize_sql(sql)
        # 第一步：校验只允许查询语句
        self._ensure_select_only(normalized_sql)

        # 获取当前角色对应的安全策略
        policy = self._get_policy(user_context.role)
        secured_sql = normalized_sql

        # 遍历当前角色所有行级过滤规则，逐个追加过滤条件
        for table_name, column_name in policy.row_level_filters:
            predicate = self._build_row_level_predicate(
                sql=secured_sql,
                table_name=table_name,
                column_name=column_name,
                user=user_context,
            )
            # predicate不为空，代表需要追加这个权限条件
            if predicate:
                secured_sql = self._append_predicate(secured_sql, predicate)

        return secured_sql

    def mask_result(
        self,
        columns: list[str],
        rows: Iterable[tuple | list],
        user: UserContext | None = None,
    ) -> tuple[list[str], list[tuple]]:
        """
        对外主入口：查询结果集脱敏
        :param columns: 查询返回字段名列表
        :param rows: 原始返回数据行
        :param user: 用户上下文
        :return: (字段名列表, 脱敏后的行数据列表)
        """
        user_context = user or UserContext.demo_admin()
        policy = self._get_policy(user_context.role)

        # 当前角色没有需要脱敏字段，直接原样返回
        if not policy.masked_columns:
            return columns, [tuple(row) for row in rows]

        # 找出所有需要脱敏的字段下标
        sensitive_indexes = {
            index
            for index, column in enumerate(columns)
            if column.lower() in policy.masked_columns
        }
        # 查询结果里没有敏感字段，直接返回原始数据
        if not sensitive_indexes:
            return columns, [tuple(row) for row in rows]

        masked_rows: list[tuple] = []
        for row in rows:
            values = list(row)
            # 把敏感下标位置的值替换成 ***
            for index in sensitive_indexes:
                values[index] = "***"
            masked_rows.append(tuple(values))

        return columns, masked_rows

    def _get_policy(self, role: str) -> SecurityPolicy:
        """根据角色名获取安全策略；找不到角色默认使用admin策略"""
        return self.role_policies.get(role.lower(), self.role_policies["admin"])

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        """
        SQL文本标准化清洗
        1. 去除markdown ```sql 代码块标记（LLM经常带这个）
        2. 去掉末尾分号（防止多语句执行风险）
        """
        normalized = sql.strip()
        # 移除 ```sql 和 ``` 标记
        normalized = re.sub(r"^```sql\s*|```$", "", normalized, flags=re.IGNORECASE)
        normalized = normalized.strip()
        # 如果最后字符是分号，删掉分号
        return normalized[:-1].strip() if normalized.endswith(";") else normalized

    def _ensure_select_only(self, sql: str) -> None:
        """
        SQL安全校验：拦截危险SQL
        校验规则：
        1. SQL不能为空
        2. 禁止多个分号（多语句注入风险）
        3. 禁止DML/DDL危险关键字
        4. 只能 SELECT / WITH 开头（支持CTE表达式）
        不满足直接抛出 SecurityError
        """
        if not sql:
            raise SecurityError("SQL 不能为空。")
        if sql.count(";") > 0:
            raise SecurityError("检测到多语句执行风险。")
        if self._DANGEROUS_KEYWORDS.search(sql):
            raise SecurityError("只允许执行查询语句。")
        if not re.match(r"^\s*(SELECT|WITH)\b", sql, flags=re.IGNORECASE):
            raise SecurityError("只允许执行查询语句。")

    def _build_row_level_predicate(
        self,
        sql: str,
        table_name: str,
        column_name: str,
        user: UserContext,
    ) -> str | None:
        """
        构建行级权限过滤条件字符串
        例：c.region = '华东大区'
        返回None表示不需要追加过滤条件
        """
        # sales角色必须携带region，否则报错
        if user.role.lower() == "sales" and not user.region:
            raise SecurityError("销售角色缺少区域权限信息。")

        value = user.region
        if not value:
            return None

        # 如果当前SQL里没有用到目标表 dim_customers，不需要追加过滤条件
        if not self._contains_table(sql, table_name):
            return None

        # 获取表别名（比如 dim_customers c，则qualifier=c）
        qualifier = self._find_table_qualifier(sql, table_name)
        # 返回过滤表达式，值做单引号转义
        return f"{qualifier}.{column_name} = '{self._escape_sql_literal(value)}'"

    @staticmethod
    def _contains_table(sql: str, table_name: str) -> bool:
        """判断SQL中是否包含目标表（FROM / JOIN后面）"""
        return re.search(
            rf"\b(?:FROM|JOIN)\s+{re.escape(table_name)}\b",
            sql,
            flags=re.IGNORECASE,
        ) is not None

    @staticmethod
    def _find_table_qualifier(sql: str, table_name: str) -> str:
        """
        查找表别名
        示例：FROM dim_customers c → 返回别名 c
             FROM dim_customers → 没有别名，返回表名 dim_customers
        """
        pattern = re.compile(
            rf"\b(?:FROM|JOIN)\s+{re.escape(table_name)}(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?",
            flags=re.IGNORECASE,
        )
        match = pattern.search(sql)
        alias = match.group(1) if match else None
        return alias or table_name

    def _append_predicate(self, sql: str, predicate: str) -> str:
        """
        核心函数：把权限条件 predicate 插入到正确位置
        逻辑：
        1. 找到 GROUP BY / ORDER BY / LIMIT / HAVING 的起始位置，条件插在这前面
        2. 如果原来已经有WHERE，用 AND 拼接；没有WHERE，新增 WHERE
        example1：原始sql select * from dim_customers
                  → select * from dim_customers WHERE c.region='华东大区'
        example2：原始sql select * from dim_customers WHERE id=1
                  → select * from dim_customers WHERE id=1 AND c.region='华东大区'
        """
        boundary = self._CLAUSE_BOUNDARY.search(sql)
        # 如果找到GROUP BY等边界，插入点为边界起始位置；否则插到SQL末尾
        insert_at = boundary.start() if boundary else len(sql)
        prefix = sql[:insert_at].rstrip()
        suffix = sql[insert_at:].lstrip()
        # 判断prefix里面是否已有WHERE
        joiner = " AND " if re.search(r"\bWHERE\b", prefix, flags=re.IGNORECASE) else " WHERE "
        updated = f"{prefix}{joiner}{predicate}"
        if suffix:
            updated = f"{updated} {suffix}"
        return updated

    @staticmethod
    def _escape_sql_literal(value: str) -> str:
        """简单单引号转义：把字符串里的单引号替换成两个单引号，防止SQL注入"""
        return value.replace("'", "''")
