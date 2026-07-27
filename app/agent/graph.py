from functools import partial

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agent.nodes import NodeDeps, finalize_node, ingest_node, migrate_file_node, plan_node
from app.agent.state import GraphState


def route_after_migrate(state: GraphState) -> str:
    if state["cursor"] >= len(state["plan"]):
        return "finalize"
    return "migrate_file"


def build_graph(deps: NodeDeps):
    graph = StateGraph(GraphState)
    graph.add_node("ingest", partial(ingest_node, deps=deps))
    graph.add_node("plan", partial(plan_node, deps=deps))
    graph.add_node("migrate_file", partial(migrate_file_node, deps=deps))
    graph.add_node("finalize", partial(finalize_node, deps=deps))

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "plan")
    graph.add_conditional_edges("plan", route_after_migrate, {
        "migrate_file": "migrate_file",
        "finalize": "finalize",
    })
    graph.add_conditional_edges("migrate_file", route_after_migrate, {
        "migrate_file": "migrate_file",
        "finalize": "finalize",
    })
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=MemorySaver())
