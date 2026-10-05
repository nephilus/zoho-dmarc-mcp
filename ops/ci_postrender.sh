#!/bin/sh
set -eu
docker run --rm -i --user root -v "$PWD:/workspace" -w /workspace dmarc-dev uv run --frozen python ops/fixture_manifest.py
