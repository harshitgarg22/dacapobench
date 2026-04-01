#!/usr/bin/env python3
"""
Avrora CustomTransmissionList benchmark.

Builds and profiles avrora before/after the struct-of-arrays patch,
then compares JFR recordings side by side.

Usage:
    python scripts/avrora/avrora-benchmark.py                          # full run
    python scripts/avrora/avrora-benchmark.py -s default -n 3          # bigger workload
    python scripts/avrora/avrora-benchmark.py --skip-baseline          # reuse last baseline
    python scripts/avrora/avrora-benchmark.py compare <a.jfr> <b.jfr>  # compare only
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
BENCH_DIR = REPO_ROOT / "benchmarks"
AVRORA_BUILD = BENCH_DIR / "bms" / "avrora" / "build" / "avrora"
PATCH_FILE = AVRORA_BUILD / "instrumented.patch"
JFR_DIR = REPO_ROOT / "scripts" / "results" / "jfr"
DEFAULT_JAVA8 = Path.home() / ".sdkman" / "candidates" / "java" / "8.0.452-amzn"

# ── Tool discovery ───────────────────────────────────────────────────────────


def find_java8() -> str:
    """Locate JDK 8 for compilation. Checks JAVA_HOME, then the sdkman default."""
    java_home = os.environ.get("JAVA_HOME", str(DEFAULT_JAVA8))
    if os.path.isfile(os.path.join(java_home, "bin", "java")):
        return java_home
    print(f"[ERROR] JDK 8 not found at {java_home}. Set JAVA_HOME.", file=sys.stderr)
    sys.exit(1)


def find_java() -> str:
    """Locate the default java binary on PATH (JDK 11+ expected for JFR support)."""
    java = shutil.which("java")
    if java:
        return java
    print("[ERROR] java not found on PATH.", file=sys.stderr)
    sys.exit(1)


def find_ant() -> str:
    """Locate the ant build tool on PATH."""
    ant = shutil.which("ant")
    if not ant:
        print("[ERROR] ant not found on PATH.", file=sys.stderr)
        sys.exit(1)
    return ant


def find_dacapo_jar() -> Path:
    """Find the most recently modified dacapo suite jar in the benchmarks directory."""
    jars = sorted(
        BENCH_DIR.glob("dacapo-*.jar"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not jars:
        print("[ERROR] No dacapo jar in benchmarks/.", file=sys.stderr)
        sys.exit(1)
    return jars[0]


def find_latest_jfr(label: str) -> Path | None:
    """Find the most recent JFR file for a given label (e.g. 'baseline' or 'modified')."""
    matches = sorted(
        JFR_DIR.glob(f"avrora-{label}-*.jfr"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


# ── Shell helpers ────────────────────────────────────────────────────────────


def banner(msg: str):
    """Print a visual section header."""
    print(f"\n{'=' * 70}\n  {msg}\n{'=' * 70}\n")


def run(cmd, cwd=None, env=None, check=True):
    """Run a shell command, printing it first. Exits on failure if check=True."""
    merged = {**os.environ, **(env or {})}
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged)
    if check and r.returncode != 0:
        print(f"  [FAIL] exit code {r.returncode}", file=sys.stderr)
        sys.exit(r.returncode)
    return r


# ── Build / restore / patch ──────────────────────────────────────────────────


def restore_original():
    """Reset the avrora source tree to the Init commit state.

    Uses git reset + checkout to revert tracked file changes, then git clean
    to remove untracked files (e.g. CustomTransmissionList.java) from the
    radio package directory.
    """
    banner("Restoring original avrora source")
    run(["git", "reset", "HEAD", "--", "."], cwd=AVRORA_BUILD)
    run(["git", "checkout", "--", "."], cwd=AVRORA_BUILD)
    run(["git", "clean", "-fd", "--", "src/avrora/sim/radio/"], cwd=AVRORA_BUILD)


def apply_patch():
    """Apply the struct-of-arrays patch (instrumented.patch) to the avrora source."""
    banner("Applying instrumented.patch")
    if not PATCH_FILE.exists():
        print(f"[ERROR] Patch not found: {PATCH_FILE}", file=sys.stderr)
        sys.exit(1)
    run(["git", "apply", str(PATCH_FILE)], cwd=AVRORA_BUILD)


def build_avrora(java8_home: str):
    """Compile avrora with JDK 8 via ant, then package into the dacapo suite jar.

    Removes stale class files first to avoid version mismatch errors when
    switching between JDK versions.
    """
    banner("Building avrora")
    build_output = BENCH_DIR / "bms" / "avrora" / "build" / "build"
    if build_output.exists():
        shutil.rmtree(build_output)
    run(
        [
            find_ant(),
            "-buildfile",
            "bms/avrora/build.xml",
            "bm-build",
            "jar",
            "data-copy-checksum",
            "complete",
            "-Dbuild.target-jar=dacapo-23.11-chopin.jar",
            "-Dbuild.target-jars=dacapo-23.11-chopin/jar",
            "-Dbuild.target-data=dacapo-23.11-chopin/dat",
        ],
        cwd=BENCH_DIR,
        env={"JAVA_HOME": java8_home},
    )


def profile_avrora(label: str, size: str, iterations: int) -> tuple:
    """Run avrora under JFR profiling and return (jfr_path, wall_clock_seconds).

    Records jdk.ExecutionSample (CPU) and jdk.ObjectAllocationSample (allocation)
    events using the built-in 'profile' settings. The JFR file is written to
    scripts/results/jfr/ with a timestamped filename.
    """
    banner(f"Profiling avrora ({label}, size={size}, n={iterations})")
    JFR_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    jfr_file = JFR_DIR / f"avrora-{label}-{size}-{ts}.jfr"
    t0 = time.monotonic()
    run(
        [
            find_java(),
            f"-XX:StartFlightRecording=filename={jfr_file},settings=profile,dumponexit=true",
            "-jar",
            str(find_dacapo_jar()),
            "-s",
            size,
            "-n",
            str(iterations),
            "--no-validation",
            "avrora",
        ],
        cwd=BENCH_DIR,
    )
    wall_secs = time.monotonic() - t0
    print(f"  JFR -> {jfr_file}")
    print(f"  Wall clock: {wall_secs:.1f}s")
    return jfr_file, wall_secs


# ── JFR parsing ──────────────────────────────────────────────────────────────

RECEIVER_METHODS = {
    "deliverByte",
    "earliestNewTransmission",
    "getIntersection",
    "isChannelClear",
    "fireUnlocked",
    "fireLocked",
}
ARBITRATOR_METHODS = {"mergeTransmissions", "lockTransmission", "computeReceivedPower"}
OVERHEAD_CLASSES = {
    "java.util.LinkedList",
    "java.util.LinkedList$Node",
    "java.util.LinkedList$ListItr",
    "java.util.AbstractList$Itr",
}
CUSTOM_LIST_CLASSES = {"avrora.sim.radio.CustomTransmissionList"}
RADIO_PACKAGE = "avrora.sim.radio"


def jfr_events(path: str, event_type: str) -> list[dict]:
    """Extract events of a given type from a JFR file using the jfr CLI tool.

    Shells out to `jfr print --json --events <type> <path>` and returns
    the list of event dicts from the parsed JSON output.
    """
    r = subprocess.run(
        ["jfr", "print", "--json", "--events", event_type, path],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        print(f"[ERROR] jfr failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(r.stdout).get("recording", {}).get("events", [])


def stack_methods(ev: dict) -> list[str]:
    """Extract fully-qualified method names from a JFR event's stack trace.

    Returns a list of strings like 'avrora.sim.radio.Medium$Receiver.deliverByte'.
    """
    st = ev.get("values", {}).get("stackTrace")
    if not st:
        return []
    out = []
    for f in st.get("frames", []):
        m = f.get("method", {})
        cls = m.get("type", {}).get("name", "").replace("/", ".")
        out.append(f"{cls}.{m.get('name', '')}")
    return out


def alloc_class(ev: dict) -> str:
    """Extract the allocated class name from a jdk.ObjectAllocationSample event.

    Normalizes JVM internal names (slashes) to dot-separated class names.
    For array types, returns JVM notation like '[B' (byte[]), '[D' (double[]).
    """
    obj = ev.get("values", {}).get("objectClass")
    if isinstance(obj, dict):
        return obj.get("name", "").replace("/", ".")
    return str(obj).replace("/", "") if obj else ""


def alloc_weight(ev: dict) -> float:
    """Extract the allocation weight (bytes) from a JFR allocation event.

    Handles numeric values directly and string values with units (e.g. '1.5 MB').
    Returns weight in bytes.
    """
    w = ev.get("values", {}).get("weight")
    if w is None:
        return 0.0
    if isinstance(w, (int, float)):
        return float(w)
    if isinstance(w, str):
        parts = w.strip().split()
        try:
            val = float(parts[0])
        except ValueError:
            return 0.0
        if len(parts) > 1:
            u = parts[1].upper()
            val *= {"KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(u, 1)
        return val
    return 0.0


# ── Analysis ─────────────────────────────────────────────────────────────────


def analyze_cpu(events):
    """Analyze jdk.ExecutionSample events for radio hot-path activity.

    For each CPU sample, walks the stack trace to identify:
    - Whether the sample is in the avrora.sim.radio package
    - Which Medium.Receiver methods appear (deliverByte, getIntersection, etc.)
    - Which Arbitrator methods appear (mergeTransmissions, lockTransmission, etc.)
    - Whether LinkedList, Iterator, or CustomTransmissionList frames are on stack

    Returns a dict with counts for each category.
    """
    total = len(events)
    radio = 0
    recv_hits = defaultdict(int)
    arb_hits = defaultdict(int)
    ll_stack = it_stack = ctl_stack = 0

    for ev in events:
        methods = stack_methods(ev)
        in_radio = False
        for m in methods:
            if RADIO_PACKAGE in m:
                in_radio = True
            for rm in RECEIVER_METHODS:
                if rm in m and "Medium" in m:
                    recv_hits[rm] += 1
            for am in ARBITRATOR_METHODS:
                if am in m:
                    arb_hits[am] += 1
            if "LinkedList" in m:
                ll_stack += 1
                break
            if "Iterator" in m or "ListItr" in m or "Itr." in m:
                it_stack += 1
                break
            if "CustomTransmissionList" in m:
                ctl_stack += 1
                break
        if in_radio:
            radio += 1

    return dict(
        total=total,
        radio=radio,
        recv_hits=dict(recv_hits),
        arb_hits=dict(arb_hits),
        ll_stack=ll_stack,
        it_stack=it_stack,
        ctl_stack=ctl_stack,
    )


def analyze_alloc(events):
    """Analyze jdk.ObjectAllocationSample events for allocation pressure.

    For each allocation sample, classifies it into:
    - Total allocation weight across all classes
    - LinkedList/Iterator overhead (OVERHEAD_CLASSES): the allocations we're eliminating
    - CustomTransmissionList allocations: tracked by class name OR by stack frame,
      to catch both the list object itself and its backing arrays (long[], double[], etc.)
    - Per-class breakdown for allocations originating from avrora.sim.radio

    Returns a dict with sample counts and byte weights for each category.
    """
    total = len(events)
    total_w = oh_n = oh_w = ctl_n = ctl_w = 0.0
    radio_classes = defaultdict(lambda: {"count": 0, "weight": 0.0})

    for ev in events:
        cls = alloc_class(ev)
        w = alloc_weight(ev)
        total_w += w
        methods = stack_methods(ev)
        if any(RADIO_PACKAGE in m for m in methods):
            radio_classes[cls]["count"] += 1
            radio_classes[cls]["weight"] += w
        short = cls.rstrip("[]")
        if any(oc in short for oc in OVERHEAD_CLASSES):
            oh_n += 1
            oh_w += w
        # Track allocations OF or FROM CustomTransmissionList
        if any(cc in cls for cc in CUSTOM_LIST_CLASSES):
            ctl_n += 1
            ctl_w += w
        elif any("CustomTransmissionList" in m for m in methods):
            ctl_n += 1
            ctl_w += w

    return dict(
        total=int(total),
        total_w=total_w,
        oh_n=int(oh_n),
        oh_w=oh_w,
        ctl_n=int(ctl_n),
        ctl_w=ctl_w,
        radio_classes=dict(radio_classes),
    )


# ── Report ───────────────────────────────────────────────────────────────────


def fmt_bytes(b):
    """Format a byte count as a human-readable string (e.g. '1.5 MB')."""
    b = float(b)
    if b >= 1024**2:
        return f"{b / 1024**2:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b:.0f} B"


def pct(old, new):
    """Compute percentage change string from old to new value."""
    old, new = float(old), float(new)
    if old == 0:
        return "(same)" if new == 0 else "(new)"
    c = ((new - old) / old) * 100
    return f"({'+'if c > 0 else ''}{c:.1f}%)"


def row(label, bv, mv, fmt=str):
    """Print a single comparison row with baseline, modified, and change columns."""
    print(f"  {label:<40s}  {fmt(bv):>12s}  {fmt(mv):>12s}  {pct(bv, mv):>12s}")


def compare_jfr(base_path: str, mod_path: str, base_wall=None, mod_wall=None):
    """Parse two JFR files and print a side-by-side comparison report.

    Compares CPU execution samples and object allocation samples between
    baseline and modified runs. Breaks down results by radio package methods,
    LinkedList/Iterator overhead, and CustomTransmissionList activity.

    Wall clock times are included in the summary if provided (only available
    when both runs were performed in the same session via cmd_run).
    """
    for p in (base_path, mod_path):
        if not Path(p).exists():
            print(f"[ERROR] Not found: {p}", file=sys.stderr)
            sys.exit(1)

    print("Parsing JFR events...")
    bc = analyze_cpu(jfr_events(base_path, "jdk.ExecutionSample"))
    mc = analyze_cpu(jfr_events(mod_path, "jdk.ExecutionSample"))
    ba = analyze_alloc(jfr_events(base_path, "jdk.ObjectAllocationSample"))
    ma = analyze_alloc(jfr_events(mod_path, "jdk.ObjectAllocationSample"))

    sep = "=" * 90
    print(f"\n{sep}")
    print("  AVRORA JFR COMPARISON: CustomTransmissionList Migration")
    print(sep)
    print(f"  Baseline : {Path(base_path).name}")
    print(f"  Modified : {Path(mod_path).name}")

    # CPU
    print(f"\n  CPU EXECUTION SAMPLES")
    print(f"  {'-' * 86}")
    print(f"  {'Metric':<40s}  {'Baseline':>12s}  {'Modified':>12s}  {'Change':>12s}")
    print(f"  {'-' * 86}")
    row("Total CPU samples", bc["total"], mc["total"])
    row("Samples in avrora.sim.radio", bc["radio"], mc["radio"])

    all_rm = sorted(set(list(bc["recv_hits"]) + list(mc["recv_hits"])))
    if all_rm:
        print(f"\n  Medium.Receiver hot methods:")
        for m in all_rm:
            row(f"    {m}", bc["recv_hits"].get(m, 0), mc["recv_hits"].get(m, 0))

    all_am = sorted(set(list(bc["arb_hits"]) + list(mc["arb_hits"])))
    if all_am:
        print(f"\n  Arbitrator methods:")
        for m in all_am:
            row(f"    {m}", bc["arb_hits"].get(m, 0), mc["arb_hits"].get(m, 0))

    print(f"\n  Iterator/LinkedList overhead in CPU stacks:")
    row("LinkedList in stack", bc["ll_stack"], mc["ll_stack"])
    row("Iterator in stack", bc["it_stack"], mc["it_stack"])
    row("CustomTransmissionList in stack", bc["ctl_stack"], mc["ctl_stack"])

    # Allocations
    print(f"\n  OBJECT ALLOCATION SAMPLES")
    print(f"  {'-' * 86}")
    print(f"  {'Metric':<40s}  {'Baseline':>12s}  {'Modified':>12s}  {'Change':>12s}")
    print(f"  {'-' * 86}")
    row("Total allocation samples", ba["total"], ma["total"])
    row("Total allocation weight", ba["total_w"], ma["total_w"], fmt_bytes)
    print()
    row("LinkedList/Iterator alloc samples", ba["oh_n"], ma["oh_n"])
    row("LinkedList/Iterator alloc weight", ba["oh_w"], ma["oh_w"], fmt_bytes)
    print()
    row("CustomTransmissionList alloc samples", ba["ctl_n"], ma["ctl_n"])
    row("CustomTransmissionList alloc weight", ba["ctl_w"], ma["ctl_w"], fmt_bytes)

    # Radio breakdown
    all_cls = sorted(set(list(ba["radio_classes"]) + list(ma["radio_classes"])))
    if all_cls:
        print(f"\n  Allocations from avrora.sim.radio:")
        for cls in all_cls:
            b = ba["radio_classes"].get(cls, {"count": 0, "weight": 0.0})
            m = ma["radio_classes"].get(cls, {"count": 0, "weight": 0.0})
            short = cls.split(".")[-1] if "." in cls else cls
            bs = f"{b['count']} ({fmt_bytes(b['weight'])})"
            ms = f"{m['count']} ({fmt_bytes(m['weight'])})"
            print(
                f"    {short:<36s}  {bs:>18s}  {ms:>18s}  {pct(b['weight'], m['weight'])}"
            )

    # Summary
    print(f"\n  SUMMARY")
    print(f"  {'-' * 86}")
    saved_w = ba["total_w"] - ma["total_w"]
    print(
        f"  Total allocation weight reduction:  {fmt_bytes(saved_w)}  {pct(ba['total_w'], ma['total_w'])}"
    )
    ll_saved = ba["oh_w"] - ma["oh_w"]
    print(
        f"  LinkedList/Iterator weight removed:  {fmt_bytes(ll_saved)}  {pct(ba['oh_w'], ma['oh_w'])}"
    )
    if base_wall is not None and mod_wall is not None:
        fmt_t = lambda s: f"{s:.1f}s"
        print(
            f"  Wall clock time:  baseline {fmt_t(base_wall)},  modified {fmt_t(mod_wall)}  {pct(base_wall, mod_wall)}"
        )

    print(f"\n{sep}\n")


# ── CLI ──────────────────────────────────────────────────────────────────────


def cmd_run(args):
    """Build and profile both baseline and modified avrora, then compare JFR results."""
    java8 = find_java8()
    print(f"JDK 8 (build): {java8}")
    print(f"Java (run):    {find_java()}")
    print(f"Ant:           {find_ant()}")

    # Baseline
    base_wall = None
    if args.skip_baseline:
        baseline = find_latest_jfr("baseline")
        if not baseline:
            print("[ERROR] No baseline JFR found.", file=sys.stderr)
            sys.exit(1)
        print(f"\nReusing baseline: {baseline}")
    else:
        restore_original()
        build_avrora(java8)
        baseline, base_wall = profile_avrora("baseline", args.size, args.iterations)

    # Modified
    mod_wall = None
    if args.skip_modified:
        modified = find_latest_jfr("modified")
        if not modified:
            print("[ERROR] No modified JFR found.", file=sys.stderr)
            sys.exit(1)
        print(f"\nReusing modified: {modified}")
    else:
        restore_original()
        apply_patch()
        build_avrora(java8)
        modified, mod_wall = profile_avrora("modified", args.size, args.iterations)

    # Compare
    banner("Comparing JFR recordings")
    compare_jfr(str(baseline), str(modified), base_wall, mod_wall)
    print(f"Baseline JFR: {baseline}")
    print(f"Modified JFR: {modified}")


def cmd_compare(args):
    """Compare two existing JFR files without building or profiling."""
    compare_jfr(args.baseline, args.modified)


def cmd_save_patch(args):
    """Save current working tree changes in src/avrora/sim/radio/ as instrumented.patch.

    Handles both modified tracked files and new untracked files (e.g.
    CustomTransmissionList.java) by temporarily staging new files for
    git diff --cached. Verifies the patch applies cleanly by stashing
    all changes, running git apply --check, then restoring the stash.
    """
    banner("Saving current changes to instrumented.patch")

    radio_dir = "src/avrora/sim/radio"
    # Find new (untracked) files in the radio directory
    r = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", radio_dir],
        capture_output=True,
        text=True,
        cwd=str(AVRORA_BUILD),
    )
    new_files = [f.strip() for f in r.stdout.splitlines() if f.strip()]

    # Stage new files temporarily so git diff --cached picks them up
    if new_files:
        subprocess.run(["git", "add"] + new_files, cwd=str(AVRORA_BUILD))

    # Generate patch: cached (new files) + unstaged (modified tracked files)
    with open(str(PATCH_FILE), "w") as pf:
        if new_files:
            subprocess.run(
                ["git", "diff", "--cached", "HEAD", "--", radio_dir],
                stdout=pf,
                cwd=str(AVRORA_BUILD),
            )
        subprocess.run(
            ["git", "diff", "HEAD", "--", radio_dir],
            stdout=pf,
            cwd=str(AVRORA_BUILD),
        )

    # Unstage new files
    if new_files:
        subprocess.run(
            ["git", "reset", "HEAD", "--"] + new_files,
            capture_output=True,
            cwd=str(AVRORA_BUILD),
        )

    print(f"  Saved: {PATCH_FILE}")

    # Verify: stash changes, check patch, restore
    subprocess.run(["git", "stash", "--include-untracked", "-q"], cwd=str(AVRORA_BUILD))
    r = subprocess.run(
        ["git", "apply", "--check", str(PATCH_FILE)],
        capture_output=True,
        text=True,
        cwd=str(AVRORA_BUILD),
    )
    subprocess.run(["git", "stash", "pop", "-q"], cwd=str(AVRORA_BUILD))
    if r.returncode == 0:
        print("  Verified: patch applies cleanly on HEAD")
    else:
        print(
            f"  [WARNING] Patch may not apply cleanly: {r.stderr.strip()}",
            file=sys.stderr,
        )


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command")

    # default: run
    run_p = sub.add_parser("run", help="Build, profile, and compare (default)")
    run_p.add_argument("-s", "--size", default="small")
    run_p.add_argument("-n", "--iterations", type=int, default=1)
    run_p.add_argument("--skip-baseline", action="store_true")
    run_p.add_argument("--skip-modified", action="store_true")

    # compare
    cmp_p = sub.add_parser("compare", help="Compare two existing JFR files")
    cmp_p.add_argument("baseline", help="Baseline .jfr file")
    cmp_p.add_argument("modified", help="Modified .jfr file")

    # save-patch
    sub.add_parser("save-patch", help="Save current changes as instrumented.patch")

    args = p.parse_args()

    if args.command == "compare":
        cmd_compare(args)
    elif args.command == "save-patch":
        cmd_save_patch(args)
    else:
        # default to "run" when no subcommand given
        if args.command is None:
            args = run_p.parse_args(sys.argv[1:])
        cmd_run(args)


if __name__ == "__main__":
    main()
