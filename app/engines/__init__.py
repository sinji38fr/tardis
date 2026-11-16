"""Engine adapters only collect files. Scheduling/catalog/retention live elsewhere."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class CaptureContext:
    configuration: dict
    work_dir: Path
    ssh_options: list[str]
    run: Callable
    checkpoint: Callable = lambda: None


class BackupEngine(Protocol):
    name: str
    label: str

    def capture(self, context: CaptureContext, destination: Path) -> None:
        """Write a complete file tree to destination, or raise on any incomplete copy."""
        ...


class EngineRegistry:
    def __init__(self):
        self.adapters = {}

    def register(self, adapter: BackupEngine):
        if adapter.name in self.adapters:
            raise ValueError('Moteur déjà enregistré.')
        self.adapters[adapter.name] = adapter

    def get(self, name):
        try:
            return self.adapters[name]
        except KeyError:
            raise ValueError(f'Moteur indisponible : {name}') from None

    def describe(self):
        return [{'id': engine.name, 'name': engine.label, 'available': True}
                for engine in self.adapters.values()]

    def validate_options(self, name, options):
        engine = self.get(name)
        validator = getattr(engine, 'validate_options', None)
        if validator:
            validator(options)
        elif options:
            raise ValueError('Ce moteur ne prend pas en charge d’options supplémentaires.')


def default_registry():
    from .rsnapshot import RsnapshotEngine
    registry = EngineRegistry()
    registry.register(RsnapshotEngine())
    return registry
