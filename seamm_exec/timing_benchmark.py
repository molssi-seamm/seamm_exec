# -*- coding: utf-8 -*-

"""The seed benchmark: standard calculations that place a machine class in the
cost model (campaign ``docs/developer_guide/campaigns/2026-10-05``, section 5).

A machine the model has never seen gets its offset from a few minutes of
standard runs; a sweep of core counts gives the parallel exponent that
production records, whose core count usually follows the size, cannot. The
runs are ordinary flowchart runs; their timing rows carry ``benchmark=<set>``
so a fit can tell them from production runs (they are chosen to span the
space) and so a code or compiler change can be checked against an earlier set.

**Each code step declares its own benchmark**, as it declares its cost-model
spec: a ``TIMING_BENCHMARK`` dict at the top of its package, found here through
the ``org.molssi.seamm`` entry points. Nothing chemical lives in seamm_exec;
a code knows what drives its own cost (molecules for ORCA, periodic boxes for
LAMMPS, cells and k-points for VASP). The declaration::

    TIMING_BENCHMARK = {
        "program": "orca",        # the timing records' program (orca.csv)
        "step": "ORCA",           # the step's name in a flowchart spec
        "section": "orca-step",   # its options section (default <program>-step)
        "parallel": True,         # sweep the core counts, or run once
        "systems": [              # in order of size; "size" is the number the
            {                     # limits below are compared with
                "name": "water",
                "size": 3,
                "steps": [{"FromSMILESStep": {"smiles string": "O"}}],
            },
            ...
        ],
        "chemistries": {          # each with the largest size it runs at, per tier
            "ORCA:DFT@B3LYP/def2-SVP": {"quick": 24, "full": 62},
            # optionally only some of the tasks below, e.g. a basis-set ladder
            "ORCA:DFT@B3LYP/def2-QZVPPD": {"quick": 15, "full": 24,
                                          "tasks": ["Energy"]},
        },
        "parameter": None,        # or the sub-step parameter the chemistry sets
                                  # (MOPAC's "hamiltonian") when the step does
                                  # not take the Model Chemistry
        "tasks": {                # sub-steps, each with its largest size per tier
            "Energy": {"quick": 62, "full": 302},
        },
        "variants": {             # optional: a task run again with these
            "Energy": [{}, {"results": {"gradients": {}}}],   # parameters;
        },                        # "_min_size"/"_max_size" bound a variant
    }

A system's ``steps`` are the flowchart steps that build it, as a spec writes
them; the ``quick`` tier keeps a code to a few minutes on one node. The
flowchart is built from the declarations with the installed plug-ins and run
once per core count with ``SEAMM_CE`` capping the cores the codes see::

    python -m seamm_exec.timing_benchmark --codes orca,mopac --cores 1,4,8 --fit
    python -m seamm_exec.timing_benchmark --build-only -o timing_benchmark.flow
"""

import argparse
import importlib
from importlib.metadata import entry_points
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil

_declarations = None


def declarations(refresh=False):
    """{program: TIMING_BENCHMARK} of the installed code steps that declare one,
    found through the ``org.molssi.seamm`` entry points (a step's package
    exports ``TIMING_BENCHMARK``). Cached; ``refresh`` looks again."""
    global _declarations
    if _declarations is None or refresh:
        found = {}
        for ep in entry_points(group="org.molssi.seamm"):
            package = ep.module.split(".", 1)[0]
            if package in found.values():
                continue
            try:
                module = importlib.import_module(package)
            except Exception:  # a broken plug-in must not stop the others
                continue
            declaration = getattr(module, "TIMING_BENCHMARK", None)
            if isinstance(declaration, dict) and declaration.get("program"):
                found[declaration["program"]] = declaration
        _declarations = found
    return _declarations


def _declaration(code):
    try:
        return declarations()[code]
    except KeyError:
        known = ", ".join(sorted(declarations())) or "none installed"
        raise ValueError(
            f"No installed step declares a timing benchmark for '{code}' "
            f"(TIMING_BENCHMARK); known: {known}"
        ) from None


