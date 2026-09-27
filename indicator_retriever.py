"""
指标 RAG 检索模块

第 19 课：用语义检索（LangChain + ChromaDB）替代原 indicator_knowledge.py 的纯关键词匹配。
从完整指标知识库（indicators_full.json）中检索与用户问题相关的指标定义和计算公式。

核心改进：
- 关键词匹配 → 语义检索（能理解"各产品线赚了多少" ≈ "产品线收入"）
- 5 个指标 → 13 个核心指标（完整覆盖业务分析场景）
- 支持依赖指标自动展开（检索到"利润"时自动注入"毛利""期间费用"）

复用第 15 课的 LangChain + ChromaDB 基础设施。
"""
import os
import json
# from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from config import LLM_CONFIG
from dashscope import TextEmbedding
from langchain_core.embeddings import Embeddings
from typing import List
from pydantic import BaseModel
from table_retriever import DashScopeEmbeddings

# ==================== 配置 ====================
INDICATORS_FILE = os.path.join(os.path.dirname(__file__), "indicators_full.json")
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "chroma_db", "indicators")

def _cosine_relevance_score_fn(distance: float) -> float:
    """余弦距离 → [0,1] 相似度得分。

    Chroma 的 cosine 距离范围是 [0, 2]，直接 1-distance 会产生负数，
    因此除以 2 归一化，保证分数落在 [0, 1] 区间。
    """
    return (1.0 - distance) / 2.0

def get_embeddings()->DashScopeEmbeddings:
    """构建阿里云DashScope Embeddings实例，原生SDK，不走OpenAI兼容网关"""
    return DashScopeEmbeddings(
        model_name=LLM_CONFIG["embedding_model"],
        api_key=LLM_CONFIG["api_key"],
    )
def get_vectorstore() -> Chroma:
    """获取或创建指标描述的 ChromaDB 向量存储实例"""
    return Chroma(
        collection_name="indicator_definitions",
        embedding_function=get_embeddings(),
        persist_directory=CHROMA_PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
        relevance_score_fn=_cosine_relevance_score_fn,
    )

