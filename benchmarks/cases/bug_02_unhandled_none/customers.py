from typing import Optional


def display_name(first: str, last: Optional[str] = None) -> str:
    """Name shown on receipts; last name is optional for guest checkouts."""
    if last:
        return f"{first} {last}".strip()
    return first.strip()
