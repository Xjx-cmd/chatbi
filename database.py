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

# MySQL驱动库
import pymysql

# 导入数据库配置字典（config.py，存放host、port、user、password、db等）
from config import DB_CONFIG
# 导入安全校验管理器、用户权限上下文（security.py）
from security import QuerySecurityManager, UserContext


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
    ):
        # 加载数据库配置
        self.config = DB_CONFIG
        """
        连接工厂：
        如果外部传入connection_factory，就使用外部的；
        否则默认使用lambda，调用pymysql.connect，基于DB_CONFIG创建连接
        好处：单元测试时可以替换工厂，返回mock连接，不用真实连数据库
        """
        self.connection_factory = connection_factory or (
            lambda: pymysql.connect(**self.config)
        )
        # 安全管理器：外部传入则使用外部实例，否则新建QuerySecurityManager
        self.security = security_manager or QuerySecurityManager()

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

        # 通过连接工厂获取数据库连接
        conn = self.connection_factory()
        try:
            # with上下文管理游标，自动关闭游标，避免游标泄露
            with conn.cursor() as cursor:
                # 执行经过安全改写后的SQL
                cursor.execute(secured_sql)
                # cursor.description 会返回每一列的元信息，第一个元素是字段名
                # 如果查询没有返回结果（如无返回的语句），description为None，返回空列表
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                # 取出全部查询结果，每行是元组tuple
                results = cursor.fetchall()
                # ==========【后置脱敏处理】对查询返回的数据做敏感字段掩码 ==========
                # mask_result：根据角色，把手机号、金额等敏感值替换为***
                # 返回(字段列表, 脱敏后的行集合)，这里丢弃第一个返回值columns，直接复用上面的columns
                _, masked_rows = self.security.mask_result(columns, results, user_context)
                # 返回字段名 + 脱敏完成的数据
                return columns, masked_rows
        finally:
            # finally块：无论正常返回还是抛出异常，一定会执行，关闭数据库连接
            conn.close()

    def validate_connection(self) -> bool:
        """验证数据库连接是否正常，给/api/health健康检查接口调用
        Returns:
            bool: True数据库连通正常；False连接失败
        """
        try:
            # 使用配置创建数据库连接
            conn = pymysql.connect(**self.config)
            # 连接成功立刻关闭
            conn.close()
            return True
        except Exception:
            # 任何异常（地址错、账号密码错、数据库离线）直接返回False，不抛出异常
            return False
