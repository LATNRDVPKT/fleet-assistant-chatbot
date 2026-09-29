"""
Step 5 — Query rewriting / expansion.

Adds glossary synonyms and a section hint for the intent, so that e.g.
"HX400 oil change?" becomes
"HX400 oil change? engine oil and oil filter change preventive maintenance schedule HX-400".
"""
from app.core.registry import find_vehicle, glossary
from app.core.text import content_tokens

INTENT_HINTS = {
    "maintenance_schedule": "preventive maintenance schedule interval",
    "specification": "technical specifications",
    "warning_light": "instrument cluster warning lamps driver action",
    "dtc_lookup": "diagnostic trouble codes recommended action",
    "telematics_threshold": "telematics thresholds warning critical",
    "tyres": "tyre specifications cold pressure",
    "charging_battery": "battery care charging state of charge",
    "fuel_energy_efficiency": "fuel economy efficiency guidance",
    "emergency": "emergency procedures",
    "compliance": "AIS-140 regulatory compliance",
    "driver_hours_fatigue": "driver hours rest break Motor Transport Workers Act",
    "driver_behaviour": "driver safety behaviour",
}


def rewrite(question: str, understanding: dict) -> dict:
    low = question.lower()
    expansions = []
    for phrase, terms in glossary()["synonyms"].items():
        if f" {phrase} " in f" {low} " or (len(phrase) > 4 and phrase in low):
            expansions += [t for t in terms if t.lower() not in low]
    hint = INTENT_HINTS.get(understanding["intent"], "")
    expansions = [t for t in expansions if t.lower() not in hint.lower()]      # no repeated phrases
    vehicle_terms = []
    for m in understanding["vehicle_models"]:
        v = find_vehicle(m)
        vehicle_terms.append(f"{v['model']} {v['name']}")
    if understanding.get("variant"):
        vehicle_terms.append(understanding["variant"])
    parts = [question, " ".join(dict.fromkeys(expansions)), hint, " ".join(vehicle_terms)]
    return {"search_query": " ".join(p for p in parts if p).strip(), "expansions": list(dict.fromkeys(expansions)),
            "intent_hint": hint}


def keyword_only(question: str, understanding: dict) -> str:
    """Recovery strategy 1: strip filler words, keep key terms + intent hint."""
    return " ".join(content_tokens(question)) + " " + INTENT_HINTS.get(understanding["intent"], "")
