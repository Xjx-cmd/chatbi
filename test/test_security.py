# 导入路径工具、系统模块
from pathlib import Path
import sys

# pytest 单元测试框架
import pytest

# 将项目根目录加入Python搜索路径，这样才能import同项目下的自定义模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 业务模块导入
from main import ChatBISystem          # ChatBI顶层主类，入口
from database import DatabaseClient    # 数据库客户端，封装连接、执行SQL
from security import QuerySecurityManager, SecurityError, UserContext
# QuerySecurityManager：SQL安全管理器（本次核心被测对象）
# SecurityError：安全校验失败自定义异常
# UserContext：用户上下文，保存用户ID、角色、所属区域等权限信息

'''文件定位：ChatBI 项目单元测试文件，**专门测试 SQL 安全管控模块（QuerySecurityManager）**
核心业务背景：ChatBI 是自然语言转 SQL 系统，最大风险是 LLM 生成危险 SQL（DELETE/DROP）、越权查数据；这套安全模块做三件事：

1. 禁止非 SELECT 语句执行
2. 行级权限：根据用户角色自动追加`region`数据过滤条件
3. 列脱敏：敏感手机号 / 金额自动打`***`掩码
同时测试 DatabaseClient 数据库客户端、ChatBISystem 顶层入口串联逻辑。'''

"""
# 整体架构剖析

## 1. 模块职责分层（从上到下）

1. **ChatBISystem (main.py)**：最外层入口
   - 接收自然语言问题
   - 调用 LLM 生成 SQL
   - 调用 Parser 校验
   - 调用 DatabaseClient 执行
   - 捕获异常，包装成统一返回体（区分 error_type）
2. **DatabaseClient (database.py)**
   - 获取数据库连接
   - 【关键】执行 SQL 前调用`QuerySecurityManager.secure_sql()`做 SQL 改写、权限注入
   - 执行改写后的 SQL
   - 查询拿到结果后调用`mask_result()`做字段脱敏
   - 关闭连接
3. **QuerySecurityManager (security.py)【核心安全层】**
   - 语法白名单校验：只允许 SELECT，拦截 DML/DDL
   - 行级权限：根据 user.role，自动拼接 WHERE 区域过滤条件
   - 列级脱敏：根据角色，把敏感字段替换`***`
4. **UserContext**：权限载体，保存用户 id、角色、所属大区，贯穿整个链路

## 2. 单元测试设计思路

✅ **全部使用 Fake 替身，无真实数据库依赖**

- FakeConnection / FakeCursor：不访问 MySQL，只捕获 SQL 字符串，校验 SQL 是否被正确改写
- FakeLLM、FakeParser、FakeDB：顶层用例直接替换组件，只测异常流转逻辑

✅ **测试分层**

1. 单独测安全管理器：SQL 拦截、行权限注入、结果脱敏（3 个单测）
2. 测数据库客户端：安全模块和数据库执行流程串联（集成小用例）
3. 测顶层 ChatBI 入口：安全异常的封装、返回格式（端到端轻量用例）

✅ **测试覆盖点**

- 负面用例：危险 SQL 拦截
- 正面用例：行权限自动追加条件
- 正面用例：返回结果脱敏
- 流程用例：DB 客户端完整调用链路
- 异常链路：安全异常向上传递后的统一返回结构

## 3. 潜在可优化点（顺带分析）

1. `secured_sql.upper().count("WHERE") ==1` 这个判断有坑：原 SQL 已有 WHERE 时，应该用`AND`拼接条件，而不是直接加 WHERE；当前代码只处理原始 SQL 无 WHERE 场景。
2. SQL 字符串拼接容易注入风险，更好方案：使用 SQL AST 语法树修改，而不是字符串查找替换。
3. 脱敏规则硬编码，可抽成配置文件，配置哪些角色哪些字段需要掩码。
4. 缺少 admin 角色的脱敏、权限测试用例（admin 应该不追加 region 过滤、不脱敏）。

## 4. 执行命令 # 运行这个安全模块单测
pytest test_security.py -v

"""

# ---------------------------
# 测试替身（Fake模拟类），不连真实数据库！单元测试核心思想
# 单元测试原则：只测业务逻辑，不依赖外部数据库服务
# FakeCursor：模拟数据库游标，捕获最终执行的SQL，不真实跑数据库
# ---------------------------
class FakeCursor:
    def __init__(self):
        # 模拟查询返回字段名，对应数据库返回列
        self.description = [
            ("customer_name",),
            ("customer_phone",),
            ("net_amount",),
        ]
        self.executed_sql = "" # 保存最终执行的SQL，用于断言校验

    def __enter__(self):
        # 支持with上下文写法：with cursor as cur:
        return self

    def __exit__(self, exc_type, exc, tb):
        # with退出时回调，False代表不吞异常
        return False

    def execute(self, sql: str):
        # 模拟执行SQL，不访问真实DB，只把SQL存起来
        self.executed_sql = sql

    def fetchall(self):
        # 模拟数据库返回结果集：一行客户数据
        return [("宁德时代", "13800001111", 1250000)]

# FakeConnection：模拟数据库连接对象
class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor() # 内置模拟游标
        self.closed = False # 标记连接是否关闭

    def cursor(self):
        # 返回模拟游标
        return self.cursor_instance

    def close(self):
        # 标记连接关闭
        self.closed = True

