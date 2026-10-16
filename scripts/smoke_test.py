from validate_agent import validate
import ast
import copy
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'assets'
DOCUMENT_PATH = ROOT / 'document-review.json'
REQUIREMENTS_PATH = ROOT / 'requirements-audit.json'


def check_structure(path):
    export = json.loads(path.read_text())
    assert isinstance(export,list) and len(export)==1
    errors, warnings = validate(export)
    assert not errors, errors
    a=export[0]
    d=a['workflowJson']['dsl']
    ids={n['id'] for n in d['nodes']}
    nodes={n['id']:n for n in d['nodes']}
    assert len(ids)==len(d['nodes'])
    assert len([n for n in nodes.values() if n['type']=='Start'])==1
    assert len([n for n in nodes.values() if n['type']=='End'])==1
    assert a['sopAgentId']==d['id']==a['workflowJson']['metadata']['id']
    assert a['workflowEn']==d['name']==a['workflowJson']['metadata']['code']
    assert ids <= d['layouts'].keys()
    for e in d['edges']:
        assert e['source'] in ids and e['target'] in ids,e
        if nodes[e['source']]['type']=='Branch':
            assert e.get('branch') in {b['id'] for b in nodes[e['source']]['branches']}
    def refs(v):
        if isinstance(v,dict):
            if 'ref_node_id' in v:
                assert v['ref_node_id'] in ids,v
                target=nodes[v['ref_node_id']]
                var=v['ref_var_name'].split('.')[0]
                exposed={f['name'] for f in target.get('outputs',[])}
                if target['type']=='Loop':
                    exposed |= {'index','arr_loop_var','intermediate_loop_var'}
                assert var in exposed,(target['id'],var,exposed)
            for x in v.values():refs(x)
        elif isinstance(v,list):
            for x in v:refs(x)
    refs(d)
    loop_bodies=set()
    for n in nodes.values():
        if n['type']=='Code':ast.parse(n['configs']['code'])
        if n['type']=='Loop':
            body=set(n['configs']['loop_body'])
            assert body <= ids and n['id'] not in body
            loop_bodies |= body | {n['id']+'_input',n['id']+'_output'}
            assert n['id']+'_input' in ids and n['id']+'_output' in ids
    reached={'node_start'}
    for _ in range(len(ids)+1):
        for e in d['edges']:
            if e['source'] in reached:reached.add(e['target'])
        for n in nodes.values():
            if n['type']=='Loop' and n['id'] in reached:
                reached.add(n['id']+'_input')
    assert reached==ids,ids-reached
    return d


class Simulator:
    """Local graph smoke test. LLM outputs are injected fixtures, not actual model calls."""
    def __init__(self,d,fixtures):
        self.d=d
        self.nodes={n['id']:n for n in d['nodes']}
        self.fixtures=copy.deepcopy(fixtures)
        self.calls=Counter()
        self.pool={}
        self.events=[]

    def value(self,f):
        v=f['value']
        if v['type']=='literal':return v.get('content')
        if v['type']=='nested':return {x['name']:self.value(x) for x in f['schema']}
        if v['type']=='ref':
            c=v['content']
            out=self.pool.get(c['ref_node_id'],{})
            for bit in c['ref_var_name'].split('.'):
                out=out.get(bit) if isinstance(out,dict) else None
            return out
        return None

    def args(self,n):return {f['name']:self.value(f) for f in n.get('inputs',[])}

    def render(self,text,args):
        return re.sub(r'\{\{(.*?)\}\}',lambda m:str(args.get(m.group(1),'')),text)

    def run(self,inputs):
        start=self.nodes['node_start']
        self.pool['node_start']={f['name']:inputs.get(f['name'],f.get('value',{}).get('content')) for f in start['outputs']}
        nxt=next(e['target'] for e in self.d['edges'] if e['source']=='node_start')
        return self.walk(nxt)

    def walk(self,node_id,stop=None):
        for _ in range(10000):
            if node_id==stop:return
            n=self.nodes[node_id]
            typ=n['type']
            args=self.args(n)
            selected=None
            if typ=='Code':
                scope={}
                exec(n['configs']['code'],scope)
                out=scope['main'](args)
                assert isinstance(out,dict)
                assert {f['name'] for f in n['outputs']} <= out.keys(),n['id']
                for f in n['outputs']:
                    val=out[f['name']]
                    expected={'string':str,'boolean':bool,'integer':int,'array':list}.get(f['type'])
                    if expected:assert isinstance(val,expected),(n['id'],f['name'],val)
                self.pool[node_id]=out
            elif typ=='LLM':
                self.calls[node_id]+=1
                fixture=self.fixtures[node_id]
                if isinstance(fixture,list):
                    out=fixture.pop(0)
                else:out=fixture
                self.pool[node_id]={'raw_output':out if isinstance(out,str) else json.dumps(out,ensure_ascii=False)}
            elif typ=='Branch':
                selected='default'
                for b in n['branches']:
                    if b['id']=='default':continue
                    c=b['configs']['conditions'][0]
                    if self.value(c['left'])==self.value(c['right']):
                        selected=b['id'];break
            elif typ=='Message':
                text=self.render(n['configs']['template'],args)
                self.events.append(text)
                self.pool[node_id]={'result':text}
            elif typ=='Aggregation':
                vals=[args[x['name']] for x in sorted(n['configs']['groups']['result'],key=lambda x:x['index'])]
                self.pool[node_id]={'result':next((v for v in vals if v is not None),None)}
            elif typ=='End':
                return self.render(n['configs']['response_template'],args)
            elif typ=='SetVariable':
                for s in n['configs']['settings']:
                    c=s['left']['value']['content']
                    bits=c['ref_var_name'].split('.')
                    target=self.pool[c['ref_node_id']]
                    for bit in bits[:-1]:target=target[bit]
                    target[bits[-1]]=self.value(s['right'])
            elif typ=='Loop':
                arrays=args['arr_loop_var']
                self.pool[node_id]={'intermediate_loop_var':args.get('intermediate_loop_var',{})}
                collected=[]
                for i,item in enumerate(arrays):
                    for bid in n['configs']['loop_body']:self.pool.pop(bid,None)
                    self.pool[node_id].update(arr_loop_var={'item':item},index=i)
                    self.walk(node_id+'_input',node_id+'_output')
                    collected.append(self.value(n['outputs'][0]))
                self.pool[node_id]['review_results']=collected
            elif typ in ['LoopInput','LoopOutput']:
                pass
            else:raise AssertionError(typ)
            successors=[e for e in self.d['edges'] if e['source']==node_id and (selected is None or e.get('branch')==selected)]
            assert len(successors)==1,(node_id,successors)
            node_id=successors[0]['target']
        raise AssertionError('Graph did not terminate')


