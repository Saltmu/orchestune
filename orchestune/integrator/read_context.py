"""Scaffold for reader-context assembly, owned by issue #1776.

``build_read_context`` will accept a narrow DTO and return ``ReadContextInputs``.
It must not import L3 ``IntegrationContext``. Until #1776, this module exports
no functions.
"""
