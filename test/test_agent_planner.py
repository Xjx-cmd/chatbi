from pathlib import Path
import sys
import re

# ----------------------路径处理----------------------
# __file__ 当前测试脚本的完整路径
# .resolve() 获取绝对路径
# .parents[1] 向上找1级目录，拿到项目根目录
# sys.path.insert(0, ...)：把根目录插入Python模块搜索路径最前面
# 作用：让 import agent_planner 能够成功找到上层目录下的agent_planner.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 从上层模块导入4个核心类，也就是我们前面学习Plan‑and‑Execute Agent骨架
# 导入核心类，TempTableResultStore就是24课新增中间结果存储组件
from agent_planner import (
    PlanAndExecuteAgent,
    PlanGenerator,
    ResultSummarizer,
    StepExecutor,
    TempTableResultStore,
)
from report_generator import ReportGenerator

# ===================== Mock 模拟MySQL临时表环境（单元测试，不需要真实MySQL） =====================
class FakeTempCursor:
    """模拟MySQL cursor游标对象，拦截execute执行的SQL，内存模拟临时表行为
    支持：DROP TEMPORARY TABLE、CREATE TEMPORARY TABLE、SELECT * FROM 临时表
    """
    def __init__(self, connection):
        self.connection = connection   # 持有上层连接对象FakeTempConnection
        self.description = None        # 模拟cursor.description 返回列元信息
        self._results = []              # 模拟fetchall返回的行数据

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        """拦截SQL，正则解析表名、字段，内存字典模拟表"""
        self.connection.executed.append((sql, params))
        sql_upper = " ".join(sql.upper().split())
        # 正则提取反引号 ``包裹的标识符：表名、字段名
        identifiers = re.findall(r"`([^`]+)`", sql)

        # 1. 删除临时表 DROP TEMPORARY TABLE IF EXISTS
        if sql_upper.startswith("DROP TEMPORARY TABLE IF EXISTS"):
            table_name = identifiers[0]
            self.connection.tables.pop(table_name, None)
            self.description = None
            self._results = []
            return

        # 2. 创建临时表 CREATE TEMPORARY TABLE
        if sql_upper.startswith("CREATE TEMPORARY TABLE"):
            table_name = identifiers[0]
            columns = identifiers[1:]
            # 在connection内存字典登记表：{表名: {"columns":[列], "rows":[行数据]}}
            self.connection.tables[table_name] = {"columns": columns, "rows": []}
            self.description = None
            self._results = []
            return

        # 3. 查询临时表 SELECT * FROM `xxx`
        if sql_upper.startswith("SELECT * FROM"):
            table_name = identifiers[0]
            table = self.connection.tables[table_name]
            columns = table["columns"]
            rows = table["rows"]
            # 模拟cursor.description，mysql返回的列元组格式
            self.description = [(column,) for column in columns]
            # 把dict行转为数据库原生tuple格式
            self._results = [tuple(row.get(column) for column in columns) for row in rows]
            return

        raise AssertionError(f"未处理的 SQL: {sql}")

    def executemany(self, sql, params_seq):
        """批量插入数据，对应 INSERT INTO ... VALUES (...),(...)"""
        params_list = list(params_seq)
        self.connection.executed.append((sql, params_list))
        identifiers = re.findall(r"`([^`]+)`", sql)
        table_name = identifiers[0]
        columns = identifiers[1:]
        table = self.connection.tables[table_name]
        # 将插入参数转为字典存入内存表rows
        table["rows"].extend(dict(zip(columns, params)) for params in params_list)

    def fetchall(self):
        """模拟获取查询结果"""
        return self._results

class FakeTempConnection:
    """模拟MySQL连接对象"""
    def __init__(self):
        self.tables = {}          # 内存存储所有临时表
        self.executed = []        # 记录全部执行过的sql，用于断言校验
        self.open = True          # 标记连接是否打开

    def cursor(self):
        return FakeTempCursor(self)

    def commit(self):
        self.executed.append(("COMMIT", None))

    def close(self):
        self.open = False