DOC_INPUT={'公文类型':'工作请示','公文原文':'申请采购服务器设备，预算金额25万元，计划11月上线，用于支撑项目管理系统三期开发，申请部门为科技部。'}
DOC_GOOD={'初审状态':'待核对','初审结论':'材料基本完整，需进一步核实','关键要素':{'预算金额':'25万元','申请部门':'科技部'},
          '待核对要点清单':['采购预算25万元是否履行立项审批','采购的时间安排是否匹配项目排期'],
          '审阅说明':'请示要素齐全，预算、用途、申请部门均已写明，建议重点核查立项审批手续。'}
ITEMS=[{'需求ID':'R001','需求名称':'项目驾驶舱报表展示','需求描述':'开发项目驾驶舱，展示各团队在研项目数量与变化趋势，用于管理监控。','提出部门':'科技部'},
       {'需求ID':'R002','需求名称':'权限改造','需求描述':'','提出部门':'科技部'},
       {'需求ID':'R003','需求名称':'日志查询功能','需求描述':'做一个日志页面，查询日志。','提出部门':'科技部'}]
PASS={'核验状态':'核验通过','核验说明':'需求要素完整，描述清晰，符合基础编写规范','优化建议':[]}
PENDING={'核验状态':'待优化','核验说明':'需求描述过于笼统，缺少查询范围等关键信息','优化建议':['补充日志类型、查询范围、筛选条件和适用的验收要求']}


