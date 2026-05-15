from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from backend.config import ModelRole, get_model
from backend.intent_router import detect_intent
from backend.orchestrator import run_goal_flow, run_planning, run_vendor_payment_workflow
from backend.agents.social_agent import SocialAgent
from backend import sumopod_client

app = FastAPI(title="COOPilot AI", version="0.2.0")


class WorkflowRequest(BaseModel):
    user_goal: str = "Bayar supplier kopi bulan ini"
    business_context: str = "Coffee shop online, budget operasional 2 juta"
    budget_available: float = 2_000_000
    payment_amount: float = 500_000
    vendor_name: str = ""
    memory_query: str = "preferred supplier coffee beans cost-efficient"
    social_topic: str = ""


class DemoChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


@app.get("/health")
def health():
    return {"status": "ok", "service": "coopilot-backend"}


@app.get("/config/models")
def models_config():
    return {
        "orchestrator": get_model(ModelRole.ORCHESTRATOR),
        "sub_agent": get_model(ModelRole.SUB_AGENT),
        "demo": get_model(ModelRole.DEMO),
    }


@app.post("/workflow/plan")
def plan(req: WorkflowRequest):
    return run_planning(req.model_dump()).to_dict()


@app.post("/workflow/vendor-payment")
def vendor_payment(req: WorkflowRequest):
    return run_vendor_payment_workflow(req.model_dump()).to_dict()


@app.post("/workflow/goal")
def workflow_goal(req: WorkflowRequest):
    intent = detect_intent(req.user_goal)
    return run_goal_flow(req.model_dump(), intent).to_dict()


@app.post("/workflow/social")
def social(req: WorkflowRequest):
    result = SocialAgent().run(req.model_dump())
    return {"step": result.__dict__}


@app.post("/demo/chat")
def demo_chat(req: DemoChatRequest):
    reply = sumopod_client.chat(
        req.message,
        system="You are COOPilot AI, an autonomous operations employee for startups. Professional, clear, actionable.",
        role=ModelRole.DEMO,
        max_tokens=1024,
    )
    return {"model": get_model(ModelRole.DEMO), "reply": reply}
