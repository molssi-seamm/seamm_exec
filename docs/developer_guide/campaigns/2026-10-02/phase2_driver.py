#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Phase 2 live test: ORCA single points as bundled tasks on a SLURM cluster.

    python phase2_driver.py <job dir> --target <name> --ini <path to targets ini>

Runs a TaskSet of small-molecule B3LYP/def2-SVP single points with the
SchedulerBackend of the target (e.g. this Mac -> TinkerCliffs over ssh with
rsync staging). Run it, kill it (kill -9) once the bundles are in the queue, and
run it again: the second run adopts the bundles from the manifest and polls
them instead of submitting again.

The tasks name only the program ("orca") and a command template; ORCA is
resolved on the cluster from its own <root>/orca.ini. ``--program mopac``
runs PM6 single points instead (MolSSI10, SLURM 20.11).
"""

import argparse
import logging
import sys
import time

from seamm_exec import Resources, Task, TaskSet
from seamm_exec.targets import find_target

MOLECULES = {
    "water": [
        ("O", 0.0, 0.0, 0.117),
        ("H", 0.0, 0.757, -0.469),
        ("H", 0.0, -0.757, -0.469),
    ],
    "ammonia": [
        ("N", 0.0, 0.0, 0.112),
        ("H", 0.0, 0.938, -0.262),
        ("H", 0.812, -0.469, -0.262),
        ("H", -0.812, -0.469, -0.262),
    ],
    "methane": [
        ("C", 0.0, 0.0, 0.0),
        ("H", 0.629, 0.629, 0.629),
        ("H", -0.629, -0.629, 0.629),
        ("H", -0.629, 0.629, -0.629),
        ("H", 0.629, -0.629, -0.629),
    ],
    "hf": [("F", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.917)],
    "co": [("C", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 1.128)],
    "n2": [("N", 0.0, 0.0, 0.0), ("N", 0.0, 0.0, 1.098)],
    "h2s": [
        ("S", 0.0, 0.0, 0.103),
        ("H", 0.0, 0.966, -0.825),
        ("H", 0.0, -0.966, -0.825),
    ],
    "hcn": [("H", 0.0, 0.0, -1.064), ("C", 0.0, 0.0, 0.0), ("N", 0.0, 0.0, 1.156)],
}


def orca_task(name, atoms, ntasks):
    lines = [
        "! B3LYP def2-SVP TightSCF",
        f"%pal nprocs {ntasks} end",
        "%maxcore 1000",
        "* xyz 0 1",
    ]
    lines += [f"{el} {x:.4f} {y:.4f} {z:.4f}" for el, x, y, z in atoms]
    lines += ["*", ""]
    return Task(
        key=name,
        program="orca",
        cmd=["{code}", "orca.inp", ">", "orca.out", "2>", "orca.err"],
        shell=True,
        files={"orca.inp": "\n".join(lines)},
        return_files=["orca.out", "orca.err", "*.property.txt"],
        resources=Resources(ntasks=ntasks, mem_per_cpu=1200 * 1024**2, walltime=600),
        success_text={"orca.out": "ORCA TERMINATED NORMALLY"},
    )


def mopac_task(name, atoms):
    lines = ["PM6 1SCF", name, ""]
    lines += [f"{el} {x:.4f} 1 {y:.4f} 1 {z:.4f} 1" for el, x, y, z in atoms]
    lines.append("")
    return Task(
        key=name,
        program="mopac",
        cmd=["{code}", "mopac.dat"],
        shell=True,  # a conda installation is a "conda run ..." command line
        files={"mopac.dat": "\n".join(lines)},
        return_files=["mopac.out", "mopac.arc"],
        resources=Resources(ntasks=1, walltime=120),
        success_text={"mopac.out": "== MOPAC DONE =="},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_directory")
    parser.add_argument("--target", required=True)
    parser.add_argument("--ini", help="the targets ini (default <root>/<host>.ini)")
    parser.add_argument("--bundle-tasks", type=int, default=4)
    parser.add_argument("--ntasks", type=int, default=4)
    parser.add_argument("--program", default="orca", choices=["orca", "mopac"])
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    import os

    if args.ini:
        os.environ["SEAMM_TARGETS"] = args.ini
    os.environ["SEAMM_TARGET"] = args.target
    section = find_target(job_directory=args.job_directory)

    from pathlib import Path

    step = Path(args.job_directory) / "orca_step"
    step.mkdir(parents=True, exist_ok=True)
    ts = TaskSet(directory=step, target=section, bundle_tasks=args.bundle_tasks)
    ts.job_directory = Path(args.job_directory)  # no flowchart here
    for name, atoms in MOLECULES.items():
        if args.program == "orca":
            ts.add(orca_task(name, atoms, args.ntasks))
        else:
            ts.add(mopac_task(name, atoms))

    t0 = time.time()
    for result in ts.run():
        energy = ""
        out = result.files.get("orca.out") or result.files.get("mopac.out") or ""
        for line in out.splitlines():
            if "FINAL SINGLE POINT ENERGY" in line or "HEAT OF FORMATION" in line:
                energy = " ".join(line.split()[-3:])
        print(
            f"{time.time() - t0:7.1f} s  {result.key:8s} {result.state:9s} "
            f"restored={result.restored} attempts={result.attempts} {energy} "
            f"{result.reason or ''}",
            flush=True,
        )
    print(ts.summary())


if __name__ == "__main__":
    sys.exit(main())
