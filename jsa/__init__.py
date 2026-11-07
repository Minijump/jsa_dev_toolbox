"""jsa: CLI toolbox for Odoo developers."""
# what a plugin imports ("from jsa import pg, config"), whatever the folders inside jsa become
from .core import addons, config, pg, ui  # noqa: F401
