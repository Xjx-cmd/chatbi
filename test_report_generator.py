import json
from pathlib import Path
import sys

# --------------------------路径处理--------------------------
# __file__ 当前测试脚本路径
# parents[1] 向上一层目录，拿到项目根目录
# sys.path.insert 把项目根目录加入模块搜索路径，让import可以找到report_generator
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 导入25课核心业务类：报告生成器
from report_generator import ReportGenerator


def sample_step_results() -> list[dict]:
    """
    构造模拟的步骤执行结果，作为ReportGenerator的输入数据
    对应Agent执行完StepExecutor之后返回的step_results列表
    包含：step_id、步骤名称、成功状态、文本摘要formatted、结构化rows数据、中间结果引用result_reference
    """
    return [
        {
            "step_id": "step_1",
            "step_name": "查看最近三个月利润趋势",
            "success": True,
            "status": "completed",
            "formatted": "2026-05 的利润最低，为 920000 元。",
            "rows": [{"month": "2026-05", "profit": 920000}],
            "result_reference": "memory://step_1",
        },
        {
            "step_id": "step_2",
            "step_name": "拆解收入与成本变化",
            "success": True,
            "status": "completed",
            "formatted": "动力电池-乘用车收入下降 8%，材料成本上升 5%。",
            "rows": [{"product_line": "动力电池-乘用车", "revenue_yoy": -0.08, "material_cost_yoy": 0.05}],
            "result_reference": "memory://step_2",
        },
    ]


def test_report_generator_parses_structured_llm_output():
    """
    测试用例1：正常场景
    场景：LLM输出符合约定格式的JSON字符串
    测试目标：
    1. ReportGenerator能把LLM返回的json解析成AnalysisReport模型
    2. 正确填充title、executive_summary、key_findings、root_causes、action_suggestions等字段
    3. 能渲染出带markdown标题的可读报告文本
    """
    # 模拟LLM生成函数，替换真实大模型调用
    def fake_text_generator(system_msg: str, prompt: str) -> str:
        # 简单校验：传给LLM的prompt里面必须带上“关键发现”，确认输入信息正常传入
        assert "关键发现" in prompt
        # 返回符合规范的json字符串，ensure_ascii=False 保留中文不转义
        return json.dumps(
            {
                "title": "最近三个月利润下降分析报告",
                "executive_summary": "利润下滑主要来自动力电池-乘用车业务收入回落和材料成本上升。",
                "key_findings": [
                    "2026-05 的利润最低。",
                    "动力电池-乘用车业务收入下降 8%。",
                ],
                "root_causes": [
                    "主要产品线收入回落。",
                    "材料成本继续上升。",
                ],
                "trend_judgment": "短期利润压力仍在，需继续跟踪 6 月订单修复情况。",
                "action_suggestions": [
                    "优先复盘动力电池-乘用车订单流失原因。",
                    "单独跟踪材料采购价格与毛利变化。",
                ],
            },
            ensure_ascii=False,
        )

    # 实例化报告生成器，注入mock的LLM生成函数，不调用真实大模型
    generator = ReportGenerator(text_generator=fake_text_generator)

    # 调用generate()，传入完整入参：原始问题、分析目标、步骤结果、汇总摘要
    report = generator.generate(
        original_question="最近三个月利润为什么下降？",
        analysis_goal="定位最近三个月利润下降的主要驱动因素",
        step_results=sample_step_results(),
        summary={
            "completed_steps": 2,
            "failed_steps": 0,
            "skipped_steps": 0,
            "summary_text": "当前结果已经可以支撑后续的中间结果管理与最终报告生成。",
        },
    )

    # 断言校验报告解析结果
    assert report.title == "最近三个月利润下降分析报告"
    assert report.key_findings[0] == "2026-05 的利润最低。"
    # 校验markdown渲染成功，包含二级标题
    assert "## 关键发现" in report.markdown
    assert "## 行动建议" in report.markdown


