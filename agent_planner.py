"""
Plan-and-Execute Agent 骨架模块

承接第 22 课的 Query 拆解结果，补齐 Planner、Executor、Summarizer 三个角色，
形成“复杂问题 -> 子任务 -> 执行计划 -> 多步执行 -> 结果汇总”的最小闭环。
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from typing import Any,Callable

from pydantic import BaseModel,Field

from main import ChatBISystem
from query_decomposer import DecomposedTask,DecompositionPlan,QueryDecomposer

class PlanStep(BaseModel):
    """
    单个执行步骤定义。
    由PlanGenerator将拆解后的DecomposedTask转换为本模型，是Executor真正执行的最小单元。
    注意：task_id来自拆解模块；step_id是执行层内部编号，二者不可混淆。
    """
    step_id: str                      # 执行层步骤唯一ID，格式 step_1 / step_2
    task_id: str                      # 上游拆解阶段的子任务ID，用于溯源
    step_name: str                    # 步骤可读名称，用于日志、报告展示
    task_type: str                    # 任务类型：趋势统计 / 维度下钻 / 归因分析等
    action: str = "text2sql"          # 执行动作，默认是文本转SQL查询（预留扩展其他动作）
    question: str                     # 传给LLM的子问题Prompt，包含任务、指标、维度约束
    description: str                  # 任务详细描述，来源于query_decomposer拆解输出
    depends_on: list[str] = Field(default_factory=list)   # 【执行层】依赖的step_id列表，不是task_id
    metrics: list[str] = Field(default_factory=list)      # 本步骤需要分析的指标集合，如profit、revenue
    dimensions: list[str] = Field(default_factory=list)   # 本步骤分析维度集合，如region、product_line
    expected_output: str              # 预期输出描述，用于约束大模型输出格式

class ExecutionPlan(BaseModel):
    """
    完整可执行计划。
    将用户原始问题经过拆解、转换之后得到的一套待执行步骤集合。
    """
    original_question: str     # 用户输入的原始业务问题
    question_type: str         # 问题分类：趋势分析、利润归因、对比分析等
    analysis_goal: str         # 本次分析整体目标，由拆解器生成
    steps: list[PlanStep]      # 有序待执行步骤列表

class StepExecutionResult(BaseModel):
    """
    单步执行完成后的结果结构体。
    Executor每执行完一个PlanStep，输出该对象；下游依赖步骤读取此对象作为上下文。
    ⚠️原始版本缺陷：context_used是字符串文本，会丢失结构化数据（第26课修复改为JSON结构）
    """
    step_id: str
    task_id: str
    step_name: str
    success: bool                          # 标记本步骤是否执行成功
    question: str                          # 实际发给大模型的完整提问（拼接前置上下文之后）
    depends_on: list[str] = Field(default_factory=list)  # 当前步骤依赖哪些step_id
    context_used: str = ""                 # 本次执行用到的前置步骤上下文（早期为纯文本摘要）
    sql: str | None = None                 # 本步骤生成的SQL语句，失败则为None
    columns: list[str] = Field(default_factory=list)     # 查询返回的字段名列表
    rows: list[dict[str, Any]] = Field(default_factory=list)  # 查询结果，已经转为字典，非数据库原始tuple
    formatted: str = ""                    # 格式化的人类可读文本，用于展示/报告
    error: str | None = None               # 失败场景存储错误信息，成功为None

class ExecutionSummary(BaseModel):
    """
    执行全局摘要。
    Summarizer收集全部StepExecutionResult生成，用于对外快速查看整体运行状态。
    注意：骨架版本仅做简单统计，**不做LLM深度业务归因报告**，报告能力在第25课扩展。
    """
    original_question: str
    analysis_goal: str
    completed_steps: int                 # 成功执行步骤计数
    failed_steps: int                    # 失败步骤计数
    key_findings: list[str] = Field(default_factory=list)  # 每一步简短结论列表
    summary_text: str                    # 汇总描述文本

class PlanGenerator:
    """
    Planner：计划生成器。
    职责：接收任务拆解结果DecompositionPlan，转换为Executor可以直接跑的ExecutionPlan。
    核心工作：ID映射转换 task_id → step_id，组装每一步的prompt、预期输出。
    """
    def build_plan(
        self,
        original_question: str,
        decomposition: dict[str, Any] | DecompositionPlan,

    ) -> ExecutionPlan:
        """
        构建可执行计划主入口
        :param original_question: 用户原始问题
        :param decomposition: 拆解输出，支持字典或者DecompositionPlan对象（方便测试传入mock dict）
        :return ExecutionPlan: 可执行执行计划
        """
        # 兼容入参，如果传入字典，则pydantic反序列化为DecompositionPlan对象
        plan = self._ensure_decomposition_plan(decomposition)

        # ==========【关键映射表】==========
        # 拆解模块内部使用task_id做依赖；执行层使用step_id。
        # 构建映射字典 task_id -> step_id，后续用来转换depends_on依赖ID。
        task_to_step = {
            task.task_id: f"step_{index}"
            for index, task in enumerate(plan.subtasks, start=1)
        }

        steps: list[PlanStep] = []
        # 遍历拆解出来的每一个子任务，逐个转为执行步骤PlanStep
        for index, task in enumerate(plan.subtasks, start=1):
            steps.append(
                PlanStep(
                    step_id=f"step_{index}",
                    task_id=task.task_id,
                    step_name=task.task_name,
                    task_type=task.task_type,
                    # 调用内部方法，组装给到LLM的子任务Prompt
                    question=self._build_step_question(task),
                    description=task.description,
                    # ==========【核心逻辑：依赖ID转换】==========
                    # task.depends_on保存的是上游的task_id；
                    # 通过task_to_step映射，全部翻译成执行层step_id；
                    # 如果不转换，Executor找不到前置执行结果，会KeyError报错。
                    depends_on=[task_to_step[dep] for dep in task.depends_on],
                    metrics=task.metrics,
                    # ⚠️课程26课暴露的问题：直接透传拆解输出的维度，若维度不在数据库schema，后续会生成非法SQL。
                    # 修复方案：此处增加维度合法性校验，过滤不存在的维度。
                    dimensions=task.dimensions,
                    expected_output=self._build_expected_output(task),
                )
            )

        # 封装返回完整执行计划对象
        return ExecutionPlan(
            original_question=original_question,
            question_type=plan.question_type,
            analysis_goal=plan.analysis_goal,
            steps=steps
        )
    @staticmethod
    def _ensure_decomposition_plan(
        decomposition: dict[str, Any] | DecompositionPlan,
    ) -> DecompositionPlan:
        """
        类型兼容工具函数：兼容dict和DecompositionPlan两种入参
        如果是字典，调用model_validate反序列化为pydantic模型对象
        """
        if isinstance(decomposition, DecompositionPlan):
            return decomposition
        return DecompositionPlan.model_validate(decomposition)

    @staticmethod
    def _build_step_question(task: DecomposedTask) -> str:
        """
        根据子任务，组装给大模型的子问题Prompt文本。
        加入约束：不要直接输出最终归因结论，优先返回查询数据结果。
        """
        lines = [f"请执行子任务：{task.task_name}。"]
        if task.description:
            lines.append(f"任务说明：{task.description}")
        if task.metrics:
            lines.append(f"关注指标：{'、'.join(task.metrics)}")
        if task.dimensions:
            lines.append(f"分析维度：{'、'.join(task.dimensions)}")
        lines.append("请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。")
        return "\n".join(lines)

    @staticmethod
    def _build_expected_output(task: DecomposedTask) -> str:
        """
        构造expected_output预期输出描述，约束模型输出内容。
        告诉模型这一步应该产出哪些指标、哪些维度的结果。
        """
        focus_parts: list[str] = []
        if task.metrics:
            focus_parts.append(f"指标：{'、'.join(task.metrics)}")
        if task.dimensions:
            focus_parts.append(f"维度：{'、'.join(task.dimensions)}")
        if not focus_parts:
            focus_parts.append("可被后续步骤复用的结构化结果")
        return "；".join(focus_parts)

# 类型别名：StepRunner，定义单步执行回调函数签名，方便替换mock做单元测试
StepRunner = Callable[[str], dict[str, Any]]

class StepExecutor:
    """
    Executor：步骤执行器
    接收ExecutionPlan，按顺序循环执行PlanStep；处理依赖、组装上下文，调用ChatBISystem执行查询。
    当前实现：顺序串行执行，没有做DAG并行执行，依赖完全依靠上下文拼接。
    """
    def __init__(
        self,
        step_runner: StepRunner | None = None,
        chatbi_system: ChatBISystem | None = None,
        chatbi_run_options: dict[str, Any] | None = None,
    ):
        self.system = chatbi_system
        # 传给ChatBISystem的配置开关：schema linking、指标RAG等
        self.chatbi_run_options = chatbi_run_options or {
            "use_schema_linking": True,
            "use_indicator_rag": True,
            "use_indicator_knowledge": True,
        }
        # 可注入自定义runner；不传默认使用内置 _run_with_chatbi
        self.step_runner = step_runner or self._run_with_chatbi

    def execute_plan(
        self,
        plan: ExecutionPlan,
        max_steps: int | None = None,
    ) -> list[StepExecutionResult]:
        """
        执行完整计划主逻辑
        :param plan: 待执行ExecutionPlan
        :param max_steps: 调试参数，限制最多执行多少步，便于分步验证链路
        :return list[StepExecutionResult]: 全部步骤执行结果列表
        """
        results: list[StepExecutionResult] = []
        # 快速索引字典 key:step_id value:StepExecutionResult，用来查找已跑完的依赖步骤
        results_by_step: dict[str, StepExecutionResult] = {}
        # 如果设置max_steps，截断步骤列表，只跑前面N步
        steps_to_run = plan.steps[:max_steps] if max_steps is not None else plan.steps

        for step in steps_to_run:
            # 1、根据本步骤的depends_on，收集前置步骤的上下文
            dependency_context = self._build_dependency_context(
                step.depends_on,
                results_by_step,
            )
            # 2、原始子问题 + 前置上下文拼接，得到真正发给LLM的完整问题
            composed_question = self._compose_question(step.question, dependency_context)
            # 3、调用执行器（默认调用ChatBISystem.run）
            raw_result = self.step_runner(composed_question)
            # 4、将原始返回dict归一化为标准StepExecutionResult对象
            normalized = self._normalize_result(
                step=step,
                question=composed_question,
                context_used=dependency_context,
                raw_result=raw_result,
            )
            results.append(normalized)
            results_by_step[step.step_id] = normalized

        return results

    def _run_with_chatbi(self, question: str) -> dict[str, Any]:
        """默认内置runner，调用ChatBISystem执行用户问题"""
        if self.system is None:
            self.system = ChatBISystem()
        return self.system.run(
            user_question=question,
            **self.chatbi_run_options,
        )

    @staticmethod
    def _compose_question(step_question: str, dependency_context: str) -> str:
        """把步骤原始问题和前置上下文拼接成最终prompt字符串"""
        if not dependency_context:
            return step_question
        return (
            f"{step_question}\n\n"
            f"前置步骤关键结果如下，请在本次查询中延续这些上下文：\n"
            f"{dependency_context}"
        )

    @staticmethod
    def _build_dependency_context(
        dependency_ids: list[str],
        results_by_step: dict[str, StepExecutionResult],
    ) -> str:
        """
        组装依赖步骤的上下文文本。
        ⚠️原始版本缺陷：输出字符串，把结构化数据压缩为简短文本摘要，造成下游信息丢失。
        第26课修复：改为返回list[dict]结构化JSON，执行链路使用结构化数据；展示层再渲染文本。
        """
        if not dependency_ids:
            return ""
        lines: list[str] = []
        for step_id in dependency_ids:
            result = results_by_step[step_id]
            lines.append(
                f"- {result.step_name}：{StepExecutor._pick_result_brief(result)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _pick_result_brief(result: StepExecutionResult) -> str:
        """提取步骤结果简短摘要，用于拼接上下文文本/日志打印"""
        if not result.success:
            return f"执行失败，错误信息：{result.error or '未知错误'}"
        if result.formatted.strip():
            first_line = result.formatted.strip().splitlines()[0]
            return first_line[:120]
        if result.rows:
            return json.dumps(result.rows[0], ensure_ascii=False)
        return "步骤执行成功，但当前无返回行。"

    @staticmethod
    def _normalize_result(
        step: PlanStep,
        question: str,
        context_used: str,
        raw_result: dict[str, Any],
    ) -> StepExecutionResult:
        """
        将ChatBI返回的原始字典结果标准化，转为StepExecutionResult模型。
        兼容不同key（results / rows），数据库返回tuple元组转为字典格式。
        """
        columns = raw_result.get("columns", [])
        rows = raw_result.get("results") or raw_result.get("rows") or []
        # 如果是数据库返回tuple行，转为dict字典，方便后续JSON序列化
        if rows and columns and isinstance(rows[0], tuple):
            normalized_rows = [dict(zip(columns, row)) for row in rows]
        else:
            normalized_rows = rows

        return StepExecutionResult(
            step_id=step.step_id,
            task_id=step.task_id,
            step_name=step.step_name,
            success=raw_result.get("success", False),
            question=question,
            depends_on=step.depends_on,
            context_used=context_used,
            sql=raw_result.get("sql"),
            columns=columns,
            rows=normalized_rows,
            formatted=raw_result.get("formatted", ""),
            error=raw_result.get("error"),
        )

class ResultSummarizer:
    """
    Summarizer结果汇总器
    收集全部步骤执行结果，统计成功失败，生成简单的全局执行摘要。
    注意：骨架版本仅做汇总统计，**不会调用LLM做深度业务归因**。
    """
    def summarize(
        self,
        original_question: str,
        plan: ExecutionPlan,
        step_results: list[StepExecutionResult],
    ) -> ExecutionSummary:
        completed = [result for result in step_results if result.success]
        failed = [result for result in step_results if not result.success]
        # 收集每一步简短发现
        findings = [
            f"{result.step_name}：{StepExecutor._pick_result_brief(result)}"
            for result in step_results
        ]
        if failed:
            summary_text = (
                f"已完成 {len(completed)} 个步骤，"
                f"失败 {len(failed)} 个步骤。"
                "当前链路已经暴露出真实执行问题，"
                "需要先修复失败步骤，再继续扩展中间结果管理与总结能力。"
            )
        else:
            summary_text = (
                f"已完成 {len(completed)} 个步骤，"
                f"失败 {len(failed)} 个步骤。"
                "当前结果已经可以支撑后续的中间结果管理与最终报告生成。"
            )
        return ExecutionSummary(
            original_question=original_question,
            analysis_goal=plan.analysis_goal,
            completed_steps=len(completed),
            failed_steps=len(failed),
            key_findings=findings,
            summary_text=summary_text,
        )

    
class PlanAndExecuteAgent:
    """
    Plan‑and‑Execute Agent总入口。
    采用依赖注入模式：decomposer/planner/executor/summarizer全部支持外部传入mock实例，方便单元测试。
    完整链路：问题拆解 → 生成计划 → 执行计划 → 结果汇总
    """
    def __init__(
        self,
        decomposer: QueryDecomposer | None = None,
        planner: PlanGenerator | None = None,
        executor: StepExecutor | None = None,
        summarizer: ResultSummarizer | None = None,
    ):
        # 不传参则实例化默认对象
        self.decomposer = decomposer or QueryDecomposer()
        self.planner = planner or PlanGenerator()
        self.executor = executor or StepExecutor()
        self.summarizer = summarizer or ResultSummarizer()

    def run(
        self,
        user_question: str,
        decomposition_override: dict[str, Any] | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        """
        Agent对外主运行接口
        :param user_question: 用户原始业务问题
        :param decomposition_override: 可传入外部已经做好的拆解结果，跳过拆解，用于调试
        :param max_steps: 限制最大执行步骤
        :return dict: 打包全部中间对象（拆解、计划、每一步结果、摘要）
        """
        # 如果外部没有传入拆解结果，则调用decomposer做问题拆解
        decomposition = decomposition_override or self.decomposer.decompose(user_question)
        # Planner生成可执行计划
        plan = self.planner.build_plan(user_question, decomposition)
        # Executor执行计划
        step_results = self.executor.execute_plan(plan, max_steps=max_steps)
        # Summarizer汇总生成摘要
        summary = self.summarizer.summarize(user_question, plan, step_results)
        # pydantic model_dump()把模型转为字典，打包返回完整链路数据
        return {
            "original_question": user_question,
            "decomposition": decomposition,
            "plan": plan.model_dump(),
            "step_results": [result.model_dump() for result in step_results],
            "summary": summary.model_dump(),
        }

def _json_default(value: Any) -> str:
    """
    json.dump序列化兜底函数。
    Decimal数据库数值类型无法直接序列化，转为字符串；其他未知类型直接转str。
    """
    if isinstance(value, Decimal):
        return str(value)
    return str(value)

def main() -> None:
    """
    CLI终端入口，支持命令行直接运行Agent，用于调试
    示例命令：uv run python agent_planner.py "最近三个月利润为什么下降？"
    """
    # 1. 创建命令行参数解析器，描述这个程序是【Plan‑and‑Execute Agent骨架】
    parser = argparse.ArgumentParser(description="Plan-and-Execute Agent 骨架")

    # 位置参数：question，用户输入的业务问题
    # nargs="?" 代表：这个参数可以不传；不传就使用default默认值
    # default 默认问题："最近三个月利润为什么下降？"
    parser.add_argument("question", nargs="?", default="最近三个月利润为什么下降？")

    # 可选参数 --plan‑only，布尔开关；action="store_true"：命令行写了这个参数值就是True，不写就是False
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="只执行真实拆解与计划生成，不执行后续查询。",
    )

    # 可选参数 --max‑steps，接收一个整数；用来限制最多执行几步，调试用
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="限制实际执行的步骤数，便于分步验证真实链路。",
    )

    # 可选参数 --disable‑schema‑linking，布尔开关：关闭Schema Linking能力
    parser.add_argument(
        "--disable-schema-linking",
        action="store_true",
        help="执行步骤时关闭 Schema Linking，直接使用基础 Text2SQL 链路。",
    )

    # 可选参数 --disable‑indicator‑rag，布尔开关：关闭指标RAG检索
    parser.add_argument(
        "--disable-indicator-rag",
        action="store_true",
        help="执行步骤时关闭指标 RAG，避免额外检索链路干扰验证。",
    )

    # 解析终端传入的所有参数，结果存入args对象，通过args.xxx读取
    args = parser.parse_args()

    # ----------------分支1：--plan‑only模式：只生成计划，不执行SQL查询----------------
    if args.plan_only:
        # 手动实例化拆解器、计划生成器
        decomposer = QueryDecomposer()
        planner = PlanGenerator()
        # 第一步：对用户问题做任务拆解，得到decomposition拆解结果
        decomposition = decomposer.decompose(args.question)
        # 第二步：把拆解结果转换为可执行ExecutionPlan计划
        plan = planner.build_plan(args.question, decomposition)
        # 组装返回结果，这里没有step_results（没有执行）
        result = {
            "original_question": args.question,
            "decomposition": decomposition,
            "plan": plan.model_dump(),  # pydantic对象转字典
        }

    # ----------------分支2：完整执行模式，走完整Agent链路----------------
    else:
        # 构造StepExecutor执行器
        # not args.disable_schema_linking：命令行不加--disable‑schema‑linking → use_schema_linking=True；加了就变成False
        executor = StepExecutor(
            chatbi_run_options={
                "use_schema_linking": not args.disable_schema_linking,
                "use_indicator_rag": not args.disable_indicator_rag,
                "use_indicator_knowledge": True,
            }
        )
        # 创建Agent总入口，传入配置好的executor
        agent = PlanAndExecuteAgent(executor=executor)
        # 调用agent.run()跑完整链路，传入用户问题，传入max_steps限制执行步数
        result = agent.run(args.question, max_steps=args.max_steps)

    # 把result字典打印成美观JSON
    # ensure_ascii=False：支持中文不乱码
    # indent=2：JSON换行缩进，方便阅读
    # default=_json_default：遇到Decimal数值，调用自定义函数转字符串，防止json.dump报错
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

















