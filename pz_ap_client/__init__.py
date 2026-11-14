"""Planet Zoo x Archipelago hooking client (Track A).

The client never holds progression *logic* - that lives entirely in the APWorld
(Track B). This package only does two things:

  * item ID  -> apply an effect in the running game   (effect_type / effect_args)
  * game event -> report a location check to the server (trigger_type / trigger_args)

Both mappings come from the shared ``data.json`` contract.
"""

from . import data

# Client build version - bump per release so beta reports (/pz_report, the startup banner) name the
# exact build. Keep in the "<major>.<minor>.<patch>-<stage>" shape; the packaged exe ships this.
CLIENT_VERSION = "1.0.0-beta1"

__all__ = ["data", "CLIENT_VERSION"]
