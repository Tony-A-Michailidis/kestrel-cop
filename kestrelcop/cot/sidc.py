"""MIL-STD-2525C symbol identification codes and Cursor-on-Target types for the things Kestrel sees.

Both schemes encode the same three facts: affiliation, battle dimension, and function.
The mapping lives in one place so it can be reviewed by someone who knows the standard.
"""

from __future__ import annotations

from ..models import Affiliation

# position 2 of a 2525C letter SIDC / second token of a CoT type
_AFF_SIDC = {"friend": "F", "neutral": "N", "unknown": "U", "suspect": "S", "hostile": "H"}
_AFF_COT = {"friend": "f", "neutral": "n", "unknown": "u", "suspect": "s", "hostile": "h"}

# function -> (2525C function id, CoT suffix)
AIR_FUNCTIONS: dict[str, tuple[str, str]] = {
    "fixed_wing": ("CF----", "C-F"),        # civil fixed wing
    "rotary": ("CH----", "C-H"),            # civil rotary wing
    "mil_fixed_wing": ("MF----", "M-F"),    # military fixed wing
    "mil_rotary": ("MH----", "M-H"),        # military rotary wing
    "unknown": ("------", ""),
}

SEA_FUNCTIONS: dict[str, tuple[str, str]] = {
    "merchant": ("XM----", "X-M"),          # merchant ship, generic
    "cargo": ("XMC---", "X-M-C"),           # merchant cargo
    "tanker": ("XMO---", "X-M-O"),          # merchant oiler / tanker
    "passenger": ("XMP---", "X-M-P"),       # passenger vessel
    "tow": ("XMTO--", "X-M-T-O"),           # towing / tug
    "fishing": ("XF----", "X-F"),           # fishing vessel
    "leisure": ("XR----", "X-R"),           # leisure craft / sailing
    "law_enforcement": ("XL----", "X-L"),   # law enforcement / coast guard
    "combatant": ("C-----", "C"),           # surface combatant (AIS type 35 "military ops")
    "unknown": ("------", ""),
}


def air_codes(affiliation: Affiliation, function: str) -> tuple[str, str]:
    fid, cot = AIR_FUNCTIONS.get(function, AIR_FUNCTIONS["unknown"])
    sidc = f"S{_AFF_SIDC[affiliation]}AP{fid}-----"[:15]
    cot_type = f"a-{_AFF_COT[affiliation]}-A" + (f"-{cot}" if cot else "")
    return sidc, cot_type


def sea_codes(affiliation: Affiliation, function: str) -> tuple[str, str]:
    fid, cot = SEA_FUNCTIONS.get(function, SEA_FUNCTIONS["unknown"])
    sidc = f"S{_AFF_SIDC[affiliation]}SP{fid}-----"[:15]
    cot_type = f"a-{_AFF_COT[affiliation]}-S" + (f"-{cot}" if cot else "")
    return sidc, cot_type


# AIS ship type (ITU-R M.1371 "type of ship and cargo") -> Kestrel function
def ais_function(ship_type: int | None) -> str:
    if ship_type is None:
        return "unknown"
    if ship_type == 30:
        return "fishing"
    if ship_type in (31, 32, 52):
        return "tow"
    if ship_type == 35:
        return "combatant"
    if ship_type in (36, 37):
        return "leisure"
    if ship_type in (51, 55):
        return "law_enforcement"
    if 60 <= ship_type <= 69:
        return "passenger"
    if 70 <= ship_type <= 79:
        return "cargo"
    if 80 <= ship_type <= 89:
        return "tanker"
    if 40 <= ship_type <= 59 or 90 <= ship_type <= 99 or 20 <= ship_type <= 29:
        return "merchant"
    return "unknown"


# ADS-B emitter category (readsb/adsb.lol "category") -> rotary or fixed wing
def adsb_function(category: str | None, military: bool) -> str:
    rotary = category == "A7"
    if military:
        return "mil_rotary" if rotary else "mil_fixed_wing"
    return "rotary" if rotary else "fixed_wing"


# Event kinds -> CoT types understood by ATAK as map markers / shapes
EVENT_COT_TYPES = {
    "fire": "b-m-p-s-m",      # spot marker (remarks carry the detection details)
    "quake": "b-m-p-s-m",
    "alert": "u-d-f",         # drawn polygon (freeform shape)
    "zone": "u-d-c-c",        # drawn circle
}
