"""Scaffold for pure stale-read detection, owned by issue #1769.

``detect_stale_reads`` will accept ``ReadContextInputs`` and return a
``StaleReadReportV1`` without repository access. Until #1769, this module
exports no functions.
"""
