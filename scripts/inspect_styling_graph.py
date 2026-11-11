"""Inspect the actual compiled styling graph without models, photos or credentials."""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.styling.workflow import StylingWorkflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--format', choices=['mermaid', 'json'], default='mermaid')
    parser.add_argument('--output', type=Path, help='Optional UTF-8 output file')
    args = parser.parse_args()
    # Drawing the graph never executes its tools or calls the configured planner.
    workflow = StylingWorkflow(registry=None)
    graph = workflow.graph.get_graph()
    if args.format == 'mermaid':
        result = graph.draw_mermaid()
    else:
        result = json.dumps({
            'engine': 'langgraph', 'version': version('langgraph'),
            'max_decisions': workflow.MAX_DECISIONS, 'max_tools': workflow.MAX_TOOLS,
            'checkpointer': False, 'nodes': list(graph.nodes),
            'edges': [{'source': edge.source, 'target': edge.target, 'conditional': edge.conditional}
                      for edge in graph.edges],
        }, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + '\n', encoding='utf-8')
    else:
        print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
