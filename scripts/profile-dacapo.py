#!/usr/bin/env python3
"""
Profile DaCapo benchmarks using Java Flight Recorder (JFR).

Usage:
    # Build DaCapo first (only needed once, or after source changes):
    python scripts/profile-dacapo.py build

    # Profile all available benchmarks:
    python scripts/profile-dacapo.py profile

    # Profile specific benchmarks:
    python scripts/profile-dacapo.py profile h2 lusearch fop

    # Profile with a specific size (small, default, large, vlarge):
    python scripts/profile-dacapo.py profile --size small h2

    # Profile with custom JFR settings:
    python scripts/profile-dacapo.py profile --jfr-settings profile

    # Profile with multiple iterations:
    python scripts/profile-dacapo.py profile --iterations 3 h2

    # List available benchmarks in the jar:
    python scripts/profile-dacapo.py list

    # Build and then profile:
    python scripts/profile-dacapo.py build profile h2
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import glob
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_JFR_DIR = RESULTS_DIR / "jfr"

# We pick up the latest dacapo jar that was built.  The build produces jars of
# the form  dacapo-evaluation-git-<hash>.jar  in the benchmarks/ directory.
# If you have a specific jar you want to use, set DACAPO_JAR env var.
DACAPO_JAR_ENV = os.environ.get("DACAPO_JAR")


def find_dacapo_jar() -> Path:
    """Find the most-recently modified DaCapo jar in the benchmarks directory."""
    if DACAPO_JAR_ENV:
        jar = Path(DACAPO_JAR_ENV)
        if jar.exists():
            return jar
        print(f"[ERROR] DACAPO_JAR={DACAPO_JAR_ENV} does not exist.", file=sys.stderr)
        sys.exit(1)

    jars = sorted(
        BENCHMARKS_DIR.glob("dacapo-*.jar"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jars:
        print(
            "[ERROR] No dacapo jar found in benchmarks/. "
            "Run 'python scripts/profile-dacapo.py build' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return jars[0]


def find_java() -> str:
    """Return the path to the java executable."""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        java = os.path.join(java_home, "bin", "java")
        if os.path.isfile(java):
            return java
    java = shutil.which("java")
    if java:
        return java
    print("[ERROR] java not found. Set JAVA_HOME or add java to PATH.", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def do_build(args: argparse.Namespace) -> None:
    """Build DaCapo using ant from the benchmarks/ directory."""
    print("=" * 70)
    print("  Building DaCapo benchmarks")
    print("=" * 70)

    java8_home = os.environ.get("JAVA_HOME", "")
    if java8_home:
        print(f"  JAVA_HOME = {java8_home}")

    ant = shutil.which("ant")
    if not ant:
        print("[ERROR] ant not found on PATH.", file=sys.stderr)
        sys.exit(1)

    # Build target – default builds everything.
    target = getattr(args, "build_target", None) or "dist"
    cmd = [ant, target]

    print(f"  Running: {' '.join(cmd)}")
    print(f"  Working directory: {BENCHMARKS_DIR}")
    print("-" * 70)

    result = subprocess.run(cmd, cwd=str(BENCHMARKS_DIR))
    if result.returncode != 0:
        print(f"\n[ERROR] Build failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    print("\n[OK] Build succeeded.")
    jar = find_dacapo_jar()
    print(f"  DaCapo jar: {jar}")


# ---------------------------------------------------------------------------
# List benchmarks
# ---------------------------------------------------------------------------

def do_list(args: argparse.Namespace) -> None:
    """List benchmarks available in the DaCapo jar."""
    jar = find_dacapo_jar()
    java = find_java()
    print(f"Using jar: {jar}")
    print(f"Using java: {java}\n")

    result = subprocess.run(
        [java, "-jar", str(jar), "--list-benchmarks"],
        capture_output=True, text=True,
    )
    # Benchmark names are printed to stdout; the banner goes to stderr.
    benchmarks = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            benchmarks.extend(line.split())

    if benchmarks:
        print("Available benchmarks:")
        for bm in benchmarks:
            print(f"  - {bm}")
    else:
        print("No benchmarks found (or jar is not built yet).")
    return benchmarks


def get_available_benchmarks() -> list[str]:
    """Return list of available benchmark names from the jar."""
    jar = find_dacapo_jar()
    java = find_java()
    result = subprocess.run(
        [java, "-jar", str(jar), "--list-benchmarks"],
        capture_output=True, text=True,
    )
    # Benchmark names are printed to stdout; the banner goes to stderr.
    benchmarks = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            benchmarks.extend(line.split())
    return benchmarks


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def profile_benchmark(
    java: str,
    jar: Path,
    benchmark: str,
    jfr_dir: Path,
    size: str,
    iterations: int,
    jfr_settings: str,
    extra_jvm_args: list[str],
    extra_dacapo_args: list[str],
    timeout: int | None,
) -> bool:
    """
    Run a single DaCapo benchmark with JFR profiling enabled.

    Returns True on success, False on failure.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    jfr_file = jfr_dir / f"{benchmark}-{size}-{timestamp}.jfr"

    # Ensure output directory exists
    jfr_dir.mkdir(parents=True, exist_ok=True)

    # Build the command
    jfr_opts = (
        f"-XX:StartFlightRecording=filename={jfr_file},"
        f"settings={jfr_settings},"
        f"dumponexit=true"
    )

    cmd = [
        java,
        jfr_opts,
        *extra_jvm_args,
        "-jar", str(jar),
        "-s", size,
        "-n", str(iterations),
        "--no-validation",
        *extra_dacapo_args,
        benchmark,
    ]

    print(f"\n{'─' * 70}")
    print(f"  Benchmark : {benchmark}")
    print(f"  Size      : {size}")
    print(f"  Iterations: {iterations}")
    print(f"  JFR file  : {jfr_file}")
    print(f"  Command   : {' '.join(cmd)}")
    print(f"{'─' * 70}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(BENCHMARKS_DIR),
            timeout=timeout,
        )
        if result.returncode != 0:
            print(f"  [FAIL] {benchmark} exited with code {result.returncode}")
            return False
        else:
            print(f"  [OK] {benchmark} completed. JFR → {jfr_file}")
            return True
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] {benchmark} exceeded {timeout}s timeout")
        return False
    except Exception as e:
        print(f"  [ERROR] {benchmark}: {e}")
        return False


