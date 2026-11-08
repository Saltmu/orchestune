"""Scaffold for the claim read checkpoint, owned by issue #1770.

Build, restore, and idempotently store the ``orchestune-read-checkpoint``
marker. The same claim does not overwrite ``input_base_sha``. Until #1770,
this module exports no functions.
"""