# ==================== 指标数据加载 ====================
def load_indicators() -> dict[str, dict]:
    """加载指标知识库，返回 {指标名: 指标定义} 字典"""
    with open(INDICATORS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {ind["name"]: ind for ind in data["indicators"]}

# ==================== 索引构建 ====================
def build_indicator_index(force_rebuild: bool = False) -> Chroma:
    """
    将指标定义向量化并写入 ChromaDB。

    每个指标的检索文档 = 指标名 + 别名 + 业务定义 + 计算公式
    这段组合文本被向量化，用于语义匹配。
    """
    vectorstore = get_vectorstore()
    existing = vectorstore._collection.count()

    # 如果旧索引使用 L2 距离，必须重建为 cosine
    existing_space = (vectorstore._collection.metadata or {}).get("hnsw:space", "l2")
    metric_changed = existing_space != "cosine"

    if existing > 0 and not force_rebuild and not metric_changed:
        print(f"指标索引已存在（{existing} 条），跳过重建。")
        return vectorstore

    if existing > 0 and (force_rebuild or metric_changed):
        # 新版 Chroma 不再接受 where={}，按 ID 删除全部
        all_ids = vectorstore._collection.get(include=["metadatas"])["ids"]
        if all_ids:
            vectorstore._collection.delete(ids=all_ids)
        print("已清空旧指标索引数据" + ("（距离度量已切换为 cosine）" if metric_changed else ""))

    indicators = load_indicators()
    documents = []
    ids = []

    for name, ind in indicators.items():
        # 构建检索文档：组合指标名、别名、定义、公式
        aliases_str = "、".join(ind.get("aliases", []))
        search_text = (
            f"指标：{name}（{aliases_str}）\n"
            f"定义：{ind['definition']}\n"
            f"计算公式：{ind['formula']}"
        )

        doc = Document(
            page_content=search_text,
            metadata={
                "name": name,
                "level": ind.get("level", ""),
                "aliases": aliases_str,
                "formula": ind["formula"],
                "data_source": ind.get("data_source", ""),
                "depends_on": ",".join(ind.get("depends_on", [])),
            },
        )
        documents.append(doc)
        ids.append(name)

    vectorstore.add_documents(documents, ids=ids)
    print(f"指标索引构建完成：{len(documents)} 个指标已写入 ChromaDB")
    print(f"持久化路径：{CHROMA_PERSIST_DIR}")
    return vectorstore

# ==================== RAG 检索 ====================
def retrieve_indicators(
    query: str,
    top_k: int = 3,
    score_threshold: float = 0.3,
    expand_dependencies: bool = True,
) -> list[dict]:
    """
    根据用户问题检索相关指标定义。

    Args:
        query: 用户的自然语言问题
        top_k: 返回前 K 个最相关的指标
        score_threshold: 相似度阈值
        expand_dependencies: 是否自动展开依赖指标

    Returns:
        [{"name": str, "score": float, "definition": str, "formula": str,
          "sql_template": str, "depends_on": [str], "is_dependency": bool}]
    """
    vectorstore = get_vectorstore()
    indicators_db = load_indicators()

    # 1. 向量检索
    results_with_scores = vectorstore.similarity_search_with_relevance_scores(
        query, k=top_k
    )

    # 2. 过滤低分结果
    matched = []
    matched_names = set()

    for doc, score in results_with_scores:
        if score < score_threshold:
            continue
        name = doc.metadata["name"]
        matched_names.add(name)
        ind = indicators_db.get(name, {})
        matched.append({
            "name": name,
            "score": round(score, 4),
            "level": ind.get("level", ""),
            "definition": ind.get("definition", ""),
            "formula": ind.get("formula", ""),
            "sql_template": ind.get("sql_template", ""),
            "data_source": ind.get("data_source", ""),
            "depends_on": ind.get("depends_on", []),
            "notes": ind.get("notes", ""),
            "is_dependency": False,
        })

    # 3. 自动展开依赖指标
    if expand_dependencies:
        deps_to_add = set()
        for item in matched:
            for dep_name in item["depends_on"]:
                if dep_name not in matched_names:
                    deps_to_add.add(dep_name)

        for dep_name in deps_to_add:
            ind = indicators_db.get(dep_name)
            if ind:
                matched_names.add(dep_name)
                matched.append({
                    "name": dep_name,
                    "score": 0.0,  # 非检索命中，通过依赖关系引入
                    "level": ind.get("level", ""),
                    "definition": ind.get("definition", ""),
                    "formula": ind.get("formula", ""),
                    "sql_template": ind.get("sql_template", ""),
                    "data_source": ind.get("data_source", ""),
                    "depends_on": ind.get("depends_on", []),
                    "notes": ind.get("notes", ""),
                    "is_dependency": True,
                })

    return matched

# ==================== Prompt 知识块生成 ====================

def build_indicator_knowledge_block_from_results(indicators: list[dict]) -> str:
    """基于已检索结果构建指标知识块，避免重复触发 RAG 检索。"""
    if not indicators:
        return ""

    blocks = ["【指标知识】"]
    for ind in indicators:
        dep_tag = "（依赖指标）" if ind["is_dependency"] else ""
        lines = [
            f"指标：{ind['name']}{dep_tag}",
            f"  定义：{ind['definition']}",
            f"  计算公式：{ind['formula']}",
            f"  数据来源：{ind['data_source']}",
        ]
        if ind["notes"]:
            note = ind["notes"].lstrip("注意：").lstrip("注意:").strip()
            lines.append(f"  注意：{note}")
        if ind["sql_template"] and not ind["sql_template"].startswith("需"):
            lines.append(f"  SQL参考：{ind['sql_template']}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_indicator_knowledge_block(query: str) -> str:
    """
    根据用户问题生成指标知识文本块，可直接注入 Prompt。

    替代原 indicator_knowledge.py 的 build_knowledge_block() 方法。
    """
    indicators = retrieve_indicators(query)
    return build_indicator_knowledge_block_from_results(indicators)


def retrieve_indicator_context(
    query: str,
    top_k: int = 3,
    score_threshold: float = 0.3,
    expand_dependencies: bool = True,
) -> dict[str, list[str] | str]:
    """一次 RAG 检索同时返回命中指标和 Prompt 知识块。"""
    indicators = retrieve_indicators(
        query,
        top_k=top_k,
        score_threshold=score_threshold,
        expand_dependencies=expand_dependencies,
    )
    return {
        "detected_indicators": [
            item["name"] for item in indicators if not item["is_dependency"]
        ],
        "indicator_block": build_indicator_knowledge_block_from_results(indicators),
    }
# ==================== 主程序：演示 ====================
if __name__ == "__main__":
    print("=" * 60)
    print("指标知识库 RAG 检索演示")
    print("=" * 60)

    # 构建索引
    print("\n--- 构建指标向量索引 ---")
    build_indicator_index(force_rebuild=True)

    # 测试用例
    test_questions = [
        # 直接命中
        ("查询上个月的毛利", "直接包含指标名'毛利'"),
        ("各产品线的利润是多少", "包含'产品线'和'利润'，语义检索优先命中'产品线收入'"),
        # 语义等价（不含指标关键词）
        ("各产品线赚了多少", "不含'收入'但语义等价'产品线收入'"),
        ("卖出去的东西成本多少", "口语化表达 → 应匹配'销售成本'"),
        # 复合指标（触发依赖展开）
        ("今年的毛利率是多少", "毛利率 → 依赖展开'收入'和'毛利'"),
        # 不相关
        ("今天天气怎么样", "无关问题，不应命中任何指标"),
    ]

    print("\n--- 语义检索测试 ---")
    for question, note in test_questions:
        print(f"\n{'='*50}")
        print(f"问题：{question}")
        print(f"说明：{note}")
        print("-" * 50)

        results = retrieve_indicators(question)
        if results:
            for r in results:
                dep_tag = " [依赖展开]" if r["is_dependency"] else ""
                print(
                    f"  {r['name']:8s} (相似度:{r['score']:.3f}) "
                    f"[{r['level']}]{dep_tag}"
                )
                print(f"    公式: {r['formula']}")
        else:
            print("  （未命中任何指标）")

    # 对比关键词匹配
    print(f"\n\n{'='*60}")
    print("关键词匹配 vs 语义检索 对比")
    print("=" * 60)

    # 模拟旧的关键词匹配
    from indicator_knowledge import IndicatorKnowledge
    old_ik = IndicatorKnowledge()

    compare_questions = [
        "各产品线赚了多少",     # 旧方案：无法命中（不含"收入"关键词）
        "卖出去的东西成本多少", # 旧方案：无法命中
        "上个月的利润",         # 两者都能命中
    ]

    for q in compare_questions:
        old_result = old_ik.detect_indicators(q)
        new_result = retrieve_indicators(q)
        new_names = [r["name"] for r in new_result if not r["is_dependency"]]

        print(f"\n  问题：{q}")
        print(f"    关键词匹配：{old_result if old_result else '未命中'}")
        print(f"    语义检索：  {new_names if new_names else '未命中'}")

    # 生成 Prompt 知识块示例
    print(f"\n\n{'='*60}")
    print("Prompt 知识块生成示例")
    print("=" * 60)
    knowledge_block = build_indicator_knowledge_block("各产品线的毛利率")
    print(f"\n问题：各产品线的毛利率")
    print(f"\n{knowledge_block}")

"""
============================================================
指标知识库 RAG 检索演示
============================================================

--- 构建指标向量索引 ---
正在处理批次 1, 文本数量:10
正在处理批次 2, 文本数量:3
指标索引构建完成：13 个指标已写入 ChromaDB
持久化路径：e:\Users\徐继璇\Desktop\Ai\chatBI\chatbi\chroma_db\indicators

--- 语义检索测试 ---

==================================================
问题：查询上个月的毛利
说明：直接包含指标名'毛利'
--------------------------------------------------
  毛利       (相似度:0.334) [派生指标]
    公式: SUM(o.net_amount * r.rate_to_cny) - SUM((p.material_cost + p.labor_cost) * o.quantity)
  利润       (相似度:0.308) [复合指标]
    公式: 毛利 - 期间费用
  期间费用     (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM(rd_expense + selling_expense + admin_expense + finance_expense)
  收入       (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM(o.net_amount * r.rate_to_cny)
  销售成本     (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM((p.material_cost + p.labor_cost) * o.quantity)

==================================================
问题：各产品线的利润是多少
说明：包含'产品线'和'利润'，语义检索优先命中'产品线收入'
--------------------------------------------------
  产品线收入    (相似度:0.340) [派生指标]
    公式: SUM(o.net_amount * r.rate_to_cny) GROUP BY p.product_line
  收入       (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM(o.net_amount * r.rate_to_cny)

==================================================
问题：各产品线赚了多少
说明：不含'收入'但语义等价'产品线收入'
--------------------------------------------------
  产品线收入    (相似度:0.364) [派生指标]
    公式: SUM(o.net_amount * r.rate_to_cny) GROUP BY p.product_line
  收入       (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM(o.net_amount * r.rate_to_cny)

==================================================
问题：卖出去的东西成本多少
说明：口语化表达 → 应匹配'销售成本'
--------------------------------------------------
  销售成本     (相似度:0.319) [原子指标]
    公式: SUM((p.material_cost + p.labor_cost) * o.quantity)

==================================================
问题：今年的毛利率是多少
说明：毛利率 → 依赖展开'收入'和'毛利'
--------------------------------------------------
  毛利率      (相似度:0.332) [派生指标]
    公式: (毛利 / 收入) * 100
  毛利       (相似度:0.308) [派生指标]
    公式: SUM(o.net_amount * r.rate_to_cny) - SUM((p.material_cost + p.labor_cost) * o.quantity)
  收入       (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM(o.net_amount * r.rate_to_cny)
  销售成本     (相似度:0.000) [原子指标] [依赖展开]
    公式: SUM((p.material_cost + p.labor_cost) * o.quantity)

==================================================
问题：今天天气怎么样
说明：无关问题，不应命中任何指标
--------------------------------------------------
  （未命中任何指标）


============================================================
关键词匹配 vs 语义检索 对比
============================================================

  问题：各产品线赚了多少
    关键词匹配：未命中
    语义检索：  ['产品线收入']

  问题：卖出去的东西成本多少
    关键词匹配：['销售成本']
    语义检索：  ['销售成本']

  问题：上个月的利润
    关键词匹配：['利润']
    语义检索：  ['利润']


============================================================
Prompt 知识块生成示例
============================================================

问题：各产品线的毛利率

【指标知识】

指标：产品线收入
  定义：按产品线维度汇总的不含税收入。需要 JOIN 产品维度表获取 product_line 字段。
  计算公式：SUM(o.net_amount * r.rate_to_cny) GROUP BY p.product_line
  数据来源：sales_orders JOIN dim_products JOIN exchange_rates
  注意：product_line 枚举值：动力电池-乘用车 / 动力电池-商用车 / 储能系统-电网级 / 储能系统-工商业 / 电池材料与回收
  SQL参考：SELECT p.product_line, SUM(o.net_amount * r.rate_to_cny) AS revenue FROM sales_orders o JOIN dim_products p ON o.product_id = p.product_id JOIN exchange_rates r ON o.order_date = r.rate_date AND o.currency = r.currency WHERE o.order_status = 'completed' GROUP BY p.product_line

指标：毛利率
  定义：毛利占收入的百分比，反映产品盈利效率。
  计算公式：(毛利 / 收入) * 100
  数据来源：sales_orders JOIN dim_products JOIN exchange_rates
  注意：结果为百分比数值，如 35.2 表示毛利率 35.2%
  SQL参考：SELECT (SUM(o.net_amount * r.rate_to_cny) - SUM((p.material_cost + p.labor_cost) * o.quantity)) / SUM(o.net_amount * r.rate_to_cny) * 100 AS gross_margin_pct FROM sales_orders o JOIN dim_products p ON o.product_id = p.product_id JOIN exchange_rates r ON o.order_date = r.rate_date AND o.currency = r.currency WHERE o.order_status = 'completed'

指标：毛利（依赖指标）
  定义：销售收入减去销售成本。反映产品本身的盈利能力。
  计算公式：SUM(o.net_amount * r.rate_to_cny) - SUM((p.material_cost + p.labor_cost) * o.quantity)
  数据来源：sales_orders JOIN dim_products JOIN exchange_rates
  注意：毛利 = 收入 - 成本，是利润计算的中间结果
  SQL参考：SELECT SUM(o.net_amount * r.rate_to_cny) - SUM((p.material_cost + p.labor_cost) * o.quantity) AS gross_profit FROM sales_orders o JOIN dim_products p ON o.product_id = p.product_id JOIN exchange_rates r ON o.order_date = r.rate_date AND o.currency = r.currency WHERE o.order_status = 'completed'

指标：收入（依赖指标）
  定义：不含税销售收入，财务口径的销售额。统一使用 net_amount 字段，按订单日期的汇率折算为人民币。只统计已完成（completed）订单。
  计算公式：SUM(o.net_amount * r.rate_to_cny)
  数据来源：sales_orders JOIN exchange_rates
  注意：gross_amount 是含税金额，除非明确要求含税否则不使用
  SQL参考：SELECT SUM(o.net_amount * r.rate_to_cny) AS revenue FROM sales_orders o JOIN exchange_rates r ON o.order_date = r.rate_date AND o.currency = r.currency WHERE o.order_status = 'completed'"""