"""Late-bind canonical models; no production schema copies before Ishan's bootstrap."""
from importlib import import_module
from typing import Any, Callable

ModelFactory = Callable[..., Any]


def canonical(name: str, **fields: Any) -> Any:
    return getattr(import_module('shared.schemas'), name)(**fields)
