"""
Query 拆解器模块

将复杂分析问题拆解为结构化子任务列表，为后续 Planner 提供稳定输入。
"""

import json
import re
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from llm_client import LLMClient


class DecomposedTask(BaseModel):
    """单个子任务定义。"""

    task_id: str
    task_name: str
    task_type: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)


class DecompositionPlan(BaseModel):
    """复杂查询拆解结果。"""

    question_type: str
    analysis_goal: str
    subtasks: list[DecomposedTask]


def build_decomposition_prompt(user_question: str) -> tuple[str, str]:
    """构造 Query 拆解 Prompt。"""
    system_msg = (
        "你是企业级 ChatBI 系统中的任务拆解器。"
        "请把复杂分析问题拆成可执行的子任务列表，"
        "输出必须是 JSON，不要输出额外解释。"
    )
    prompt = f"""
请将下面的复杂分析问题拆解为结构化子任务，并严格输出 JSON：

用户问题：{user_question}

输出要求：
1. 顶层字段包含 question_type、analysis_goal、subtasks
2. subtasks 是有序数组，每个任务必须包含：
   - task_id
   - task_name
   - task_type
   - description
   - depends_on
   - dimensions
   - metrics
3. task_id 使用 task_1、task_2 这类格式
4. depends_on 只能引用前面已经出现的 task_id
5. 如果问题涉及时间对比、维度对比、指标拆解，请显式拆成多个子任务
6. 仅返回 JSON 对象，不要使用 Markdown
""".strip()
    return system_msg, prompt


class QueryDecomposer:
    """复杂问题拆解器。"""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        response_generator: Callable[[str, str], str] | None = None,
    ):
        self.llm = llm_client or LLMClient()
        self.response_generator = response_generator or self._generate_response

    def decompose(self, user_question: str) -> dict:
        """将复杂问题拆解为结构化子任务。"""
        question = user_question.strip()
        if not question:
            raise ValueError("输入问题不能为空")

        system_msg, prompt = build_decomposition_prompt(question)
        raw_response = self.response_generator(system_msg, prompt)
        print(f"LLM 原始输出: \n{raw_response}")
        plan = self._parse_plan(raw_response)
        print(f"解析后的子任务: \n{plan}")
        self._validate_dependencies(plan)
        return plan.model_dump()

    def _generate_response(self, system_msg: str, prompt: str) -> str:
        """默认通过 LLM 生成 JSON 结果。"""
        response = self.llm.client.chat.completions.create(
            model=self.llm.model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM 没有返回可用的拆解结果")
        return content

    def _parse_plan(self, raw_response: str) -> DecompositionPlan:
        """解析 LLM 返回结果，兼容 Markdown 代码块。"""
        json_text = self._extract_json(raw_response)
        try:
            return DecompositionPlan.model_validate_json(json_text)
        except ValidationError as exc:
            raise ValueError(f"拆解结果结构不合法: {exc}") from exc

    @staticmethod
    def _extract_json(raw_response: str) -> str:
        """从模型输出中提取 JSON 文本。"""
        text = raw_response.strip()
        fence_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()

        try:
            json.loads(text)
            return text
        except json.JSONDecodeError as exc:
            raise ValueError("LLM 返回的内容不是合法 JSON") from exc

    @staticmethod
    def _validate_dependencies(plan: DecompositionPlan) -> None:
        """校验依赖任务是否存在且顺序合法。"""
        seen_ids: set[str] = set()
        for task in plan.subtasks:
            for dep_id in task.depends_on:
                if dep_id not in seen_ids:
                    raise ValueError(f"任务 {task.task_id} 依赖了不存在的任务: {dep_id}")
            seen_ids.add(task.task_id)


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]).strip() or "最近三个月利润为什么下降？"
    decomposer = QueryDecomposer()
    result = decomposer.decompose(question)
    print(json.dumps(result, ensure_ascii=False, indent=2))


