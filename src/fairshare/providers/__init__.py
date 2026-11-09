from __future__ import annotations

from . import claude

REGISTRY = {claude.name: claude}


def get(name: str):
    try:
        return REGISTRY[name]
    except KeyError:
        raise SystemExit(f"fairshare: unknown provider '{name}' (known: {', '.join(REGISTRY)})")