def build_spec(codes=None, tier="quick"):
    """The YAML spec of the benchmark flowchart for ``codes`` (default every
    code with a declaration) at ``tier``: each code's systems in order of size,
    each built once, then its chemistries, tasks and variants up to the tier's
    size limits."""
    codes = tuple(codes or sorted(declarations()))
    lines = [f'title: "Timing benchmark ({tier}: {", ".join(codes)})"', "steps:"]
    for code in codes:
        spec = _declaration(code)
        for system in spec["systems"]:
            size = system["size"]
            emitted = False
            for chemistry, limits in spec["chemistries"].items():
                if size > limits[tier]:
                    continue
                for task, task_limits in spec["tasks"].items():
                    if size > task_limits[tier]:
                        continue
                    if "tasks" in limits and task not in limits["tasks"]:
                        continue
                    for variant in spec.get("variants", {}).get(task, [{}]):
                        variant = dict(variant)
                        if size < variant.pop("_min_size", 0):
                            continue
                        if size > variant.pop("_max_size", 10**12):
                            continue
                        if not emitted:
                            for step in system["steps"]:
                                lines.append(f"- {json.dumps(step)}")
                            emitted = True
                        if spec.get("parameter"):
                            # The method is a parameter of the sub-step
                            variant = {spec["parameter"]: chemistry, **variant}
                        else:
                            lines.append(
                                "- Model Chemistry: {model chemistry: "
                                f"{json.dumps(chemistry)}}}"
                            )
                        lines.append(f"- {spec['step']}:")
                        lines.append("    steps:")
                        if variant:
                            params = ", ".join(
                                f"{k}: {json.dumps(v)}" for k, v in variant.items()
                            )
                            lines.append(f"    - {task}: {{{params}}}")
                        else:
                            lines.append(f"    - {task}")
    return "\n".join(lines) + "\n"


#: The directory holding run_flowchart and seamm-flowchart to use (``--bin``);
#: default this Python's own, else the PATH
_bin = None


def performance_cores():
    """The cores a core sweep may use: on Apple silicon the performance cores
    only (``hw.perflevel0.physicalcpu``; a run on more adds efficiency cores,
    whose different speed would distort the parallel exponent), elsewhere the
    physical cores."""
    import platform

    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return psutil.cpu_count(logical=False) or 1


def _tool(name):
    """A console script: from ``--bin``, this Python's environment, or the PATH."""
    for directory in (_bin, Path(sys.executable).parent):
        if directory is not None:
            path = Path(directory) / name
            if path.exists():
                return str(path)
    return shutil.which(name) or name


def build_flowchart(spec_text, output):
    """Build the flowchart from the spec with ``seamm-flowchart build``."""
    output = Path(output)
    spec = output.with_suffix(".yaml")
    spec.write_text(spec_text)
    subprocess.run(
        [_tool("seamm-flowchart"), "build", str(spec), "-o", str(output)], check=True
    )
    return output


