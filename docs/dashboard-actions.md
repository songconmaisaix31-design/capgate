# 看板操作：人工验收 / pin / 重跑

对应 issue #010「看板只能看不能操作」。看板原本只读决策链，验收要回 CLI 敲
`capgate rate`、pin 要手改 `router.yaml`、失败任务不能重跑。本改动把这三个
动作搬回页面，让「我这一下改变了什么」立即可见——这是系统能不能被信任的关键：
看不到反馈闭环，就不会去验收；没有验收标签，路由就退化成静态表。

## 三个动作

| 动作 | 入口 | 落点 | 立即反馈 |
|---|---|---|---|
| 验收（好 / 差） | `POST /api/tasks/{id}/outcome` | `outcomes` 表，压过自动判定 | 刷新 `/api/stats`，该 env 胜率即时变化 |
| pin 当前 env | `POST /api/pins` | `router.yaml` 的 `pins` 节 | 之后同 `task_type` 的决策链显示 `strategy=pin` |
| 重跑 | `POST /api/tasks/{id}/rerun` | 新 `task_id` | 旧任务保持不可变，新任务进入列表 |

## 人工验收

复用已有的 `/api/tasks/{id}/outcome` 端点（`success` + 可选 `note`）。
`perf()` 的成败判定 = 运行成功 **且** 最新一条人工标签不是差评，所以打完差评
`/api/stats` 里该 env 的胜率立刻下降。看板在验收后重新拉取 stats 并按
`env_id` 聚合 `perf`，在「环境胜率」表里显示 `wins/n` 与胜率条。

## 按任务类型 pin

pin 的真源是 `router.yaml` 的 `pins` 节：

```yaml
# 人工 pin：任务类型 -> {env, model}。优先级最高，用于线上救火与 A/B 基线。
pins:
  爬虫:            # task_type：TaskSpec.type 或 tags[0]
    env: crawler   # 领域路由固定到 env
    model: m1      # 可选：env 内第二层路由固定到 model
```

`Router.route_domains` / `route_agent` 已经优先读 `config.pins` 并产出
`strategy="pin"` 的决策，所以「显示在决策链上」这一半在路由里早就就位；
缺的只是**把 pin 写回文件**。新增的 `capgate/pins.py` 负责这件事：

- `load_pins(root)`：读 `pins` 节，缺失返回空 dict；
- `set_pin(root, task_type, *, env=None, model=None)`：设置并写回，已有 pin 只
  更新指定键（先 pin env 再 pin model 两者并存）；
- `remove_pin(root, task_type, *, key=None)`：解除整类或单个键。

读写用 `ruamel.yaml` 的 round-trip 模式：完整载入 → 改 `pins` → 完整保存，
注释、其它字段、已有 model pin 原样保留，不手写 YAML 解析。写回走「同目录
临时文件 + 原子替换」，写到一半进程被中断也不会留下半截文件。

> **依赖交接**：`ruamel.yaml`（PyYAML 的 `safe_load/safe_dump` 会丢注释，round-trip
> 是保留注释的唯一成熟做法）由集成侧统一加入 `pyproject.toml`。本模块不碰依赖
> 清单；集成前请先 `pip install ruamel.yaml`。

## 重跑

重跑由另一条路径负责，这里只定义看板侧契约与按钮。要点：**旧任务不可变**，
重跑产出全新 `task_id`，原有 `tasks / decisions / runs / artifacts / outcomes`
一律不动。

## server.py 端点契约

`capgate/server.py` 由 agent-autoloop 路径实现，看板与 `pins.py` 已按下列契约
就位。实现时注意两点：pin 写盘后要**同时**更新运行中的 `Router` 配置，否则
不重启不生效；错误信息写给用户看，用中文。

```
GET    /api/pins                     → {"pins": {task_type: {env?, model?}}}
POST   /api/pins                     body {task_type, env?, model?}
                                     → {"pins": {...}}   # 至少 env/model 之一
DELETE /api/pins/{task_type}[?key=env|model]
                                     → {"pins": {...}}
POST   /api/tasks/{task_id}/rerun    → {"id": 新 task_id, "status": "running"}
```

POST / DELETE 处理完磁盘后，把结果同步进内存：

```python
# 示意：server 装配处
from .pins import set_pin as _set_pin
pins = _set_pin(system.paths.root, body.task_type, env=body.env, model=body.model)
system.router.config.pins = pins
```

`system.router.config` 是可变 `RouterConfig`，直接改 `pins` 字段即可让后续任务
立即读到新 pin，无需重建 `Router`。

## 验收标准对照

- [x] 网页上打完差评，`/api/stats` 里该 env 的胜率立刻变化
      —— 验收后刷新 stats，按 env 聚合的胜率表即时更新
- [x] pin 之后同一 `task_type` 的决策链显示 `pin`
      —— 见 `tests/test_dashboard_actions.py::test_pin_roundtrip_drives_router_decision`
- [ ] 重跑产生新 `task_id`，旧任务保持不可变 —— 由重跑路径负责

## 测试

`tests/test_dashboard_actions.py` 覆盖 `pins.py` 的读写、幂等、注释保留、错误
分支，以及「写回 → `load_router_config` → `Router` 决策」闭环；另有一条看板
契约烟测，锁住按钮与端点名，防止多路 Agent 各写各的。
