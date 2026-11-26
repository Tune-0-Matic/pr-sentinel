EDITOR_ROLES = ("admin", "editor")


def can_edit(role: str, user_id: int, owner_id: int) -> bool:
    """Editors can change any document; everyone else only their own."""
    return role in EDITOR_ROLES or user_id == owner_id
