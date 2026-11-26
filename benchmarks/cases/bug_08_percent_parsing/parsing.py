def parse_percent(text: str) -> float:
    """'12.5%' -> 0.125"""
    return float(text.strip().rstrip("%")) / 100
