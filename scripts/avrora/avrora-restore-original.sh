#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_DIR="$REPO_ROOT/benchmarks"

export JAVA_HOME="${JAVA_HOME:-$HOME/.sdkman/candidates/java/8.0.452-amzn}"

cd "$BENCH_DIR"
ant -buildfile bms/avrora/build.xml incremental \
  -Dbuild.target-jar=dacapo-23.11-chopin.jar \
  -Dbuild.target-jars=dacapo-23.11-chopin/jar \
  -Dbuild.target-data=dacapo-23.11-chopin/dat
