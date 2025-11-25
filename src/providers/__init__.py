from __future__ import annotations

from src.providers.pelikan import PelikanProvider
from .base import BaseProvider
from .demo_static import DemoStaticProvider
from .skyscanner_api import SkyscannerAPIProvider
from .letuska import LetuskaProvider

REGISTRY: dict[str, type[BaseProvider]] = {
    DemoStaticProvider.NAME: DemoStaticProvider,
    #SkyscannerAPIProvider.NAME: SkyscannerAPIProvider,
    LetuskaProvider.NAME: LetuskaProvider,
    PelikanProvider.NAME: PelikanProvider
}

__all__ = ["REGISTRY"]
