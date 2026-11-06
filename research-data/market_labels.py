"""Names of the supported mainland stock boards.

The exchange code remains the stable database and folder key. The readable
market name is derived from the exchange and security code for display.
Unknown code ranges are deliberately excluded until verified.
"""

MARKETS = (
    "上海证券交易所主板A股",
    "深圳证券交易所主板A股",
    "深圳证券交易所创业板",
    "上海证券交易所科创板",
    "北京证券交易所股票",
)


def market_name(exchange: str, code: str) -> str | None:
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    number = int(code)
    if exchange == "SH":
        if code.startswith(("600", "601", "603", "605")):
            return MARKETS[0]
        if code.startswith("688"):
            return MARKETS[3]
    elif exchange == "SZ":
        if code.startswith("000") or 1200 <= number <= 1999 or 2000 <= number <= 4999:
            return MARKETS[1]
        if 300000 <= number <= 309799:
            return MARKETS[2]
    elif exchange == "BJ":
        # BJ comes from the source's exchange column, not from a code prefix:
        # historic Beijing-listed codes overlap with NEEQ code ranges.
        return MARKETS[4]
    return None