def do_profile(args: argparse.Namespace) -> None:
    """Profile one or more DaCapo benchmarks with JFR."""
    jar = find_dacapo_jar()
    java = find_java()

    print(f"Using jar : {jar}")
    print(f"Using java: {java}")

    # Determine which benchmarks to run
    available = get_available_benchmarks()
    if not available:
        print("[ERROR] No benchmarks available in the jar.", file=sys.stderr)
        sys.exit(1)

    if args.benchmarks:
        # Validate requested benchmarks
        requested = args.benchmarks
        for bm in requested:
            if bm not in available:
                print(f"[WARN] Benchmark '{bm}' not found in jar (available: {', '.join(available)}). Skipping.")
        benchmarks = [bm for bm in requested if bm in available]
    else:
        benchmarks = available

    if not benchmarks:
        print("[ERROR] No valid benchmarks to profile.", file=sys.stderr)
        sys.exit(1)

    # JFR output directory
    jfr_dir = Path(args.jfr_dir) if args.jfr_dir else DEFAULT_JFR_DIR
    jfr_dir.mkdir(parents=True, exist_ok=True)

    # Extra JVM arguments
    extra_jvm_args = []
    if args.jvm_args:
        extra_jvm_args = args.jvm_args.split()

    # Extra DaCapo arguments
    extra_dacapo_args = []
    if args.dacapo_args:
        extra_dacapo_args = args.dacapo_args.split()

    print(f"\nBenchmarks to profile: {', '.join(benchmarks)}")
    print(f"JFR output directory : {jfr_dir}")
    print(f"Size                 : {args.size}")
    print(f"Iterations           : {args.iterations}")
    print(f"JFR settings         : {args.jfr_settings}")
    if args.timeout:
        print(f"Timeout per benchmark: {args.timeout}s")
    print()

    results: dict[str, bool] = {}
    for bm in benchmarks:
        ok = profile_benchmark(
            java=java,
            jar=jar,
            benchmark=bm,
            jfr_dir=jfr_dir,
            size=args.size,
            iterations=args.iterations,
            jfr_settings=args.jfr_settings,
            extra_jvm_args=extra_jvm_args,
            extra_dacapo_args=extra_dacapo_args,
            timeout=args.timeout,
        )
        results[bm] = ok

    # Print summary
    print(f"\n{'=' * 70}")
    print("  Profiling Summary")
    print(f"{'=' * 70}")
    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    for bm, ok in results.items():
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  {bm}")
    print(f"\n  Total: {passed} passed, {failed} failed out of {len(results)}")
    print(f"  JFR recordings saved to: {jfr_dir}")
    print(f"{'=' * 70}")

    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and profile DaCapo benchmarks with JFR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # --- build ---
    build_parser = subparsers.add_parser("build", help="Build the DaCapo benchmark suite")
    build_parser.add_argument(
        "build_target", nargs="?", default="dist",
        help="Ant build target (default: dist). Use a benchmark name to build just one, e.g. 'h2'.",
    )

    # --- list ---
    subparsers.add_parser("list", help="List available benchmarks in the DaCapo jar")

    # --- profile ---
    profile_parser = subparsers.add_parser(
        "profile", help="Profile benchmarks with JFR"
    )
    profile_parser.add_argument(
        "benchmarks", nargs="*",
        help="Benchmark name(s) to profile. If omitted, all available benchmarks are profiled.",
    )
    profile_parser.add_argument(
        "-s", "--size", default="default",
        choices=["small", "default", "large", "vlarge"],
        help="Workload size (default: default).",
    )
    profile_parser.add_argument(
        "-n", "--iterations", type=int, default=1,
        help="Number of benchmark iterations (default: 1).",
    )
    profile_parser.add_argument(
        "--jfr-settings", default="profile",
        help="JFR settings profile to use (default: 'profile'). "
             "Use 'default' for lower overhead or a path to a custom .jfc file.",
    )
    profile_parser.add_argument(
        "--jfr-dir",
        help=f"Directory for JFR output files (default: {DEFAULT_JFR_DIR}).",
    )
    profile_parser.add_argument(
        "--jvm-args",
        help="Extra JVM arguments as a single quoted string, e.g. '--jvm-args=\"-Xmx4g -Xms2g\"'.",
    )
    profile_parser.add_argument(
        "--dacapo-args",
        help="Extra DaCapo harness arguments as a single quoted string.",
    )
    profile_parser.add_argument(
        "--timeout", type=int, default=None,
        help="Timeout in seconds per benchmark (default: no timeout).",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Support chaining: "build profile h2" is treated as build first then profile
    # But argparse sub-commands don't natively chain, so we handle the simple
    # case of "build" being a positional followed by profile-like args.
    if args.command == "build":
        do_build(args)
    elif args.command == "list":
        do_list(args)
    elif args.command == "profile":
        do_profile(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
