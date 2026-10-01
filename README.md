# skill-runtime

Sandboxed skills runtime for agents.

```
request → env prep → sandbox build → execute → trace
```

- Skills are versioned packages (`skill.yaml` + `SKILL.md`) mounted read-only at `/skills`
- Sandbox is an E2B-style microVM (control plane here, data plane in VM)
- Thin agent executor (model SDK + tool layer) baked into the image — not a full CLI agent
- Capability intersection across mounted skills narrows the sandbox policy
- Trace starts at env-prep, stored outside the sandbox

## Decisions (locked)

| Topic | Choice |
|-------|--------|
| Snapshot granularity | Image layers (`hash(base_image + packages + skills)`) |
| Agent executor | Baked into image, thin custom loop + model SDK |
| Capability conflict | Intersection (never widens) |

## Layout

```
src/skill_runtime/
  models.py            # request/result/skill/policy + capability intersection
  policy.py            # SkillMount + capability → sandbox policy / E2B network
  state_machine.py     # execution FSM (5-stage pipeline + abort edges)
  orchestrator.py      # control-plane driver
  ports.py             # SkillRegistry / Environment / Sandbox / Agent / Trace protocols
  factory.py           # wire memory or E2B adapters into Orchestrator
  adapters/memory.py   # in-memory + fake adapters for prototype & tests
  adapters/e2b.py      # E2B microVM: snapshot env layers + skill mounts + net policy
tests/                 # FSM, capabilities, policy, e2b adapter, orchestrator
docs/design.md         # component diagram + API draft
```

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q

# with real E2B (optional extra)
uv pip install -e ".[e2b]"
# export E2B_API_KEY=...  then use skill_runtime.factory.build_e2b_orchestrator

# end-to-end demo (real LLM loop; E2B if E2B_API_KEY is set, else fake sandbox)
.venv/bin/python examples/e2e_demo.py

# HTTP API
.venv/bin/python -m skill_runtime.api          # :8080, auto-selects e2b/memory
# or: uvicorn skill_runtime.api.__main__:build --factory --port 8080
```

## HTTP API

| Method | Path | 说明 |
|--------|------|------|
| GET | `/healthz` | 健康检查 |
| POST | `/v1/executions?mode=sync\|async` | 提交执行（sync→200 result，async→202 poll_url） |
| GET | `/v1/executions/{id}` | 状态 + FSM history + resolved skills |
| GET | `/v1/executions/{id}/trace` | 全链路 span |
| GET | `/v1/executions/{id}/artifacts` | 输出物 |
| POST | `/v1/executions/{id}/cancel` | 取消 |
| POST | `/v1/executions/{id}/replay` | 同 request 重跑 |
| GET | `/v1/skills` | skill 列表 |
| GET | `/v1/skills/{name}/versions` | 版本列表 |
| POST | `/v1/skills/{name}/versions` | 发布 skill 包 |

```bash
curl -s localhost:8080/v1/executions -H 'content-type: application/json' -d '{
  "skill_refs": [{"name": "demo", "version": "*"}],
  "task": {"prompt": "write /workspace/a.txt", "outputs": [{"path": "/workspace/a.txt"}]}
}'
```

## Agent executor

Thin control-plane loop (`ToolGatedAgentExecutor`):
- LLM: OpenAI-compatible tool calling (default `mimo-v2.6-pro` via MiMoCode auth)
- Tools: `bash` / `python` / `write_file` / `read_file` / `list_dir` / `finish`
- Capability intersection gates which tools exist; FS policy gates paths
- Effects execute inside the microVM through `SandboxPort`

## E2B adapter behavior

| Concern | Implementation |
|---------|----------------|
| Snapshot layers | `hash(base_image + packages + skill_fingerprint)` → `sr-env-<key>` template/snapshot |
| Cache | `prefer_cache` hits local + remote snapshot; `always_build` bakes fresh |
| Skill mount | write files under `/skills/<name>`, `chmod -R a-w` |
| Capability net | intersection hosts → `network.allow_out`; empty → `deny_out 0.0.0.0/0` |
| Workspace | `/workspace` ensured `u+w` |
| pause/resume | `sandbox.pause(keep_memory=True)` / `Sandbox.connect(id)` |

## Pipeline states

```
CREATED → RESOLVING_SKILLS → PREPARING_ENV → BUILDING_SANDBOX
        → MOUNTING_SKILLS → EXECUTING → COLLECTING_TRACE → SUCCEEDED
```

Any non-terminal state may abort to `FAILED` / `TIMEOUT` / `CANCELLED`.
