"""Niche Profile package: the data model plus the 8 built-in profiles."""

from .builtins import BUILTIN_PROFILES, get_profile
from .models import ArchetypeName, NicheProfile

__all__ = [
    "ArchetypeName",
    "BUILTIN_PROFILES",
    "NicheProfile",
    "get_profile",
]