def sample_decomposition() -> dict:
    """
    模拟 QueryDecomposer.decompose() 的输出结果
    返回字典格式的拆解任务，对应DecompositionPlan模型。
    模拟业务场景："最近三个月利润为什么下降？"拆解出3个子任务，带依赖关系
    task_1：无依赖
    task_2：依赖 task_1
    task_3：依赖 task_2
    """
    return {
        "question_type": "profit_decline_analysis",
        "analysis_goal": "定位最近三个月利润下降的主要驱动因素",
        "subtasks": [
            {
                "task_id": "task_1",
                "task_name": "查看最近三个月利润趋势",
                "task_type": "trend_analysis",
                "description": "先找出利润下降最明显的月份",
                "depends_on": [],        # 无依赖，第一个执行
                "dimensions": ["月份"],
                "metrics": ["利润"],
            },
            {
                "task_id": "task_2",
                "task_name": "拆解收入与成本变化",
                "task_type": "metric_decomposition",
                "description": "围绕利润 = 收入 - 成本，确认是哪一端拖累了利润",
                "depends_on": ["task_1"], # 依赖task_1（拆解层task_id），Planner会转为step_1
                "dimensions": ["月份"],
                "metrics": ["收入", "成本", "利润"],
            },
            {
                "task_id": "task_3",
                "task_name": "定位利润下滑最严重的区域",
                "task_type": "dimension_drilldown",
                "description": "结合前两步结果，观察哪个区域的利润恶化更明显",
                "depends_on": ["task_2"], # 依赖task_2，Planner会转为step_2
                "dimensions": ["区域"],
                "metrics": ["利润", "收入", "成本"],
            },
        ],
    }

def test_plan_generator_builds_ordered_steps():
    """
    测试用例1：测试 PlanGenerator.build_plan()
    测试点：
    1. 能够把dict格式的拆解结果，转换成ExecutionPlan对象
    2. step_id按顺序生成 step_1 / step_2 / step_3
    3. 【重点】依赖ID转换：原始depends_on是task_id，转换后变成step_id
    4. _build_step_question 组装的question文本，正确包含指标信息
    """
    # 获取模拟拆解结果字典
    decomposition = sample_decomposition()
    # 实例化计划生成器
    planner = PlanGenerator()

    # 调用build_plan，传入原始问题 + 模拟拆解dict
    # build_plan内部会调用 _ensure_decomposition_plan() 将dict反序列化为DecompositionPlan对象
    plan = planner.build_plan(
        original_question="最近三个月利润为什么下降？",
        decomposition=decomposition,
    )

    # --------断言：判断结果是否符合预期，不满足则pytest直接报错--------
    # 断言1：分析目标正确透传
    assert plan.analysis_goal == "定位最近三个月利润下降的主要驱动因素"

    # 断言2：生成的step_id顺序为 step_1、step_2、step_3
    assert [step.step_id for step in plan.steps] == ["step_1", "step_2", "step_3"]

    # 断言3：第2步（索引为1）的依赖已经从 task_1 翻译成 step_1
    assert plan.steps[1].depends_on == ["step_1"]

    # 断言4：组装出来的question字符串，包含指标文本“关注指标：收入、成本、利润”
    assert "关注指标：收入、成本、利润" in plan.steps[1].question

