"""Planet Zoo x Archipelago hooking client (Track A).

The client never holds progression *logic* — that lives entirely in the APWorld
(Track B). This package only does two things:

  * item ID  -> apply an effect in the running game   (effect_type / effect_args)
  * game event -> report a location check to the server (trigger_type / trigger_args)

Both mappings come from the shared ``data.json`` contract.
"""

from . import data

__all__ = ["data"]
