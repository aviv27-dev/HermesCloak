"""HermesCloak — reversible PII pseudonymization for Hermes LLM agents."""
__version__ = "0.1.0"

# Lazy exports (PEP 562): importing a lightweight submodule (e.g. egress / the
# requests integration loaded from a .pth at interpreter startup) must NOT drag in
# engine→profile→yaml. The public names below resolve on first access.
__all__ = ["Engine", "RestoreReport", "Profile", "StaticFileSource", "CallableSource", "EntitySource"]

_LAZY = {
    "Engine": ("hermescloak.engine", "Engine"),
    "RestoreReport": ("hermescloak.engine", "RestoreReport"),
    "Profile": ("hermescloak.profile", "Profile"),
    "StaticFileSource": ("hermescloak.entities", "StaticFileSource"),
    "CallableSource": ("hermescloak.entities", "CallableSource"),
    "EntitySource": ("hermescloak.entities", "EntitySource"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    return getattr(importlib.import_module(target[0]), target[1])


def __dir__():
    return sorted(__all__)