def test_report_generator_falls_back_to_template_when_llm_output_is_invalid():
    """
    测试用例2：异常兜底场景（25课重点考点）
    场景：LLM返回的内容不是合法JSON（乱码、自然语言、格式错误）
    测试目标：
    ✅ 不抛出异常，程序不崩溃
    ✅ 自动切换到模板兜底模式，直接读取step_results拼接生成基础报告
    """
    # mock LLM返回一段完全不是JSON的文本，模拟LLM输出异常
    generator = ReportGenerator(text_generator=lambda _system_msg, _prompt: "not-json")

    # 同样调用generate，入参和上面一致
    report = generator.generate(
        original_question="最近三个月利润为什么下降？",
        analysis_goal="定位最近三个月利润下降的主要驱动因素",
        step_results=sample_step_results(),
        summary={
            "completed_steps": 2,
            "failed_steps": 0,
            "skipped_steps": 0,
            "summary_text": "当前结果已经可以支撑后续的中间结果管理与最终报告生成。",
        },
    )

    # 兜底模板的标题直接使用原始用户提问
    assert report.title == "最近三个月利润为什么下降？"
    # 兜底模式直接从step_results里提取步骤名称和formatted结果作为key_findings
    assert report.key_findings[0].startswith("查看最近三个月利润趋势：")
    # 校验步骤里面的数据被拼入markdown报告
    assert "动力电池-乘用车收入下降 8%" in report.markdown


    """
    LLM 原始输出: 
{
  "question_type": "trend_analysis",
  "analysis_goal": "识别并解释近半年内利润的变动趋势、关键拐点及潜在驱动因素",
  "subtasks": [
    {
      "task_id": "task_1",
      "task_name": "确定时间范围与数据可用性",
      "task_type": "data_discovery",
      "description": "确认系统中可获取的最近年度财务数据时间粒度（日/月），明确近半年起止日期（如2023-07-01至2024-01-31），检查利润字段（如net_profit、gross_profit）是否存在及完整性",
      "depends_on": [],
      "dimensions": ["date"],
      "metrics": []
    },
    {
      "task_id": "task_2",
      "task_name": "提取近半年月度利润序列",
      "task_type": "data_extraction",
      "description": "按自然月聚合，提取近六个月的总利润（net_profit）及构成指标（如营业收入、营业成本、税费等）",
      "depends_on": ["task_1"],
      "dimensions": ["year_month"],
      "metrics": ["net_profit", "revenue", "cost_of_goods_sold", "operating_expense", "tax_expense"]
    },
    {
      "task_id": "task_3",
      "task_name": "计算月度环比与同比变化率",
      "task_type": "trend_calculation",
      "description": "基于task_2结果，计算各月 net_profit 的环比增长率（vs 上月）和同比增长率（vs 去年同月，若数据可得）",
      "depends_on": ["task_2"],
      "dimensions": ["year_month"],
      "metrics": ["net_profit_mom_growth_pct", "net_profit_yoy_growth_pct"]
    },
    {
      "task_id": "task_4",
      "task_name": "识别关键趋势特征",
      "task_type": "pattern_identification",
      "description": "识别上升/下降趋势段、最大单月增幅/降幅、连续增长/下滑月数、拐点月份（如由正转负的月份）",
      "depends_on": ["task_3"],
      "dimensions": ["year_month"],
      "metrics": ["net_profit", "net_profit_mom_growth_pct"]
    },
    {
      "task_id": "task_5",
      "task_name": "按业务维度拆解利润变动归因",
      "task_type": "dimensional_breakdown",
      "description": "在近半年范围内，按产品线、区域、客户类型等主维度分组，分析各维度对整体利润变化的贡献度（如利润增量占比、边际贡献变化）",
      "depends_on": ["task_2"],
      "dimensions": ["product_line", "region", "customer_segment"],
      "metrics": ["net_profit_contribution", "profit_delta_vs_prev_month"]
    },
    {
      "task_id": "task_6",
      "task_name": "生成综合趋势分析结论",
      "task_type": "insight_synthesis",
      "description": "整合task_3、task_4、task_5结果，总结核心趋势、关键驱动维度、异常波动原因假设，并标注数据置信度（如‘Q4促销导致华东区利润跳升+32%，但毛利率下降5pct’）",
      "depends_on": ["task_3", "task_4", "task_5"],
      "dimensions": [],
      "metrics": ["net_profit_trend_summary", "top_drivers", "risk_flags"]
    }
  ]
}
解析后的子任务: 
question_type='trend_analysis' analysis_goal='识别并解释近半年内利润的变动趋势、关键拐点及潜在驱动因素' subtasks=[DecomposedTask(task_id='task_1', task_name='确定时间范围与数据可用性', task_type='data_discovery', description='确认系统中可获取的最近年度财务数据时间粒度（日/月），明确近半年起止日期（如2023-07-01至2024-01-31），检查利润字段（如net_profit、gross_profit）是否存在及完整性', depends_on=[], dimensions=['date'], metrics=[]), DecomposedTask(task_id='task_2', task_name='提取近半年月度利润序列', task_type='data_extraction', description='按自然月聚合，提取近六个月的总利润（net_profit）及构成指标（如营业收入、营业成本、税费等）', depends_on=['task_1'], dimensions=['year_month'], metrics=['net_profit', 'revenue', 'cost_of_goods_sold', 'operating_expense', 'tax_expense']), DecomposedTask(task_id='task_3', task_name='计算月度环比与同比变化率', task_type='trend_calculation', description='基于task_2结果，计算各月 net_profit 的环比增长率（vs 上月）和同比增长率（vs 去年同月，若数据可得）', depends_on=['task_2'], dimensions=['year_month'], metrics=['net_profit_mom_growth_pct', 'net_profit_yoy_growth_pct']), DecomposedTask(task_id='task_4', task_name='识别关键趋势特征', task_type='pattern_identification', description='识别上升/下降趋势段、最大单月增幅/降幅、连续增长/下滑月数、拐点月份（如由正转负的月份）', depends_on=['task_3'], dimensions=['year_month'], metrics=['net_profit', 'net_profit_mom_growth_pct']), DecomposedTask(task_id='task_5', task_name='按业务维度拆解利润变动归因', task_type='dimensional_breakdown', description='在近半年范围内，按产品线、区域、客户类型等主维度分组，分析各维度对整体利润变化的贡献度（如利润增量占比、边际贡献变化）', depends_on=['task_2'], dimensions=['product_line', 'region', 'customer_segment'], metrics=['net_profit_contribution', 'profit_delta_vs_prev_month']), DecomposedTask(task_id='task_6', task_name='生成综合趋势分析结论', task_type='insight_synthesis', description='整合task_3、task_4、task_5结果，总结核心趋势、关键驱动维度、异常波动原因假设，并标注数据置信度（如‘Q4促销导致华东区利润跳升+32%，但毛利率下降5pct’）', depends_on=['task_3', 'task_4', 'task_5'], dimensions=[], metrics=['net_profit_trend_summary', 'top_drivers', 'risk_flags'])]
{
  "original_question": "分析近半年的利润变化情况",
  "decomposition": {
    "question_type": "trend_analysis",
    "analysis_goal": "识别并解释近半年内利润的变动趋势、关键拐点及潜在驱动因素",
    "subtasks": [
      {
        "task_id": "task_1",
        "task_name": "确定时间范围与数据可用性",
        "task_type": "data_discovery",
        "description": "确认系统中可获取的最近年度财务数据时间粒度（日/月），明确近半年起止日期（如2023-07-01至2024-01-31），检查利润字段（如net_profit、gross_profit）是否存在及完整性",
        "depends_on": [],
        "dimensions": [
          "date"
        ],
        "metrics": []
      },
      {
        "task_id": "task_2",
        "task_name": "提取近半年月度利润序列",
        "task_type": "data_extraction",
        "description": "按自然月聚合，提取近六个月的总利润（net_profit）及构成指标（如营业收入、营业成本、税费等）",
        "depends_on": [
          "task_1"
        ],
        "dimensions": [
          "year_month"
        ],
        "metrics": [
          "net_profit",
          "revenue",
          "cost_of_goods_sold",
          "operating_expense",
          "tax_expense"
        ]
      },
      {
        "task_id": "task_3",
        "task_name": "计算月度环比与同比变化率",
        "task_type": "trend_calculation",
        "description": "基于task_2结果，计算各月 net_profit 的环比增长率（vs 上月）和同比增长率（vs 去年同月，若数据可得）",
        "depends_on": [
          "task_2"
        ],
        "dimensions": [
          "year_month"
        ],
        "metrics": [
          "net_profit_mom_growth_pct",
          "net_profit_yoy_growth_pct"
        ]
      },
      {
        "task_id": "task_4",
        "task_name": "识别关键趋势特征",
        "task_type": "pattern_identification",
        "description": "识别上升/下降趋势段、最大单月增幅/降幅、连续增长/下滑月数、拐点月份（如由正转负的月份）",
        "depends_on": [
          "task_3"
        ],
        "dimensions": [
          "year_month"
        ],
        "metrics": [
          "net_profit",
          "net_profit_mom_growth_pct"
        ]
      },
      {
        "task_id": "task_5",
        "task_name": "按业务维度拆解利润变动归因",
        "task_type": "dimensional_breakdown",
        "description": "在近半年范围内，按产品线、区域、客户类型等主维度分组，分析各维度对整体利润变化的贡献度（如利润增量占比、边际贡献变化）",
        "depends_on": [
          "task_2"
        ],
        "dimensions": [
          "product_line",
          "region",
          "customer_segment"
        ],
        "metrics": [
          "net_profit_contribution",
          "profit_delta_vs_prev_month"
        ]
      },
      {
        "task_id": "task_6",
        "task_name": "生成综合趋势分析结论",
        "task_type": "insight_synthesis",
        "description": "整合task_3、task_4、task_5结果，总结核心趋势、关键驱动维度、异常波动原因假设，并标注数据置信度（如‘Q4促销导致华东区利润跳升+32%，但毛利率下降5pct’）",
        "depends_on": [
          "task_3",
          "task_4",
          "task_5"
        ],
        "dimensions": [],
        "metrics": [
          "net_profit_trend_summary",
          "top_drivers",
          "risk_flags"
        ]
      }
    ]
  },
  "plan": {
    "original_question": "分析近半年的利润变化情况",
    "question_type": "trend_analysis",
    "analysis_goal": "识别并解释近半年内利润的变动趋势、关键拐点及潜在驱动因素",
    "steps": [
      {
        "step_id": "step_1",
        "task_id": "task_1",
        "step_name": "确定时间范围与数据可用性",
        "task_type": "data_discovery",
        "action": "text2sql",
        "question": "请执行子任务：确定时间范围与数据可用性。\n任务说明：确认系统中可获取的最近年度财务数据时间粒度（日/月），明确近半年起止日期（如2023-07-01至2024-01-31），检查利润字段（如net_profit、gross_profit）是否存在及完整性\n分析维度：date\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
        "description": "确认系统中可获取的最近年度财务数据时间粒度（日/月），明确近半年起止日期（如2023-07-01至2024-01-31），检查利润字段（如net_profit、gross_profit）是否存在及完整性",
        "depends_on": [],
        "metrics": [],
        "dimensions": [
          "date"
        ],
        "expected_output": "维度：date"
      },
      {
        "step_id": "step_2",
        "task_id": "task_2",
        "step_name": "提取近半年月度利润序列",
        "task_type": "data_extraction",
        "action": "text2sql",
        "question": "请执行子任务：提取近半年月度利润序列。\n任务说明：按自然月聚合，提取近六个月的总利润（net_profit）及构成指标（如营业收入、营业成本、税费等）\n关注指标：net_profit、revenue、cost_of_goods_sold、operating_expense、tax_expense\n分析维度：year_month\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
        "description": "按自然月聚合，提取近六个月的总利润（net_profit）及构成指标（如营业收入、营业成本、税费等）",
        "depends_on": [
          "step_1"
        ],
        "metrics": [
          "net_profit",
          "revenue",
          "cost_of_goods_sold",
          "operating_expense",
          "tax_expense"
        ],
        "dimensions": [
          "year_month"
        ],
        "expected_output": "指标：net_profit、revenue、cost_of_goods_sold、operating_expense、tax_expense；维度：year_month"
      },
      {
        "step_id": "step_3",
        "task_id": "task_3",
        "step_name": "计算月度环比与同比变化率",
        "task_type": "trend_calculation",
        "action": "text2sql",
        "question": "请执行子任务：计算月度环比与同比变化率。\n任务说明：基于task_2结果，计算各月 net_profit 的环比增长率（vs 上月）和同比增长率（vs 去年同月，若数据可得）\n关注指标：net_profit_mom_growth_pct、net_profit_yoy_growth_pct\n分析维度：year_month\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
        "description": "基于task_2结果，计算各月 net_profit 的环比增长率（vs 上月）和同比增长率（vs 去年同月，若数据可得）",
        "depends_on": [
          "step_2"
        ],
        "metrics": [
          "net_profit_mom_growth_pct",
          "net_profit_yoy_growth_pct"
        ],
        "dimensions": [
          "year_month"
        ],
        "expected_output": "指标：net_profit_mom_growth_pct、net_profit_yoy_growth_pct；维度：year_month"
      },
      {
        "step_id": "step_4",
        "task_id": "task_4",
        "step_name": "识别关键趋势特征",
        "task_type": "pattern_identification",
        "action": "text2sql",
        "question": "请执行子任务：识别关键趋势特征。\n任务说明：识别上升/下降趋势段、最大单月增幅/降幅、连续增长/下滑月数、拐点月份（如由正转负的月份）\n关注指标：net_profit、net_profit_mom_growth_pct\n分析维度：year_month\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
        "description": "识别上升/下降趋势段、最大单月增幅/降幅、连续增长/下滑月数、拐点月份（如由正转负的月份）",
        "depends_on": [
          "step_3"
        ],
        "metrics": [
          "net_profit",
          "net_profit_mom_growth_pct"
        ],
        "dimensions": [
          "year_month"
        ],
        "expected_output": "指标：net_profit、net_profit_mom_growth_pct；维度：year_month"
      },
      {
        "step_id": "step_5",
        "task_id": "task_5",
        "step_name": "按业务维度拆解利润变动归因",
        "task_type": "dimensional_breakdown",
        "action": "text2sql",
        "question": "请执行子任务：按业务维度拆解利润变动归因。\n任务说明：在近半年范围内，按产品线、区域、客户类型等主维度分组，分析各维度对整体利润变化的贡献度（如利润增量占比、边际贡献变化）\n关注指标：net_profit_contribution、profit_delta_vs_prev_month\n分析维度：product_line、region、customer_segment\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
        "description": "在近半年范围内，按产品线、区域、客户类型等主维度分组，分析各维度对整体利润变化的贡献度（如利润增量占比、边际贡献变化）",
        "depends_on": [
          "step_2"
        ],
        "metrics": [
          "net_profit_contribution",
          "profit_delta_vs_prev_month"
        ],
        "dimensions": [
          "product_line",
          "region",
          "customer_segment"
        ],
        "expected_output": "指标：net_profit_contribution、profit_delta_vs_prev_month；维度：product_line、region、customer_segment"
      },
      {
        "step_id": "step_6",
        "task_id": "task_6",
        "step_name": "生成综合趋势分析结论",
        "task_type": "insight_synthesis",
        "action": "text2sql",
        "question": "请执行子任务：生成综合趋势分析结论。\n任务说明：整合task_3、task_4、task_5结果，总结核心趋势、关键驱动维度、异常波动原因假设，并标注数据置信度（如‘Q4促销导致华东区利润跳升+32%，但毛利率下降5pct’）\n关注指标：net_profit_trend_summary、top_drivers、risk_flags\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
        "description": "整合task_3、task_4、task_5结果，总结核心趋势、关键驱动维度、异常波动原因假设，并标注数据置信度（如‘Q4促销导致华东区利润跳升+32%，但毛利率下降5pct’）",
        "depends_on": [
          "step_3",
          "step_4",
          "step_5"
        ],
        "metrics": [
          "net_profit_trend_summary",
          "top_drivers",
          "risk_flags"
        ],
        "dimensions": [],
        "expected_output": "指标：net_profit_trend_summary、top_drivers、risk_flags"
      }
    ]
  },
  "step_results": [
    {
      "step_id": "step_1",
      "task_id": "task_1",
      "step_name": "确定时间范围与数据可用性",
      "success": true,
      "status": "completed",
      "attempts": 1,
      "question": "请执行子任务：确定时间范围与数据可用性。\n任务说明：确认系统中可获取的最近年度财务数据时间粒度（日/月），明确近半年起止日期（如2023-07-01至2024-01-31），检查利润字段（如net_profit、gross_profit）是否存在及完整性\n分析维度：date\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
      "depends_on": [],
      "context_used": "",
      "storage_backend": "memory",
      "result_reference": "memory://step_1",
      "sql": "SELECT \n  MIN(o.order_date) AS earliest_order_date,\n  MAX(o.order_date) AS latest_order_date,\n  MIN(e.expense_date) AS earliest_expense_date,\n  MAX(e.expense_date) AS latest_expense_date,\n  COUNT(DISTINCT DATE(o.order_date)) AS distinct_order_dates,\n  COUNT(DISTINCT LAST_DAY(o.order_date)) AS distinct_order_months,\n  COUNT(DISTINCT DATE(e.expense_date)) AS distinct_expense_dates,\n  COUNT(DISTINCT LAST_DAY(e.expense_date)) AS distinct_expense_months\nFROM sales_orders o\nCROSS JOIN finance_expenses e\nWHERE o.order_status = 'completed';",
      "columns": [
        "earliest_order_date",
        "latest_order_date",
        "earliest_expense_date",
        "latest_expense_date",
        "distinct_order_dates",
        "distinct_order_months",
        "distinct_expense_dates",
        "distinct_expense_months"
      ],
      "rows": [
        {
          "earliest_order_date": "2026-01-15",
          "latest_order_date": "2026-03-15",
          "earliest_expense_date": "2026-01-31",
          "latest_expense_date": "2026-03-31",
          "distinct_order_dates": 3,
          "distinct_order_months": 3,
          "distinct_expense_dates": 3,
          "distinct_expense_months": 3
        }
      ],
      "formatted": "---------------------+-------------------+-----------------------+---------------------+----------------------+-----------------------+------------------------+-------------------------\nearliest_order_date  |latest_order_date  |earliest_expense_date  |latest_expense_date  |distinct_order_dates  |distinct_order_months  |distinct_expense_dates  |distinct_expense_months  \n---------------------+-------------------+-----------------------+---------------------+----------------------+-----------------------+------------------------+-------------------------\n2026-01-15           |2026-03-15         |2026-01-31             |2026-03-31           |3                     |3                      |3                       |3                        \n---------------------+-------------------+-----------------------+---------------------+----------------------+-----------------------+------------------------+-------------------------",
      "error": null
    },
    {
      "step_id": "step_2",
      "task_id": "task_2",
      "step_name": "提取近半年月度利润序列",
      "success": false,
      "status": "failed",
      "attempts": 1,
      "question": "请执行子任务：提取近半年月度利润序列。\n任务说明：按自然月聚合，提取近六个月的总利润（net_profit）及构成指标（如营业收入、营业成本、税费等）\n关注指标：net_profit、revenue、cost_of_goods_sold、operating_expense、tax_expense\n分析维度：year_month\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。\n\n前置步骤关键结果如下，请在本次查询中延续这些上下文：\n- 确定时间范围与数据可用性：---------------------+-------------------+-----------------------+---------------------+----------------------+---------",
      "depends_on": [
        "step_1"
      ],
      "context_used": "- 确定时间范围与数据可用性：---------------------+-------------------+-----------------------+---------------------+----------------------+---------",
      "storage_backend": "memory",
      "result_reference": null,
      "sql": "SELECT \n    DATE_FORMAT(o.order_date, '%Y-%m') AS year_month,\n    SUM(o.net_amount * r.rate_to_cny) AS revenue,\n    SUM((p.material_cost + p.labor_cost) * o.quantity) AS cost_of_goods_sold,\n    SUM(f.rd_expense + f.selling_expense + f.admin_expense + f.finance_expense) AS operating_expense,\n    0 AS tax_expense,\n    SUM(o.net_amount * r.rate_to_cny) - SUM((p.material_cost + p.labor_cost) * o.quantity) - SUM(f.rd_expense + f.selling_expense + f.admin_expense + f.finance_expense) AS net_profit\nFROM sales_orders o\nJOIN dim_products p ON o.product_id = p.product_id\nJOIN exchange_rates r ON o.order_date = r.rate_date AND o.currency = r.currency\nJOIN finance_expenses f ON DATE_FORMAT(o.order_date, '%Y-%m') = DATE_FORMAT(f.expense_date, '%Y-%m')\nWHERE o.order_status = 'completed'\n    AND o.order_date >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)\n    AND o.order_date < CURDATE()\n    AND f.expense_date >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)\n    AND f.expense_date < CURDATE()\nGROUP BY DATE_FORMAT(o.order_date, '%Y-%m')\nORDER BY year_month;",
      "columns": [],
      "rows": [],
      "formatted": "",
      "error": "(1064, \"You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near 'year_month,\\n    SUM(o.net_amount * r.rate_to_cny) AS revenue,\\n    SUM((p.materia' at line 2\")"
    },
    {
      "step_id": "step_3",
      "task_id": "task_3",
      "step_name": "计算月度环比与同比变化率",
      "success": false,
      "status": "skipped",
      "attempts": 1,
      "question": "请执行子任务：计算月度环比与同比变化率。\n任务说明：基于task_2结果，计算各月 net_profit 的环比增长率（vs 上月）和同比增长率（vs 去年同月，若数据可得）\n关注指标：net_profit_mom_growth_pct、net_profit_yoy_growth_pct\n分析维度：year_month\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
      "depends_on": [
        "step_2"
      ],
      "context_used": "",
      "storage_backend": null,
      "result_reference": null,
      "sql": null,
      "columns": [],
      "rows": [],
      "formatted": "",
      "error": "前序步骤失败，当前执行策略为 abort，后续步骤停止执行。"
    },
    {
      "step_id": "step_4",
      "task_id": "task_4",
      "step_name": "识别关键趋势特征",
      "success": false,
      "status": "skipped",
      "attempts": 1,
      "question": "请执行子任务：识别关键趋势特征。\n任务说明：识别上升/下降趋势段、最大单月增幅/降幅、连续增长/下滑月数、拐点月份（如由正转负的月份）\n关注指标：net_profit、net_profit_mom_growth_pct\n分析维度：year_month\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
      "depends_on": [
        "step_3"
      ],
      "context_used": "",
      "storage_backend": null,
      "result_reference": null,
      "sql": null,
      "columns": [],
      "rows": [],
      "formatted": "",
      "error": "前序步骤失败，当前执行策略为 abort，后续步骤停止执行。"
    },
    {
      "step_id": "step_5",
      "task_id": "task_5",
      "step_name": "按业务维度拆解利润变动归因",
      "success": false,
      "status": "skipped",
      "attempts": 1,
      "question": "请执行子任务：按业务维度拆解利润变动归因。\n任务说明：在近半年范围内，按产品线、区域、客户类型等主维度分组，分析各维度对整体利润变化的贡献度（如利润增量占比、边际贡献变化）\n关注指标：net_profit_contribution、profit_delta_vs_prev_month\n分析维度：product_line、region、customer_segment\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
      "depends_on": [
        "step_2"
      ],
      "context_used": "",
      "storage_backend": null,
      "result_reference": null,
      "sql": null,
      "columns": [],
      "rows": [],
      "formatted": "",
      "error": "前序步骤失败，当前执行策略为 abort，后续步骤停止执行。"
    },
    {
      "step_id": "step_6",
      "task_id": "task_6",
      "step_name": "生成综合趋势分析结论",
      "success": false,
      "status": "skipped",
      "attempts": 1,
      "question": "请执行子任务：生成综合趋势分析结论。\n任务说明：整合task_3、task_4、task_5结果，总结核心趋势、关键驱动维度、异常波动原因假设，并标注数据置信度（如‘Q4促销导致华东区利润跳升+32%，但毛利率下降5pct’）\n关注指标：net_profit_trend_summary、top_drivers、risk_flags\n请优先返回支撑后续分析的查询结果，不要直接跳到最终归因结论。",
      "depends_on": [
        "step_3",
        "step_4",
        "step_5"
      ],
      "context_used": "",
      "storage_backend": null,
      "result_reference": null,
      "sql": null,
      "columns": [],
      "rows": [],
      "formatted": "",
      "error": "前序步骤失败，当前执行策略为 abort，后续步骤停止执行。"
    }
  ],
  "summary": {
    "original_question": "分析近半年的利润变化情况",
    "analysis_goal": "识别并解释近半年内利润的变动趋势、关键拐点及潜在驱动因素",
    "completed_steps": 1,
    "failed_steps": 1,
    "skipped_steps": 4,
    "key_findings": [
      "确定时间范围与数据可用性：---------------------+-------------------+-----------------------+---------------------+----------------------+---------",
      "提取近半年月度利润序列：执行失败，错误信息：(1064, \"You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near 'year_month,\\n    SUM(o.net_amount * r.rate_to_cny) AS revenue,\\n    SUM((p.materia' at line 2\")",
      "计算月度环比与同比变化率：执行失败，错误信息：前序步骤失败，当前执行策略为 abort，后续步骤停止执行。",
      "识别关键趋势特征：执行失败，错误信息：前序步骤失败，当前执行策略为 abort，后续步骤停止执行。",
      "按业务维度拆解利润变动归因：执行失败，错误信息：前序步骤失败，当前执行策略为 abort，后续步骤停止执行。",
      "生成综合趋势分析结论：执行失败，错误信息：前序步骤失败，当前执行策略为 abort，后续步骤停止执行。"
    ],
    "summary_text": "已完成 1 个步骤，失败 1 个步骤，跳过 4 个步骤。当前链路已经暴露出真实执行问题，需要先修复失败步骤，再继续扩展中间结果管理与总结能力。"
  },
  "report": {
    "title": "近半年利润变化分析报告",
    "executive_summary": "仅完成时间范围与数据可用性确认，后续所有分析步骤均因SQL语法错误而中止。当前无法获取月度利润数据，亦无法计算变化率、识别趋势或归因分析。",
    "key_findings": [
      "可查订单最早日期为2026-01-15，最晚为2026-03-15；费用最早日期为2026-01-31，最晚为2026-03-31。",
      "订单与费用数据仅覆盖3个自然日和3个自然月，实际可用时间跨度不足半年。",
      "提取月度利润序列的SQL执行失败，错误原因为语法错误，导致全部后续分析中断。"
    ],
    "root_causes": [
      "SQL查询存在语法错误，具体位于'year_month,'附近，导致核心利润数据无法提取。",
      "原始数据时间覆盖范围仅有约三个月，不满足‘近半年’分析要求。"
    ],
    "trend_judgment": "无法判断利润趋势，因关键利润序列数据缺失，且时间跨度不足半年。",
    "action_suggestions": [
      "修复SQL语法错误，重新执行月度利润提取。",
      "核实数据源是否真实覆盖近半年，如不满足需明确分析边界。",
      "在获取有效利润数据前，暂停所有衍生分析步骤。"
    ],
    "markdown": "# 近半年利润变化分析报告\n\n## 执行摘要\n仅完成时间范围与数据可用性确认，后续所有分析步骤均因SQL语法错误而中止。当前无法获取月度利润数据，亦无法计算变化率、识别趋势或归因分析。\n\n## 关键发现\n- 可查订单最早日期为2026-01-15，最晚为2026-03-15；费用最早日期为2026-01-31，最晚为2026-03-31。\n- 订单与费用数据仅覆盖3个自然日和3个自然月，实际可用时间跨度不足半年。\n- 提取月度利润序列的SQL执行失败，错误原因为语法错误，导致全部后续分析中断。\n\n## 归因分析\n- SQL查询存在语法错误，具体位于'year_month,'附近，导致核心利润数据无法提取。\n- 原始数据时间覆盖范围仅有约三个月，不满足‘近半年’分析要求。\n\n## 趋势判断\n无法判断利润趋势，因关键利润序列数据缺失，且时间跨度不足半年。\n\n## 行动建议\n- 修复SQL语法错误，重新执行月度利润提取。\n- 核实数据源是否真实覆盖近半年，如不满足需明确分析边界。\n- 在获取有效利润数据前，暂停所有衍生分析步骤。"
  }
}
    """
