"""
数据库模块
负责数据库连接、SQL 执行和结果获取。
将数据库操作封装为独立模块，便于后续扩展（如连接池、读写分离等）。
职责：
1. 封装MySQL底层pymysql操作
2. 在SQL执行前调用安全模块做SQL校验、行级权限改写（security.secure_sql）
3. SQL查询返回后，调用安全模块做敏感字段结果脱敏（security.mask_result）
4. 提供数据库连通性探测接口，给健康检查接口使用
"""
# 支持前向引用注解（类内部引用自身类型）
from __future__ import annotations

# 类型注解：Any任意类型；Callable代表可调用对象（函数/工厂方法）
from typing import Any, Callable
import logging
from queue import Empty, LifoQueue
from time import perf_counter
# MySQL驱动库
import pymysql

# 导入数据库配置字典（config.py，存放host、port、user、password、db等）
from config import DB_CONFIG, DB_RUNTIME_CONFIG
# 导入安全校验管理器、用户权限上下文（security.py）
from security import QuerySecurityManager, UserContext


logger = logging.getLogger("chatbi.database")

class QueryExecutionError(RuntimeError):
    """数据库执行失败后的统一异常。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.metadata = metadata or {}


class DatabaseConnectionPool:
    """轻量连接池，优先复用空闲连接，避免每次查询重新建连。"""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        pool_size: int,
        max_overflow: int = 0,
        pool_timeout: float = 3.0,
    ):
        self.connection_factory = connection_factory
        self.pool_size = max(pool_size, 1)
        self.max_overflow = max(max_overflow, 0)
        self.pool_timeout = pool_timeout
        self._idle_connections: LifoQueue[Any] = LifoQueue(maxsize=self.pool_size)
        self._total_connections = 0

    def acquire(self) -> Any:
        try:
            conn = self._idle_connections.get_nowait()
        except Empty:
            if self._total_connections < self.pool_size + self.max_overflow:
                conn = self.connection_factory()
                self._total_connections += 1
                return conn
            conn = self._idle_connections.get(timeout=self.pool_timeout)

        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            self._discard_connection(conn)
            conn = self.connection_factory()
            self._total_connections += 1
            return conn

    def release(self, conn: Any) -> None:
        try:
            self._idle_connections.put_nowait(conn)
        except Exception:
            self._discard_connection(conn)

    def close_all(self) -> None:
        while True:
            try:
                conn = self._idle_connections.get_nowait()
            except Empty:
                break
            self._discard_connection(conn)

    def _discard_connection(self, conn: Any) -> None:
        try:
            conn.close()
        finally:
            self._total_connections = max(self._total_connections - 1, 0)


class DatabaseClient:
    """MySQL 数据库客户端，数据库访问层，所有SQL查询统一入口
    设计思路：依赖注入，连接工厂、安全管理器都支持外部传入，方便单元测试mock
    """
    def __init__(
        self,
        # 连接工厂函数：无参，返回数据库连接对象；可选参数，用于依赖注入
        connection_factory: Callable[[], Any] | None = None,
        # 安全管理器实例，可选，用于依赖注入（单元测试可以传入mock对象）
        security_manager: QuerySecurityManager | None = None,
        slow_query_threshold_ms: float | None = None,
        time_fn: Callable[[], float] | None = None,
        connection_pool: DatabaseConnectionPool | None = None,
    ):
        # 加载数据库配置
        self.config = DB_CONFIG
        
        # 111安全管理器：外部传入则使用外部实例，否则新建QuerySecurityManager
        self.security = security_manager or QuerySecurityManager()

        self.time_fn = time_fn or perf_counter
        self.slow_query_threshold_ms = (
            slow_query_threshold_ms
            if slow_query_threshold_ms is not None
            else DB_RUNTIME_CONFIG["slow_query_threshold_ms"]
        )
        self.connection_factory = connection_factory
        self.connection_pool = connection_pool
        self.last_query_info: dict[str, Any] = {}

        if self.connection_factory is None and self.connection_pool is None:
            self.connection_pool = DatabaseConnectionPool(
                connection_factory=lambda: pymysql.connect(**self.config),
                pool_size=DB_RUNTIME_CONFIG["pool_size"],
                max_overflow=DB_RUNTIME_CONFIG["max_overflow"],
                pool_timeout=DB_RUNTIME_CONFIG["pool_timeout"],
            )


    def execute(
        self,
        sql: str,
        user: UserContext | None = None,
    ) -> tuple[list[str], list[tuple]]:
        """
        执行 SQL 并返回结果，**整个ChatBI项目数据库查询统一入口**
        完整链路：原始SQL → security校验+改写SQL → 数据库执行 → 查询结果脱敏 → 返回
        Args:
            sql: LLM生成的原始待执行 SQL 语句
            user: 用户权限上下文UserContext，携带角色、区域信息，用于权限控制
        Returns:
            tuple[list[str], list[tuple]]: (列名列表, 脱敏后的结果行列表)
        """
        # 如果没有传入用户上下文，默认使用demo_admin管理员上下文
        user_context = user or UserContext.demo_admin()

        # ==========【安全前置处理】调用security模块：校验SQL+追加行级权限过滤条件 ==========
        # 1. 拦截危险SQL(DDL/DML)
        # 2. 根据用户角色，自动追加WHERE行级过滤条件（sales角色会追加region区域过滤）
        secured_sql = self.security.secure_sql(sql, user_context)
        started_at = self.time_fn()
        conn = None

        try:
            conn = self._acquire_connection()
            # with上下文管理游标，自动关闭游标，避免游标泄露
            with conn.cursor() as cursor:
                # 执行经过安全改写后的SQL
                cursor.execute(secured_sql)
                # cursor.description 会返回每一列的元信息，第一个元素是字段名
                # 如果查询没有返回结果（如无返回的语句），description为None，返回空列表
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                # 取出全部查询结果，每行是元组tuple
                results = cursor.fetchall()

                duration_ms = round((self.time_fn() - started_at) * 1000, 2)
                explain_plan = self._explain(cursor, secured_sql, duration_ms)
                self.last_query_info = {
                    "sql": secured_sql,
                    "duration_ms": duration_ms,
                    "slow_query": bool(explain_plan),
                    "explain_plan": explain_plan,
                }
                if explain_plan:
                    logger.warning(
                        "Slow query detected: duration_ms=%s sql=%s",
                        duration_ms,
                        secured_sql,
                    )
                # ==========【后置脱敏处理】对查询返回的数据做敏感字段掩码 ==========
                # mask_result：根据角色，把手机号、金额等敏感值替换为***
                # 返回(字段列表, 脱敏后的行集合)，这里丢弃第一个返回值columns，直接复用上面的columns
                _, masked_rows = self.security.mask_result(columns, results, user_context)
                # 返回字段名 + 脱敏完成的数据
                return columns, masked_rows
        except (pymysql.MySQLError, OSError) as exc:
            duration_ms = round((self.time_fn() - started_at) * 1000, 2)
            self.last_query_info = {
                "sql": secured_sql,
                "duration_ms": duration_ms,
                "slow_query": False,
                "explain_plan": [],
            }
            raise self._translate_db_error(exc, secured_sql, duration_ms) from exc
        
        finally:
            if conn is not None:
                self._release_connection(conn)

    def validate_connection(self) -> bool:
        """验证数据库连接是否正常，给/api/health健康检查接口调用
        Returns:
            bool: True数据库连通正常；False连接失败
        """
        try:
            conn = self._acquire_connection()
            self._release_connection(conn)
            return True
        except Exception:
            # 任何异常（地址错、账号密码错、数据库离线）直接返回False，不抛出异常
            return False
        
    def _acquire_connection(self) -> Any:
        if self.connection_pool is not None:
            return self.connection_pool.acquire()
        if self.connection_factory is None:
            raise RuntimeError("数据库连接工厂未初始化")
        return self.connection_factory()

    def _release_connection(self, conn: Any) -> None:
        if self.connection_pool is not None:
            self.connection_pool.release(conn)
        else:
            conn.close()

    def _explain(self, cursor: Any, sql: str, duration_ms: float) -> list[dict[str, Any]]:
        if duration_ms < self.slow_query_threshold_ms:
            return []
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            return []
        try:
            cursor.execute(f"EXPLAIN {sql}")
            explain_columns = [desc[0] for desc in cursor.description] if cursor.description else []
            explain_rows = cursor.fetchall()
            return [
                dict(zip(explain_columns, row))
                for row in explain_rows
            ]
        except Exception as exc:
            logger.warning("Failed to capture EXPLAIN plan: %s", exc)
            return []

    def _translate_db_error(
        self,
        exc: Exception,
        sql: str,
        duration_ms: float,
    ) -> QueryExecutionError:
        error_code = exc.args[0] if getattr(exc, "args", None) else None
        error_text = str(exc)
        metadata = {
            "sql": sql,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "raw_error": error_text,
        }

        if isinstance(exc, pymysql.err.ProgrammingError) or error_code == 1064:
            return QueryExecutionError(
                "sql_syntax",
                "SQL 语法错误，请检查字段、聚合和别名是否正确",
                metadata,
            )
        if error_code in {1044, 1045, 1142, 1143, 1227}:
            return QueryExecutionError(
                "permission_denied",
                "数据库拒绝执行该查询，请检查当前账号权限",
                metadata,
            )
        if error_code in {1205, 3024} or (
            error_code in {2013}
            and "timed out" in error_text.lower()
        ):
            return QueryExecutionError(
                "query_timeout",
                "SQL 执行超时，请缩小时间范围或减少返回列后重试",
                metadata,
            )
        if isinstance(exc, OSError) or error_code in {2003, 2006, 2013}:
            return QueryExecutionError(
                "connection_error",
                "数据库连接失败，请检查连接池和数据库状态",
                metadata,
            )
        return QueryExecutionError(
            "execution_error",
            f"SQL 执行失败：{error_text}",
            metadata,
        )
