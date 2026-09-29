"""
Step 12 — Grounding validator: claim ↔ chunk check, citation validation, hallucination check.

For every factual sentence (claim) in the answer:
  1. it must cite at least one source id, and every cited id must exist        (citation validation)
  2. ≥ CLAIM_SUPPORT_THRESHOLD of its key words must appear in the cited chunks (claim ↔ chunk)
  3. every NUMBER in it must appear in the cited chunks                          (hallucination check)
The answer is grounded when ≥ GROUNDING_MIN_RATIO of claims pass and no citation is invalid.
Numbers get a hard rule because in a fleet manual a wrong number (0.30 g vs 0.40 g) is the costliest error.
"""
import re

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.text import content_tokens, numbers_in
from app.pipeline.prompts import NOT_FOUND

log = get_logger("pipeline.grounding")
_CITE = re.compile(r"\[S(\d+)\]")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_SKIP_CLAIM = ("verify with", "consult", "contact your", "please check")


def _claims(answer: str) -> list[str]:
    claims = []
    for line in answer.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line or line.endswith(":") or line.startswith("#"):
            continue
        # split sentences but keep citations attached to the sentence they follow
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])|(?<=\])\s+(?=[A-Z])", line)
        claims += [p.strip() for p in parts if p.strip()]
    return claims


def check(answer: str, sources: list[dict]) -> dict:
    s = get_settings()
    if NOT_FOUND.lower() in answer.lower():
        return {"grounded": True, "is_refusal": True, "ratio": 1.0, "claims": 0, "unsupported": [], "invalid_citations": []}

    by_id = {src["id"]: src for src in sources}
    results, invalid = [], set()
    for claim in _claims(answer):
        ids = [f"S{n}" for n in _CITE.findall(claim)]
        body = _CITE.sub("", claim).strip()
        toks = set(content_tokens(body))
        nums = numbers_in(body)
        if len(toks) < 3 and not nums:
            continue                                        # too short to be a factual claim
        if any(p in body.lower() for p in _SKIP_CLAIM) and not nums:
            continue                                        # generic advice / disclaimers
        bad = [i for i in ids if i not in by_id]
        invalid.update(bad)
        cited = [by_id[i] for i in ids if i in by_id]
        if not cited:
            results.append({"claim": body, "supported": False, "reason": "no valid citation"})
            log.info(f"  ├─ claim ✗ no valid citation | \"{body[:70]}\"")
            continue
        evidence = " ".join(c["text"] + " " + c["section"] + " " + c["doc_title"] for c in cited)
        support = len(toks & set(content_tokens(evidence))) / len(toks) if toks else 1.0
        missing_nums = sorted(nums - numbers_in(evidence))
        ok = support >= s.claim_support_threshold and not missing_nums
        reason = "" if ok else ("numbers not in cited source: " + ", ".join(missing_nums) if missing_nums
                                else f"low word support {support:.2f}")
        results.append({"claim": body, "supported": ok, "support": round(support, 2), "reason": reason})
        mark = "✓" if ok else "✗"
        log.info(f"  ├─ claim {mark} cites={','.join(ids)} support={support:.2f} "
                 f"{'| ' + reason + ' ' if reason else ''}| \"{body[:70]}\"")

    total = len(results)
    passed = sum(r["supported"] for r in results)
    ratio = passed / total if total else 0.0
    return {
        "grounded": total > 0 and ratio >= s.grounding_min_ratio and not invalid,
        "is_refusal": False,
        "ratio": round(ratio, 2),
        "claims": total,
        "unsupported": [r for r in results if not r["supported"]],
        "invalid_citations": sorted(invalid),
        "cited_ids": sorted({i for c in _claims(answer) for i in (f"S{n}" for n in _CITE.findall(c))}),
    }


def keep_supported(answer: str, report: dict) -> str:
    """Last-resort fallback: drop the unsupported sentences, keep the verified ones."""
    bad = {u["claim"] for u in report["unsupported"]}
    kept = [c for c in _claims(answer) if _CITE.sub("", c).strip() not in bad]
    return "\n".join(f"- {c}" for c in kept)