def test_step_executor_passes_dependency_context_to_later_steps():
    """
    测试用例2：测试 StepExecutor.execute_plan()
    测试点：
    1. 串行执行3个步骤；
    2. step_1没有依赖，发给runner的question**不会携带前置上下文**；
    3. step_2会把step_1执行结果拼接进question；
    4. step_3会把step_2的上下文拼接进question；
    5. 验证上下文文本能够正确组装并传给后续步骤。

    ⚠️关键点：这里使用fake_runner作为mock，**完全不调用ChatBISystem，不调用LLM，不访问数据库**。
    fake_runner只是记录收到的question，返回写死的模拟成功结果。
    """
    # 拿到模拟拆解数据，生成执行计划
    decomposition = sample_decomposition()
    planner = PlanGenerator()
    plan = planner.build_plan("最近三个月利润为什么下降？", decomposition)

    # 用来收集每一步传给step_runner的question，用于后面断言
    captured_questions: list[str] = []

    # 自定义mock回调函数 fake_runner，替换真实的 _run_with_chatbi
    def fake_runner(question: str) -> dict:
        """模拟单步执行，保存收到的question，返回写死的成功结果"""
        captured_questions.append(question)
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": "模拟执行成功",
        }

    # StepExecutor注入自定义fake_runner，代替真实调用ChatBI
    executor = StepExecutor(step_runner=fake_runner)
    # 执行整个计划
    results = executor.execute_plan(plan)

    # 断言
    assert len(results) == 3  # 一共执行完成3步

    # 第0步=step_1，没有依赖，question中不应该出现“前置步骤关键结果如下”
    assert "前置步骤关键结果如下" not in captured_questions[0]

    # step_2的question，包含前置步骤名称："查看最近三个月利润趋势"
    assert "查看最近三个月利润趋势" in captured_questions[1]

    # step_3的question，包含前置步骤名称："拆解收入与成本变化"
    assert "拆解收入与成本变化" in captured_questions[2]

def test_agent_returns_summary_after_execution():
    """
    测试用例3：完整端到端测试 PlanAndExecuteAgent.run()
    测试全链路：输入拆解dict → planner生成计划 → executor执行（mock） → summarizer生成摘要
    测试点：
    1. 使用decomposition_override，跳过QueryDecomposer拆解，直接传入模拟拆解结果
    2. 全部步骤模拟执行成功，completed_steps=3，failed_steps=0
    3. key_findings列表长度等于步骤数量3
    4. 计划中第3步（index=2）依赖是 ["step_2"]，ID转换正确
    """
    decomposition = sample_decomposition()

    # mock runner，模拟每一步执行成功，formatted文本携带问题首行
    def fake_runner(question: str) -> dict:
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": f"已执行：{question.splitlines()[0]}",
        }

    # 构造Agent，全部组件手动传入，executor使用mock runner
    agent = PlanAndExecuteAgent(
        planner=PlanGenerator(),
        executor=StepExecutor(step_runner=fake_runner),
        summarizer=ResultSummarizer(),
    )

    # 运行Agent；decomposition_override直接传入模拟拆解字典，**跳过QueryDecomposer**
    result = agent.run(
        "最近三个月利润为什么下降？",
        decomposition_override=decomposition,
    )

    # 断言
    assert result["summary"]["completed_steps"] == 3   # 成功3步
    assert result["summary"]["failed_steps"] == 0      # 失败0步
    assert len(result["summary"]["key_findings"]) == 3 # 收集3条发现
    # 第3步的依赖确认是step_2，task_id→step_id转换正确
    assert result["plan"]["steps"][2]["depends_on"] == ["step_2"]

    """

    1. **mock 思想**：单元测试不要依赖大模型、数据库；把`step_runner`替换为假函数，只校验**代码逻辑流转是否正确**。
    2. `decomposition_override`：Agent.run 的入参，可以跳过 QueryDecomposer 拆解，直接喂给 Agent 已经写好的拆解 dict，专门用于单元测试。
    3. 核心验证点就是课程重点：**task_id（拆解层）和 step_id（执行层）的映射转换**。如果转换错误，下游找不到前置步骤，上下文拼接逻辑就会完全失效。
    4. `assert`断言：如果实际运行结果不等于预期值，pytest 直接标记测试失败，快速定位 bug。
    

    以 test_agent_returns_summary_after_execution 为例(数据流)
    sample_decomposition() → dict模拟拆解结果
        ↓
    agent.run(..., decomposition_override=decomposition)
        ↓ 跳过QueryDecomposer，直接使用传入的decomposition
    PlanGenerator.build_plan() → ExecutionPlan（task_id全部转为step_id）
        ↓
    StepExecutor.execute_plan()，fake_runner模拟执行，不调用ChatBI
        ↓
    ResultSummarizer.summarize() → ExecutionSummary摘要
        ↓
    返回完整result字典，执行assert断言校验摘要字段

    """

