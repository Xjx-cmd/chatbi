from pathlib import Path
import sys

# ----------------------路径处理----------------------
# __file__ 当前测试脚本的完整路径
# .resolve() 获取绝对路径
# .parents[1] 向上找1级目录，拿到项目根目录
# sys.path.insert(0, ...)：把根目录插入Python模块搜索路径最前面
# 作用：让 import agent_planner 能够成功找到上层目录下的agent_planner.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 从上层模块导入4个核心类，也就是我们前面学习Plan‑and‑Execute Agent骨架
from agent_planner import (
    PlanAndExecuteAgent,   # Agent总入口
    PlanGenerator,         # Planner：计划生成器
    ResultSummarizer,      # Summarizer：结果汇总
    StepExecutor,          # Executor：步骤执行器
)

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

