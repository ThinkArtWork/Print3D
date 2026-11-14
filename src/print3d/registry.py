"""Adapter registry.

The core only knows capability interfaces; every vendor implementation is a
factory registered under ``(kind, name)``.  Unknown names fail loudly and list
what is available, so an agent can recover without guessing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import AdapterUnknown, InvalidArgument

KINDS: tuple[str, ...] = (
    "provider.cloud",
    "provider.local",
    "image",
    "cad",
    "render",
    "slicer",
    "printer",
)

Factory = Callable[..., Any]


class Registry:
    """A tiny typed-ish registry: ``kind -> name -> factory``."""

    def __init__(self, kinds: tuple[str, ...] = KINDS) -> None:
        self.kinds = tuple(kinds)
        self._items: dict[str, dict[str, Factory]] = {kind: {} for kind in self.kinds}

    def _bucket(self, kind: str) -> dict[str, Factory]:
        if kind not in self._items:
            raise AdapterUnknown(
                f"Unknown adapter kind: {kind}",
                hint=f"Known kinds: {', '.join(self.kinds)}",
            )
        return self._items[kind]

    def register(
        self,
        kind: str,
        name: str,
        factory: Factory,
        *,
        replace: bool = False,
    ) -> None:
        bucket = self._bucket(kind)
        if not isinstance(name, str) or not name.strip():
            raise InvalidArgument("Adapter name must be a non-empty string")
        if name in bucket and not replace:
            raise InvalidArgument(
                f"Adapter already registered: {kind}/{name}",
                hint="Pass replace=True to override (built-in adapters do this for user config).",
            )
        bucket[name] = factory

    def unregister(self, kind: str, name: str) -> None:
        self._bucket(kind).pop(name, None)

    def get(self, kind: str, name: str) -> Factory:
        bucket = self._bucket(kind)
        try:
            return bucket[name]
        except KeyError:
            names = ", ".join(sorted(bucket)) or "(none)"
            raise AdapterUnknown(
                f"Unknown {kind} adapter: {name}",
                hint=f"Available: {names}. Register custom adapters in config.json.",
                details={"kind": kind, "name": name, "available": sorted(bucket)},
            ) from None

    def create(self, kind: str, name: str, **kwargs: Any) -> Any:
        return self.get(kind, name)(**kwargs)

    def names(self, kind: str) -> list[str]:
        return sorted(self._bucket(kind))

    def has(self, kind: str, name: str) -> bool:
        return name in self._bucket(kind)

    def snapshot(self) -> dict[str, list[str]]:
        return {kind: sorted(bucket) for kind, bucket in self._items.items()}

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._items.values())


def builtin_registry() -> Registry:
    """A registry with every adapter that ships in this build."""
    from .images import generator_from_config
    from .printers import register_adapters as register_printers
    from .slicers import register_adapters as register_slicers

    registry = Registry()
    registry.register("image", "openai", generator_from_config)
    register_printers(registry)
    register_slicers(registry)
    return registry
