"""
The end-to-end query pipeline. One method, `run`, walks the architecture top to bottom:

 [01] gateway (done in the API)      [08] context validation
 [02] user / vehicle context         [09] retrieval recovery      (only if insufficient)
 [03] query preprocessing            [10] context builder
 [04] query understanding            [11] LLM generation
 [05] query rewriting / expansion    [12] grounding validator
 [06] retrieval planner              [13] regenerate / re-retrieve (only if not grounded)
 [07] hybrid retrieval engine        [14] final response

Every step is timed and logged; the timings are returned in `trace` (and shown in the UI debug panel).
"""
import time
import uuid

from app.core import metrics
from app.core.config import get_settings
from app.core.logging import Trace, get_logger, request_id_var
from app.core.registry import find_vehicle
from app.pipeline import grounding
from app.pipeline.context_builder import build_context
from app.pipeline.llm import get_llm
from app.pipeline.planner import plan as make_plan
from app.pipeline.preprocess import mask_pii, preprocess
from app.pipeline.prompts import NOT_FOUND, REGENERATE_FEEDBACK
from app.pipeline.rewriter import keyword_only, rewrite
from app.pipeline.understanding import understand
from app.pipeline.validator import validate
from app.retrieval.hybrid import HybridRetriever

log = get_logger("pipeline.orchestrator")


