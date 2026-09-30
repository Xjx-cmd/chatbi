# 导入路径工具
from pathlib import Path
import sys

# mysql驱动、pytest测试框架
import pymysql
import pytest

# 将项目根目录加入python模块搜索路径，这样才能import同项目的database、security模块
# __file__是当前测试文件；parents[1]代表向上一级目录（项目根目录）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 导入待测试模块：数据库客户端、连接池、自定义数据库异常
from database import (
    DatabaseClient,
    DatabaseConnectionPool,
    QueryExecutionError,
)
# 导入权限上下文实体，用于传入execute做权限控制
from security import UserContext

# ===================== Mock 模拟游标：模拟数据库游标行为 =====================
class ExplainableCursor:
    """模拟pymysql游标对象
    支持普通SELECT查询 + EXPLAIN慢查询分析，用于测试慢查询自动捕获执行计划逻辑
    """
    def __init__(self):
        # 模拟查询返回的字段定义：region、revenue
        self.description = [("region",), ("revenue",)]
        # 记录所有执行过的SQL语句，方便断言校验
        self.executed_sqls = []
        self._last_sql = ""

    def __enter__(self):
        # 支持with上下文管理器，和真实pymysql cursor行为对齐
        return self

    def __exit__(self, exc_type, exc, tb):
        # 上下文退出，返回False表示不吞异常，异常向上抛出
        return False

    def execute(self, sql: str):
        """模拟cursor.execute()执行SQL"""
        self.executed_sqls.append(sql)
        self._last_sql = sql
        # 如果是EXPLAIN语句，修改游标字段定义为explain输出的字段
        if sql.startswith("EXPLAIN "):
            self.description = [("id",), ("select_type",), ("table",), ("type",), ("key",)]
        else:
            # 普通查询，返回业务字段
            self.description = [("region",), ("revenue",)]

    def fetchall(self):
        """模拟fetchall读取结果集"""
        if self._last_sql.startswith("EXPLAIN "):
            # EXPLAIN的模拟返回结果
            return [(1, "SIMPLE", "sales_orders", "ALL", None)]
        # 普通业务查询模拟返回：华东大区，营收125万
        return [("华东大区", 1250000)]


class SyntaxErrorCursor:
    """Mock游标：模拟SQL语法错误（mysql错误码1064）"""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        # 抛出真实pymysql语法异常，用于测试database模块的异常翻译逻辑
        raise pymysql.err.ProgrammingError(1064, "You have an error in your SQL syntax")

    def fetchall(self):
        return []


class TimedOutCursor:
    """Mock游标：模拟SQL查询超时异常（2013丢失连接/读超时）"""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        raise pymysql.err.OperationalError(
            2013,
            "Lost connection to MySQL server during query (The read operation timed out)",
        )

    def fetchall(self):
        return []


class FakeConnection:
    """模拟MySQL数据库连接对象"""
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        # 返回传入的mock游标
        return self._cursor

    def close(self):
        # 标记连接关闭
        self.closed = True

    def ping(self, reconnect=False):
        # 模拟连接心跳检测，连接池acquire的时候会调用conn.ping()校验连接有效性
        return None

# ===================== 测试用例开始 =====================
def test_database_client_classifies_sql_syntax_errors():
    """测试：数据库模块能够识别SQL语法错误，并包装为error_type=sql_syntax的QueryExecutionError"""
    # 构造DatabaseClient，传入连接工厂，每次返回携带语法错误游标的假连接
    client = DatabaseClient(connection_factory=lambda: FakeConnection(SyntaxErrorCursor()))

    # 预期执行execute会抛出QueryExecutionError
    with pytest.raises(QueryExecutionError) as exc_info:
        client.execute("SELECT broken FROM sales_orders")

    # 断言异常类型是sql_syntax，并且元数据里携带mysql原始错误码1064
    assert exc_info.value.error_type == "sql_syntax"
    assert exc_info.value.metadata["error_code"] == 1064


def test_database_client_classifies_connection_factory_errors():
    """测试：账号密码错误、权限拒绝类异常，会被识别为permission_denied"""
    # 连接工厂在创建连接时直接抛出mysql访问拒绝异常
    client = DatabaseClient(
        connection_factory=lambda: (_ for _ in ()).throw(
            pymysql.err.OperationalError(
                1045,
                "Access denied for user 'chatbi'@'localhost' (using password: YES)",
            )
        )
    )
    with pytest.raises(QueryExecutionError) as exc_info:
        client.execute("SELECT * FROM sales_orders")

    assert exc_info.value.error_type == "permission_denied"
    assert exc_info.value.metadata["error_code"] == 1045


def test_database_client_records_explain_for_slow_queries():
    """测试：慢查询（超过阈值）会自动执行EXPLAIN，保存执行计划到last_query_info"""
    # 模拟时间序列：第一次取10.0，第二次取10.25 → 耗时0.25s = 250ms
    time_points = iter([10.0, 10.25])
    client = DatabaseClient(
        connection_factory=lambda: FakeConnection(ExplainableCursor()),
        slow_query_threshold_ms=100, # 阈值100ms，250ms超过阈值，触发EXPLAIN
        time_fn=lambda: next(time_points), # 替换时间函数，不用真实系统时间
    )

    # 执行查询，传入管理员用户上下文
    columns, rows = client.execute(
        "SELECT region, SUM(net_amount) AS revenue FROM sales_orders GROUP BY region",
        user=UserContext(user_id="u_admin", role="admin"),
    )

    # 断言返回字段和数据正确
    assert columns == ["region", "revenue"]
    assert rows == [("华东大区", 1250000)]
    # 耗时250ms
    assert client.last_query_info["duration_ms"] == 250.0
    # 标记为慢查询
    assert client.last_query_info["slow_query"] is True
    # 验证explain计划捕获到表名 sales_orders
    assert client.last_query_info["explain_plan"][0]["table"] == "sales_orders"


