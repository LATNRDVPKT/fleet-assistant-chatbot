"""
Step 6 — Retrieval planner: decides WHAT to search, WHERE (metadata filters) and HOW MUCH (top-k).

Vehicle isolation rule: a question about the EV-60 may only see chunks whose vehicle_model is
"EV-60" or "ALL" (the fleet-wide handbook). This is what prevents the HX-400 oil interval
from ever being given as an EV-60 answer.
"""
from app.core.config import get_settings


def plan(understanding: dict, rewritten: dict, region: str | None, tenant: str) -> dict:
    s = get_settings()
    models = understanding["vehicle_models"]
    filters: dict = {}
    if models:
        filters["vehicle_model"] = models + ["ALL"]
    if region:
        filters["region"] = region
    top_k = s.top_k + 3 if understanding["is_comparison"] else s.top_k
    return {
        "search_query": rewritten["search_query"],
        "filters": filters,
        "top_k": top_k,
        "candidate_k": s.candidate_k,
        "prefer_vehicle": models[0] if len(models) == 1 else None,
        "tenant": tenant,
    }
