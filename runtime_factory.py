# 启用前向类型注解，支持在类定义内部引用自身类型（Python3.7+语法）
from __future__ import annotations

# dataclass：数据类，快速定义纯数据载体，自动生成__init__、__repr__等
from dataclasses import dataclass

# 项目配置模块
from config import APP_CONFIG, get_database_source_config
# 数据库客户端（前面我们详细解析的database.py）
from database import DatabaseClient
# 指标知识库，存放业务指标定义、口径、计算公式
from indicator_knowledge import IndicatorKnowledge
# 大模型客户端，负责调用LLM生成SQL、解释结果
from llm_client import LLMClient
# 查询解析器：自然语言查询预处理、校验、解析
from query_parser import QueryParser
# 结果格式化器：把数据库返回的原始数据，转换成前端友好的输出结构
from result_formatter import ResultFormatter


@dataclass(slots=True)
class AppRuntime:
    """
    应用运行时上下文实体
    职责：一次性装载本次查询链路需要的全部组件实例，作为「统一容器」向下传递
    属于ChatBI的核心上下文对象，贯穿一次问答的完整生命周期
    slots=True优化：禁止动态新增属性，节省内存，访问速度更快，适合频繁创建实例
    """
    # 数据源ID，支持多数据源切换（比如数据源A销售库、数据源B库存库）
    source_id: str
    # 查询解析器实例：自然语言预处理、校验
    parser: QueryParser
    # LLM客户端实例：调用大模型生成SQL、生成结论
    llm: LLMClient
    # 数据库客户端实例：执行SQL、连接池、异常翻译、慢查询捕获
    db: DatabaseClient
    # 结果格式化器：数据库原始元组结果 → 前端json结构
    formatter: ResultFormatter
    # 指标知识库：存放指标名称、业务口径、关联表、字段说明
    indicator_knowledge: IndicatorKnowledge


def build_database_client(app_config: dict | None = None, source_id: str | None = None) -> DatabaseClient:
    """
    工厂函数：根据数据源ID，构建对应的DatabaseClient数据库客户端
    设计目的：解耦配置读取逻辑，统一数据库实例创建入口，支持多数据源
    :param app_config: 可选，外部传入的应用配置字典；不传则使用全局APP_CONFIG
    :param source_id: 可选，数据源ID；不传读取配置里默认数据源
    :return: 实例化完成的DatabaseClient对象
    """
    # 优先使用外部传入配置，没有则加载全局APP_CONFIG
    config = app_config or APP_CONFIG
    # 确定最终使用的数据源ID，不传则读取配置中默认数据源
    resolved_source_id = source_id or config["database"]["default_source"]
    # 根据source_id，读取对应数据源的数据库连接配置（多数据源核心逻辑）
    db_config = get_database_source_config(resolved_source_id, config)
    # 创建并返回数据库客户端实例
    return DatabaseClient(db_config=db_config, source_id=resolved_source_id)


def build_runtime(app_config: dict | None = None, source_id: str | None = None) -> AppRuntime:
    """
    顶层工厂函数：构建完整AppRuntime运行时上下文，**项目组件组装入口**
    一次性实例化所有依赖组件：parser、llm、db、formatter、指标知识库
    作用：依赖组装，把各个独立模块组装成一套可直接处理自然语言查询的运行环境
    :param app_config: 外部传入配置，用于单元测试注入自定义配置，不传使用全局APP_CONFIG
    :param source_id: 指定数据源ID，实现多数据源切换
    :return: 完整AppRuntime上下文实例
    """
    # 加载配置，外部传入优先，否则全局配置
    config = app_config or APP_CONFIG
    # 解析数据源ID，默认使用配置中默认数据源
    resolved_source_id = source_id or config["database"]["default_source"]

    # 组装全部组件，返回AppRuntime上下文对象
    return AppRuntime(
        source_id=resolved_source_id,
        parser=QueryParser(),                # 查询解析器
        llm=LLMClient(),                     # LLM客户端
        db=build_database_client(config, resolved_source_id), # 通过工厂创建数据库客户端
        formatter=ResultFormatter(),         # 结果格式化器
        indicator_knowledge=IndicatorKnowledge(), # 业务指标知识库
    )