# ======================================
# 测试用例1：test_security_manager_rejects_non_select_sql
# 测试目标：安全管理器拦截DELETE/UPDATE/DROP这类修改类SQL
# ======================================
def test_security_manager_rejects_non_select_sql():
    manager = QuerySecurityManager()
    # 构造管理员用户上下文
    user = UserContext(user_id="u_admin", role="admin")

    # pytest断言：预期会抛出SecurityError，异常信息匹配"只允许执行查询语句"
    with pytest.raises(SecurityError, match="只允许执行查询语句"):
        # 传入DELETE语句，安全校验必须抛出异常
        manager.secure_sql("DELETE FROM sales_orders", user)

# ======================================
# 测试用例2：test_security_manager_injects_region_filter_for_sales_role
# 测试目标：销售角色【行级权限】自动追加区域过滤条件
# 业务规则：销售只能看自己所属大区的数据，不能看全国数据
# 原始SQL：GROUP BY c.region
# 安全处理后自动追加 WHERE c.region = '华东大区'
# ======================================
def test_security_manager_injects_region_filter_for_sales_role():
    manager = QuerySecurityManager()
    # 销售账号：华东大区销售
    user = UserContext(user_id="u_sales_east", role="sales", region="华东大区")

    # 原始LLM生成的SQL
    secured_sql = manager.secure_sql(
        """
        SELECT c.region, SUM(o.net_amount) AS revenue
        FROM sales_orders o
        JOIN dim_customers c ON o.customer_id = c.customer_id
        GROUP BY c.region
        """,
        user,
    )

    # 断言1：自动注入的区域条件存在于处理后的SQL
    assert "c.region = '华东大区'" in secured_sql
    # 断言2：WHERE只出现一次，防止多次拼接WHERE造成语法错误（BUG防护）
    assert secured_sql.upper().count("WHERE") == 1

# ======================================
# 测试用例3：test_security_manager_masks_sensitive_columns_for_sales_role
# 测试目标：销售角色【列脱敏】，返回结果敏感字段掩码
# 业务规则：销售查询结果里手机号、金额不能明文，替换为***
# ======================================
def test_security_manager_masks_sensitive_columns_for_sales_role():
    manager = QuerySecurityManager()
    user = UserContext(user_id="u_sales_east", role="sales", region="华东大区")

    # 调用脱敏方法：传入列名、原始行数据、用户上下文
    masked_columns, masked_rows = manager.mask_result(
        columns=["customer_name", "customer_phone", "net_amount"],
        rows=[("宁德时代", "13800001111", 1250000)],
        user=user,
    )

    # 断言：字段名称不变；手机号、金额被替换为***，客户名称保留
    assert masked_columns == ["customer_name", "customer_phone", "net_amount"]
    assert masked_rows == [("宁德时代", "***", "***")]

# ======================================
# 测试用例4：test_database_client_applies_security_before_query_execution
# 测试目标：DatabaseClient 数据库客户端完整串联流程
# 流程：接收原始SQL -> QuerySecurityManager安全校验+自动追加行权限 -> 执行SQL -> 查询结果自动脱敏
# 职责划分：DatabaseClient是粘合剂，把安全模块和数据库操作绑定在一起
# ======================================
def test_database_client_applies_security_before_query_execution():
    fake_connection = FakeConnection()
    # 构造DB客户端，传入工厂函数，返回模拟连接（不连真实数据库）
    client = DatabaseClient(connection_factory=lambda: fake_connection)
    user = UserContext(user_id="u_sales_east", role="sales", region="华东大区")

    # 执行查询，内部自动调用安全模块
    columns, rows = client.execute(
        """
        SELECT c.customer_name, c.customer_phone, o.net_amount
        FROM sales_orders o
        JOIN dim_customers c ON o.customer_id = c.customer_id
        """,
        user=user,
    )

    # 校验：SQL已经自动追加华东大区过滤条件
    assert "c.region = '华东大区'" in fake_connection.cursor_instance.executed_sql
    # 校验返回字段
    assert columns == ["customer_name", "customer_phone", "net_amount"]
    # 校验结果已经脱敏
    assert rows == [("宁德时代", "***", "***")]
    # 校验：数据库连接执行完毕后会自动关闭
    assert fake_connection.closed is True

# ======================================
# 测试用例5：test_chatbi_system_returns_security_error_type
# 测试目标：顶层 ChatBISystem 入口，安全异常统一封装返回结果
# 关注点：安全异常不会直接抛出给上层，而是包装成结构化返回，区分error_type="security"
# 方便前端展示对应提示，区分语法错误、LLM错误、权限错误
# ======================================
def test_chatbi_system_returns_security_error_type():
    # FakeParser：模拟SQL解析器，直接返回输入SQL，不做真实解析
    class FakeParser:
        def parse(self, question: str) -> str:
            return question

        def validate(self, parsed: str) -> bool:
            return True

    # FakeLLM：模拟大模型，固定生成SQL
    class FakeLLM:
        def generate_sql(self, _system_msg: str, _prompt: str) -> str:
            return "SELECT * FROM sales_orders"

    # FakeDB：模拟数据库客户端，直接抛出SecurityError权限异常
    class FakeDB:
        def execute(self, sql: str, user=None):
            raise SecurityError(f"权限不足：{user.role}")

    # 实例化ChatBI主系统，替换内部组件为模拟对象
    system = ChatBISystem()
    system.parser = FakeParser()
    system.llm = FakeLLM()
    system.db = FakeDB()

    # 调用顶层run入口：自然语言问题 + 用户权限上下文
    result = system.run(
        "查看订单明细",
        security_context=UserContext(user_id="u_sales_east", role="sales", region="华东大区"),
    )

    # 断言：返回结果是统一JSON结构
    assert result["success"] is False
    assert result["error_type"] == "security" # 错误类型标记为安全权限类
    assert "sales" in result["error"]

