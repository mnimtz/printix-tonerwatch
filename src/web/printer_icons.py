"""Classify a Printix printer model into one of six own-brand pictograms.

We deliberately ship generic SVG pictograms rather than manufacturer photos:

* Manufacturer images are copyrighted; bundling them into a commercial
  B2B SaaS is a legal risk and hotlinking to hp.com / canon.com is a
  TOS breach that also breaks the UI when they add Referer checks.
* Six pictograms — laser SFP, laser MFP, inkjet SFP, inkjet MFP, LFP,
  production — cover the visual differentiation MSP techs actually need
  ("is that the little desktop unit or the big floor MFP?").
* An enrichment layer (Wikidata / Commons SPARQL lookup) can add real
  photos later, cached locally with attribution — that's a P5+ item.

The classifier is regex-based and deliberately conservative: unknown
models fall back to ``laser_sfp`` (the most common bauform).
"""

from __future__ import annotations

import re

ICON_KEYS: tuple[str, ...] = (
    "laser_sfp",
    "laser_mfp",
    "inkjet_sfp",
    "inkjet_mfp",
    "lfp",
    "production",
)


# Order matters — most specific wins.
_RULES: list[tuple[re.Pattern[str], str]] = [
    # Production presses / high-volume
    (re.compile(r"\b(iGen|Versant|PrimeLink C\d{4}|imagePRESS|"
                r"AccurioPress|bizhub PRESS|Nexfinity)\b", re.I),
     "production"),

    # Large-format / plotters
    (re.compile(r"\b(DesignJet|PageWide XL|imagePROGRAF|SureColor|"
                r"latex|LFP|plotter)\b", re.I),
     "lfp"),

    # Inkjet MFP
    (re.compile(r"\b(OfficeJet\s+Pro\s+\d{4}[a-z]*|"
                r"MAXIFY\s+MB\d{4}|"
                r"WorkForce\s+Pro|"
                r"MFC-J\d{3,5}|"
                r"PIXMA\s+G\d{4}|"
                r"HP\s+Smart\s+Tank\s+\d{4})\b", re.I),
     "inkjet_mfp"),

    # Inkjet SFP
    (re.compile(r"\b(OfficeJet\s+\d{4}|DeskJet|PIXMA(?!\s+G\d{4})|"
                r"MegaTank|EcoTank|Envy|Photosmart|Stylus|"
                r"HP\s+Smart\s+Tank\s+5\d{2})\b", re.I),
     "inkjet_sfp"),

    # Laser MFP — vendor-specific model families
    (re.compile(r"\b(imageRUNNER|imageCLASS\s+MF|"
                r"LaserJet\s+.*\s+MFP|LaserJet\s+MFP|"
                r"Color\s+LaserJet\s+.*\s+MFP|"
                r"WorkCentre|VersaLink\s+[CB]\d{3,4}[a-z]{0,3}|"
                r"AltaLink|"
                r"bizhub\s+[A-Z]?\d{3,4}[a-z]{0,3}|"
                r"MP\s+[CW]?\d{3,4}|IM\s+[CW]?\d{3,4}|"
                r"ECOSYS\s+M\d{4}|TASKalfa|"
                r"MFC-L\d{4}|DCP-L\d{4}|"
                r"CX\d{3,4}|XM\d{3,4}|MX\d{3,4}|"
                r"AR-[MB]\d{3,4}|MX-[BM]\d{3,4})\b", re.I),
     "laser_mfp"),

    # Laser SFP — fallback lasers
    (re.compile(r"\b(LaserJet|Color\s+LaserJet|"
                r"imageCLASS|LBP\d{3,4}|"
                r"Phaser|VersaLink\s+[CB]\d{3,4}n|"
                r"bizhub\s+\d{3}[A-Z]?|"
                r"P\d{3,4}\w{0,3}|ECOSYS\s+P\d{4}|FS-\d{4}|"
                r"HL-L\d{4}|CS\d{3,4}|"
                r"MS\d{3,4})\b", re.I),
     "laser_sfp"),
]


def classify_model(model: str | None) -> str:
    """Return one of :data:`ICON_KEYS` for the given printer model string."""
    if not model:
        return "laser_sfp"
    text = model.strip()
    for pattern, key in _RULES:
        if pattern.search(text):
            return key
    return "laser_sfp"
