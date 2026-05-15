from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agents.communication_agent import CommunicationAgent
from backend.agents.finance_agent import FinanceAgent
from backend.agents.memory_agent import MemoryAgent
from backend.agents.operations_agent import OperationsAgent
from backend.agents.payment_agent import PaymentAgent
from backend.agents.strategy_agent import StrategyAgent
from backend.agents.base_agent import AgentResult
from backend.business_profile import build_business_context, is_profile_complete
from backend.config import ModelRole
from backend.intent_router import UserIntent
from backend import sumopod_client


@dataclass
class WorkflowRun:
    workflow: str
    user_goal: str
    steps: list[AgentResult] = field(default_factory=list)
    feed: list[dict[str, str]] = field(default_factory=list)
    status: str = "running"
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "user_goal": self.user_goal,
            "status": self.status,
            "feed": self.feed,
            "steps": [s.__dict__ for s in self.steps],
            "context": {k: v for k, v in self.context.items() if k != "memories"},
        }


def _append_step(run: WorkflowRun, result: AgentResult) -> None:
    run.steps.append(result)
    label = result.agent.replace("_", " ").title()
    run.feed.append(
        {
            "agent": result.agent,
            "status": result.status,
            "message": f"[{label}] {result.message}",
        }
    )
    if result.data:
        run.context.update(result.data)


def _operations_planning(context: dict[str, Any]) -> AgentResult:
    goal = context.get("user_goal", "")
    biz = context.get("business_context", "")
    suppliers = context.get("registered_suppliers") or []
    sup_line = ", ".join(s.get("name", "") for s in suppliers) or "belum ada"
    prompt = f"""Goal: {goal}
Business: {biz}
Supplier terdaftar: {sup_line}

Buat task list operasional (max 5 item). Jangan mengarang nama pihak baru."""
    try:
        tasks = sumopod_client.chat(
            prompt,
            system="COOPilot Operations Agent. Facts only.",
            role=ModelRole.SUB_AGENT,
        )
        return AgentResult("operations_plan", "ok", "Operations plan generated", {"operations_tasks": tasks})
    except Exception as e:
        return AgentResult("operations_plan", "error", str(e), {})


def orchestrate_plan(user_goal: str, business_context: str = "") -> str:
    prompt = f"""Business goal: {user_goal}
Context: {business_context}

Buat rencana operasional (max 5 bullet). Jangan mengarang nama supplier/perusahaan."""
    return sumopod_client.chat(
        prompt,
        system="COOPilot Orchestrator. Use only provided context.",
        role=ModelRole.ORCHESTRATOR,
    )


def run_goal_flow(context: dict[str, Any], intent: UserIntent) -> WorkflowRun:
    goal = context.get("user_goal", "")
    profile = context.get("profile")
    if not is_profile_complete(profile):
        run = WorkflowRun(workflow=intent.value, user_goal=goal)
        _append_step(
            run,
            AgentResult(
                "system",
                "blocked",
                "Profil perusahaan belum lengkap/spesifik. Lengkapi via /setup (badan usaha, alamat, kota, modal).",
                {},
            ),
        )
        run.status = "blocked"
        return run

    biz = build_business_context(profile)
    run = WorkflowRun(workflow=intent.value, user_goal=goal, context=dict(context))
    ctx = {**context, "business_context": biz, "user_goal": goal}

    try:
        ctx["budget_available"] = float(
            str(profile.get("budget", profile.get("modal", "0"))).replace(".", "").replace(",", "")
        )
    except (TypeError, ValueError):
        ctx["budget_available"] = 0

    strategy = StrategyAgent().run(ctx)
    _append_step(run, strategy)
    if strategy.status == "error":
        run.status = "error"
        return run
    ctx.update(strategy.data)

    memory = MemoryAgent().run(ctx)
    _append_step(run, memory)
    if memory.status in ("blocked", "error"):
        run.status = memory.status
        return run
    ctx.update(memory.data)

    try:
        plan_text = orchestrate_plan(goal, biz)
        _append_step(run, AgentResult("orchestrator", "ok", "Roadmap created", {"plan": plan_text}))
        ctx["orchestrator_plan"] = plan_text
    except Exception as e:
        _append_step(run, AgentResult("orchestrator", "error", str(e), {}))

    ops = _operations_planning(ctx)
    _append_step(run, ops)
    ctx.update(ops.data)

    if intent == UserIntent.VENDOR_PAYMENT:
        if not ctx.get("selected_supplier"):
            _append_step(
                run,
                AgentResult(
                    "system",
                    "blocked",
                    "Pilih supplier terdaftar: /bayar <nama_supplier>",
                    {},
                ),
            )
            run.status = "blocked"
            return run

        ctx["payment_allowed"] = True
        sel = ctx["selected_supplier"]
        ctx["vendor_name"] = sel.get("name")
        ctx["payment_amount"] = int(ctx.get("payment_amount") or sel.get("default_monthly_amount") or 0)

        for agent in [FinanceAgent(), PaymentAgent(), CommunicationAgent(), OperationsAgent()]:
            result = agent.run(ctx)
            _append_step(run, result)
            if result.status in ("blocked", "error"):
                run.status = result.status
                run.context = ctx
                return run
            ctx.update(result.data)

    run.status = "ok"
    run.context = ctx
    return run


def run_vendor_payment_workflow(context: dict[str, Any]) -> WorkflowRun:
    return run_goal_flow(context, UserIntent.VENDOR_PAYMENT)


def run_planning(context: dict[str, Any]) -> WorkflowRun:
    return run_goal_flow(context, UserIntent.PLANNING)
