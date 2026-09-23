"""One install serves every profile in the process (hermex#634).

hermes-agent keeps one ``PluginManager`` per profile home and discovers a profile's plugins once,
on first use, from that profile's own ``plugins/`` directory and ``plugins.enabled`` list. So a
profile created after pairing never pushed, and a later per-profile install only took effect
after the backend restarted. Every lifecycle hook reaches plugins through
``PluginManager.invoke_hook`` on the active profile's manager (``post_tool_call`` is first gated
on ``has_hook``). The first copy of this plugin loaded in a process wraps those two methods and
serves the push hooks for any manager without its own copy, with one ``HermexPush`` per profile.

These are hermes-agent internals, used on purpose because upstream changes are ruled out. A host
without the methods gets one warning, and only profiles with their own copy push, as before.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from .hooks import HOOK_NAMES, HermexPush

logger = logging.getLogger("hermex_push")

# Stored on the manager class, not in this module: hermes-agent may import a fresh copy of the
# plugin per profile, and every copy must see that one of them already wrapped the class.
_MARK = "_hermex_push_serves_every_profile"
_WRAPPED = ("invoke_hook", "has_hook")
_lock = threading.Lock()


def _has_own_copy(manager: Any, hook_name: str) -> bool:
    return any(getattr(getattr(callback, "__self__", None), "is_hermex_push", False)
               for callback in manager.iter_hook_callbacks(hook_name))


def serve_every_profile(
    manager_class: type, active_profile: Callable[[], str],
    make_push: Callable[[str], HermexPush] = lambda profile: HermexPush(profile=profile),
) -> Optional[Callable[[], None]]:
    """Wrap ``manager_class`` once per process. Returns the undo for the copy that wrapped it,
    or None when another copy already did or the host lacks the methods."""
    with _lock:
        if getattr(manager_class, _MARK, None) is not None:
            return None
        if not all(callable(getattr(manager_class, name, None)) for name in (*_WRAPPED, "iter_hook_callbacks")):
            logger.warning("hermex-push: this hermes-agent cannot share one install across profiles; "
                           "install the plugin in each profile that should push")
            return None
        own = {name: manager_class.__dict__.get(name) for name in _WRAPPED}
        invoke, has = manager_class.invoke_hook, manager_class.has_hook
        pushes: dict[str, HermexPush] = {}

        def push_for_active_profile() -> HermexPush:
            profile = active_profile()
            with _lock:
                if profile not in pushes:
                    pushes[profile] = make_push(profile)
                return pushes[profile]

        def invoke_hook(self: Any, hook_name: str, **kwargs: Any) -> list:
            results = invoke(self, hook_name, **kwargs)
            try:
                if hook_name in HOOK_NAMES and not _has_own_copy(self, hook_name):
                    getattr(push_for_active_profile(), hook_name)(**kwargs)
            except Exception:
                logger.warning("hermex-push: serving %s for a profile without its own copy failed",
                               hook_name, exc_info=True)
            return results

        def has_hook(self: Any, hook_name: str) -> bool:
            return hook_name in HOOK_NAMES or has(self, hook_name)

        manager_class.invoke_hook, manager_class.has_hook = invoke_hook, has_hook
        token = object()
        setattr(manager_class, _MARK, token)

    def undo() -> None:
        with _lock:
            if getattr(manager_class, _MARK, None) is not token:
                return
            for name, original in own.items():
                if original is None:
                    delattr(manager_class, name)
                else:
                    setattr(manager_class, name, original)
            delattr(manager_class, _MARK)
            closing = list(pushes.values())
            pushes.clear()
        for push in closing:
            push.close()

    return undo


def serve_every_profile_in_host() -> Optional[Callable[[], None]]:
    """``serve_every_profile`` for the running hermes-agent; None outside one."""
    try:
        from hermes_cli.plugins import PluginManager
        from hermes_cli.profiles import get_active_profile_name
    except Exception:
        return None
    return serve_every_profile(PluginManager, get_active_profile_name)