"""LLM 原始输出: 
{
  "question_type": "trend_analysis",
  "analysis_goal": "识别导致最近三个月利润下降的关键驱动因素",
  "subtasks": [
    {
      "task_id": "task_1",
      "task_name": "提取最近三个月及上年同期利润数据",
      "task_type": "data_retrieval",
      "description": "获取2024年4月-6月及2023年4月-6月的总利润数值，用于同比和环比基准分析",
      "depends_on": [],
      "dimensions": ["time"],
      "metrics": ["profit"]
    },
    {
      "task_id": "task_2",
      "task_name": "计算利润月度趋势与变化率",
      "task_type": "trend_calculation",
      "description": "计算2024年4月、5月、6月逐月利润值、环比变化率及三个月累计降幅",
      "depends_on": ["task_1"],
      "dimensions": ["time"],
      "metrics": ["profit", "profit_mom_change_pct", "profit_3m_sum"]
    },
    {
      "task_id": "task_3",
      "task_name": "按业务线拆解利润贡献",
      "task_type": "dimensional_breakdown",
      "description": "将最近三个月利润按业务线（如产品A、产品B、服务C）维度分解，识别贡献下降最显著的业务线",
      "depends_on": ["task_1"],
      "dimensions": ["time", "business_line"],
      "metrics": ["profit", "profit_share_pct"]
    },
    {
      "task_id": "task_4",
      "task_name": "分析各业务线下收入与成本变动",
      "task_type": "driver_analysis",
      "description": "对task_3中识别出的负向贡献业务线，分别分析其收入、COGS、运营费用的月度变化，定位利润下滑主因（收入减少 or 成本上升）",
      "depends_on": ["task_3"],
      "dimensions": ["time", "business_line"],
      "metrics": ["revenue", "cogs", "operating_expense", "gross_profit", "net_profit_margin"]
    },
    {
      "task_id": "task_5",
      "task_name": "交叉验证关键维度异常点",
      "task_type": "anomaly_validation",
      "description": "在task_4发现的异常业务线中，进一步按区域、客户类型、渠道维度下钻，确认是否存在结构性偏移（如高毛利区域销售萎缩）",
      "depends_on": ["task_4"],
      "dimensions": ["time", "business_line", "region", "customer_segment", "sales_channel"],
      "metrics": ["revenue", "profit", "avg_order_value", "unit_volume"]
    }
  ]
}
解析后的子任务: 
question_type='trend_analysis' analysis_goal='识别导致最近三个月利润下降的关键驱动因素' subtasks=[DecomposedTask(task_id='task_1', task_name='提取最近三个月及上年同期利润数据', task_type='data_retrieval', description='获取2024年4月-6月及2023年4月-6月的总利润数值，用于同比和环比基准分析', depends_on=[], dimensions=['time'], metrics=['profit']), DecomposedTask(task_id='task_2', task_name='计算利润月度趋势与变化率', task_type='trend_calculation', description='计算2024年4月、5月、6月逐月利润值、环比变化率及三个月累计降幅', depends_on=['task_1'], dimensions=['time'], metrics=['profit', 'profit_mom_change_pct', 'profit_3m_sum']), DecomposedTask(task_id='task_3', task_name='按业务线拆解利润贡献', task_type='dimensional_breakdown', description='将最近三个月利润按业务线（如产品A、产品B、服务C）维度分解，识别贡献下降最显著的业务线', depends_on=['task_1'], dimensions=['time', 'business_line'], metrics=['profit', 'profit_share_pct']), DecomposedTask(task_id='task_4', task_name='分析各业务线下收入与成本变动', task_type='driver_analysis', description='对task_3中识别出的负向贡献业务线，分别分析其收入、COGS、运营费用的月度变化，定位利润下滑主因（收入减少 or 成本上升）', depends_on=['task_3'], dimensions=['time', 'business_line'], metrics=['revenue', 'cogs', 'operating_expense', 'gross_profit', 'net_profit_margin']), DecomposedTask(task_id='task_5', task_name='交叉验证关键维度异常点', task_type='anomaly_validation', description='在task_4发现的异常业务线中，进一步按区域、客户类型、渠道维度下钻，确认是否存在结构性偏移（如高毛利区域销售萎缩）', depends_on=['task_4'], dimensions=['time', 'business_line', 'region', 'customer_segment', 'sales_channel'], metrics=['revenue', 'profit', 'avg_order_value', 'unit_volume'])]
{
  "question_type": "trend_analysis",
  "analysis_goal": "识别导致最近三个月利润下降的关键驱动因素",
  "subtasks": [
    {
      "task_id": "task_1",
      "task_name": "提取最近三个月及上年同期利润数据",
      "task_type": "data_retrieval",
      "description": "获取2024年4月-6月及2023年4月-6月的总利润数值，用于同比和环比基准分析",
      "depends_on": [],
      "dimensions": [
        "time"
      ],
      "metrics": [
        "profit"
      ]
    },
    {
      "task_id": "task_2",
      "task_name": "计算利润月度趋势与变化率",
      "task_type": "trend_calculation",
      "description": "计算2024年4月、5月、6月逐月利润值、环比变化率及三个月累计降幅",
      "depends_on": [
        "task_1"
      ],
      "dimensions": [
        "time"
      ],
      "metrics": [
        "profit",
        "profit_mom_change_pct",
        "profit_3m_sum"
      ]
    },
    {
      "task_id": "task_3",
      "task_name": "按业务线拆解利润贡献",
      "task_type": "dimensional_breakdown",
      "description": "将最近三个月利润按业务线（如产品A、产品B、服务C）维度分解，识别贡献下降最显著的业务线",
      "depends_on": [
        "task_1"
      ],
      "dimensions": [
        "time",
        "business_line"
      ],
      "metrics": [
        "profit",
        "profit_share_pct"
      ]
    },
    {
      "task_id": "task_4",
      "task_name": "分析各业务线下收入与成本变动",
      "task_type": "driver_analysis",
      "description": "对task_3中识别出的负向贡献业务线，分别分析其收入、COGS、运营费用的月度变化，定位利润下滑主因（收入减少 or 成本上升）",
      "depends_on": [
        "task_3"
      ],
      "dimensions": [
        "time",
        "business_line"
      ],
      "metrics": [
        "revenue",
        "cogs",
        "operating_expense",
        "gross_profit",
        "net_profit_margin"
      ]
    },
    {
      "task_id": "task_5",
      "task_name": "交叉验证关键维度异常点",
      "task_type": "anomaly_validation",
      "description": "在task_4发现的异常业务线中，进一步按区域、客户类型、渠道维度下钻，确认是否存在结构性偏移（如高毛利区域销售萎缩）",
      "depends_on": [
        "task_4"
      ],
      "dimensions": [
        "time",
        "business_line",
        "region",
        "customer_segment",
        "sales_channel"
      ],
      "metrics": [
        "revenue",
        "profit",
        "avg_order_value",
        "unit_volume"
      ]
    }
  ]
}"""