def test_agent_returns_business_report_after_execution():
    decomposition = sample_decomposition()

    def fake_runner(question: str) -> dict:
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": f"已执行：{question.splitlines()[0]}",
        }

    report_generator = ReportGenerator(
        text_generator=lambda _system_msg, _prompt: """
        {
          "title": "利润下降分析报告",
          "executive_summary": "利润下降主要来自收入回落。",
          "key_findings": ["最近一个月利润明显走低。"],
          "root_causes": ["收入下降快于成本下降。"],
          "trend_judgment": "短期仍需跟踪。",
          "action_suggestions": ["继续观察核心产品线订单恢复情况。"]
        }
        """
    )

    agent = PlanAndExecuteAgent(
        planner=PlanGenerator(),
        executor=StepExecutor(step_runner=fake_runner),
        summarizer=ResultSummarizer(),
        report_generator=report_generator,
    )

    result = agent.run(
        "最近三个月利润为什么下降？",
        decomposition_override=decomposition,
    )

    assert result["report"]["title"] == "利润下降分析报告"
    assert "## 关键发现" in result["report"]["markdown"]

# ===================== 【第24课新增测试用例：中间结果管理】 =====================
def test_step_executor_retries_failed_step_and_records_result_reference():
    """
    测试点：步骤失败自动重试 + 生成中间结果引用 result_reference
    参数说明（StepExecutor新增入参，24课）
    - max_retries=1：最大重试1次
    - failure_policy="abort"：失败策略abort：本步骤重试耗尽失败后，直接终止整个执行
    - storage_backend="memory"：存储后端使用内存存储中间结果
    """
    decomposition = sample_decomposition()
    plan = PlanGenerator().build_plan("最近三个月利润为什么下降？", decomposition)
    attempts = {"step_1": 0}

    def flaky_runner(question: str) -> dict:
        """模拟不稳定任务：step_1第一次执行失败，第2次（重试）执行成功"""
        primary_instruction = question.splitlines()[0]
        if "查看最近三个月利润趋势" in primary_instruction:
            attempts["step_1"] += 1
            if attempts["step_1"] == 1:
                # 第一次返回失败
                return {
                    "success": False,
                    "error": "数据库连接超时",
                    "formatted": "第一次执行失败",
                }
        # 重试之后返回成功
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": "重试后执行成功",
        }

    executor = StepExecutor(
        step_runner=flaky_runner,
        max_retries=1,
        failure_policy="abort",
        storage_backend="memory",
    )
    results = executor.execute_plan(plan)

    # 断言：总共尝试2次：第1次失败，重试1次成功
    assert attempts["step_1"] == 2
    assert results[0].success is True
    assert results[0].attempts == 2          # StepExecutionResult新增字段attempts记录执行次数
    assert results[0].status == "completed"  # 状态：completed / failed / skipped
    # 生成内存中间结果引用地址
    assert results[0].result_reference == "memory://step_1"

def test_step_executor_skips_downstream_steps_after_failed_dependency():
    """
    测试点：failure_policy="skip"策略
    当前步骤执行失败，则**所有依赖它的下游步骤标记为 skipped，不执行runner**
    step_1执行失败；step_2依赖step_1 → skipped；step_3依赖step_2 → skipped
    """
    decomposition = sample_decomposition()
    plan = PlanGenerator().build_plan("最近三个月利润为什么下降？", decomposition)

    def failing_runner(question: str) -> dict:
        primary_instruction = question.splitlines()[0]
        if "查看最近三个月利润趋势" in primary_instruction:
            # 第一步直接失败，无重试 max_retries=0
            return {
                "success": False,
                "error": "SQL 语法错误",
                "formatted": "首步失败",
            }
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": "后续不应执行到这里",
        }

    executor = StepExecutor(
        step_runner=failing_runner,
        max_retries=0,
        failure_policy="skip",
    )
    results = executor.execute_plan(plan)

    assert results[0].status == "failed"
    assert results[1].status == "skipped"
    assert results[1].success is False
    assert "依赖步骤失败" in results[1].error
    assert results[2].status == "skipped"

