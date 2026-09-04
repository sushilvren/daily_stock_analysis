from __future__ import annotations

# Curated liquid/core baskets used only for relative-strength monitoring.
# They are intentionally compact, transparent, and replaceable by iFinD industry/concept
# membership once the licensed API is enabled.
THEMES: dict[str, dict] = {
    "CPO": {
        "label": "CPO/光通信",
        "codes": ["300308", "300502", "300394", "002281", "603083"],
    },
    "PCB": {
        "label": "PCB/AI硬件",
        "codes": ["002384", "300476", "002463", "002916"],
    },
    "LIQUID_COOLING": {
        "label": "液冷",
        "codes": ["002837", "300499", "301018", "002536", "603757", "301489", "002909", "603270", "003018", "603090"],
    },
    "SEMICONDUCTOR": {
        "label": "半导体",
        "codes": ["688008", "300666", "688256", "688041", "688981", "002371", "300604", "688012"],
    },
    "AI_APPS": {
        "label": "AI应用/软件传媒",
        "codes": ["301171", "300413", "300624", "300418", "605398", "605577", "000892"],
    },
}
