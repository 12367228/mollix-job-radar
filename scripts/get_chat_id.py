"""Compatibility entry point; use `python -m jobradar telegram-chat-id`."""

from pathlib import Path
import sys

# Direct script execution otherwise puts only scripts/ on the module search path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobradar.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main(["telegram-chat-id"]))
