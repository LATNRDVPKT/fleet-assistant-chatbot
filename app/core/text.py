"""Small text helpers shared by ingestion, retrieval, grounding and evals."""
import re

STOPWORDS = set("""
a an the and or of to in on for at by with from is are was were be been being it its this that these those
what which who whom how when where why do does did can could should would will shall may might must i me my we our
you your he she they them their there here as if than then so not no yes any all each per about into over under
tell please give show explain much many long often whats ok also vs versus get got need needs up out
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _stem(t: str) -> str:
    """Very light stemmer: working→work, hours→hour, batteries→battery. Good enough for matching."""
    if len(t) > 5 and t.endswith("ing"):
        return t[:-3]
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[-2].isdigit():
        return t[:-1]
    return t


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens. Keeps compounds like 'hx-400', '0.30', 'km/h' AND adds their parts
    ('battery-temperature' → 'battery-temperature', 'battery', 'temperature')."""
    out = []
    for tok in _TOKEN_RE.findall(text.lower().replace(",", "")):
        out.append(tok)
        if ("-" in tok or "/" in tok) and not tok.replace(".", "").isdigit():
            out += [p for p in re.split(r"[-/]", tok) if p]
    return out


def content_tokens(text: str) -> list[str]:
    return [_stem(t) for t in tokenize(text) if t not in STOPWORDS and len(t) > 1]


def numbers_in(text: str) -> set[str]:
    """Numbers normalised so '40,000' == '40000' and '0.30' == '0.3'."""
    out = set()
    for n in _NUMBER_RE.findall(text):
        n = n.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".") or "0"
        out.add(n)
    return out


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(text.strip()) if s.strip()]


def jaccard(a: str, b: str) -> float:
    sa, sb = set(content_tokens(a)), set(content_tokens(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0
