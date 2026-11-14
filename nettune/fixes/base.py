"""Base classes for fixes.

A *fix* is one reversible change to the system. To add one:

1. Subclass ``Fix`` in a module inside this package.
2. Implement ``status``, ``apply`` and ``revert``.
3. Call ``self.remember(ctx, original)`` *before* changing anything, so that
   ``revert`` can restore exactly what was there.
4. Register the instance in ``nettune/fixes/__init__.py``.

Rules every fix must follow:

* Never accept free-form values from the API. Parameters select from options
  that are defined in code.
* Never interpolate outside values into a PowerShell script - pass them through
  ``run_ps(..., args={...})``.
* ``revert`` must work even when NetTune was restarted in between.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..state import Store

ON = "on"                    # the optimized setting is active
OFF = "off"                  # can be applied
UNSUPPORTED = "unsupported"  # this adapter / connection cannot use the fix
ERROR = "error"              # status could not be read


class FixError(RuntimeError):
    """A fix could not be applied or reverted; the message is shown to the user."""


@dataclass
class Context:
    conn: dict
    store: Store

    @property
    def connected(self) -> bool:
        return bool(self.conn.get("connected"))

    @property
    def if_index(self) -> int | None:
        return self.conn.get("if_index")

    @property
    def adapter(self) -> str | None:
        return self.conn.get("adapter")

    @property
    def is_wifi(self) -> bool:
        return bool(self.conn.get("is_wifi"))

    @property
    def wifi_profile(self) -> str | None:
        wifi = self.conn.get("wifi") or {}
        return wifi.get("profile") or wifi.get("ssid")

    @property
    def label(self) -> str | None:
        return self.conn.get("label")


def status(state: str, detail: str = "", **extra) -> dict:
    return {"state": state, "detail": detail, **extra}


class Fix:
    id = ""
    title = ""
    summary = ""            # one line: what it does
    details = ""            # why it helps, and the trade-off
    category = "adapter"    # adapter | dns | system | data
    requires_admin = True
    drops_connection = False  # the Wi-Fi link restarts for a few seconds

    # -- to implement -------------------------------------------------------
    def scope(self, ctx: Context) -> str:
        """What the saved original belongs to (an adapter, a Wi-Fi profile...)."""
        return ctx.adapter or "-"

    def status(self, ctx: Context) -> dict:
        raise NotImplementedError

    def apply(self, ctx: Context, params: dict) -> str:
        raise NotImplementedError

    def revert(self, ctx: Context) -> str:
        raise NotImplementedError

    # -- saved originals ----------------------------------------------------
    def _key(self, ctx: Context) -> str:
        return f"{self.id}|{self.scope(ctx)}"

    def saved(self, ctx: Context):
        return ctx.store.get("fixes", {}).get(self._key(ctx))

    def remember(self, ctx: Context, original) -> None:
        """Save the pre-change value once; later applies must not overwrite it."""
        key = self._key(ctx)

        def update(fixes):
            if key not in fixes:
                fixes[key] = {"original": original, "applied_at": time.time()}
            return fixes

        ctx.store.update("fixes", {}, update)

    def forget(self, ctx: Context) -> None:
        key = self._key(ctx)

        def update(fixes):
            fixes.pop(key, None)
            return fixes

        ctx.store.update("fixes", {}, update)

    # -- API shape ----------------------------------------------------------
    def describe(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "details": self.details,
            "category": self.category,
            "requires_admin": self.requires_admin,
            "drops_connection": self.drops_connection,
        }

    def full_status(self, ctx: Context) -> dict:
        if not ctx.connected:
            return status(UNSUPPORTED, "No active network connection.", managed=False)
        try:
            result = self.status(ctx)
        except Exception as exc:  # a broken fix must not take the dashboard down
            result = status(ERROR, str(exc))
        result["managed"] = self.saved(ctx) is not None
        return result
