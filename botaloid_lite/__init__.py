"""Botaloid-lite: reply with real lyrics, plus translation, time and credits."""

__version__ = "0.2.0"

__all__ = ["Library", "LineSpan", "ModelError", "Passage", "Settings"]


def __getattr__(name):
    # Import lazily so that `botaloid_lite.media` works without numpy/scikit-learn.
    if name in __all__:
        from . import library
        return getattr(library, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
