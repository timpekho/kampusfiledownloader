__all__ = ["BS_PARSER"]


def _pick_bs_parser() -> str:
    try:
        import lxml  # noqa: F401

        return "lxml"
    except ImportError:
        return "html.parser"


BS_PARSER = _pick_bs_parser()
