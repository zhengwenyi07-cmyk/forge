"""Compatibility entry for the Linux Docker Flash/Pro smoke test.

Run `python smoke_test.py` or `python -m evaluation.smoke_agent`.
Credentials are loaded with config.schema; local keys stay outside Git.
"""
from evaluation.smoke_agent import main


if __name__ == "__main__":
    main()
