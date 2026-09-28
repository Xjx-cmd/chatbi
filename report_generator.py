"""
分析报告生成模块
第25课代码：把多步 SQL 执行后的结构化结果，整理为业务可读的分析报告。
默认优先调用 LLM 输出结构化 JSON；如果模型不可用或返回格式异常，
则回退到确定性的模板化报告，保证 Agent 输出层始终可用。

核心设计：双模式
1、正常模式：调用大模型，智能生成完整业务分析报告
2、降级兜底模式：LLM不可用、报错、返回格式错误，使用固定模板生成报告，链路不会崩溃
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, Field

# LLM配置：读取api_key等参数
from config import LLM_CONFIG
# LLM客户端，封装大模型网络请求
from llm_client import LLMClient

# 类型别名：定义报告生成回调函数的签名
# 入参：system_msg系统提示词，prompt用户提示词；返回值：大模型输出原始字符串
ReportTextGenerator = Callable[[str, str], str]

class AnalysisReport(BaseModel):
    """
    面向业务读者的结构化分析报告 Pydantic数据模型
    不管是LLM生成，还是兜底模板，最终都输出该模型对象
    """
    title: str                          # 报告标题，一般复用用户原始业务问题
    executive_summary: str              # 执行摘要：高度浓缩整体分析结论
    key_findings: list[str] = Field(default_factory=list)     # 关键发现列表，多条事实结论
    root_causes: list[str] = Field(default_factory=list)       # 根因/归因分析列表
    trend_judgment: str                 # 趋势判断文字，描述上升/下降/波动情况
    action_suggestions: list[str] = Field(default_factory=list) # 业务可落地行动建议
    markdown: str = ""                  # 渲染后的Markdown完整文本，用于CLI打印、前端展示

class ReportGenerator:
    """
    报告生成器主类
    输入：agent_planner输出的step_results、summary统计摘要
    输出：AnalysisReport结构化报告对象
    支持依赖注入自定义text_generator，方便单元测试Mock，不需要真实调用大模型
    """

    def __init__(self, text_generator: ReportTextGenerator | None = None):
        """
        :param text_generator: 可选，自定义LLM文本生成回调；不传使用内部默认LLM实现
        """
        # 如果外部没有传入自定义生成器，则调用内部方法构建默认的LLM生成器
        self.text_generator = text_generator or self._build_default_text_generator()

    def generate(
        self,
        original_question: str,
        analysis_goal: str,
        step_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> AnalysisReport:
        """
        =========对外公开主入口方法=========
        根据Agent多步执行结果生成业务分析报告
        逻辑：优先尝试LLM生成；发生任何异常，直接降级为模板兜底报告

        :param original_question: 用户原始业务问题，如"最近三个月利润为什么下降？"
        :param analysis_goal: 拆解得到本次分析的目标
        :param step_results: 所有步骤执行结果，StepExecutionResult的model_dump字典列表
        :param summary: ResultSummarizer输出的ExecutionSummary字典，包含成功/失败/跳过计数
        :return: AnalysisReport 结构化报告对象
        """
        # 判断：没有可用的LLM生成器（配置没有api_key），直接走降级模板，不调用大模型
        if self.text_generator is None:
            return self._build_fallback_report(
                original_question=original_question,
                analysis_goal=analysis_goal,
                step_results=step_results,
                summary=summary,
            )

        # 1.组装系统角色提示词
        system_msg = self._build_system_message()
        # 2.组装完整用户Prompt，带上压缩后的执行上下文数据
        prompt = self._build_prompt(
            original_question=original_question,
            analysis_goal=analysis_goal,
            step_results=step_results,
            summary=summary,
        )

        try:
            # 调用LLM，拿到原始返回文本
            raw_text = self.text_generator(system_msg, prompt)
            # 清洗字符串，剥离```json代码块标记，解析为AnalysisReport模型
            parsed = self._parse_report_json(raw_text)
            # 将结构化模型数据渲染成markdown字符串，存入markdown字段
            parsed.markdown = self._render_markdown(parsed)
            return parsed
        except Exception:
            """
            捕获全部异常：
            包括网络超时、LLM返回乱码、JSON解析失败、Pydantic字段校验不通过
            只要发生异常，不抛出错误，直接进入兜底模板，保证链路不崩溃
            """
            return self._build_fallback_report(
                original_question=original_question,
                analysis_goal=analysis_goal,
                step_results=step_results,
                summary=summary,
            )
    @staticmethod
    def _build_system_message() -> str:
        """构建System系统提示词，定义大模型的身份与基础约束"""
        return (
            "你是企业经营分析师，负责把 SQL 查询结果整理成业务人员可读的分析报告。"
            "只基于提供的数据和执行结果写结论，不要编造未出现的事实。"
            "输出必须是 JSON。"
        )

    def _build_prompt(
        self,
        original_question: str,
        analysis_goal: str,
        step_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> str:
        """
        构造送给大模型的完整用户Prompt
        重点：调用_compress_step_result压缩每一步结果，只预览少量行，防止token爆炸
        """
        # 组装全部上下文数据
        report_context = {
            "original_question": original_question,
            "analysis_goal": analysis_goal,
            "summary": summary,
            # 对每一步结果做压缩处理，避免把全量数据库数据丢给大模型
            "step_results": [self._compress_step_result(step_result) for step_result in step_results],
        }
        # 上下文转为JSON字符串，Decimal自动处理，中文不转unicode编码
        context_json = json.dumps(
            report_context,
            ensure_ascii=False,
            indent=2,
            default=self._json_default,
        )
        # 拼接指令 + 上下文数据，完整prompt返回
        return (
            "请根据下面的 Agent 执行结果，生成一份结构化分析报告。\n"
            "报告应包含：标题、执行摘要、关键发现、归因分析、趋势判断、行动建议。\n"
            "要求：\n"
            "1. 只使用上下文里已经出现的信息。\n"
            "2. 如果数据不足，结论要保守。\n"
            "3. 关键发现和行动建议用简短句子表达。\n"
            "4. 只返回 JSON，不要额外输出 Markdown。\n"
            "5. JSON 必须包含 title、executive_summary、key_findings、root_causes、trend_judgment、action_suggestions 这 6 个字段。\n\n"
            f"上下文如下：\n{context_json}"
        )

    @staticmethod
    def _compress_step_result(step_result: dict[str, Any]) -> dict[str, Any]:
        """
        【重要优化】压缩单步执行结果，控制token消耗
        问题：数据库查询返回成千上万行，如果全部传给LLM，token直接超限
        处理：rows_preview只保留前3行作为样例，不全量传递数据
        """
        rows = step_result.get("rows") or []
        return {
            "step_id": step_result.get("step_id"),
            "step_name": step_result.get("step_name"),
            "status": step_result.get("status"),
            "success": step_result.get("success"),
            "formatted": step_result.get("formatted"),
            "result_reference": step_result.get("result_reference"),
            "rows_preview": rows[:3],   # 只取前3行预览，减少token
            "error": step_result.get("error"),
        }

    @staticmethod
    def _parse_report_json(raw_text: str) -> AnalysisReport:
        """
        解析LLM返回的原始文本，转为AnalysisReport模型
        兼容场景：很多大模型会用 ```json ... ``` 代码块包裹输出，需要正则去除标记
        """
        cleaned = raw_text.strip()
        # 移除开头 ```json
        cleaned = re.sub(r"^```json\s*", "", cleaned)
        # 移除结尾 ```
        cleaned = re.sub(r"\s*```$", "", cleaned)
        # 解析json字符串为python字典
        payload = json.loads(cleaned)
        # pydantic校验字段，转为模型对象；字段缺失直接抛异常，外层会捕获进入fallback
        return AnalysisReport.model_validate(payload)

    def _build_fallback_report(
        self,
        original_question: str,
        analysis_goal: str,
        step_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> AnalysisReport:
        """
        🛡️兜底降级模板报告
        触发条件：无LLM密钥、LLM报错、json解析失败、pydantic校验失败
        特点：完全不调用大模型，纯确定性模板拼接，保证系统一定输出报告，不会崩溃
        """
        # 过滤筛选出执行成功完成的步骤
        completed_results = [
            step_result for step_result in step_results
            if step_result.get("status") == "completed" and step_result.get("success")
        ]
        # 遍历成功步骤，提取简短摘要作为关键发现；无数据则填充默认提示
        key_findings = [
            f"{step_result.get('step_name')}：{self._pick_step_brief(step_result)}"
            for step_result in completed_results
        ] or ["当前执行结果不足，暂时无法提炼关键发现。"]

        # 默认根因文本
        root_causes = [
            "目前先根据各步骤返回的结果做归纳，尚未引入额外业务规则。"
        ]
        # 如果存在失败步骤，追加提示信息
        if summary.get("failed_steps", 0) > 0:
            root_causes.append("部分步骤执行失败，当前归因只覆盖已成功返回的部分。")

        # 模板固定行动建议
        action_suggestions = [
            "优先复核关键产品线、区域或费用项的波动来源。",
            "继续补充失败步骤或缺失维度，再迭代报告结论。",
        ]
        # 组装结构化报告对象
        report = AnalysisReport(
            title=original_question,
            executive_summary=(
                f"{analysis_goal}。"
                f"本次共完成 {summary.get('completed_steps', 0)} 个步骤，"
                f"失败 {summary.get('failed_steps', 0)} 个步骤，"
                f"跳过 {summary.get('skipped_steps', 0)} 个步骤。"
            ),
            key_findings=key_findings,
            root_causes=root_causes,
            trend_judgment=summary.get("summary_text", "当前结果可作为后续进一步分析的基础。"),
            action_suggestions=action_suggestions,
        )
        # 兜底报告同样渲染markdown文本
        report.markdown = self._render_markdown(report)
        return report

    @staticmethod
    def _pick_step_brief(step_result: dict[str, Any]) -> str:
        """从单步结果提取简短摘要文本，用于兜底报告展示"""
        formatted = (step_result.get("formatted") or "").strip()
        # 优先取formatted格式化文本，取第一行，截断120字符
        if formatted:
            return formatted.splitlines()[0][:120]
        rows = step_result.get("rows") or []
        # 其次取第一条行数据json字符串
        if rows:
            return json.dumps(rows[0], ensure_ascii=False, default=ReportGenerator._json_default)
        error = step_result.get("error")
        # 如果步骤失败，返回错误信息
        if error:
            return f"执行失败：{error}"
        # 默认兜底文字
        return "步骤已完成，但当前没有可展示的结果摘要。"

    @staticmethod
    def _render_markdown(report: AnalysisReport) -> str:
        """
        将AnalysisReport结构化对象，渲染为可读Markdown字符串
        输出用于终端打印、前端展示
        """
        sections = [
            f"# {report.title}",
            "",
            "## 执行摘要",
            report.executive_summary,
            "",
            "## 关键发现",
            *[f"- {finding}" for finding in report.key_findings],
            "",
            "## 归因分析",
            *[f"- {cause}" for cause in report.root_causes],
            "",
            "## 趋势判断",
            report.trend_judgment,
            "",
            "## 行动建议",
            *[f"- {suggestion}" for suggestion in report.action_suggestions],
        ]
        return "\n".join(sections)

    @staticmethod
    def _json_default(value: Any) -> str:
        """
        json序列化的自定义兜底处理器
        MySQL查询出来的Decimal类型，json库无法直接序列化，转为字符串
        """
        if isinstance(value, Decimal):
            return str(value)
        return str(value)

    @staticmethod
    def _build_default_text_generator() -> ReportTextGenerator | None:
        """
        构建默认的LLM回调生成函数
        如果配置文件中没有api_key，返回None，上层直接走fallback，完全不请求大模型
        """
        # 判断：没有配置api_key，返回None
        if not LLM_CONFIG.get("api_key"):
            return None

        # 实例化LLM客户端
        client = LLMClient()

        # 内部闭包函数：包装调用llm_client.generate_text
        def _generate(system_msg: str, prompt: str) -> str:
            return client.generate_text(system_msg=system_msg, prompt=prompt)

        return _generate