class CopilotPipeline:
    def __init__(self):
        self.retriever = HybridRetriever()
        self.llm = get_llm()

    # ------------------------------------------------------------------ main entry
    def run(self, question: str, vehicle_model: str | None = None, variant: str | None = None,
            region: str | None = "India", tenant: str = "default", request_id: str | None = None) -> dict:
        s = get_settings()
        request_id = request_id or uuid.uuid4().hex[:12]
        request_id_var.set(request_id)
        trace = Trace()
        t_start = time.perf_counter()
        log.info(f"▶ question received | tenant={tenant} vehicle={vehicle_model or '-'} q=\"{mask_pii(question)[:120]}\"")

        # [02] user / vehicle context
        with trace.step(2, "vehicle_context", log) as info:
            selected = find_vehicle(vehicle_model)
            ctx = {"vehicle_model": selected["model"] if selected else None,
                   "variant": variant, "region": region or "India", "tenant": tenant}
            info.update(vehicle=ctx["vehicle_model"] or "-", region=ctx["region"], tenant=tenant)

        # [03] preprocessing
        with trace.step(3, "preprocess", log) as info:
            pre = preprocess(question)
            info.update(lang=pre["language"], fixes=len(pre["spell_fixes"]), blocked=pre["blocked"])
        if pre["blocked"]:
            return self._finish(request_id, trace, t_start, status="blocked", answer=pre["block_reason"],
                                extra={"code": pre["block_code"], "block_category": pre["block_category"]},
                                ctx=ctx, question=question)
        q = pre["normalized"]

        # [04] understanding
        with trace.step(4, "understanding", log) as info:
            u = understand(q, ctx["vehicle_model"])
            if u["variant"] and not ctx["variant"]:
                ctx["variant"] = u["variant"]
            info.update(intent=u["intent"], vehicles=",".join(u["vehicle_models"]) or "-",
                        dtc=",".join(u["dtc_codes"]) or "-", conflict=u["vehicle_conflict"])
        if u["needs_clarification"]:
            names = ", ".join(f"{v['model']} ({v['name']})" for v in _all_vehicles())
            return self._finish(request_id, trace, t_start, status="clarification_needed",
                                answer=f"Which vehicle is this about? The answer depends on the model: {names}.",
                                ctx=ctx, question=question, understanding=u, extra={"code": "FC-2004"})

        # [05] rewriting / expansion
        with trace.step(5, "query_rewrite", log) as info:
            rw = rewrite(q, u)
            info.update(expansions=len(rw["expansions"]), query_len=len(rw["search_query"]))

        # [06] planning
        with trace.step(6, "retrieval_plan", log) as info:
            p = make_plan(u, rw, ctx["region"], tenant)
            info.update(filters=p["filters"], top_k=p["top_k"], prefer=p["prefer_vehicle"] or "-")

        # [07] + [08] retrieval + validation, [09] recovery loop
        kept, vreport, retrieval_attempts, retrieval_degraded = self._retrieve_and_validate(q, u, p, trace)
        if not vreport["sufficient"]:
            return self._finish(request_id, trace, t_start, status="not_found", answer=NOT_FOUND, ctx=ctx,
                                question=question, understanding=u,
                                extra={"code": "FC-2005", "top_score": vreport["top_score"],
                                       "retrieval_attempts": retrieval_attempts})

        # [10] context builder
        with trace.step(10, "context_builder", log) as info:
            sources, context = build_context(kept)
            info.update(sources=len(sources), chars=len(context),
                        docs=",".join(sorted({src["doc_id"] for src in sources})))

        vehicle_label = ", ".join(u["vehicle_models"]) or "not specified"
        # [11] generation
        with trace.step(11, "llm_generate", log) as info:
            gen = self.llm.answer(q, vehicle_label, context, sources)
            info.update(model=gen.model, in_tok=gen.input_tokens, out_tok=gen.output_tokens)

        # [12] grounding
        with trace.step(12, "grounding_check", log) as info:
            try:
                g = grounding.check(gen.text, sources)
            except Exception as e:                 # fail CLOSED: an unchecked answer is never returned
                log.error(f"grounding check crashed ({e.__class__.__name__}: {e}); treating answer as ungrounded")
                g = {"grounded": False, "is_refusal": False, "ratio": 0.0, "claims": 0,
                     "unsupported": [], "invalid_citations": [], "cited_ids": []}
            info.update(grounded=g["grounded"], ratio=g["ratio"], claims=g["claims"], unsupported=len(g["unsupported"]))

        # [13] regenerate / re-retrieve when not grounded
        regenerations = 0
        if not g["grounded"]:
            with trace.step(13, "regenerate", log) as info:
                answer_text, g, sources, regenerations = self._repair(q, u, p, vehicle_label, gen.text, g, sources)
                gen.text = answer_text
                info.update(attempts=regenerations, grounded=g["grounded"])

        status = "not_found" if g.get("is_refusal") else "answered"
        code = None if status == "answered" else ("FC-2006" if regenerations else "FC-2005")
        degraded = gen.degraded or retrieval_degraded
        cited = set(g.get("cited_ids", [])) or {src["id"] for src in sources}
        citations = [{k: v for k, v in src.items() if k != "text"} | {"snippet": src["text"][:300]}
                     for src in sources if src["id"] in cited]
        top = sources[0]["score"] if sources else 0.0
        confidence = "high" if g["grounded"] and top >= 0.5 else "medium" if g["grounded"] else "low"
        return self._finish(request_id, trace, t_start, status=status, answer=gen.text, ctx=ctx, question=question,
                            understanding=u, citations=citations if status == "answered" else [],
                            extra={"code": code, "degraded": degraded,
                                   "grounded": g["grounded"], "grounding_ratio": g["ratio"], "confidence": confidence,
                                   "top_score": top, "regenerations": regenerations,
                                   "retrieval_attempts": retrieval_attempts, "llm_model": gen.model,
                                   "input_tokens": gen.input_tokens, "output_tokens": gen.output_tokens})

    # ------------------------------------------------------------------ evaluation helper
    def retrieve_only(self, question: str, vehicle_model: str | None = None, region: str = "India") -> dict:
        """Runs steps 03-09 only and returns the validated chunks (used by the retrieval evaluation)."""
        pre = preprocess(question)
        if pre["blocked"]:
            return {"skipped": "blocked", "chunks": []}
        selected = find_vehicle(vehicle_model)
        u = understand(pre["normalized"], selected["model"] if selected else None)
        if u["needs_clarification"]:
            return {"skipped": "clarification", "chunks": []}
        p = make_plan(u, rewrite(pre["normalized"], u), region, "eval")
        kept, report, attempts, _ = self._retrieve_and_validate(pre["normalized"], u, p, Trace())
        return {"skipped": None, "chunks": kept, "report": report, "filters": p["filters"],
                "vehicle_models": u["vehicle_models"]}

    # ------------------------------------------------------------------ helpers
    def _retrieve_and_validate(self, q, u, p, trace):
        s = get_settings()
        queries = [p["search_query"], keyword_only(q, u)]
        if self.llm.is_generative:
            queries.append(None)                       # placeholder → LLM rewrite, computed lazily
        attempts = 0
        degraded = None
        kept, report = [], {"sufficient": False, "top_score": 0.0}
        for attempt, query in enumerate(queries[: 1 + s.max_retrieval_retries]):
            attempts += 1
            step_no, name = (7, "hybrid_retrieval") if attempt == 0 else (9, f"retrieval_recovery_{attempt}")
            if query is None:
                query = self.llm.rewrite_query(q) or q
            with trace.step(step_no, name, log) as info:
                log.info(f"  query: \"{query[:140]}\"")
                cands, stats = self.retriever.retrieve(query, q, p["filters"], p["top_k"], p["prefer_vehicle"],
                                                       candidate_k=p["candidate_k"] * (1 + attempt))
                info.update(**stats)
                if stats.get("degraded"):
                    degraded = stats["degraded"]
            with trace.step(8, "context_validation", log) as info:
                kept, report = validate(cands, p)
                info.update(**report)
            if report["sufficient"]:
                break
        return kept, report, attempts, degraded

    def _repair(self, q, u, p, vehicle_label, answer, g, sources):
        """Regenerate with feedback; if still ungrounded, re-retrieve more context and try again;
        finally keep only the verified sentences (or refuse)."""
        s = get_settings()
        attempts = 0
        for _ in range(s.max_regenerations):
            attempts += 1
            problems = "\n".join(f"- {x['claim']} ({x['reason']})" for x in g["unsupported"][:5]) or "- invalid citations"
            _, context = build_context([{"chunk_id": x["chunk_id"], "text": x["text"], "score": x["score"],
                                          "metadata": self.retriever.by_id[x["chunk_id"]]["metadata"]} for x in sources])
            regen = self.llm.answer(q, vehicle_label, context, sources, REGENERATE_FEEDBACK.format(problems=problems))
            g2 = grounding.check(regen.text, sources)
            log.info(f"regeneration {attempts}: grounded={g2['grounded']} ratio={g2['ratio']}")
            if g2["grounded"]:
                return regen.text, g2, sources, attempts
            answer, g = regen.text, g2
        # re-retrieve with a wider net
        attempts += 1
        wider = dict(p, top_k=p["top_k"] + 3)
        cands, _ = self.retriever.retrieve(p["search_query"], q, wider["filters"], wider["top_k"], p["prefer_vehicle"],
                                           candidate_k=p["candidate_k"] * 2)
        kept, report = validate(cands, wider)
        if report["sufficient"]:
            new_sources, context = build_context(kept)
            regen = self.llm.answer(q, vehicle_label, context, new_sources)
            g2 = grounding.check(regen.text, new_sources)
            log.info(f"re-retrieve + regenerate: grounded={g2['grounded']} ratio={g2['ratio']}")
            if g2["grounded"]:
                return regen.text, g2, new_sources, attempts
        # fallback: keep verified sentences only
        trimmed = grounding.keep_supported(answer, g)
        if trimmed.strip():
            g3 = grounding.check(trimmed, sources)
            if g3["grounded"]:
                log.warning("answer trimmed to verified sentences only")
                return trimmed, g3, sources, attempts
        log.warning("could not produce a grounded answer → refusing")
        return NOT_FOUND, grounding.check(NOT_FOUND, sources), sources, attempts

    def _finish(self, request_id, trace, t_start, status, answer, ctx, question, understanding=None,
                citations=None, extra=None):
        extra = extra or {}
        latency = round((time.perf_counter() - t_start) * 1000, 1)
        with trace.step(14, "final_response", log) as info:
            info.update(status=status, citations=len(citations or []), latency_ms=latency)
        response = {
            "request_id": request_id,
            "status": status,
            "answer": answer,
            "citations": citations or [],
            "vehicle_model": ", ".join(understanding["vehicle_models"]) if understanding and understanding["vehicle_models"] else ctx.get("vehicle_model"),
            "intent": understanding["intent"] if understanding else None,
            "code": extra.get("code"),
            "degraded": extra.get("degraded"),
            "confidence": extra.get("confidence", "n/a"),
            "grounded": extra.get("grounded"),
            "latency_ms": latency,
            "trace": trace.steps,
            **{k: v for k, v in extra.items() if k not in ("confidence", "grounded", "code", "degraded")},
        }
        self._observe(response, ctx, question)
        return response

    def _observe(self, r, ctx, question):
        """Audit log line + metrics for monitoring (CloudWatch Logs Insights / EMF)."""
        s = get_settings()
        step_ms = {st["name"]: st["ms"] for st in r["trace"]}
        retrieval_ms = sum(v for k, v in step_ms.items() if "retrieval" in k)
        metrics.incr(f"status_{r['status']}")
        metrics.incr("requests")
        metrics.observe_latency(r["latency_ms"])
        if r.get("grounded") is False:
            metrics.incr("ungrounded")
        log.info("chat_completed", extra={"extra_fields": {
            "event": "chat_completed", "status": r["status"], "code": r.get("code"), "degraded": r.get("degraded"),
            "intent": r["intent"],
            "vehicle_model": r["vehicle_model"] or "none", "tenant": ctx["tenant"],
            "question": mask_pii(question)[:500], "answer": r["answer"][:1000],
            "citations": [c["chunk_id"] for c in r["citations"]], "grounded": r.get("grounded"),
            "grounding_ratio": r.get("grounding_ratio"), "top_score": r.get("top_score"),
            "latency_ms": r["latency_ms"], "retrieval_ms": retrieval_ms, "llm_ms": step_ms.get("llm_generate", 0),
            "regenerations": r.get("regenerations", 0), "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0)}})
        metrics.emit_emf(
            {"Requests": 1, "Latency": r["latency_ms"], "RetrievalLatency": retrieval_ms,
             "LLMLatency": step_ms.get("llm_generate", 0),
             "Answered": int(r["status"] == "answered"), "NotFound": int(r["status"] == "not_found"),
             "Blocked": int(r["status"] == "blocked"), "Clarification": int(r["status"] == "clarification_needed"),
             "Ungrounded": int(r.get("grounded") is False), "Regenerations": r.get("regenerations", 0),
             "TopScore": r.get("top_score") or 0, "InputTokens": r.get("input_tokens", 0),
             "OutputTokens": r.get("output_tokens", 0)},
            {"Environment": s.environment, "VehicleModel": r["vehicle_model"] or "none"},
            {"Latency": "Milliseconds", "RetrievalLatency": "Milliseconds", "LLMLatency": "Milliseconds",
             "TopScore": "None"})


def _all_vehicles():
    from app.core.registry import vehicles
    return vehicles()
