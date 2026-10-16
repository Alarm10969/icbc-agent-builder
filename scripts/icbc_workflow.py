"""Native ICBC workflow helpers, using the user-confirmed successful schema."""
import copy
import json
import textwrap
import uuid
from pathlib import Path


def load_export(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError('Expected a one-agent native export array')
    agent = data[0]
    dsl = agent.get('workflowJson', {}).get('dsl')
    if not isinstance(dsl, dict) or not isinstance(dsl.get('nodes'), list):
        raise ValueError('Missing workflowJson.dsl.nodes')
    return agent


def field(name, kind='string', source='user', required=False, value=None, description=''):
    result = dict(name=name, type=kind, source=source, required=required,
                  reflection=False, description=description,
                  value=copy.deepcopy(value) if value is not None else {'type': 'generated'})
    if kind == 'array':
        result['schema'] = {'name': '', 'type': 'string', 'source': source}
    return result


def literal(name, content, kind='string', source='user', required=False):
    return field(name, kind, source, required,
                 {'type': 'literal', 'content': content, 'hint': ''})


def ref(name, node, var, kind='string', origin='user', source='user', required=False):
    return field(name, kind, source, required, {'type': 'ref', 'content': {
        'ref_node_id': node, 'ref_var_name': var, 'source': origin}})


def code(node_id, name, script, inputs, outputs):
    return dict(id=node_id, name=name, type='Code', inputs=inputs,
                outputs=outputs, configs={'code': textwrap.dedent(script).strip()+'\n',
                'enable_sandbox': True, 'exec_env': 'sandbox', 'isDefaultName': False})


def model(template, node_id, name, prompt, inputs, system):
    exemplar = next(n for n in template['workflowJson']['dsl']['nodes'] if n['type'] == 'LLM')
    node = copy.deepcopy(exemplar)
    node.update(id=node_id, name=name, inputs=inputs, outputs=[field('raw_output', description='该节点原始输出')])
    node['configs'].pop('exception_process', None)
    node['configs'].update(system_prompt=system, template_content=prompt,
        stream=False, response_format='text', enable_history=False,
        temperature=0.1, top_p=0.5, max_tokens=4096, isDefaultName=False)
    return node


def branch(node_id, name, cases):
    """cases: [(left_field, operator, right_field), ...]; append default."""
    branches = []
    for i, (left, operator, right) in enumerate(cases):
        branches.append({'id': 'if' if i == 0 else f'elseIf-{i}',
            'configs': {'logic': 'and', 'conditions': [
                {'left': left, 'operator': operator, 'right': right}]}})
    branches.append({'id': 'default'})
    return dict(id=node_id, name=name, type='Branch', branches=branches,
                configs={'isDefaultName': False})


def edge(source, target, port=None):
    result = {'source': source, 'target': target, 'exception_branch': False}
    if port is not None:
        result['branch'] = port
    return result


def aggregate(node_id, name, choices):
    inputs = [ref(f'candidate_{i}', n, v) for i, (n, v) in enumerate(choices)]
    return dict(id=node_id, name=name, type='Aggregation', inputs=inputs,
        outputs=[field('result')], configs={'mode': 'first-non-null',
            'isDefaultName': False, 'groups': {'result': [
                {'name': f['name'], 'index': i} for i, f in enumerate(inputs)]}})


def end(source, var='result'):
    return dict(id='node_end', name='结束', type='End',
        inputs=[ref('result', source, var)],
        outputs=[field('response_content', source='system', required=True)],
        configs={'is_stream_out': False, 'response_mode': 'directResponse',
                 'response_template': '{{result}}'})


def loop(node_id, name, source, var, body, record_node, record_var='result'):
    current = literal('current_result', '{}', source='pre_defined')
    mid = field('intermediate_loop_var', 'object', source='pre_defined', required=True,
                value={'type': 'nested', 'content': None, 'hint': ''})
    mid['schema'] = [current]
    return dict(id=node_id, name=name, type='Loop',
        inputs=[ref('arr_loop_var', source, var, 'array', source='pre_defined', required=True), mid],
        outputs=[ref('review_results', record_node, record_var, 'array', required=True)],
        configs={'loop_type': 'arrayLoop', 'loop_body': list(body), 'isDefaultName': False})


def setter(node_id, name, loop_id, source, var):
    left = ref('current_result', loop_id, 'intermediate_loop_var.current_result',
               origin='pre_defined', required=True)
    right = ref('value', source, var)
    meta = lambda f: {k: copy.deepcopy(f[k]) for k in ('type', 'value', 'schema') if k in f}
    return dict(id=node_id, name=name, type='SetVariable', inputs=[copy.deepcopy(left)],
                configs={'settings': [{'left': meta(left), 'right': meta(right)}], 'isDefaultName': False})


def new_draft(template, graph, name, description=None, prologue=None):
    """Clone the known native envelope and consistently replace new entity identity."""
    agent = copy.deepcopy(template)
    workflow = agent['workflowJson']
    flow_id, flow_code = str(uuid.uuid4()), 'homework_'+uuid.uuid4().hex
    description = description if description is not None else agent.get('agentDescription', '')
    agent.update(id=None, agentName=name, encryptedAgentName=name,
                 agentDescription=description, sopAgentId=flow_id, workflowEn=flow_code,
                 llmTemperature=0.1, llmTopP=0.5)
    dsl = workflow['dsl']
    for key in ('nodes', 'edges', 'layouts'):
        dsl[key] = copy.deepcopy(graph[key])
    dsl.update(id=flow_id, name=flow_code, description=description, comments=[])
    dsl.setdefault('configs', {}).update(copy.deepcopy(graph.get('configs', {})))
    if prologue is not None:
        dsl['configs']['prologue'] = prologue
    metadata = workflow['metadata']
    metadata.update(id=flow_id, code=flow_code, name=name, description=description,
        trace_id=flow_id, status='draft', test_status=0,
        dsl_path=f'workflow/flow/{flow_id}/{flow_id}.json',
        ir_path=f'workflow/ir/{flow_id}/{flow_id}.json')
    for key in ('last_version_id', 'published_at', 'deploy_wf_version'):
        if key in metadata:
            metadata[key] = None
    return [agent]
