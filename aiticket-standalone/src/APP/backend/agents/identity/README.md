# Agent 配置索引（自动生成于 2026-04-27 09:24）

> 由 `python3 scripts/agent_memory_cli.py index` 生成，勿手动编辑。
> 规范详见 `_local/design/specs/MC-AGENT-MEMORY-V1.0.md`

## 矩阵

| Agent                | scope    | trigger                   | tool_chain 数 | guidelines 数 |
|----------------------|----------|---------------------------|---------------|---------------|
| adopted              | shared   | on_pattern_confirmed      |             5 |             5 |
| claude               | private  | on_discovery              |             7 |             5 |
| competitor           | shared   | on_discovery              |             5 |             5 |
| darwin               | private  | on_evaluation             |             7 |             5 |
| handover_suggest     | shared   | on_adoption               |             5 |             5 |
| kb_fact              | shared   | on_pattern_confirmed      |             5 |             5 |
| reply                | private  | on_adoption               |             7 |             5 |

## 公共记忆写入者（scope=shared，慎重新增）
- adopted, competitor, handover_suggest, kb_fact

## 私有记忆写入者（scope=private）
- claude, darwin, reply

## 用户级记忆写入者（scope=user）
- 无

## 操作手册

```bash
# 查看矩阵
python3 scripts/agent_memory_cli.py ls

# 查看某 agent 详情
python3 scripts/agent_memory_cli.py show <agent_name>

# 校验全部 YAML
python3 scripts/agent_memory_cli.py validate all

# 调试某 agent 的 L3 recall
python3 scripts/agent_memory_cli.py recall <agent> "查询关键词"

# 手动写入一条 L3 记忆
python3 scripts/agent_memory_cli.py remember <agent> "记忆内容"

# 清空某 agent 私有记忆（带确认）
python3 scripts/agent_memory_cli.py clear <agent> --scope private

# 重生此索引
python3 scripts/agent_memory_cli.py index
```

## 接入新 Agent 的 6 步 checklist

1. 复制 `agents/identity/_schema.yaml` → `agents/identity/{name}.yaml`，按注释填写
2. 写 `agents/{name}_agent.py` 继承 `BaseAgent`（自带 Mixin）
3. `run_task` 内按 trigger 调 `self.remember(scope=...)`
4. 推理前调 `self.recall_both(query)` 注入 prompt
5. `registry.py` 注册 Agent 类（参考现有写法）
6. 跑 `python3 scripts/agent_memory_cli.py validate {name}` → 通过才提交
