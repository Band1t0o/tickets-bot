from __future__ import annotations

from .base import BaseProvider
from .demo_static import DemoStaticProvider
from .letuska import LetuskaProvider
from .pelikan import PelikanProvider

# Skyscanner-via-RapidAPI was removed rather than left commented out. It had
# been unreachable since the registry entry was disabled, it needed a key with
# a 100-call monthly free tier that no sweep could live inside, and its price
# handling divided by 1000 to undo "cents" - which turned a genuine 1,200 EUR
# fare into 1.20. It is in the history if a paid API is ever worth revisiting.
REGISTRY: dict[str, type[BaseProvider]] = {
    DemoStaticProvider.NAME: DemoStaticProvider,
    LetuskaProvider.NAME: LetuskaProvider,
    PelikanProvider.NAME: PelikanProvider,
}

__all__ = ["REGISTRY"]
