# Avrora Radio Simulation: Struct-of-Arrays Migration

This folder contains the tooling for benchmarking and profiling a struct-of-arrays
optimization to avrora's radio transmission hot path.

## What changed

The original avrora radio simulation (`Medium.java`) uses `LinkedList<Transmission>`
and Java iterators to track active transmissions. In the hot path methods
(`deliverByte`, `getIntersection`, `earliestNewTransmission`, `isChannelClear`),
this causes heavy allocation pressure from `LinkedList`, `LinkedList$Node`,
and `LinkedList$ListItr` objects on every call.

The migration replaces this with `CustomTransmissionList` -- a struct-of-arrays
container that stores transmission fields in parallel primitive arrays
(`long[] firstBit`, `double[] Pt`, `double[] Freq`, etc.) plus a
`Transmission[] txRefs` array for fields that are mutated after insertion
(`lastBit`, `data`, `counter`).

Key design decisions:

- **Scratch-list reuse**: Each `Receiver` holds a single `scratchIntersection`
  list that is cleared and reused on every `getIntersection` call, eliminating
  per-call allocations entirely.
- **`addFrom(src, idx)`**: Copies fields directly between `CustomTransmissionList`
  instances without going through `Transmission` objects.
- **Indexed iteration**: All for-each/iterator loops replaced with index-based
  loops over the parallel arrays.
- **Interface changes**: The `Arbitrator` interface methods now take individual
  fields (`Transmitter`, `double tPt`, `double tF`) instead of `Transmission`
  objects, so implementations (`LossyModel`, `RadiusModel`, `BasicArbitrator`)
  never need to dereference the original object in the common case.

## Files modified

| File | Change |
|---|---|
| `CustomTransmissionList.java` | New file -- struct-of-arrays container |
| `Medium.java` | `Arbitrator` interface, `BasicArbitrator`, `deliverByte`, `getIntersection`, `earliestNewTransmission`, `isChannelClear`, `transmissions` field |
| `LossyModel.java` | Method signatures + bodies updated to use individual fields |
| `RadiusModel.java` | Method signatures + bodies updated to use individual fields |

All changes are captured in `instrumented.patch` (in the avrora build directory)
and `instrumentation.patch` (in this scripts directory).

## Results

Measured with JFR profiling on the DaCapo `default` size workload:

| Metric | Baseline | Modified | Change |
|---|---|---|---|
| Total allocation weight | 296.6 MB | 153.9 MB | **-48.1%** |
| LinkedList/Iterator allocations | 1379 samples / 123.3 MB | 2 samples / 244 KB | **-99.8%** |
| CustomTransmissionList allocations | -- | 0 samples / 0 B | zero hot-path allocation |

## Benchmark script

`avrora-benchmark.py` automates the full workflow: build baseline, apply patch,
build modified, profile both with JFR, and compare results.

### Prerequisites

- **JDK 8** for compilation (scripts look for `~/.sdkman/candidates/java/8.0.452-amzn`
  or `JAVA8_HOME` env var)
- **JDK 11+** for running (JFR support required)
- **Ant** for the DaCapo build system
- **uv** (optional, for running without manual venv setup)

### Usage

```bash
# Full run: build both, profile both, compare
uv run scripts/avrora/avrora-benchmark.py run

# Specify workload size and iterations
uv run scripts/avrora/avrora-benchmark.py run -s default -n 3

# Skip baseline if already profiled
uv run scripts/avrora/avrora-benchmark.py run --skip-baseline

# Compare two existing JFR files
uv run scripts/avrora/avrora-benchmark.py compare baseline.jfr modified.jfr

# Save current working tree changes as instrumented.patch
uv run scripts/avrora/avrora-benchmark.py save-patch
```

### How it works

1. Restores avrora source to the `Init` commit state (`git reset` + `checkout` + `clean`)
2. Builds with Ant using JDK 8, packages into `dacapo-23.11-chopin.jar`
3. Profiles with JFR (`jdk.ExecutionSample` + `jdk.ObjectAllocationSample`)
4. Applies `instrumented.patch`, rebuilds, profiles again
5. Parses both JFR recordings and prints a side-by-side comparison

## Patch management

The `save-patch` subcommand captures the current working tree diff (including
untracked new files) as `instrumented.patch`. It verifies the patch applies
cleanly on a fresh checkout before saving.

To manually apply the patch:

```bash
cd benchmarks/bms/avrora/build/avrora
git apply instrumented.patch
```

## Troubleshooting

- **Build fails with "source option 7 is no longer supported"**: JDK 8 is not
  being used for compilation. Set `JAVA8_HOME` or install via sdkman.
- **JFR parsing fails**: Ensure you're running with JDK 11+ (the `jfr` CLI tool
  is required).
- **Stale class files**: The script cleans `build/build/` before each build to
  avoid JDK version mismatch errors.
