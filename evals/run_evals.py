"""
Evaluation runner — three suites, one command.

  python -m evals.run_evals                      # all suites, profile picked from LLM_PROVIDER
  python -m evals.run_evals --suite retrieval    # only retrieval
  python -m evals.run_evals --judge              # also score faithfulness/relevance with the LLM (production)
  python -m evals.run_evals --profile production

1. RETRIEVAL  (did we find the right evidence?)          hit@1, hit@5, MRR@5, nDCG@5, context recall,
                                                          context precision@5, vehicle leakage
2. GENERATION (did we answer correctly and only from it?) answer correctness, faithfulness, numeric
                                                          faithfulness, citation validity/coverage, answer
                                                          leakage, refusal, false refusal, safety, clarification
3. PERFORMANCE (fast and error-free?)                     latency p50/p95/p99, per-step p95, error rate

Output: evals/reports/<timestamp>.json and .md, and exit code 1 if any gate in thresholds.yaml fails.
For load testing a running server see evals/load_test.py.
"""
import argparse
import json
import math
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT, get_settings
from app.core.logging import get_logger
from app.core.text import numbers_in

log = get_logger("evals")
EVAL_DIR = PROJECT_ROOT / "evals"
CITE = re.compile(r"\[S(\d+)\]")


# ====================================================================== helpers
def norm(text: str) -> str:
    """Lower-case, unify dashes/spaces and drop thousands separators so '40,000 km' == '40000 km'."""
    text = text.lower().replace("–", "-").replace("—", "-").replace(" ", " ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    return re.sub(r"\s+", " ", text)


def contains(text: str, fact) -> bool:
    """A fact is a string, or a list of acceptable alternatives."""
    options = fact if isinstance(fact, list) else [fact]
    return any(norm(o) in norm(text) for o in options)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, math.ceil(p * len(v)) - 1)], 1)


def mean(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else 0.0


def load_items() -> list[dict]:
    with open(EVAL_DIR / "golden_set.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def grade(chunk: dict, item: dict) -> int:
    """2 = expected document AND expected section, 1 = expected document only, 0 = neither."""
    m = chunk["metadata"]
    if m["doc_id"] not in item["expected_doc_ids"]:
        return 0
    return 2 if any(sec in m["section_path"] for sec in item["expected_sections"]) else 1


def allowed_vehicles(item: dict, doc_vehicle: dict) -> set | None:
    """Vehicles a chunk/citation may come from. None = no vehicle was asked about, so nothing can leak."""
    vehicles = {doc_vehicle[d] for d in item["expected_doc_ids"]} - {"ALL"}
    return (vehicles | {"ALL"}) if vehicles else None


def is_leak(vehicle_model: str, item: dict, doc_vehicle: dict) -> bool:
    allowed = allowed_vehicles(item, doc_vehicle)
    return allowed is not None and vehicle_model not in allowed


# ====================================================================== 1. retrieval suite
def run_retrieval(pipeline, items: list[dict], doc_vehicle: dict) -> tuple[dict, list[dict]]:
    log.info("=" * 78)
    log.info("SUITE 1/3 RETRIEVAL")
    log.info("=" * 78)
    rows = []
    for it in [i for i in items if i["expected_status"] == "answered"]:
        res = pipeline.retrieve_only(it["question"], it["vehicle_model"])
        chunks = res["chunks"][:5]
        grades = [grade(c, it) for c in chunks]
        first = next((i for i, g in enumerate(grades, 1) if g == 2), None)
        dcg = sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(grades, 1))
        ideal = sorted(grades + [2], reverse=True)[:5]          # at least one perfect chunk exists
        idcg = sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(ideal, 1))
        context = " ".join(c["text"] for c in chunks)
        leaked = [c["chunk_id"] for c in chunks if is_leak(c["metadata"]["vehicle_model"], it, doc_vehicle)]
        row = {
            "id": it["id"], "hit_at_1": int(first == 1), "hit_at_5": int(first is not None),
            "rr": 1 / first if first else 0.0, "ndcg": dcg / idcg if idcg else 0.0,
            "context_recall": mean(contains(context, f) for f in it["must_contain"]),
            "precision": mean(g >= 1 for g in grades) if grades else 0.0,
            "leaked": leaked, "top": [c["chunk_id"] for c in chunks],
        }
        rows.append(row)
        mark = "✓" if row["hit_at_5"] and row["context_recall"] == 1 else "✗"
        log.info(f"{mark} {it['id']:<16} hit@5={row['hit_at_5']} rank={first or '-'} recall={row['context_recall']:.2f} "
                 f"precision={row['precision']:.2f} leak={len(leaked)}")
    metrics = {
        "hit_at_1": mean(r["hit_at_1"] for r in rows), "hit_at_5": mean(r["hit_at_5"] for r in rows),
        "mrr_at_5": mean(r["rr"] for r in rows), "ndcg_at_5": mean(r["ndcg"] for r in rows),
        "context_recall": mean(r["context_recall"] for r in rows),
        "context_precision_at_5": mean(r["precision"] for r in rows),
        "vehicle_leakage": sum(len(r["leaked"]) for r in rows), "items": len(rows),
    }
    return metrics, rows


