# 原生结构与兼容规则

## 证据范围

以本技能两份资产为优先实现样本。它们于2026-10-16由当前用户确认可以导入；确认针对当时的行内平台环境。保留本用户原导出中的模型与环境标识用于同环境复用；换账号、租户、模型部署或平台版本时优先提供该环境的新导出，通过`--template`绑定。不要把本地模拟等同于调用行内模型，也不要声称已经覆盖平台所有模块。

最接近的公开参照为`openJiuwen-ai/agent-studio`的`studio-2.0-dev`原生工作流结构。只将其当作结构排查线索，不断言工行平台必然来自该项目，也不以标准Dify导入格式替换行内JSON。新节点必须使用用户导出的实例。

## 导入封装

根对象是只含一个智能体的数组。新建草稿使用以下一致映射，由`new_draft()`统一生成：

| 位置 | 新建值 |
|---|---|
| 外层`id` | `null` |
| 外层`agentName`、`encryptedAgentName`、metadata.name | 用户可见中文名 |
| 外层`sopAgentId`、dsl.id、metadata.id、metadata.trace_id | 同一个新UUID |
| 外层`workflowEn`、dsl.name、metadata.code | 同一个新英文内部code |
| metadata.status | `draft` |
| metadata.last_version_id、published_at、deploy_wf_version | 存在时置`null` |
| metadata.dsl_path | `workflow/flow/<UUID>/<UUID>.json` |
| metadata.ir_path | `workflow/ir/<UUID>/<UUID>.json` |

成功方案确实会生成新的UUID和路径，不能笼统规定“ID和OBS路径一律不许修改”。关键是新实体语义一致，保留真实平台封装、模型配置以及相关资源依赖。外层头像、来源标志、租户、部门、引擎等没有业务修改需要时沿用原导出。不得伪造可用的模型部署标识。

`workflowJson`除`dsl`和`metadata`外还可能有`cards/functions/import_type/mcp_servers/plugins/share_reference/sub_workflows`。同环境修改时保留真实依赖；不要凭空新增、删除插件或子流程引用。本技能两个基线没有这些外部依赖。生成器不自动复制外部服务资源。

## 节点与参数

节点采用`id/name/type/inputs/outputs/configs`；布局位于`dsl.layouts[node_id]`。标准连线示例：

```json
{"source":"node_check","target":"node_model","branch":"if","exception_branch":false}
```

原生引用参数：

```json
{"name":"doc_type","type":"string","source":"user","required":false,"reflection":false,"description":"","value":{"type":"ref","content":{"ref_node_id":"node_parse","ref_var_name":"doc_type","source":"user"}}}
```

字面量使用`value.type=literal`、`content`和`hint`；节点输出使用`value.type=generated`。`array`另带元素`schema`。字段自身的`source`和`value.content.source`是不同层次，不要混改。系统query引用的内层source为`system`；循环内置变量引用的内层source为`pre_defined`。

开始节点的系统`query`承接当前聊天输入。自定义开始参数是独立字段，不会自动从“公文类型：工作请示……”这句话填充。`sys.conversation_history`是历史对象，也不应当成当前公文。推荐从query用代码解析明确字段；确需自然语言识别时增加模型识别节点，随后执行确定性必要校验。

## 条件与汇合

Branch的`branches`依次使用`if`、`elseIf-1`、`elseIf-2`、`default`。非默认分支包含`configs.logic`和`conditions`，条件含`operator/left/right`。连线的`branch`必须精确等于分支id。不要使用`branch_0/branch_1`，即使JSON引用彼此一致，画布也可能无法恢复端口。

Aggregation示例使用`configs.mode=first-non-null`，把互斥分支候选放在同一`groups.result`，每项`name`对应输入参数名，`index`表示顺序；输出`result`。它不是字符串拼接。空字符串可能被视为非null，因此让被执行分支返回有效结果，不依赖用空字符串跳过候选。优先选择最具体的完整报告，再放无需模型的基础报告或错误报告。

End只保留一个。输入`result`引用聚合节点输出，`configs.response_mode=directResponse`、`response_template={{result}}`、`is_stream_out=false`，输出系统`response_content`。聊天显示JSON文本与接口返回JSON对象不同；两个已确认基线使用字符串中的有效JSON或报告文本，不能声称已经具备对象类型API输出。

## 模型与代码

从真实LLM复制`configs.model`和顶层`default_model`，包含部署id、名称和类型。使用非流式文本输出，节点公开`raw_output`字符串；通过后续Code提取约定JSON字段。输入参数名必须与提示词里的`{{变量}}`一致。

不要仅添加一个名为`raw_output`的输入，就假定上游会产生内容。先核对上游实际输出声明与运行日志，再核对引用。

已核对的公开适配器约束：非流式、非JSON格式的LLM只允许中断型异常处理。`response_format=text`配`exception_process.handle_type=errorbranch`可能导致编译失败。当前成功基线不添加`exception_process`或模型异常边。不要为了容错随意改成额外异常分支；需要新异常能力时先取得当前平台的原生配置实例并验证。

Code配置使用`exec_env=sandbox`、`enable_sandbox=true`，入口`main(args: dict) -> dict`。声明的输出名称与返回字典键一致。导入JSON中的代码是可执行内容：结构校验只做AST解析，只有已审阅且为本次编写的脚本才允许本地模拟执行。

## 循环与变量赋值

参考`assets/requirements-audit.json`：

1. Loop的`configs.loop_type=arrayLoop`，`loop_body`列出本轮业务节点。
2. 显式加入`<loop_id>_input`类型LoopInput和`<loop_id>_output`类型LoopOutput；加布局和内部实际连线。仅放锚点布局不够。
3. Loop输入`arr_loop_var`为array，`source=pre_defined`、`required=true`，引用解析节点的字符串数组；数组元素各自保存一个条目JSON。
4. Loop输入`intermediate_loop_var`为object、`value.type=nested`，schema包含`current_result`字符串字面量`{}`；子字段source为`pre_defined`。
5. 本轮条目引用`arr_loop_var.item`。循环中间结果引用`intermediate_loop_var.current_result`，引用内层source为`pre_defined`。
6. SetVariable既要有`inputs`左值引用，也要有`configs.settings`。settings的left/right保留`type/value/schema`元数据，避免只写settings导致画布显示未配置。
7. 完整和不完整两路都先赋值本轮`current_result`，再进入记录Code。必须覆盖每轮所有路径，避免带入上一轮结果。
8. Loop输出`review_results`为array、`required=true`，引用记录Code的字符串输出。循环外汇总代码解析这个字符串数组。

不要让字段缺失条目进入规范性模型；用代码直接标“信息不全”。统计在循环后由代码完成，只在存在“待优化”条目时进入统一建议模型，全通过路径直接输出报告。
