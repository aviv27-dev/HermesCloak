"""Israeli phone numbers: one canonical form for a dozen spellings.

WhatsApp hands over E.164 (+972547765611); CRM rows, scanned protocols and letters write the same
number many ways: 054-7765611, 054 776 5611, 0547765611, 054.776.5611, 972-54-7765611,
+972 (0)54 776 5611, 00972547765611. A spelling-based regex misses most of them, so detection
works from the national significant number (NSN: the digits after the leading 0 / after 972):
mobile and VoIP = 9 digits (5X / 7X + 7), landline = 8 digits (area digit + 7).

``CANDIDATE`` finds anything shaped like a phone; ``normalize`` says whether it IS one.
"""
import re

# a run of digits with optional single separators (space, hyphen, dot) and an optional "(0)"
# after the country code; 8–15 digits in total. Digit boundaries keep longer numbers intact.
CANDIDATE = re.compile(
    r"(?<![\d+])(?:\+|00)?(?:972[\s\-.]?(?:\(0\)[\s\-.]?)?)?\(?0?\)?\d(?:[\s\-.]?\d){7,10}(?!\d)"
)
_NSN = re.compile(r"^[2-9]\d{7,8}$")


def normalize(raw: str) -> str | None:
    """The NSN ("547765611") of an Israeli phone number in any common spelling, else None."""
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return None
    if d.startswith("00972"):
        d = d[2:]
    if d.startswith("9720"):                 # "+972 (0)54…" — the national 0 kept in brackets
        d = "972" + d[4:]
    if d.startswith("972") and len(d) in (11, 12):
        nsn = d[3:]
    elif d.startswith("0") and len(d) in (9, 10):
        nsn = d[1:]
    else:
        return None                          # no leading 0 / 972 → not a phone (an ID, an amount…)
    if not _NSN.match(nsn):
        return None
    if len(nsn) == 9 and nsn[0] not in "57":     # 9-digit NSN exists only for mobile (5X) / VoIP (7X)
        return None
    return nsn
