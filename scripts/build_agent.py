#!/usr/bin/env python3
"""Create a fresh agent from a confirmed recipe or a native custom graph."""
import argparse
import copy
import json
from pathlib import Path

from icbc_workflow import end, edge, load_export, model, new_draft, ref
from validate_agent import validate

ASSETS = Path(__file__).resolve().parents[1] / 'assets'


def build(recipe, name, template_path=None, graph_path=None, description=None, prologue=None):
    baseline = load_export(ASSETS / ('requirements-audit.json' if recipe == 'requirements-audit' else 'document-review.json'))
    template = load_export(template_path) if template_path else baseline
    if graph_path:
        graph = json.loads(Path(graph_path).read_text(encoding='utf-8-sig'))
    elif recipe == 'basic':
        start = copy.deepcopy(next(n for n in template['workflowJson']['dsl']['nodes'] if n['type'] == 'Start'))
        if start['id'] != 'node_start':
            raise ValueError('Native Start must use node_start for the basic recipe')
        llm = model(template, 'node_model', '处理用户请求', '{{query}}',
                    [ref('query', 'node_start', 'query', origin='system')],
                    '按用户指定的业务目标完成任务；信息不足时说明缺失内容，不编造事实。')
        graph = {'nodes': [start, llm, end('node_model', 'raw_output')],
                 'edges': [edge('node_start', 'node_model'), edge('node_model', 'node_end')],
                 'layouts': {'node_start': {'x': 0, 'y': 0}, 'node_model': {'x': 400, 'y': 0}, 'node_end': {'x': 800, 'y': 0}},
                 'configs': {'variables': [], 'suggest_queries': [], 'prologue': '请提交需要处理的内容。'}}
    else:
        bd = baseline['workflowJson']['dsl']
        graph = {k: copy.deepcopy(bd[k]) for k in ('nodes', 'edges', 'layouts')}
        graph['configs'] = {k: copy.deepcopy(bd.get('configs', {}).get(k, [] if k != 'prologue' else ''))
                            for k in ('variables', 'suggest_queries', 'prologue')}
    if template_path:
        native_start = next(n for n in template['workflowJson']['dsl']['nodes'] if n['type'] == 'Start')
        native_llm = next(n for n in template['workflowJson']['dsl']['nodes'] if n['type'] == 'LLM')
        for n in graph['nodes']:
            if n['type'] == 'Start':
                n['outputs'] = copy.deepcopy(native_start['outputs'])
            elif n['type'] == 'LLM':
                n['configs']['model'] = copy.deepcopy(native_llm['configs']['model'])
        native_configs = template['workflowJson']['dsl'].get('configs', {})
        if 'default_model' in native_configs:
            graph.setdefault('configs', {})['default_model'] = copy.deepcopy(native_configs['default_model'])
    if description is None and not graph_path and recipe != 'basic':
        description = baseline.get('agentDescription', '')
    data = new_draft(template, graph, name, description, prologue)
    errors, warnings = validate(data)
    if errors:
        raise ValueError('\n'.join(errors))
    return data, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', choices=['document-review', 'requirements-audit', 'basic'], default='basic')
    parser.add_argument('--template', type=Path)
    parser.add_argument('--graph', type=Path)
    parser.add_argument('--name', required=True)
    parser.add_argument('--description')
    parser.add_argument('--prologue')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        data, warnings = build(args.recipe, args.name, args.template, args.graph, args.description, args.prologue)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    except (ValueError, KeyError, StopIteration, OSError) as exc:
        parser.exit(1, f'Build failed: {exc}\n')
    print(json.dumps({'file': str(args.output.resolve()), 'warnings': warnings,
                      'validation': 'local_structure_only'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