"""
## 模块定位

这是**组件组装工厂模块**，负责实例化、组装整套 ChatBI 系统的各个模块，产出`AppRuntime`运行时上下文。

> 
> 职责划分：
> 
> 
> - 各个模块（`database.py`、`llm_client.py`、`query_parser.py`）只负责**自己内部逻辑**，不关心其他组件；
> - 本模块负责**实例化、组装所有组件**，打包进`AppRuntime`，统一交给上层业务调用。

### 整体调用链路

上层业务（如 main.py/api 接口）调用 `build_runtime(source_id="xxx")` →

1. 读取对应数据源配置
2. 实例化 QueryParser、LLMClient、ResultFormatter、IndicatorKnowledge
3. 调用`build_database_client`创建 DatabaseClient
4. 全部组件打包放进`AppRuntime`，返回给上层
5. 上层拿到`runtime`对象，调用`runtime.parser` / `runtime.llm` / `runtime.db`完成问答全流程

## 核心类：AppRuntime

`@dataclass(slots=True)`

- dataclass：自动生成构造函数，不用手写`__init__`，代码简洁，专门用来存放数据与组件引用
- `slots=True`
  - 优点：**禁止实例动态新增属性**，防止写错变量名；减少对象内存占用，实例创建更快
  - 适合：这种固定组件集合的上下文对象

AppRuntime 字段含义：

表格

| 字段 | 作用 |
| --- | --- |
| source_id | 多数据源标识，支持切换不同数据库（业务库 / 测试库） |
| parser | 自然语言查询解析、校验 |
| llm | 大模型，生成 SQL、生成解释文本 |
| db | DatabaseClient，执行 SQL，连接池、权限、慢查询、异常翻译 |
| formatter | 原始数据库结果 → 前端可展示的结构化数据 |
| indicator_knowledge | 指标知识库，给 LLM 提供业务指标口径，辅助生成正确 SQL |

> 
> 核心思想：**把一次查询需要的全部依赖打包成一个对象，统一传递**。
> 不需要在函数之间传递一堆零散参数（db、llm、parser...），只传一个`runtime`。

## 两个工厂函数

### 1. build_database_client

专门负责创建数据库客户端，**分离数据库实例创建逻辑**。

- 支持传入自定义`app_config`，单元测试时传入 mock 配置，不用读真实配置文件
- 支持`source_id`，实现多数据源：不同 source_id 读取不同数据库连接信息
- 调用`get_database_source_config`，从 APP_CONFIG 里根据 source_id 取出对应数据库配置

### 2. build_runtime

顶层组装工厂，一次性创建全部组件，生成完整运行时上下文。

- 同样支持外部传入自定义配置，**单元测试友好**（可以注入 mock 组件）
- 内部调用`build_database_client`，复用数据库创建逻辑，避免重复代码

## ✨ 设计亮点

1. **依赖集中组装**
所有组件实例化统一放在这里，上层业务不需要关心各个模块怎么初始化，只调用`build_runtime`即可。
2. **天然支持多数据源**
通过`source_id`，可以快速切换不同数据库，同一个服务对接多个业务库。
3. **方便单元测试**
`app_config`支持外部传入，测试时可以传入自定义配置，甚至后续可以改造支持传入 mock 的 parser/llm/db。
4. **上下文统一传递**
函数调用链只传递`AppRuntime`一个对象，参数简洁，可读性强。

## ⚠️ 当前代码潜在问题（面试可讲）

1. **每次调用 build_runtime 都会新建全套组件实例**
`QueryParser()`、`LLMClient()`、`IndicatorKnowledge()`每次都会新建。

> 
> LLMClient、IndicatorKnowledge 这类重对象，不适合频繁新建，会造成资源浪费。
> 优化：做成单例，或者用对象池，复用实例。

2. 组件硬编码实例化
`parser=QueryParser()`直接 new，没有外部注入。

> 
> 单元测试想替换成 MockParser，需要修改本文件代码；
> 优化：增加参数，支持外部传入 parser/llm/db，实现依赖注入。

3. 没有组件生命周期管理
组件没有 start/close 释放逻辑，例如 LLM 连接、数据库连接池没有统一关闭入口。
4. 没有缓存`get_database_source_config`每次都会读取配置，高频调用可以加缓存。

## 和其他模块关系

1. `config.py`：读取全局配置、按 source_id 获取数据库配置
2. `database.py`：创建 DatabaseClient 实例
3. `llm_client` / `query_parser` / `indicator_knowledge` / `result_formatter`：业务组件
4. `main.py` /api 服务层：调用`build_runtime`拿到上下文，驱动整个 ChatBI 问答流程
"""