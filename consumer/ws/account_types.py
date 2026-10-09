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
    "LOCKED_IN",
}

# the config-level account model: margin accounts borrow against
# holdings (margin requirement is real); non_margin accounts
# (registered plans - RRSP/TFSA/FHSA follow this model) do not
ACCOUNT_TYPES = ("margin", "non_margin")


def account_is_non_margin(acct, api_type="", label=""):
    """True when the account models as non-margin.

    An explicit config type ("margin" / "non_margin") always
    wins; otherwise the Wealthsimple API's unifiedAccountType
    decides, falling back to registered-plan keywords in the
    label."""
    t = str(getattr(acct, "type", "") or "").strip().lower()
    if t in ACCOUNT_TYPES:
        return t == "non_margin"
    if str(api_type or "").upper() in REGISTERED_ACCOUNT_TYPES:
        return True
    label_upper = str(
        label if label else getattr(acct, "label", "")
    ).upper()
    return any(t in label_upper for t in REGISTERED_ACCOUNT_TYPES)
