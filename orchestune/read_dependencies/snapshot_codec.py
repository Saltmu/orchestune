"""Scaffold for the parent snapshot codec, owned by issue #1774.

Strict parse, serialize, and digest of ``ParentReadSnapshotV1``. Original
reads stay separate from validation checkpoints. Until #1774, this module
exports no functions.
"""
