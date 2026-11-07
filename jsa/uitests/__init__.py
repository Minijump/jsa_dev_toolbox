"""UI tests (optional feature): acceptance tests written with Odoo keywords, played in a real browser.

commands.py and keyword_list.py are the jsa commands. keywords/, examples/ and requirements.txt are what Robot
Framework runs, in the feature's own environment: jsa never imports them.
"""
from .commands import register  # noqa: F401