def verify():
    d1=check_structure(DOCUMENT_PATH)
    d2=check_structure(REQUIREMENTS_PATH)
    cases=[]
    def record(name):cases.append(name)
    def run1(data,fixture=DOC_GOOD):
        s=Simulator(d1,{'node_review':fixture})
        r=json.loads(s.run(data));return s,r
    s,r=run1({'query':json.dumps(DOC_INPUT,ensure_ascii=False)})
    assert r['公文类型']=='工作请示' and len(r['待核对要点清单'])==2 and s.calls['node_review']==1
    assert len(r)==4 and s.pool['node_format']['key_info']
    record('公文样例：模板匹配、关键变量、差异化提示和四字段JSON')
    for query in [{'公文类型':'工作请示','公文原文':'  '},{'公文类型':'未知类型','公文原文':'正文'},[],None]:
        s,r=run1({'query':json.dumps(query,ensure_ascii=False)})
        assert r['初审结论']=='入参异常' and not s.calls
    record('公文非法类型、空正文、非对象和null：直接结束，不调用模型')
    s,r=run1({'query':json.dumps({'doc_type':'请示','doc_text':'申请采购设备'},ensure_ascii=False)})
    assert r['公文类型']=='工作请示'
    s,r=run1({'query':'公文类型：工作请示\n公文原文：申请采购设备'})
    assert r['公文类型']=='工作请示'
    record('公文JSON英文键、类型别名和中文样例文本输入')
    sample='公文类型：工作请示\n公文原文：'+DOC_INPUT['公文原文']
    for q in [sample, '```\n'+sample+'\n\n```', sample.replace('\n',' '),
              sample.replace('类型：','类型是'), sample.replace('工作请示','“工作请示”'),
              sample.replace('类型：','类型:'), json.dumps({'query':sample},ensure_ascii=False)]:
        s,r=run1({'query':q})
        assert r['公文类型']=='工作请示' and s.pool['node_validate']['valid']
        assert s.pool['node_validate']['doc_text']==DOC_INPUT['公文原文']
    record('用户原样输入及围栏、同行、中英文冒号、引号、query包装：均识别工作请示')
    s,r=run1({'query':''})
    assert '未读取到公文类型' in r['审阅说明'] and not s.calls
    record('公文类型未取到时明确报输入读取问题，不混报为不支持的类型')
    s,r=run1({'query':json.dumps(DOC_INPUT)},'not json')
    assert s.pool['node_format']['status']=='需人工复核'
    record('公文模型输出异常：人工复核，不误判通过')
    for wrapper in [lambda x:json.dumps(x,ensure_ascii=False),lambda x:'```json\n'+json.dumps(x,ensure_ascii=False)+'\n```',lambda x:'<think>思考</think>'+json.dumps(x,ensure_ascii=False)]:
        s,r=run1({'query':json.dumps(DOC_INPUT)},wrapper(DOC_GOOD))
        assert r['初审结论']==DOC_GOOD['初审结论']
    record('模型输出JSON、围栏JSON与think标签解析')
    def run2(items,item_models,summary='先补齐R002需求描述；为R003明确查询范围、筛选条件及验收规则。'):
        s=Simulator(d2,{'node_item_llm':item_models,'node_advice_llm':summary})
        report=s.run({'query':json.dumps(items,ensure_ascii=False)})
        return s,report
    s,r=run2(ITEMS,[PASS,PENDING])
    assert '总条目3条，合规1条，待优化1条，信息不全1条' in r
    assert s.calls==Counter({'node_item_llm':2,'node_advice_llm':1})
    assert s.pool['node_summary']['total']==sum(s.pool['node_summary'][k] for k in ('passed','pending','incomplete'))
    record('需求作业样例：3条/合规1/待优化1/信息不全1，缺失条目跳过模型')
    all_pass=[ITEMS[0],{**ITEMS[0],'需求ID':'R004'}]
    s,r=run2(all_pass,[PASS,PASS])
    assert '全部需求核验通过' in r and s.calls['node_advice_llm']==0
    record('需求全部通过：不调用汇总模型')
    s,r=run2([ITEMS[1]],[])
    assert s.calls==Counter() and '信息不全1条' in r
    record('需求仅缺字段：不调用条目或汇总模型')
    s,r=run2([ITEMS[1],ITEMS[0]],[PASS])
    assert s.pool['node_summary']['passed']==1 and s.pool['node_summary']['incomplete']==1
    record('循环分支切换：不复用上一轮的缺失结果')
    s,r=run2([ITEMS[0],ITEMS[1]],[PASS])
    assert s.pool['node_summary']['passed']==1 and s.pool['node_summary']['incomplete']==1
    record('循环反向分支切换：不复用上一轮的模型结果')
    s,r=run2([ITEMS[0],ITEMS[0]],[PASS,PASS])
    assert s.pool['node_summary']['pending']==2 and '重复' in r
    record('重复需求ID：保留两条并标为待优化')
    s,r=run2([None,{},'字符串',{'需求ID':0}],[])
    assert s.pool['node_summary']['incomplete']==4 and not s.calls
    record('非对象条目及字段类型错误：逐条标记信息不全')
    s,r=run2([ITEMS[0]],['bad model output'])
    assert s.pool['node_summary']['pending']==1
    record('需求模型格式异常：标为待优化，保留人工复核提示')
    s,r=run2([],[])
    assert '需求清单为空' in r and not s.calls
    record('空清单：输出零条统计和空清单提示')
    s,r=run2({'wrong':'shape'},[])
    assert '输入异常' in r and not s.calls
    record('错误根结构：直接输出异常报告')
    print('PASS:',len(cases),'local checks; LLM fixtures only; platform import unverified')
    for c in cases:print(c)
    return s,r


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='Fixture-only graph simulation; never calls the bank model')
    p.add_argument('--document', type=Path, default=DOCUMENT_PATH)
    p.add_argument('--requirements', type=Path, default=REQUIREMENTS_PATH)
    args=p.parse_args()
    DOCUMENT_PATH, REQUIREMENTS_PATH = args.document, args.requirements
    verify()
