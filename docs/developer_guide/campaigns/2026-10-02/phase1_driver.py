#!/usr/bin/env python3
"""Phase 1 check: real MOPAC and ORCA runs, concurrently, through the task layer.

Runs a set of small MOPAC and ORCA calculations as one TaskSet in a LocalPool
on this machine, using the codes as configured in ``<root>/mopac.ini`` and
``<root>/orca.ini``, and prints one line per task as it finishes::

    key  state  restored  seconds  energy

Run it, kill it part way (Ctrl-C, ``kill``, or ``kill -9``), and run it again in
the same directory: finished tasks come back as ``restored`` and are not
recomputed, and the leftovers of a ``kill -9`` are killed and rerun.

    python phase1_driver.py --directory /tmp/phase1 [--root ~/SEAMM]
        [--mopac 8] [--orca 4] [--orca-cores 2]

Only the standard library and seamm_exec are needed.
"""

import argparse
import configparser
import os
from pathlib import Path
import re
import time

from seamm_exec import Local, LocalPool, Resources, Task, TaskSet

# Small molecules as XYZ (Angstrom)
MOLECULES = {
    "water": [
        ("O", 0.0, 0.0, 0.1173),
        ("H", 0.0, 0.7572, -0.4692),
        ("H", 0.0, -0.7572, -0.4692),
    ],
    "ammonia": [
        ("N", 0.0, 0.0, 0.1128),
        ("H", 0.0, 0.9377, -0.2633),
        ("H", 0.8121, -0.4689, -0.2633),
        ("H", -0.8121, -0.4689, -0.2633),
    ],
    "methane": [
        ("C", 0.0, 0.0, 0.0),
        ("H", 0.6276, 0.6276, 0.6276),
        ("H", -0.6276, -0.6276, 0.6276),
        ("H", -0.6276, 0.6276, -0.6276),
        ("H", 0.6276, -0.6276, -0.6276),
    ],
    "hf": [("F", 0.0, 0.0, 0.0917), ("H", 0.0, 0.0, -0.8255)],
    "formaldehyde": [
        ("C", 0.0, 0.0, -0.5297),
        ("O", 0.0, 0.0, 0.6755),
        ("H", 0.0, 0.9370, -1.1150),
        ("H", 0.0, -0.9370, -1.1150),
    ],
    "methanol": [
        ("C", -0.0469, 0.6638, 0.0),
        ("O", -0.0469, -0.7570, 0.0),
        ("H", -1.0862, 0.9732, 0.0),
        ("H", 0.4330, 1.0809, 0.8857),
        ("H", 0.4330, 1.0809, -0.8857),
        ("H", 0.8613, -1.0770, 0.0),
    ],
}


def mopac_task(name, i):
    atoms = MOLECULES[name]
    lines = ["PM7 PRECISE", f"{name} {i}", ""]
    for symbol, x, y, z in atoms:
        lines.append(f"{symbol} {x:.4f} 1 {y:.4f} 1 {z:.4f} 1")
    return Task(
        key=f"mopac-{name}-{i}",
        program="mopac",
        cmd=["{code}", "mopac.dat", ">", "stdout.txt", "2>", "stderr.txt"],
        files={"mopac.dat": "\n".join(lines) + "\n"},
        return_files=["mopac.out", "mopac.arc", "mopac.aux"],
        in_situ=True,
        shell=True,
        resources=Resources(ntasks=1),
        estimated_seconds=1.0,
    )


def orca_config(root):
    """ORCA's section of orca.ini; the binary must be called by its full path."""
    full = configparser.ConfigParser()
    full.read(Path(root).expanduser() / "orca.ini")
    config = dict(full.items("local"))
    config["code"] = str(Path(config["code"]).expanduser())
    return config


def orca_task(name, i, ncores, config):
    atoms = MOLECULES[name]
    # Distinct inputs per i (a comment), so each is its own calculation.
    lines = [f"# {name} {i}", "! B3LYP def2-SVP TightSCF"]
    if ncores > 1:
        lines.append(f"%pal nprocs {ncores} end")
    lines.append("%maxcore 1000")
    lines.append("* xyz 0 1")
    for symbol, x, y, z in atoms:
        lines.append(f"{symbol} {x:.4f} {y:.4f} {z:.4f}")
    lines.append("*")
    env = {}
    if ncores > 1:
        env["OMPI_MCA_hwloc_base_binding_policy"] = "none"
        # ORCA runs whatever mpirun is first on PATH; it must be the OpenMPI
        # it was built with (orca.ini's library-path), as orca_step arranges.
        library_path = config.get("library-path", "")
        if library_path:
            bindir = Path(library_path).expanduser().parent / "bin"
            env["PATH"] = f"{bindir}:{os.environ['PATH']}"
            env["LD_LIBRARY_PATH"] = str(Path(library_path).expanduser())
    return Task(
        key=f"orca-{name}-{i}",
        program="orca",
        cmd=["{code}", "orca.inp", ">", "orca.out", "2>", "orca.err"],
        files={"orca.inp": "\n".join(lines) + "\n"},
        return_files=["orca.out", "orca.err", "orca.xyz"],
        in_situ=True,
        shell=True,
        env=env,
        config=config,
        resources=Resources(ntasks=ncores, mem_per_cpu=1000 * 1_000_000),
        estimated_seconds=5.0,
        success_text={"orca.out": "ORCA TERMINATED NORMALLY"},
    )


def energy(result):
    if result.key.startswith("mopac"):
        text = result.files.get("mopac.out") or ""
        m = re.search(r"FINAL HEAT OF FORMATION =\s*(-?[0-9.]+) KCAL/MOL", text)
        return f"{float(m.group(1)):.5f} kcal/mol" if m else "-"
    text = result.files.get("orca.out") or ""
    m = re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?[0-9.]+)", text)
    return f"{float(m[-1]):.8f} Eh" if m else "-"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--directory", required=True)
    parser.add_argument("--root", default="~/SEAMM")
    parser.add_argument("--mopac", type=int, default=8)
    parser.add_argument("--orca", type=int, default=4)
    parser.add_argument("--orca-cores", type=int, default=2)
    args = parser.parse_args()

    names = list(MOLECULES)
    executor = Local()
    pool = LocalPool(executor, root=Path(args.root).expanduser())
    ts = TaskSet(
        directory=args.directory, executor=executor, backend=pool, poll_interval=0.2
    )
    config = orca_config(args.root) if args.orca else None
    for i in range(args.orca):
        ts.add(orca_task(names[i % len(names)], i, args.orca_cores, config))
    for i in range(args.mopac):
        ts.add(mopac_task(names[i % len(names)], i))

    print(f"pool: {pool.capacity()['cores']} cores; pid {os.getpid()}", flush=True)
    t0 = time.monotonic()
    for r in ts.run():
        print(
            f"{r.key:24s} {r.state:9s} {'restored' if r.restored else 'ran':8s} "
            f"{time.monotonic() - t0:7.1f}  {energy(r)}",
            flush=True,
        )
    print(f"summary: {ts.summary()}; {time.monotonic() - t0:.1f} s", flush=True)


if __name__ == "__main__":
    main()
