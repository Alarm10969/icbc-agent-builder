#!/usr/bin/env python3
"""Check native structure and Python syntax; never execute code from an export."""
import argparse
import ast
import json
import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path

KNOWN = {'Start', 'End', 'Code', 'LLM', 'Branch', 'Message', 'Aggregation',
         'SetVariable', 'Loop', 'LoopInput', 'LoopOutput'}


def validate(data, require_new=True):
    errors, warnings = [], []
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return ['Root must be a one-agent array'], []
    a = data[0]
    w = a.get('workflowJson', {})
    d, m = w.get('dsl', {}), w.get('metadata', {})
    if not isinstance(d, dict) or not isinstance(d.get('nodes'), list):
        return ['Missing workflowJson.dsl.nodes'], []
    raw_nodes, edges = d['nodes'], d.get('edges', [])
    if not raw_nodes or not all(isinstance(n, dict) and n.get('id') for n in raw_nodes):
        return ['Nodes must be nonempty objects with ids'], []
    nodes = {n['id']: n for n in raw_nodes}
    if len(nodes) != len(raw_nodes):
        errors.append('Duplicate node ids')
    for typ in ('Start', 'End'):
        if sum(n.get('type') == typ for n in raw_nodes) != 1:
            errors.append(f'Exactly one {typ} required')
    if len({a.get('sopAgentId'), d.get('id'), m.get('id'), m.get('trace_id')}) != 1:
        errors.append('Workflow UUIDs disagree between outer/dsl/metadata/trace')
    if len({a.get('workflowEn'), d.get('name'), m.get('code')}) != 1:
        errors.append('Workflow internal codes disagree')
    if require_new:
        if a.get('id') is not None or m.get('status') != 'draft':
            errors.append('New agent must have outer id=null and metadata status=draft')
        try:
            uuid.UUID(d.get('id', ''))
        except (ValueError, TypeError, AttributeError):
            errors.append('Workflow id must be a UUID')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', d.get('name', '')):
            errors.append('Internal workflow code must be an English identifier')
        for key in ('last_version_id', 'published_at', 'deploy_wf_version'):
            if m.get(key) is not None:
                errors.append(f'Stale published metadata: {key}')
        for key, part in [('dsl_path', 'flow'), ('ir_path', 'ir')]:
            expected = f"workflow/{part}/{d.get('id')}/{d.get('id')}.json"
            if m.get(key) != expected:
                errors.append(f'{key} does not match new workflow UUID')
    layouts = d.get('layouts', {})
    for nid in nodes:
        if nid not in layouts:
            errors.append(f'{nid}: missing layout')
    outgoing = defaultdict(list)
    if not isinstance(edges, list):
        return errors+['Edges must be an array'], warnings
    for e in edges:
        if not isinstance(e, dict) or e.get('source') not in nodes or e.get('target') not in nodes:
            errors.append(f'Dangling/malformed edge: {e}')
            continue
        outgoing[e['source']].append(e)

    def check_ref(c):
        nid, path = c.get('ref_node_id'), c.get('ref_var_name', '')
        target = nodes.get(nid)
        if not target:
            errors.append(f'Reference targets missing node: {nid}')
            return
        exposed = {f.get('name') for f in target.get('outputs', [])}
        if target.get('type') == 'Loop':
            exposed |= {'arr_loop_var', 'intermediate_loop_var', 'index'}
        if not isinstance(path, str) or path.split('.')[0] not in exposed:
            errors.append(f'Reference targets missing output: {nid}.{path}')
        elif target.get('type') == 'Loop' and path.startswith('intermediate_loop_var.'):
            mid = next((f for f in target.get('inputs', []) if f.get('name') == 'intermediate_loop_var'), {})
            names = {f.get('name') for f in mid.get('schema', [])}
            if path.split('.')[1] not in names:
                errors.append(f'Unknown loop intermediate variable: {nid}.{path}')

    def walk(value):
        if isinstance(value, dict):
            if 'ref_node_id' in value:
                check_ref(value)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(d)
    for nid, n in nodes.items():
        typ, c = n.get('type'), n.get('configs', {})
        if typ not in KNOWN:
            warnings.append(f'{nid}: unverified node type {typ}; use a native exemplar')
        for group in ('inputs', 'outputs'):
            fs = n.get(group, [])
            names = [f.get('name') for f in fs]
            if any(not x for x in names) or len(set(names)) != len(names):
                errors.append(f'{nid}: blank/duplicate {group} names')
            for f in fs:
                if f.get('type') == 'array' and not f.get('schema'):
                    errors.append(f"{nid}.{f.get('name')}: missing array schema")
        input_names = {f.get('name') for f in n.get('inputs', [])}
        if typ == 'Start' and any(f.get('source') == 'user' for f in n.get('outputs', [])):
            warnings.append('Start has custom fields; chat query does not automatically populate them')
        if typ == 'Code':
            try:
                tree = ast.parse(c.get('code', ''))
                if not any(isinstance(x, ast.FunctionDef) and x.name == 'main' for x in tree.body):
                    errors.append(f'{nid}: missing main(args) function')
            except SyntaxError as exc:
                errors.append(f'{nid}: Python syntax error: {exc.msg}')
            if c.get('enable_sandbox') is not True or c.get('exec_env') != 'sandbox':
                errors.append(f'{nid}: expected sandbox Code configuration')
        if typ == 'Branch':
            ids = [b.get('id') for b in n.get('branches', [])]
            if not ids or ids[0] != 'if' or ids[-1] != 'default' or len(set(ids)) != len(ids):
                errors.append(f'{nid}: invalid branch ports')
            if ids and ids[1:-1] != [f'elseIf-{i}' for i in range(1, len(ids)-1)]:
                errors.append(f'{nid}: elseif ids must use elseIf-1, elseIf-2, ...')
            actual = Counter(e.get('branch') for e in outgoing[nid])
            if actual != Counter(ids):
                errors.append(f'{nid}: branch edges do not match configured ports')
        if typ == 'LLM':
            if not c.get('model'):
                errors.append(f'{nid}: missing native model configuration')
            ex = c.get('exception_process') or {}
            handle = str(ex.get('handle_type', '')).lower()
            if c.get('stream') is False and c.get('response_format', 'text').lower() != 'json' and handle not in ('', 'interrupt'):
                errors.append(f'{nid}: nonstream text LLM only supports interrupt exception handling')
            if c.get('stream') and (handle not in ('', 'interrupt') or ex.get('retry_times', 0)):
                errors.append(f'{nid}: streaming LLM has incompatible exception settings')
        if typ in ('LLM', 'Message', 'End'):
            for key in ('system_prompt', 'template_content', 'template', 'response_template'):
                for var in re.findall(r'\{\{\s*([A-Za-z_][\w.]*)\s*\}\}', c.get(key, '')):
                    if var.split('.')[0] not in input_names:
                        errors.append(f'{nid}: template variable {var} has no input mapping')
        if typ == 'Aggregation':
            if not c.get('groups'):
                errors.append(f'{nid}: aggregation groups missing')
            for name, members in c.get('groups', {}).items():
                if not members or any(x.get('name') not in input_names for x in members):
                    errors.append(f'{nid}.{name}: invalid aggregation members')
        if typ == 'SetVariable':
            if not n.get('inputs') or not c.get('settings'):
                errors.append(f'{nid}: SetVariable requires inputs AND settings')
            for s in c.get('settings', []):
                if not s.get('left', {}).get('value') or not s.get('right', {}).get('value'):
                    errors.append(f'{nid}: incomplete assignment')
        if typ == 'Loop':
            body = c.get('loop_body', [])
            anchors = [nid+'_input', nid+'_output']
            if any(x not in nodes for x in body+anchors) or nid in body:
                errors.append(f'{nid}: missing loop body/anchors or recursive body')
            for anchor, expected in zip(anchors, ('LoopInput', 'LoopOutput')):
                if nodes.get(anchor, {}).get('type') != expected:
                    errors.append(f'{nid}: {expected} anchor missing')
            if not outgoing[anchors[0]] or not any(e.get('target') == anchors[1] for e in edges):
                errors.append(f'{nid}: loop inner entry/exit is disconnected')
            if not n.get('outputs') or not n.get('inputs') or not all(f.get('required') for f in n.get('inputs', [])+n.get('outputs', [])):
                errors.append(f'{nid}: loop input/output required flags missing')
            for f in n.get('outputs', []):
                if f.get('value', {}).get('content', {}).get('ref_node_id') not in body:
                    errors.append(f'{nid}: loop output must collect a body-node output')
    reached = {nid for nid, n in nodes.items() if n.get('type') == 'Start'}
    for _ in range(len(nodes)+1):
        before = len(reached)
        for nid in list(reached):
            reached.update(e['target'] for e in outgoing[nid])
            if nodes[nid].get('type') == 'Loop' and nid+'_input' in nodes:
                reached.add(nid+'_input')
        if len(reached) == before:
            break
    if set(nodes)-reached:
        errors.append('Unreachable nodes: '+', '.join(sorted(set(nodes)-reached)))
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--existing', action='store_true', help='Allow native existing/published identity')
    args = parser.parse_args()
    try:
        errors, warnings = validate(json.loads(args.file.read_text(encoding='utf-8-sig')), not args.existing)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors, warnings = [f'Malformed export: {exc}'], []
    print(json.dumps({'ok': not errors, 'errors': errors, 'warnings': warnings,
        'scope': 'local_structure_and_ast_only'}, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
