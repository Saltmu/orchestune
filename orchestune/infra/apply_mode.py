"""Register the common opt-in flag for commands that preview by default."""

import argparse


def add_apply_switch(parser: argparse.ArgumentParser, *, help: str) -> None:
    """Enable writes only when the caller explicitly requests apply mode."""
    parser.add_argument("--apply", action="store_true", default=False, help=help)
