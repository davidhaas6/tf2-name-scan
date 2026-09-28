"""Convenience wrapper: python eval/run_benchmark.py manifest.jsonl --config config.yaml."""

import sys

from tf2scan.cli import main

sys.argv.insert(1, "benchmark")
main()
