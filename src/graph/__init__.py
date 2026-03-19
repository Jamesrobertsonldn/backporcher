"""Code dependency graph for intelligent PR review context and agent navigation."""


def ensure_graph(*args, **kwargs):
    """Lazy wrapper — imports on first call to avoid startup failure when tree-sitter is missing."""
    from .context import ensure_graph as _ensure_graph
    return _ensure_graph(*args, **kwargs)


def build_review_context(*args, **kwargs):
    """Lazy wrapper — imports on first call."""
    from .context import build_review_context as _build_review_context
    return _build_review_context(*args, **kwargs)


def build_navigation_context(*args, **kwargs):
    """Lazy wrapper — imports on first call."""
    from .context import build_navigation_context as _build_navigation_context
    return _build_navigation_context(*args, **kwargs)


__all__ = ["ensure_graph", "build_review_context", "build_navigation_context"]
