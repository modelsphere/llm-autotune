"""Shim so `python3 -m sglang.launch_server <flags>` runs the mock engine."""

from mock_engine import main

if __name__ == "__main__":
    main(mode="sglang")
