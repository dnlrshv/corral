"""Lazy rules-loader seam for the later user-scope implementation."""


def _load_layers(**kwargs):
    from corral.rules import load_layers
    return load_layers(**kwargs)