def run(
    codes=None,
    cores=(1, 4, 8, 16),
    tier="quick",
    directory=None,
    set_id=None,
    fit_after=False,
    dry_run=False,
):
    """Build the benchmark flowchart and run it once per core count.

    ``SEAMM_CE`` caps the cores (and memory) the codes see in each run, and each
    code's own ``ncores`` option is set on the command line (``<code>-step
    --ncores N``), since a machine's seamm.ini may pin it; ``SEAMM_TIMING_BENCHMARK``
    marks the timing rows. Core counts above this machine's usable cores (its
    performance cores on Apple silicon, else its physical cores) are skipped; a
    code that is not parallel runs once.
    """
    codes = tuple(codes or sorted(declarations()))
    directory = Path(directory or Path.cwd() / "timing_benchmark").expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    set_id = set_id or time.strftime("%Y%m%d-%H%M%S")
    physical = psutil.cpu_count(logical=False) or 1
    usable = performance_cores()
    memory = psutil.virtual_memory().available
    parallel = [c for c in codes if _declaration(c).get("parallel")]
    serial = [c for c in codes if not _declaration(c).get("parallel")]
    runs = []
    if parallel:
        skipped = sorted({c for c in cores if c > usable})
        if skipped:
            print(
                f"Core counts {skipped} skipped: this machine has {usable} "
                + ("performance " if usable < physical else "")
                + "core(s) for a sweep."
            )
        for n in sorted({c for c in cores if c <= usable}):
            runs.append((n, parallel))
    if serial:
        runs.append((1, serial))
    for n, these in runs:
        flow = build_flowchart(
            build_spec(these, tier), directory / f"benchmark_{'_'.join(these)}.flow"
        )
        run_dir = directory / f"cores_{n}_{'_'.join(these)}"
        run_dir.mkdir(exist_ok=True)
        env = dict(os.environ)
        env["SEAMM_CE"] = json.dumps(
            {
                "NTASKS": n,
                "MEM_PER_NODE": memory,
                "MEM_PER_CPU": memory // physical,
                "NGPUS": 0,
            }
        )
        env["SEAMM_TIMING_BENCHMARK"] = set_id
        # SEAMM's global cores cap, then each plug-in's own ncores option
        # (seamm.ini may pin either, e.g. to 1 or 5): on the command line a
        # section's options follow its name, SEAMM's come first.
        command = [_tool("run_flowchart"), str(flow), "--ncores", str(n)]
        for code in these:
            section = _declaration(code).get("section") or f"{code}-step"
            command += [section, "--ncores", str(n)]
        print(f"{n} core(s), {', '.join(these)}: {' '.join(command)} in {run_dir}")
        if dry_run:
            continue
        t0 = time.time()
        result = subprocess.run(
            command, cwd=run_dir, env=env, capture_output=True, text=True
        )
        (run_dir / "run.log").write_text(result.stdout + "\n" + result.stderr)
        status = "ok" if result.returncode == 0 else f"FAILED (rc {result.returncode})"
        print(f"    {status} in {time.time() - t0:.0f} s")
    if fit_after and not dry_run:
        from . import timing_model

        for code in codes:
            model = timing_model.fit(code)
            if model is None:
                print(f"{code}: still too few records to fit")
            else:
                timing_model.save_model(model)
                print(timing_model.report_text(model))
    return set_id


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m seamm_exec.timing_benchmark",
        description=(
            "Run the standard timing benchmark that places this machine in the "
            "cost model."
        ),
    )
    parser.add_argument(
        "--codes",
        help="comma-separated codes; default every installed step that declares "
        "a benchmark",
    )
    parser.add_argument(
        "--cores", default="1,4,8,16", help="core counts for the parallel codes"
    )
    parser.add_argument("--tier", choices=("quick", "full"), default="quick")
    parser.add_argument("--directory", help="where to run (default ./timing_benchmark)")
    parser.add_argument(
        "--set", dest="set_id", help="the benchmark set id written to the rows"
    )
    parser.add_argument("--fit", action="store_true", help="fit the models afterwards")
    parser.add_argument(
        "--dry-run", action="store_true", help="build and show, do not run"
    )
    parser.add_argument(
        "--build-only", action="store_true", help="only write the flowchart"
    )
    parser.add_argument("-o", "--output", default="timing_benchmark.flow")
    parser.add_argument(
        "--bin",
        help="the installation's bin directory (run_flowchart, seamm-flowchart), e.g. "
        "~/SEAMM/venv/bin; default this Python's",
    )
    args = parser.parse_args(argv)
    global _bin
    _bin = Path(args.bin).expanduser() if args.bin else None
    codes = tuple(c.strip() for c in (args.codes or "").split(",") if c.strip())
    codes = codes or tuple(sorted(declarations()))
    unknown = [c for c in codes if c not in declarations()]
    if unknown:
        parser.error(
            f"no installed step declares a benchmark for {unknown}; known: "
            + (", ".join(sorted(declarations())) or "none")
        )
    if not codes:
        parser.error("no installed step declares a timing benchmark")
    if args.build_only:
        path = build_flowchart(build_spec(codes, args.tier), args.output)
        print(f"wrote {path}")
        return 0
    run(
        codes,
        cores=tuple(int(c) for c in args.cores.split(",")),
        tier=args.tier,
        directory=args.directory,
        set_id=args.set_id,
        fit_after=args.fit,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