def test_database_client_classifies_read_timeout_as_query_timeout():
    """测试：查询超时异常，被识别为 query_timeout 类型"""
    client = DatabaseClient(connection_factory=lambda: FakeConnection(TimedOutCursor()))

    with pytest.raises(QueryExecutionError) as exc_info:
        client.execute("SELECT SLEEP(2)")

    assert exc_info.value.error_type == "query_timeout"
    assert exc_info.value.metadata["error_code"] == 2013


def test_connection_pool_reuses_idle_connections():
    """测试连接池：释放后的空闲连接可以复用，不会重复新建连接"""
    created = [] # 记录创建出来的连接对象

    def connection_factory():
        # 工厂函数：新建FakeConnection，存入created列表
        conn = FakeConnection(ExplainableCursor())
        created.append(conn)
        return conn

    # 初始化连接池，最大空闲连接数1
    pool = DatabaseConnectionPool(
        connection_factory=connection_factory,
        pool_size=1,
    )

    # 第一次获取连接
    first = pool.acquire()
    # 归还连接到空闲队列
    pool.release(first)
    # 第二次获取连接，应该拿到同一个对象，不新建
    second = pool.acquire()

    # 断言两次拿到同一个连接实例
    assert first is second
    # 只新建了1个连接
    assert len(created) == 1
    pool.release(second)
    pool.close_all()


def test_chatbi_system_returns_granular_database_error_type_for_timeout():
    """端到端集成测试：测试ChatBISystem顶层能否捕获数据库QueryExecutionError，转换为业务结果"""
    from main import ChatBISystem

    # Mock LLM、Parser、Database，隔离外部依赖
    class FakeParser:
        def parse(self, question: str) -> str:
            return question

        def validate(self, parsed: str) -> bool:
            return True

    class FakeLLM:
        def generate_sql(self, _system_msg: str, _prompt: str) -> str:
            return "SELECT * FROM sales_orders"

    class FakeDB:
        # 模拟数据库抛出查询超时异常
        def execute(self, sql: str, user=None):
            raise QueryExecutionError(
                "query_timeout",
                "SQL 执行超时，请缩小时间范围后重试",
                metadata={"duration_ms": 3100.0},
            )

    # 实例化ChatBI主系统，替换内部组件为mock对象
    system = ChatBISystem()
    system.parser = FakeParser()
    system.llm = FakeLLM()
    system.db = FakeDB()

    # 调用顶层run方法，模拟用户提问
    result = system.run("查看最近 12 个月订单明细")

    # 断言返回结果结构：success=false，错误类型转换为业务级database_query_timeout
    assert result["success"] is False
    assert result["error_type"] == "database_query_timeout"
    assert result["metadata"]["db_duration_ms"] == 3100.0


"""
# 整体解析

## 模块定位

这是 **`database.py` 的 pytest 单元测试 + 简易集成测试文件**。
核心思路：**Mock（模拟）数据库连接与游标，不依赖真实 MySQL 实例**，只测试 DatabaseClient、连接池、异常翻译逻辑。

> 
> 分层：
> 单元测试：单独测 DatabaseClient / DatabaseConnectionPool
> 集成测试：模拟 ChatBISystem 调用链路，验证异常向上传递、包装

## 核心设计思路

1. **Fake/Mock 对象**
   - `FakeConnection`：模拟 pymysql 连接
   - `ExplainableCursor`、`SyntaxErrorCursor`、`TimedOutCursor`：模拟不同场景下的游标
   好处：不用启动 mysql，测试速度快，稳定，只验证代码逻辑，不验证数据库本身。
2. **测试覆盖场景清单**

表格

| 测试用例 | 测试目标 |
| --- | --- |
| test_database_client_classifies_sql_syntax_errors | SQL 语法错误 → 识别为`sql_syntax` |
| test_database_client_classifies_connection_factory_errors | 账号权限拒绝 → `permission_denied` |
| test_database_client_records_explain_for_slow_queries | 慢查询自动执行 EXPLAIN，保存执行计划、打 warning 日志 |
| test_database_client_classifies_read_timeout_as_query_timeout | SQL 超时识别为`query_timeout` |
| test_connection_pool_reuses_idle_connections | 连接池空闲连接复用逻辑 |
| test_chatbi_system_returns_granular_database_error_type_for_timeout | 集成测试：底层数据库异常向上传递，ChatBISystem 包装成业务返回结果 |
3. **依赖注入的价值在这里体现**`DatabaseClient`支持传入`connection_factory`，所以单元测试可以注入 FakeConnection，替换真实 pymysql 连接。这就是为什么前面 database.py 设计成工厂注入模式，**专门为单元测试做准备**。

## 关键知识点

1. `(_ for _ in ()).throw(...)`：Python 小技巧，在 lambda 里主动抛出异常，用来模拟工厂创建连接时报错。
2. `pytest.raises(异常类)`：捕获预期抛出的异常，校验异常类型和内部 metadata。
3. 最后一个用例是**集成测试**，不再只测 database 模块；模拟完整链路：`ChatBISystem.run()` → 调用 db.execute → 捕获 QueryExecutionError，转成对外输出的 error_type。验证各模块之间交互是否正常。

## 面试可讲的亮点

- 测试分层：单元测试测数据库客户端、连接池；少量集成测试验证跨模块异常透传
- 完全解耦真实数据库，CI 流水线可以直接跑，不需要数据库容器
- 覆盖异常分支：语法错、权限错、超时、连接失败（这些是生产高频故障）
- 覆盖业务特性：慢查询自动 EXPLAIN 采集执行计划

"""