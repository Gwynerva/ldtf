"""LDTF (Local DTF): local archives of DTF profiles — posts, site-wide comments with context, media."""

__version__ = "1.5.0"
# Bump when the on-disk layout of raw/ or state.sqlite changes incompatibly.
SCHEMA_VERSION = 1
DEFAULT_PORT = 8765   # the local site; the next free one is taken when it is busy
