"""Scaffold for Git observation, owned by issue #1768.

``observe_changed_resources`` and ``lookup_resource`` will return contract
DTOs. Commands go through ``infra.git_cli.run_git`` only. Until #1768, this
module exports no functions and runs no subprocess.
"""
