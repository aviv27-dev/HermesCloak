from typing import Callable, Iterable, Protocol, runtime_checkable


@runtime_checkable
class EntitySource(Protocol):
    def names(self) -> Iterable[tuple[str, str]]:
        """Yield (surface_form, entity_type) pairs."""
        ...


class StaticFileSource:
    def __init__(self, path: str, default_type: str = "לקוח") -> None:
        self._path = path
        self._default_type = default_type

    def names(self) -> Iterable[tuple[str, str]]:
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if "\t" in line:
                    surface, etype = line.split("\t", 1)
                    yield surface.strip(), etype.strip()
                else:
                    yield line.strip(), self._default_type


class CallableSource:
    def __init__(self, fn: Callable[[], Iterable[tuple[str, str]]]) -> None:
        self._fn = fn

    def names(self) -> Iterable[tuple[str, str]]:
        return self._fn()