# ====================================================================== 2. generation suite (+ 3. performance data)
JUDGE_PROMPT = """You are grading an answer from a fleet-maintenance assistant.
QUESTION: {q}
SOURCES:
{sources}
ANSWER:
{a}
Score 1-5 for FAITHFULNESS (every statement is supported by the SOURCES) and RELEVANCE (it answers the
question for the right vehicle). Reply with JSON only: {{"faithfulness": n, "relevance": n}}"""


def judge(llm, question, answer, source_texts) -> dict | None:
    try:
        raw = llm.primary.complete("You are a strict evaluator.",
                                   JUDGE_PROMPT.format(q=question, a=answer, sources="\n---\n".join(source_texts)[:6000])).text
        return json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception as e:
        log.warning(f"judge failed: {e}")
        return None


def run_generation(pipeline, items: list[dict], doc_vehicle: dict, use_judge: bool) -> tuple[dict, dict, list[dict]]:
    log.info("=" * 78)
    log.info("SUITE 2/3 GENERATION (full pipeline)" + (" + LLM judge" if use_judge else ""))
    log.info("=" * 78)
    rows, latencies, step_ms, errors = [], [], {}, 0
    for it in items:
        t0 = time.perf_counter()
        try:
            r = pipeline.run(it["question"], it["vehicle_model"], request_id=f"eval-{it['id']}")
        except Exception as e:                                   # counted as an error, never crashes the run
            errors += 1
            log.error(f"✗ {it['id']} crashed: {e.__class__.__name__}: {e}")
            rows.append({"id": it["id"], "category": it["category"], "status": "error", "ok_status": False})
            continue
        latencies.append((time.perf_counter() - t0) * 1000)
        for st in r["trace"]:
            step_ms.setdefault(f"{st['step']:02d}_{st['name']}", []).append(st["ms"])
        row = {"id": it["id"], "category": it["category"], "status": r["status"],
               "ok_status": r["status"] == it["expected_status"], "answer": r["answer"]}
        if it["expected_status"] == "answered" and r["status"] == "answered":
            ans = r["answer"]
            cited_ids = {f"S{n}" for n in CITE.findall(ans)}
            returned = {c["id"] for c in r["citations"]}
            claims = [l for l in ans.splitlines() if l.strip() and not l.strip().endswith(":")]
            full_texts = [pipeline.retriever.by_id[c["chunk_id"]]["text"] for c in r["citations"]]
            answer_nums = numbers_in(CITE.sub("", ans))
            row.update(
                correct=all(contains(ans, f) for f in it["must_contain"]),
                faithfulness=r.get("grounding_ratio", 0.0),
                numeric_ok=answer_nums <= numbers_in(" ".join(full_texts)),
                citation_valid=cited_ids <= returned,
                citation_coverage=mean(bool(CITE.search(l)) for l in claims) if claims else 1.0,
                leak=any(contains(ans, bad) for bad in it["must_not_contain"])
                or any(is_leak(c["vehicle_model"], it, doc_vehicle) for c in r["citations"]),
            )
            if use_judge:
                row["judge"] = judge(pipeline.llm, it["question"], ans, full_texts)
        rows.append(row)
        extra = ""
        if "correct" in row:
            extra = f"correct={row['correct']} faithful={row['faithfulness']} leak={row['leak']}"
        mark = "✓" if row["ok_status"] and row.get("correct", True) and not row.get("leak") else "✗"
        log.info(f"{mark} {it['id']:<16} expected={it['expected_status']:<20} got={r['status']:<20} "
                 f"{latencies[-1]:7.1f} ms {extra}")

    ans_rows = [r for r in rows if "correct" in r]
    by_cat = lambda cat: [r for r in rows if r["category"] == cat]
    in_scope = [r for r in rows if r["category"] in ("vehicle_value", "handbook", "comparison")]
    judged = [r["judge"] for r in ans_rows if r.get("judge")]
    gen = {
        "answer_correctness": mean(r["correct"] for r in ans_rows),
        "faithfulness_claims": mean(r["faithfulness"] for r in ans_rows),
        "numeric_faithfulness": mean(r["numeric_ok"] for r in ans_rows),
        "citation_validity": mean(r["citation_valid"] for r in ans_rows),
        "citation_coverage": mean(r["citation_coverage"] for r in ans_rows),
        "answer_leakage": sum(r["leak"] for r in ans_rows),
        "refusal_accuracy": mean(r["ok_status"] for r in by_cat("out_of_scope")),
        "false_refusal_rate": mean(r["status"] == "not_found" for r in in_scope),
        "safety_block_rate": mean(r["ok_status"] for r in by_cat("red_team")),
        "clarification_accuracy": mean(r["ok_status"] for r in by_cat("clarification")),
        "status_accuracy": mean(r["ok_status"] for r in rows),
        "answered_items": len(ans_rows),
    }
    if judged:
        gen["judge_faithfulness"] = mean(j.get("faithfulness", 0) for j in judged)
        gen["judge_relevance"] = mean(j.get("relevance", 0) for j in judged)
    perf = {
        "requests": len(items), "errors": errors, "error_rate": round(errors / len(items), 4),
        "latency_p50_ms": pct(latencies, 0.50), "latency_p95_ms": pct(latencies, 0.95),
        "latency_p99_ms": pct(latencies, 0.99), "latency_mean_ms": round(statistics.mean(latencies), 1) if latencies else 0,
        "step_p95_ms": {k: pct(v, 0.95) for k, v in sorted(step_ms.items())},
    }
    return gen, perf, rows


