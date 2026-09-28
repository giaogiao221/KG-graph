from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.extraction.contracts import PermanentAdapterError


RouteSet = tuple[str, ...]


class CapabilityError(PermanentAdapterError):
    def __init__(
        self,
        *,
        engine: str,
        requested_routes: Iterable[str],
        supported_route_sets: Iterable[Iterable[str]],
    ) -> None:
        self.engine = engine
        self.requested_routes = tuple(sorted(requested_routes))
        self.supported_route_sets = tuple(
            sorted(tuple(sorted(routes)) for routes in supported_route_sets)
        )
        super().__init__(
            f"{engine} cannot independently execute routes {self.requested_routes}; "
            f"supported combined route sets: {self.supported_route_sets}"
        )


@dataclass(frozen=True, slots=True)
class LegacyCapability:
    engine: str
    mode: str
    supported_route_sets: tuple[RouteSet, ...]

    def require(self, requested_routes: Iterable[str]) -> None:
        requested = tuple(sorted(set(requested_routes)))
        if requested not in self.supported_route_sets:
            raise CapabilityError(
                engine=self.engine,
                requested_routes=requested,
                supported_route_sets=self.supported_route_sets,
            )
