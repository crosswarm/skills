# JobMaster 决策守则 — 待审稿（来源：multica-ai 研究）

> 状态：pending — 未经用户 approve 不得进入运行时
> 生成日期：2026-05-02
> 来源：multica-research-20260502.md + multica-vs-aiticket-20260502.md
> 审批路径：agents.html → action_required → jobmaster_playbook_review → approve

---

## 守则集（格式：当 X 时，应当 Y，因为 Z）

### 孤儿任务恢复（来源：§2 生命周期）

**守则 R1**：当 daemon 启动或重启时，应当扫描所有 `status='running'` 且 `started_at` 超过 30 分钟的任务并将其置为 `FAILED`（`error_kind=daemon_restart_recovery`），因为 daemon 崩溃后这些任务永远不会自行完成，只会卡住后续依赖它们的 pipeline。

**守则 R2**：当标记孤儿任务为 FAILED 时，应当按 agent 类型区分超时阈值（`req_analyst`/`req_enricher`/`req_solution`：30min；`daily_summary`/`daily_training`：15min；`nightly_exploration`：4h），因为误杀正常运行的夜间长任务的代价远高于多等几分钟。

**守则 R3**：当孤儿任务批量出现（>3 个同 agent_name 的 stuck 任务）时，应当在飞书发送一次汇总告警而不是每个任务单独告警，因为批量崩溃通常有同一根因，淹没通知比没通知危害更大。

---

### 并发保护（来源：§3 编排 + §2 生命周期）

**守则 C1**：当启动 req_analyst / req_enricher / req_solution 等批量 pipeline 子任务时，应当用 semaphore 将同类 agent 的并发数限制在 4 以内，因为超过 4 个并发 LLM 调用会触发 rate limit，导致整批任务 miss 而非单个任务失败，调试成本极高。

**守则 C2**：当 pipeline fan-out 超过 8 个子任务时，应当先启动前 4 个，等任意一个完成后再补入下一个（滑动窗口），因为全量并发会在任务队列期间集中消耗 LLM 配额，而滑动窗口能更平滑地利用配额。

**守则 C3**：当检测到某 agent 类型的当前 running 任务数 >= 上限时，应当将新任务写入 `queued` 状态而不是直接启动线程，因为过量线程会消耗内存并增加锁竞争，即使 LLM 不限流也会降低整体吞吐。

---

### 任务可见性（来源：§3 编排可见性差距）

**守则 V1**：当 JobMaster 需要执行 subprocess 类任务（nightly_exploration、reply_training、darwin_eval 等）时，应当在 subprocess 启动前先在 `agent_tasks` 表写入一行 `RUNNING` 状态，在 `finally` 块中更新为 `SUCCEEDED`/`FAILED`，因为绕过 agent_tasks 的任务在 agents.html 上不可见，运维排障无从下手。

**守则 V2**：当任务失败时，应当将失败原因写入 `result_json.error_kind`（分类值：`daemon_restart_recovery` / `timeout` / `llm_rate_limit` / `upstream_dependency` / `unknown`），因为无结构化的失败原因会导致每次排障都需要读 log_tail 全文，不可自动化处理。

---

### Anti-Loop 保护（来源：§3 编排 Anti-loop 机制）

**守则 L1**：当 OMC subagent（omc_*）产出结果并触发下游时，应当在 prompt 中注入「若触发方是 agent 且本次无实质产出，保持沉默，不要触发下一轮」，因为 agent-to-agent 通知链在异常情况下会形成循环，消耗配额并产生大量噪音任务。

**守则 L2**：当同一 parent_id 下的子任务已全部达到终态（succeeded/failed/cancelled）时，应当立即将 parent 任务标记为完成态，因为 parent 任务 stuck-running 会阻碍 agents.html 的进度视图更新，让用户误以为流程还在继续。

---

### 知识摄取（来源：§4 记忆与状态）

**守则 K1**：当本 playbook 被成功加载时，应当在 events.jsonl 写入一行 `{"event": "playbook_loaded", "chars": <N>}`，因为只有可观测的加载行为才能在排障时证明守则已生效。

**守则 K2**：当 JobMaster 发现某类任务连续失败 3 次且无人工干预时，应当在飞书发送「学习请求」——描述失败模式并询问是否需要更新守则，因为连续失败暗示守则未覆盖新场景，被动等待人工发现的成本高于主动询问。

---

## 来源章节索引

| 守则 | 来源章节 | 对应文件 |
|------|---------|---------|
| R1-R3 | §2.2 孤儿任务恢复 | multica-research-20260502.md §2 |
| C1-C3 | §3.2 并发控制 | multica-research-20260502.md §3 |
| V1-V2 | §3 可见性差距 | multica-vs-aiticket-20260502.md §3 |
| L1-L2 | §3.2 Comment 触发 Anti-loop | multica-research-20260502.md §3 |
| K1-K2 | §4.3 知识摄取意图 | multica-research-20260502.md §4 |
