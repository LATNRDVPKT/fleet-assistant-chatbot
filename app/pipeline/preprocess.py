"""
Step 3 — Query preprocessing: normalise → language detection → spell correction → safety checks.
"""
import difflib
import re
import unicodedata

from app.core.logging import get_logger
from app.core.registry import glossary, vehicles

log = get_logger("pipeline.preprocess")

_UNIT_FIXES = [
    (r"\bkmph\b|\bkm ph\b|\bkm per hour\b", "km/h"),
    (r"\bkmpl\b|\bkm per litre\b|\bkm per liter\b", "km/L"),
    (r"\bdeg(?:ree)?s? ?c\b", "°C"),
    (r"\btire(s)?\b", r"tyre\1"),
]
# Unicode script ranges we can recognise (v1 answers English only).
_SCRIPTS = {"hi": r"[ऀ-ॿ]", "ta": r"[஀-௿]", "te": r"[ఀ-౿]",
            "kn": r"[ಀ-೿]", "bn": r"[ঀ-৿]"}
_PHONE = re.compile(r"(?<!\d)(\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, repl in _UNIT_FIXES:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def detect_language(text: str) -> str:
    for lang, pattern in _SCRIPTS.items():
        if len(re.findall(pattern, text)) >= 3:
            return lang
    return "en"


def _protected_words() -> set[str]:
    words = set(glossary()["vocabulary"])
    for v in vehicles():
        words |= {a.lower() for a in v["aliases"]} | {v["model"].lower()}
    return words


def spell_correct(text: str) -> tuple[str, list[str]]:
    """Correct only near-misses of domain words (e.g. 'maintanance' → 'maintenance'). Never touches
    numbers, codes or short words, so it cannot damage 'P0217' or '6x4'."""
    vocab = sorted(glossary()["vocabulary"])
    protected = _protected_words()
    fixes = []

    def fix(match):
        word = match.group(0)
        low = word.lower()
        if len(low) < 5 or low in protected or any(ch.isdigit() for ch in low):
            return word
        best = difflib.get_close_matches(low, vocab, n=1, cutoff=0.84)
        if best and best[0] != low:
            fixes.append(f"{word}->{best[0]}")
            return best[0]
        return word

    return re.sub(r"[A-Za-z]+", fix, text), fixes


def safety_check(text: str) -> tuple[str | None, str | None]:
    """Returns (category, reason) if the request must be blocked, else (None, None)."""
    low = text.lower()
    g = glossary()
    for p in g["injection_patterns"]:
        if re.search(p, low):
            return "prompt_injection", "The request tries to override the assistant's instructions."
    for p in g["blocked_patterns"]:
        if re.search(p, low):
            return "unsafe_request", ("I can't help with bypassing, disabling or falsifying safety or compliance "
                                      "equipment and records. Please contact your fleet safety manager.")
    return None, None


def mask_pii(text: str) -> str:
    """Used before writing questions to logs."""
    return _EMAIL.sub("<email>", _PHONE.sub("<phone>", text))


BLOCK_CODES = {"prompt_injection": "FC-2002", "unsafe_request": "FC-2001", "unsupported_language": "FC-2003"}


def preprocess(question: str) -> dict:
    # 3a normalise text and units
    normalized = normalize(question)
    log.info(f"  ├─ 3a normalise        | \"{mask_pii(normalized)[:100]}\"")
    # 3b detect language
    language = detect_language(normalized)
    log.info(f"  ├─ 3b language         | {language}")
    # 3c spell-correct domain words (never fails the request)
    try:
        corrected, fixes = spell_correct(normalized) if language == "en" else (normalized, [])
    except Exception as e:
        log.warning(f"  ├─ 3c spell-correct skipped: {e}")
        corrected, fixes = normalized, []
    log.info(f"  ├─ 3c spell-correct    | fixes={fixes or 'none'}")
    # 3d safety + prompt-injection check (fails CLOSED: an error here blocks the request)
    try:
        category, reason = safety_check(corrected)
    except Exception as e:
        log.error(f"  ├─ 3d safety check crashed ({e}); blocking request to stay safe")
        category, reason = "unsafe_request", "Your request could not be checked safely. Please rephrase it."
    if not category and language != "en":
        category, reason = "unsupported_language", "Right now I can answer questions in English only."
    log.info(f"  └─ 3d safety check     | {'BLOCKED ' + category if category else 'passed'}")
    return {"normalized": corrected, "language": language, "spell_fixes": fixes,
            "blocked": category is not None, "block_category": category, "block_reason": reason,
            "block_code": BLOCK_CODES.get(category)}
