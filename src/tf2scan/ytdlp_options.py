"""Shared yt-dlp options for YouTube's JavaScript challenges."""


def with_node(**options):
    """Equivalent to yt-dlp's ``--js-runtimes node`` CLI option."""
    return {"js_runtimes": {"node": {}}, **options}
