"""本地 HTTP 服务 + 看板。

看板只做一件事：把决策链摊开给人看。路由再聪明，如果解释不了就是黑盒。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import __version__
from .models import TaskSpec
from .system import System


class TaskIn(BaseModel):
    goal: str
    tags: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    hint_envs: list[str] = Field(default_factory=list)
    type: str | None = None
    allow_parallel: bool = True


class OutcomeIn(BaseModel):
    success: bool
    quality: float | None = None
    human_edits: int = 0
    note: str | None = None


def create_app(system: System) -> FastAPI:
    app = FastAPI(title="capgate", version=__version__)
    app.state.system = system
    app.state.jobs = set()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return DASHBOARD

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "errors": system.errors}

    @app.get("/api/envs")
    async def list_envs() -> list[dict[str, Any]]:
        return [
            {
                "id": env.id,
                "domain": env.domain,
                "tags": env.tags,
                "capabilities": [f"{c.id}@{c.version}" for c in env.capabilities],
                "adapter": env.agent.adapter,
                "models": env.agent.models,
                "prior": env.prior,
                "warm": env.resources.warm,
            }
            for env in system.registry.all()
        ]

    @app.get("/api/caps")
    async def list_caps() -> list[dict[str, Any]]:
        return [
            {
                "id": cap.id,
                "kind": cap.kind,
                "title": cap.title,
                "versions": [v.version for v in cap.versions],
            }
            for cap in system.pool.all()
        ]

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return {
            "ledger": system.ledger.stats(),
            "perf": system.ledger.perf(),
            "warm": system.warm(),
        }

    @app.get("/api/tasks")
    async def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
        return system.ledger.list_tasks(limit=limit)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        chain = system.ledger.chain(task_id)
        if not chain.get("task"):
            raise HTTPException(404, "task not found")
        return chain

    @app.post("/api/tasks")
    async def create_task(body: TaskIn) -> dict[str, Any]:
        spec = TaskSpec(
            goal=body.goal,
            tags=body.tags,
            inputs=body.inputs,
            hint_envs=body.hint_envs,
            type=body.type,
            allow_parallel=body.allow_parallel,
        )
        system.ledger.add_task(spec)

        async def runner() -> None:
            try:
                await system.orchestrator.run(spec)
            except Exception:  # noqa: BLE001 - 失败已落账，这里只保证后台任务不炸
                pass

        job = asyncio.create_task(runner())
        app.state.jobs.add(job)
        job.add_done_callback(app.state.jobs.discard)
        return {"id": spec.id, "status": "running"}

    @app.post("/api/tasks/{task_id}/outcome")
    async def record_outcome(task_id: str, body: OutcomeIn) -> dict[str, Any]:
        if not system.ledger.task(task_id):
            raise HTTPException(404, "task not found")
        system.ledger.outcome(
            task_id,
            success=body.success,
            quality=body.quality,
            human_edits=body.human_edits,
            note=body.note,
        )
        return {"ok": True}

    return app


DASHBOARD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>capgate · 本地能力网关</title>
<style>
  :root{--bg:#0f1216;--panel:#161b22;--line:#232b36;--fg:#d7dee8;--dim:#7d8899;--acc:#4ea1ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
  header{display:flex;align-items:baseline;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:15px;margin:0;letter-spacing:.5px}
  header .meta{color:var(--dim);font-size:12px}
  main{display:grid;grid-template-columns:minmax(320px,420px) 1fr;gap:0;height:calc(100vh - 53px)}
  #list{border-right:1px solid var(--line);overflow:auto}
  #detail{overflow:auto;padding:18px 22px}
  .task{padding:11px 16px;border-bottom:1px solid var(--line);cursor:pointer}
  .task:hover{background:#1b222c}
  .task.sel{background:#1e2733;box-shadow:inset 3px 0 0 var(--acc)}
  .task .goal{font-weight:600;margin-bottom:3px}
  .task .sub{color:var(--dim);font-size:12px;display:flex;gap:8px;flex-wrap:wrap}
  .pill{font-size:11px;padding:1px 7px;border-radius:9px;border:1px solid var(--line)}
  .ok{color:var(--ok);border-color:#1f6f33}.failed{color:var(--bad);border-color:#7a2622}
  .degraded,.running{color:var(--warn);border-color:#7a5b18}
  h2{font-size:13px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.7px;margin:20px 0 8px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
  .dec .stage{color:var(--acc);font-weight:600}
  .dec .reason{color:var(--dim);font-size:12px;margin:3px 0 8px}
  .cand{display:grid;grid-template-columns:150px 1fr 90px;gap:10px;align-items:center;font-size:12px;padding:2px 0}
  .bar{height:6px;background:#20293a;border-radius:3px;overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--acc)}
  .cand.win .bar>i{background:var(--ok)}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:600}
  pre{background:#0b0e12;border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;font-size:12px}
  input,button,textarea{font:inherit;background:#0b0e12;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 9px}
  button{cursor:pointer;background:#1b2432}button:hover{border-color:var(--acc)}
  .form{display:flex;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
  .form input{flex:1}
  .muted{color:var(--dim)}
</style></head><body>
<header>
  <h1>capgate</h1><span class="meta">本地能力网关 · 控制面 / 能力池 / 领域环境 / 任务编排</span>
  <span class="meta" id="stats"></span>
</header>
<div class="form">
  <input id="goal" placeholder="任务目标，例如：抓取示例站点并汇总链接"/>
  <input id="tags" placeholder="标签，逗号分隔，例如：爬虫,汇总" style="max-width:260px"/>
  <button onclick="submitTask()">提交</button>
</div>
<main>
  <div id="list"></div>
  <div id="detail"><p class="muted">左侧选一个任务，或提交一个新的。</p></div>
</main>
<script>
let current=null, timer=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error(r.status);return r.json()}
async function refresh(){
  try{
    const [tasks,stats]=await Promise.all([j('/api/tasks'),j('/api/stats')]);
    const l=stats.ledger;
    document.getElementById('stats').textContent=`任务 ${l.tasks} · 成功 ${l.tasks_ok} · 运行 ${l.runs} · 产物 ${l.artifacts}`;
    document.getElementById('list').innerHTML=tasks.map(t=>`
      <div class="task ${t.id===current?'sel':''}" onclick="open_('${t.id}')">
        <div class="goal">${esc(t.goal)}</div>
        <div class="sub"><span class="pill ${t.status}">${esc(t.status)}</span>
          <span>${esc(t.task_type)}</span><span>${esc((t.created_at||'').slice(11,19))}</span></div>
      </div>`).join('')||'<p class="muted" style="padding:16px">还没有任务。</p>';
    if(current) await open_(current,true);
  }catch(e){}
}
async function open_(id,quiet){
  current=id;
  if(!quiet) refresh();
  const c=await j('/api/tasks/'+id);
  if(!c.task)return;
  const decs=(c.decisions||[]).map(d=>{
    const max=Math.max(...d.candidates.map(x=>x.score||0),0.0001);
    const rows=d.candidates.map(x=>`
      <div class="cand ${d.chosen.includes(x.id)?'win':''}">
        <span>${esc(x.id)}</span>
        <span class="bar"><i style="width:${Math.min(100,(x.score/max)*100)}%"></i></span>
        <span class="muted">${x.score.toFixed(3)} · n=${x.success+x.fail}</span>
      </div>`).join('');
    return `<div class="card dec"><div class="stage">${esc(d.stage)} · ${esc(d.strategy)} → ${esc(d.chosen.join(', '))}</div>
      <div class="reason">${esc(d.reason)}</div>${rows}</div>`;
  }).join('');
  const runs=(c.runs||[]).map(r=>`<tr><td>${esc(r.env_id)}</td><td>${esc(r.adapter)}</td><td>${esc(r.model)}</td>
    <td class="pill ${r.status}">${esc(r.status)}</td><td>${r.latency_ms}</td></tr>`).join('');
  const arts=(c.artifacts||[]).map(a=>`<tr><td>${esc(a.name)}</td><td>${esc(a.sha256.slice(0,12))}</td>
    <td>${a.bytes}</td><td class="muted">${esc(a.path)}</td></tr>`).join('');
  document.getElementById('detail').innerHTML=`
    <h2>任务</h2><div class="card"><b>${esc(c.task.goal)}</b><div class="muted">${esc(c.task.id)} · ${esc(c.task.task_type)} · ${esc(c.task.status)}</div></div>
    <h2>决策链</h2>${decs||'<p class="muted">暂无</p>'}
    <h2>执行</h2><table><tr><th>env</th><th>adapter</th><th>model</th><th>status</th><th>ms</th></tr>${runs}</table>
    <h2>产物</h2><table><tr><th>name</th><th>sha256</th><th>bytes</th><th>path</th></tr>${arts}</table>
    <h2>事件</h2><pre>${esc(JSON.stringify((c.events||[]).map(e=>({kind:e.kind,ts:e.ts.slice(11,23)})),null,1))}</pre>`;
}
async function submitTask(){
  const goal=document.getElementById('goal').value.trim();
  if(!goal)return;
  const tags=document.getElementById('tags').value.split(',').map(s=>s.trim()).filter(Boolean);
  await j('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal,tags,inputs:{url:null}})});
  document.getElementById('goal').value='';
  refresh();
}
refresh(); timer=setInterval(refresh,3000);
</script></body></html>
"""
