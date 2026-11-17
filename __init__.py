"""apple_container — Hermes terminal backend that runs commands in an Apple
container Linux VM (github.com/apple/container) while Hermes stays on the Mac.

Select it with ``terminal.backend: apple_container``. Its options are the
plugin's own settings (``plugins.entries.apple_container.settings``, a form in
Hermes Desktop) or the ``terminal.apple_container_*`` keys; ``settings.py``
resolves the two. See README.md for every setting and the security posture.
"""

from __future__ import annotations

import logging

from . import settings
from .provider import AppleContainerProvider

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    # The id Hermes stores this plugin's settings under (its registry key).
    try:
        settings.set_plugin_id(getattr(ctx, "plugin_id", None))
    except Exception:
        logger.debug("apple_container: no plugin id on the context", exc_info=True)
    ctx.register_terminal_environment_provider(AppleContainerProvider())
