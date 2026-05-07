"""
Tool ReAct agents — web_search / database / visualize.

Each agent:
    • has its own LLM brain (configured via *_AGENT_LLM env vars),
    • is a `ReactAgent` instance bound to the relevant tool functions
      from `api/tools/*.py` (we don't reimplement them — we wrap them
      via thin adapters into the AgentTool contract),
    • runs independently per OrchestratorTask, returning an AgentResult.

Tool agents are constructed once per request and cached on the state
so we can hand the LLM handle the matching prompt without re-reading
disk on every call.
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig, RunnableLambda

from graph.llm_router import get_llm
from graph.react_agent import AgentTool, ReactAgent, run_agents_parallel
from graph.state import ChatState, OrchestratorTask
from prompts import (
    get_database_query_prompt,
    get_visualize_prompt,
    get_web_search_prompt,
)
from tools.database_query import (
    DATABASE_QUERY_TOOL_SCHEMAS,
    DESCRIBE_TABLE_TOOL_SCHEMA,
    LIST_TABLES_TOOL_SCHEMA,
    aggregate as db_aggregate,
    describe_table as db_describe_table,
    distinct_values as db_distinct_values,
    list_tables as db_list_tables,
    select_rows as db_select_rows,
)
from tools.visualize import (
    VISUALIZE_TOOL_SCHEMAS,
    bar as viz_bar,
    chart_from_data as viz_chart_from_data,
    line as viz_line,
    pie as viz_pie,
)
from tools.web_search import WEB_SEARCH_TOOL_SCHEMA, web_search

log = logging.getLogger("finhouse.graph.tool_agents")


# ── Adapter: AgentTool handlers wrap the raw tool functions ──


async def _h_web_search(args: dict):
    return await web_search(args.get("query", "")[:500])


async def _h_list_tables(args: dict):  # noqa: ARG001
    return await db_list_tables()


async def _h_describe_table(args: dict):
    return await db_describe_table(args.get("table", ""))


async def _h_distinct_values(args: dict):
    return await db_distinct_values(
        table=args.get("table", ""),
        column=args.get("column", ""),
        filters=args.get("filters") or None,
        limit=args.get("limit", 100),
        use_final=args.get("use_final", True),
    )


async def _h_select_rows(args: dict):
    return await db_select_rows(
        table=args.get("table", ""),
        columns=args.get("columns") or None,
        filters=args.get("filters") or None,
        order_by=args.get("order_by") or None,
        limit=args.get("limit", 100),
        use_final=args.get("use_final", True),
    )


async def _h_aggregate(args: dict):
    return await db_aggregate(
        table=args.get("table", ""),
        aggregations=args.get("aggregations") or [],
        group_by=args.get("group_by") or None,
        filters=args.get("filters") or None,
        order_by=args.get("order_by") or None,
        limit=args.get("limit", 100),
        use_final=args.get("use_final", True),
    )


async def _h_bar(args: dict):
    return await viz_bar(
        table=args.get("table", ""),
        x_column=args.get("x_column", ""),
        y_columns=args.get("y_columns") or [],
        filters=args.get("filters") or None,
        order_by=args.get("order_by") or None,
        limit=args.get("limit", 50),
        use_final=args.get("use_final", True),
        title=args.get("title"),
    )


async def _h_line(args: dict):
    return await viz_line(
        table=args.get("table", ""),
        x_column=args.get("x_column", ""),
        y_columns=args.get("y_columns") or [],
        filters=args.get("filters") or None,
        order_by=args.get("order_by") or None,
        limit=args.get("limit", 50),
        use_final=args.get("use_final", True),
        title=args.get("title"),
    )


async def _h_pie(args: dict):
    return await viz_pie(
        table=args.get("table", ""),
        label_column=args.get("label_column", ""),
        value_column=args.get("value_column", ""),
        filters=args.get("filters") or None,
        order_by=args.get("order_by") or None,
        limit=args.get("limit", 10),
        use_final=args.get("use_final", True),
        title=args.get("title"),
    )


async def _h_chart_from_data(args: dict):
    return await viz_chart_from_data(
        mark=args.get("mark", ""),
        x_labels=args.get("x_labels") or [],
        y_series=args.get("y_series") or [],
        title=args.get("title"),
    )


# ── Schema lookup helper ────────────────────────────────────


def _schema_by_name(schemas: list[dict], name: str) -> dict:
    for s in schemas:
        if (s.get("function") or {}).get("name") == name:
            return s
    raise KeyError(f"Schema for tool {name!r} not found")


# ── Agent factories ─────────────────────────────────────────


def make_web_agent(session_model: str) -> ReactAgent:
    return ReactAgent(
        name="web_search_agent",
        tool_type="web_search",
        llm=get_llm("web", session_model),
        system_prompt=get_web_search_prompt(),
        tools=[
            AgentTool(
                name="web_search",
                schema=WEB_SEARCH_TOOL_SCHEMA,
                handler=_h_web_search,
            ),
        ],
    )


def make_db_agent(session_model: str) -> ReactAgent:
    return ReactAgent(
        name="database_agent",
        tool_type="database",
        llm=get_llm("database", session_model),
        system_prompt=get_database_query_prompt(),
        tools=[
            AgentTool(
                name="list_tables",
                schema=_schema_by_name(DATABASE_QUERY_TOOL_SCHEMAS, "list_tables"),
                handler=_h_list_tables,
            ),
            AgentTool(
                name="describe_table",
                schema=_schema_by_name(DATABASE_QUERY_TOOL_SCHEMAS, "describe_table"),
                handler=_h_describe_table,
            ),
            AgentTool(
                name="select_rows",
                schema=_schema_by_name(DATABASE_QUERY_TOOL_SCHEMAS, "select_rows"),
                handler=_h_select_rows,
            ),
            AgentTool(
                name="distinct_values",
                schema=_schema_by_name(DATABASE_QUERY_TOOL_SCHEMAS, "distinct_values"),
                handler=_h_distinct_values,
            ),
            AgentTool(
                name="aggregate",
                schema=_schema_by_name(DATABASE_QUERY_TOOL_SCHEMAS, "aggregate"),
                handler=_h_aggregate,
            ),
        ],
    )


def make_viz_agent(session_model: str) -> ReactAgent:
    return ReactAgent(
        name="visualize_agent",
        tool_type="visualize",
        llm=get_llm("visualize", session_model),
        system_prompt=get_visualize_prompt(),
        tools=[
            # Discovery tools first — let the agent verify table/column
            # names against ClickHouse before calling bar/line/pie. Without
            # these the LLM sometimes hallucinates a table (e.g.
            # `stock_prices` instead of `stock_price_history`) and the
            # render fails with a 404 it can't recover from.
            AgentTool(
                name="list_tables",
                schema=LIST_TABLES_TOOL_SCHEMA,
                handler=_h_list_tables,
            ),
            AgentTool(
                name="describe_table",
                schema=DESCRIBE_TABLE_TOOL_SCHEMA,
                handler=_h_describe_table,
            ),
            AgentTool(
                name="bar",
                schema=_schema_by_name(VISUALIZE_TOOL_SCHEMAS, "bar"),
                handler=_h_bar,
            ),
            AgentTool(
                name="line",
                schema=_schema_by_name(VISUALIZE_TOOL_SCHEMAS, "line"),
                handler=_h_line,
            ),
            AgentTool(
                name="pie",
                schema=_schema_by_name(VISUALIZE_TOOL_SCHEMAS, "pie"),
                handler=_h_pie,
            ),
            # Fallback path — when OLAP doesn't have what the user asked
            # for, the agent runs `web_search` to harvest numbers from the
            # web, then calls `chart_from_data` with the parsed labels +
            # values to render the PNG. Web_search alone is NOT the answer:
            # the user asked for a chart, so we keep going until we have a
            # PNG (or honestly tell the collector we couldn't get one).
            AgentTool(
                name="web_search",
                schema=WEB_SEARCH_TOOL_SCHEMA,
                handler=_h_web_search,
            ),
            AgentTool(
                name="chart_from_data",
                schema=_schema_by_name(VISUALIZE_TOOL_SCHEMAS, "chart_from_data"),
                handler=_h_chart_from_data,
            ),
        ],
    )


# ── Dispatcher node — fan-out tasks → agents → AgentResults ─


async def _dispatcher_node(state: ChatState, config: RunnableConfig) -> dict:
    if state.rewrite and state.rewrite.needs_clarification:
        return {"agent_results": []}
    if not state.plan or not state.plan.tasks:
        return {"agent_results": []}

    web_agent = db_agent = viz_agent = None
    runs: list[tuple[ReactAgent, str, dict | None]] = []
    for task in state.plan.tasks:
        if task.tool_type == "web_search":
            if web_agent is None:
                web_agent = make_web_agent(state.session_model)
            runs.append((web_agent, task.goal, task.args))
        elif task.tool_type == "database":
            if db_agent is None:
                db_agent = make_db_agent(state.session_model)
            runs.append((db_agent, task.goal, task.args))
        elif task.tool_type == "visualize":
            if viz_agent is None:
                viz_agent = make_viz_agent(state.session_model)
            runs.append((viz_agent, task.goal, task.args))

    log.info("[dispatcher] running %d agent task(s) in parallel", len(runs))
    results = await run_agents_parallel(runs, config)
    return {"agent_results": results}


dispatcher_runnable = RunnableLambda(_dispatcher_node).with_config(
    run_name="dispatcher",
)