def test_result_summarizer_counts_skipped_steps_separately():
    """
    测试点：ResultSummarizer新增skipped_steps计数
    区分三类状态：completed(成功) / failed(失败) / skipped(跳过)
    step1 failed；step2、step3 skipped
    completed_steps=0, failed_steps=1, skipped_steps=2
    """
    decomposition = sample_decomposition()
    plan = PlanGenerator().build_plan("最近三个月利润为什么下降？", decomposition)

    def failing_runner(question: str) -> dict:
        primary_instruction = question.splitlines()[0]
        if "查看最近三个月利润趋势" in primary_instruction:
            return {
                "success": False,
                "error": "SQL 语法错误",
                "formatted": "首步失败",
            }
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": "后续不应执行到这里",
        }

    executor = StepExecutor(step_runner=failing_runner, failure_policy="skip")
    step_results = executor.execute_plan(plan)
    summary = ResultSummarizer().summarize(
        original_question="最近三个月利润为什么下降？",
        plan=plan,
        step_results=step_results,
    )

    assert summary.completed_steps == 0
    assert summary.failed_steps == 1
    assert summary.skipped_steps == 2

def test_temp_table_result_store_put_get_and_cleanup():
    """
    测试 TempTableResultStore：MySQL临时表存储中间结果
    put：把步骤输出columns+rows写入临时表，返回result_reference
    get：通过reference读取数据
    cleanup：关闭连接、删除临时表资源释放
    reference格式：temp_table://tmp_agent_step_1
    """
    fake_connection = FakeTempConnection()
    store = TempTableResultStore(connection_factory=lambda: fake_connection)

    # 存入中间结果
    reference = store.put(
        step_id="step_1",
        columns=["month", "profit"],
        rows=[{"month": "2026-05", "profit": 920000}],
    )
    # 根据引用取出数据
    loaded_rows = store.get(reference)

    assert reference == "temp_table://tmp_agent_step_1"
    assert loaded_rows == [{"month": "2026-05", "profit": 920000}]

    # 资源清理：删除临时表，关闭连接
    store.cleanup()

    assert fake_connection.open is False
    assert fake_connection.tables == {}

def test_step_executor_can_load_rows_from_temp_table_reference():
    """
    测试Executor完整链路使用temp_table存储后端：
    1.step_1执行完成，结果存入MySQL临时表，产出 result_reference
    2.下游step_2执行时，Executor根据result_reference调用get_intermediate_result拿到原始结构化数据
    > 课程意义：解决第26课之前旧版本缺陷：不再把大段文本摘要塞到prompt，可读取原始结构化中间结果
    """
    decomposition = sample_decomposition()
    plan = PlanGenerator().build_plan("最近三个月利润为什么下降？", decomposition)
    fake_connection = FakeTempConnection()
    captured_questions: list[str] = []

    def runner(question: str) -> dict:
        captured_questions.append(question)
        primary_instruction = question.splitlines()[0]
        if "查看最近三个月利润趋势" in primary_instruction:
            return {
                "success": True,
                "sql": "SELECT month, profit FROM profit_trend",
                "columns": ["month", "profit"],
                "rows": [{"month": "2026-05", "profit": 920000}],
                "formatted": "",
            }

        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["value"],
            "rows": [{"value": 1}],
            "formatted": "后续执行成功",
        }

    executor = StepExecutor(
        step_runner=runner,
        storage_backend="temp_table",
        storage_connection_factory=lambda: fake_connection,
    )
    results = executor.execute_plan(plan, max_steps=2)
    # 通过reference读取中间结果
    loaded_rows = executor.get_intermediate_result(results[0].result_reference)

    assert results[0].result_reference == "temp_table://tmp_agent_step_1"
    assert loaded_rows == [{"month": "2026-05", "profit": 920000}]
    # 下游question中带上结构化的行数据
    assert '{"month": "2026-05", "profit": 920000}' in captured_questions[1]  

