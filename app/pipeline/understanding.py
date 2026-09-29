"""
Step 4 — Query understanding: intent, entities (vehicles, variants, DTC codes) and the vehicle to use.
Rule-based on purpose: fast, free, explainable, and easy to extend by editing the keyword lists.
"""
import re

from app.core.registry import vehicles

INTENT_KEYWORDS = {
    "maintenance_schedule": ["service", "maintenance", "interval", "oil change", "change the engine oil", "schedule",
                             "replace", "replacement", "greasing", "how often"],
    "specification": ["spec", "specification", "power", "torque", "capacity", "tank", "gvw", "gcw", "payload",
                      "range", "seats", "berths", "length", "weight", "litre", "liters", "how much oil"],
    "warning_light": ["warning light", "warning lamp", "lamp", "light", "indicator", "dashboard", "turtle"],
    "dtc_lookup": ["dtc", "fault code", "error code", "trouble code"],
    "telematics_threshold": ["threshold", "harsh", "overspeed", "idling", "alert", "rpm", " g ", "limit"],
    "tyres": ["tyre", "pressure", "tread", "psi", "retread"],
    "charging_battery": ["charge", "charging", "soc", "state of charge", "regen", "regenerative", "turtle"],
    "fuel_energy_efficiency": ["fuel", "mileage", "economy", "km/l", "wh/km", "consumption", "efficiency"],
    "emergency": ["emergency", "fire", "accident", "evacuat", "breakdown", "brake failure", "burst", "panic",
                  "smoke", "overheat"],
    "compliance": ["ais-140", "ais 140", "vltd", "regulation", "compliance", "rto", "motor vehicles act", "permit"],
    "driver_hours_fatigue": ["working hours", "driving hours", "fatigue", "rest", "break", "shift", "night",
                             "continuous driving", "hours per day", "co-driver"],
    "driver_behaviour": ["driver score", "behaviour", "behavior", "defensive", "seat belt", "phone"],
}
# Intents whose answer depends on the vehicle model.
VEHICLE_SPECIFIC = {"maintenance_schedule", "specification", "warning_light", "dtc_lookup",
                    "telematics_threshold", "tyres", "charging_battery", "fuel_energy_efficiency"}
# Words that show the user wants a concrete value (→ we must know which vehicle).
VALUE_WORDS = ["interval", "how often", "capacity", "pressure", "threshold", "limit", "how many", "how much",
               "when should", "what is the", "what are the", "schedule", "spec", "at what"]
_DTC = re.compile(r"\b[PCBU][0-9][0-9A-F]{3}\b", re.IGNORECASE)


def _mentions(text: str) -> tuple[list[str], str | None]:
    low = f" {text.lower()} "
    models, variant = [], None
    for v in vehicles():
        if any(re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", low) for a in v["aliases"] + [v["model"].lower()]):
            models.append(v["model"])
        for var in v["variants"]:
            if re.search(rf"(?<![\w-]){re.escape(var.lower())}(?![\w-])", low) if len(var) > 2 else \
               re.search(rf"\b{re.escape(var)}\b", text):          # short variants (SR/LR) must be upper-case
                variant = var
    return models, variant


def understand(text: str, selected_vehicle: str | None) -> dict:
    low = f" {text.lower()} "
    scores = {intent: sum(kw in low for kw in kws) for intent, kws in INTENT_KEYWORDS.items()}
    dtc_codes = sorted({c.upper() for c in _DTC.findall(text)})
    if dtc_codes:
        scores["dtc_lookup"] += 3
    intent = max(scores, key=scores.get) if max(scores.values()) > 0 else "general"

    mentioned, variant = _mentions(text)
    conflict = bool(selected_vehicle and mentioned and selected_vehicle not in mentioned)
    # An explicit mention in the question wins over the UI selection.
    if len(mentioned) == 1:
        vehicle_models = mentioned
    elif len(mentioned) > 1:
        vehicle_models = mentioned                    # comparison question
    elif selected_vehicle:
        vehicle_models = [selected_vehicle]
    else:
        vehicle_models = []

    wants_value = any(w in low for w in VALUE_WORDS)
    needs_clarification = not vehicle_models and intent in VEHICLE_SPECIFIC and (wants_value or bool(dtc_codes))
    return {
        "intent": intent,
        "intent_scores": {k: v for k, v in scores.items() if v},
        "vehicle_models": vehicle_models,
        "mentioned_models": mentioned,
        "vehicle_conflict": conflict,
        "variant": variant,
        "dtc_codes": dtc_codes,
        "is_comparison": len(vehicle_models) > 1,
        "needs_clarification": needs_clarification,
    }
