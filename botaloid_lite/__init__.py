"""Botaloid-lite: reply with real lyrics, plus translation, time and credits."""

__version__ = "0.3.0"

__all__ = ["Library", "LineSpan", "MissingIndexError", "ModelError", "Passage", "Settings"]


def __getattr__(name):
    # Import lazily so that `botaloid_lite.media` and `botaloid_lite.build`
    # stay usable without pulling in the retrieval runtime.
    if name in __all__:
        from . import library
        return getattr(library, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
