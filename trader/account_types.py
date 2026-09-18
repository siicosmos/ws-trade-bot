"""Account types that never carry margin.

On registered plans Wealthsimple still reports a per-position
"margin requirement", but there it is the cash covering the
position, not borrowing - so we do not display it.
"""

REGISTERED_ACCOUNT_TYPES = {
    "RRSP",
    "TFSA",
    "FHSA",
    "RESP",
    "RRIF",
    "LIRA",
    "LIF",
    "LRSP",
    "LRIF",
    "PRIF",
    "RDSP",
    "RESP",
    "LOCKED_IN",
}