# ====================================================================== gates + report
def check_gates(results: dict, gates: dict) -> list[dict]:
    verdicts = []
    for suite, metrics in gates.items():
        for name, rule in metrics.items():
            value = results.get(suite, {}).get(name)
            if value is None:
                continue                                         # e.g. judge metrics when --judge not used
            ok = value >= rule["min"] if "min" in rule else value <= rule["max"]
            verdicts.append({"suite": suite, "metric": name, "value": value,
                             "gate": (">= " + str(rule["min"])) if "min" in rule else ("<= " + str(rule["max"])),
                             "pass": ok})
    return verdicts


def write_report(results: dict, verdicts: list[dict], details: dict, profile: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = EVAL_DIR / "reports"
    out.mkdir(exist_ok=True)
    s = get_settings()
    payload = {"timestamp": stamp, "profile": profile, "config": {
        "llm": s.llm_provider, "embedding": s.embedding_provider, "reranker": s.reranker},
        "results": results, "gates": verdicts, "details": details}
    (out / f"{stamp}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    lines = [f"# Evaluation report {stamp}", "",
             f"Profile **{profile}** · LLM `{s.llm_provider}` · embedding `{s.embedding_provider}` · reranker `{s.reranker}`", "",
             "| Suite | Metric | Value | Gate | Result |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {v['suite']} | {v['metric']} | {v['value']} | {v['gate']} | {'PASS' if v['pass'] else '**FAIL**'} |"
              for v in verdicts]
    lines += ["", "## Per-step latency (p95, ms)", ""]
    lines += [f"- {k}: {v}" for k, v in results.get("performance", {}).get("step_p95_ms", {}).items()]
    failures = [r for r in details.get("generation", []) if not r.get("ok_status") or r.get("correct") is False or r.get("leak")]
    if failures:
        lines += ["", "## Items to review", ""]
        lines += [f"- `{r['id']}` status={r['status']} correct={r.get('correct')} leak={r.get('leak')}" for r in failures]
    (out / f"{stamp}.md").write_text("\n".join(lines) + "\n")
    return out / f"{stamp}.md"


def main():
    parser = argparse.ArgumentParser(description="Run Fleet Copilot evaluations")
    parser.add_argument("--suite", choices=["retrieval", "generation", "all"], default="all")
    parser.add_argument("--profile", choices=["offline", "production"])
    parser.add_argument("--judge", action="store_true", help="LLM-as-judge scores (needs a generative LLM)")
    args = parser.parse_args()

    s = get_settings()
    profile = args.profile or ("offline" if s.llm_provider == "extractive" else "production")
    gates = yaml.safe_load(open(EVAL_DIR / "thresholds.yaml"))[profile]
    items = load_items()
    log.info(f"EVALUATION START | items={len(items)} profile={profile} llm={s.llm_provider} "
             f"embedding={s.embedding_provider} reranker={s.reranker}")

    from app.core.registry import manifest
    from app.pipeline.orchestrator import CopilotPipeline
    doc_vehicle = {d["doc_id"]: d["vehicle_model"] for d in manifest()["documents"]}
    pipeline = CopilotPipeline()
    if args.judge and not pipeline.llm.is_generative:
        log.warning("--judge ignored: LLM_PROVIDER=extractive has no model to judge with")
        args.judge = False

    import logging
    logging.getLogger("pipeline").setLevel(logging.WARNING)     # keep the eval output readable
    logging.getLogger("retrieval").setLevel(logging.WARNING)

    results, details = {}, {}
    if args.suite in ("retrieval", "all"):
        results["retrieval"], details["retrieval"] = run_retrieval(pipeline, items, doc_vehicle)
    if args.suite in ("generation", "all"):
        results["generation"], results["performance"], details["generation"] = run_generation(
            pipeline, items, doc_vehicle, args.judge)

    verdicts = check_gates(results, gates)
    report = write_report(results, verdicts, details, profile)
    log.info("=" * 78)
    log.info("RESULTS")
    for v in verdicts:
        log.info(f"  {'PASS' if v['pass'] else 'FAIL'}  {v['suite']:<11} {v['metric']:<24} {v['value']!s:<8} gate {v['gate']}")
    failed = [v for v in verdicts if not v["pass"]]
    log.info(f"report: {report}")
    log.info(f"EVALUATION {'PASSED ✓' if not failed else f'FAILED ✗ ({len(failed)} gate(s))'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
