# -*- coding: utf-8 -*-

"""The ExecWorkflow object does what it name implies: it executes, or
runs, a given flowchart.

It provides the environment for running the computational tasks
locally or remotely, using what is commonly called workflow management
system (WMS).  The WMS concept, as used here, means tools that run
given tasks without knowing anything about chemistry. The chemistry
specialization is contained in the Flowchart and the nodes that it
contains."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import calendar
import json
import locale
import logging
import os
import os.path
import platform
import re
import shutil
import string
import sys
import textwrap
import time
import traceback
import uuid

import fasteners
import cpuinfo

from molsystem import SystemDB
from seamm_util import getParser
from seamm_util.printing import FormattedText as __
import reference_handler
import seamm
import seamm_exec
import seamm_util
import seamm_util.printing as printing
from ._version import __version__

logger = logging.getLogger("seamm-exec")
printer = printing.getPrinter()

# logging.basicConfig(level="WARNING")
# logging.basicConfig(level="DEBUG")
variables = seamm.Variables()
header_line = "!MolSSI job_data 1.0\n"


class ExecFlowchart(object):
    def __init__(self, flowchart=None, cmdline=None, plan=None):
        """Execute a flowchart, providing support for the actual
        execution of codes

        Parameters
        ----------
        flowchart : seamm.Flowchart
            The flowchart to run.
        cmdline : [str]
            The command-line arguments, recorded in and compared with the
            checkpoint.
        plan : dict
            How to start (see :func:`plan_start`), if already decided.
        """
        logger.info("In ExecFlowchart.init()")

        self.flowchart = flowchart
        self.cmdline = [] if cmdline is None else list(cmdline)
        self.plan = plan

    def _archive_previous_database(self, options, root):
        moved = archive_previous_database(root, options)
        if moved is not None:
            message = (
                "This directory had the job database of an earlier run, so it was "
                f"moved to {moved.relative_to(Path(root))}/ and the flowchart "
                "starts from the top. Finished calculations are reused."
            )
            logger.warning(message)
            printer.job(message)

    def run(self, root=None, job_id=None):
        logger.info("In ExecFlowchart.run()")
        if not self.flowchart:
            raise RuntimeError("There is no flowchart to run!")

        # Get the command line options
        parser = getParser(name="SEAMM")
        options = parser.get_options()

        # Set the options in each step
        for node in self.flowchart:
            node.all_options = options

        # Create the global context
        logger.info("Creating global variables space")
        seamm.flowchart_variables = seamm.Variables()

        # Put the current time as a variable
        seamm.flowchart_variables.set_variable("_start_time", time.time())
        seamm.flowchart_variables.set_variable("_job_id", job_id)

        # And add the printer
        seamm.flowchart_variables.set_variable("printer", printer)

        # Resume from the checkpoint, or set a previous run's job database aside
        # and start from the top. Either way the step directories (and their
        # task manifests) stay, so finished calculations are reused.
        root = root or self.flowchart.root_directory or os.getcwd()
        plan = self.plan
        if plan is None:
            # The checkpoint's fingerprint uses the steps' ids
            self.flowchart.set_ids()
            plan = plan_start(root, options, self.flowchart, self.cmdline)
        resume = plan["resume"]
        if resume is None:
            self._archive_previous_database(options, root)
        if plan["message"] is not None:
            logger.warning(plan["message"])
            printer.job(__(plan["message"], indent="", indent_initial=False))
            printer.job("")

        # Setup the citations, keeping those of the steps already run if resuming
        filename = Path(root) / "references.db"
        if resume is None:
            filename.unlink(missing_ok=True)
        references = None
        try:
            references = reference_handler.Reference_Handler(str(filename))
        except Exception as e:
            printer.job("Error with references:")
            printer.job(e)

        if references is not None:
            template = string.Template("""\
                @misc{seamm,
                  address      = {Virginia Tech, Blacksburg, VA, USA},
                  author       = {Jessica Nash and
                                  Eliseo Marin-Rimoldi and
                                  Mohammad Mostafanejad and
                                  Paul Saxe},
                  doi          = {10.5281/zenodo.5153984},
                  month        = {$month},
                  note         = {Funding: NSF OAC-1547580 and CHE-2136142},
                  organization = {The Molecular Sciences Software Institute (MolSSI)},
                  publisher    = {Zenodo},
                  title        = {SEAMM: Simulation Environment for Atomistic and
                                  Molecular Modeling},
                  url          = {https://doi.org/10.5281/zenodo.5153984},
                  version      = {$version},
                  year         = $year
                }""")

            try:
                version = __version__
                year, month = version.split(".")[0:2]
                month = calendar.month_abbr[int(month)].lower()
                citation = template.substitute(
                    month=month,
                    version=version,
                    year=year,
                )

                references.cite(
                    raw=citation,
                    alias="SEAMM",
                    module="seamm",
                    level=1,
                    note="The principle citation for SEAMM.",
                )
            except Exception as e:
                printer.job(f"Exception in citation {type(e)}: {e}")
                printer.job(traceback.format_exc())

        # Create the system database, default system and configuration. The job's
        # own database defers molsystem's commits, so each step is one
        # transaction, committed with the checkpoint when the step finishes.
        checkpointing = plan["checkpointing"]
        if "SEAMM" in options:
            seamm_options = options["SEAMM"]
            read_only = "read_only" in seamm_options and seamm_options["read_only"]
            db_file = seamm_options["database"]
            if ":memory:" in db_file:
                db = SystemDB(filename=db_file)
            else:
                path = Path(db_file).expanduser().resolve()
                uri = "file:" + str(path)
                if read_only:
                    uri += "?mode=ro"
                db = SystemDB(filename=uri, deferred_commit=checkpointing)
        else:
            db = SystemDB(filename="file:seamm.db", deferred_commit=checkpointing)

        # Put the system database in the global context for access.
        seamm.flowchart_variables.set_variable("_system_db", db)

        self.flowchart.root_directory = root

        # Correctly number the nodes
        self.flowchart.set_ids()

        # Tables live in the job database: refuse plug-ins too old for that before
        # running anything, rather than failing part way through.
        problems = seamm.table.check_table_plugins(self.flowchart)
        if len(problems) > 0:
            message = "This flowchart cannot run here:\n    " + "\n    ".join(problems)
            printer.job(message)
            raise RuntimeError(message)

        checkpointer = None
        if checkpointing:
            checkpointer = seamm.Checkpointer(
                db,
                root,
                self.flowchart,
                comparable_command_line(self.cmdline),
                resume=resume,
            )
            seamm.checkpoint.set_checkpointer(checkpointer)
            checkpointer.restore_variables(seamm.flowchart_variables)

        # Write out an initial summary of the flowchart before doing anything
        # Reset the visited flag for traversal
        self.flowchart.reset_visited()

        # Get the start node
        next_node = self.flowchart.get_node("1")

        # describe ourselves
        printer.job(("\nDescription of the flowchart" "\n----------------------------"))

        while next_node:
            # and print the description
            try:
                next_node = next_node.describe()
            except Exception:
                message = "Error describing the flowchart\n\n" + traceback.format_exc()
                print(message)
                logger.critical(message)
                raise
            except:  # noqa: E722
                message = (
                    "Unexpected error describing the flowchart\n\n"
                    + traceback.format_exc()
                )
                print(message)
                logger.critical(message)
                raise

        printer.job("")

        # And actually run it!
        printer.job(("Running the flowchart\n" "---------------------"))

        failed = False
        try:
            next_node = self.flowchart.get_node("1")
            if checkpointer is not None:
                next_node = checkpointer.start(next_node)
            while next_node is not None:
                try:
                    node = next_node
                    next_node = next_node.run()
                    seamm.step_completed(node, next_node)
                except DeprecationWarning as e:
                    print("\nDeprecation warning: " + str(e))
                    traceback.print_exc(file=sys.stderr)
                    traceback.print_exc(file=sys.stdout)
        except BaseException:
            failed = True
            raise
        finally:
            # Write the final structure
            db = seamm.flowchart_variables.get_variable("_system_db")
            system = db.system
            if system is not None:
                configuration = system.configuration
                if configuration is not None:
                    output = []
                    if configuration.n_atoms > 0:
                        # MMCIF file has bonds
                        filename = os.path.join(
                            self.flowchart.root_directory, "final_structure.mmcif"
                        )
                        text = None
                        try:
                            text = configuration.to_mmcif_text()
                        except Exception:
                            pass
                        if text is not None:
                            with open(filename, "w") as fd:
                                print(text, file=fd)
                            output.append("final_structure.mmcif")

                        # CIF file has cell and may have bonds
                        if configuration.periodicity == 3:
                            text = None
                            try:
                                text = configuration.to_cif_text()
                            except Exception:
                                pass
                            if text is not None:
                                filename = os.path.join(
                                    self.flowchart.root_directory, "final_structure.cif"
                                )
                                with open(filename, "w") as fd:
                                    print(configuration.to_cif_text(), file=fd)
                                    output.append("final_structure.cif")
                        if len(output) > 0:
                            files = "' and '".join(output)
                            printer.job(
                                f"\nWrote the final structure to '{files}' for viewing."
                            )
                        else:
                            printer.job(
                                "\nWas unable to write the final structure as either "
                                "an mmcif or cif file for viewing."
                            )

            # The checkpoint's last word. After an error the failing step's
            # writes are rolled back (the files written above keep what it did),
            # so a resume re-runs it.
            if checkpointer is not None:
                try:
                    checkpointer.finish("error" if failed else "finished")
                except Exception:
                    logger.exception("Could not write the final checkpoint")
                seamm.checkpoint.set_checkpointer(None)

            # And print out the references
            filename = os.path.join(self.flowchart.root_directory, "references.db")
            try:
                references = reference_handler.Reference_Handler(filename)
            except Exception as e:
                printer.job("Error with references:")
                printer.job(e)

            if references.total_citations() > 0:
                tmp = {}
                citations = references.dump(fmt="text")
                for citation, text, count, level in citations:
                    if level not in tmp:
                        tmp[level] = {}
                    tmp[level][citation] = (text, count)

                n = 0
                for level in sorted(tmp.keys()):
                    ref_dict = tmp[level]
                    if level == 1:
                        printer.job("\nPrimary references:\n")
                        n = 0
                    elif level == 2:
                        printer.job("\nSecondary references:\n")
                        n = 0
                    else:
                        printer.job("\nLess important references:\n")
                        n = 0

                    lines = []
                    for citation in sorted(ref_dict.keys()):
                        n += 1
                        text, count = ref_dict[citation]
                        if count == 1:
                            lines.append("({}) {:s}".format(n, text))
                        else:
                            lines.append(
                                "({}) {:s} (used {:d} times)".format(n, text, count)
                            )
                    printer.job(
                        __("\n\n".join(lines), indent=4 * " ", indent_initial=False)
                    )


class cd:
    """Context manager for changing the current working directory"""

    def __init__(self, newPath):
        self.newPath = os.path.expanduser(newPath)

    def __enter__(self):
        self.savedPath = os.getcwd()
        os.chdir(self.newPath)

    def __exit__(self, etype, value, traceback):
        os.chdir(self.savedPath)


def credential_sections(root):
    """The seammrc sections to look in for datastore credentials, in order.

    ``[Dashboard: <name>]`` where name is the installation's root directory name
    (``SEAMM_DEV``, ``SEAMM_NEW``, ...), then, as before, ``dev`` for a root whose
    path contains "dev" or ``localhost`` otherwise, then this host's name.
    """
    name = Path(root).expanduser().name
    legacy = "dev" if "dev" in str(root).lower() else "localhost"
    sections = []
    for section in (name, legacy, platform.node()):
        if section and section not in sections:
            sections.append(section)
    return sections


def open_datastore(root, datastore, timeout=20.0):
    """Open the database via the datastore"""
    import seamm_datastore

    # Get the user information for the datastore
    rc = seamm.SEAMMrc()

    user = None
    password = None

    for section in credential_sections(root):
        section = "Dashboard: " + section
        if section in rc:
            if user is None and rc.has_option(section, "user"):
                user = rc.get(section, "user")
            if password is None and rc.has_option(section, "password"):
                password = rc.get(section, "password")

    if user is None or password is None:
        raise RuntimeError(
            "You need credentials in '~/.seamm.d/seammrc' to run jobs from the "
            "commandline. See the documentation for more details."
        )

    # Add to the database
    db_path = Path(datastore).expanduser().resolve() / "seamm.db"
    db_uri = "sqlite:///" + str(db_path)
    db = seamm_datastore.connect(
        database_uri=db_uri,
        datastore_location=datastore,
        username=user,
        password=password,
        timeout=timeout,
    )

    return db


_DATABASE_FILES = (
    "seamm.db",
    "seamm.db-wal",
    "seamm.db-shm",
    "references.db",
    "checkpoint.json",
)

RESUME_ENVIRONMENT = "SEAMM_RESUME"


def _job_database(options, root):
    """The job database's path, or None if it is not a file in ``root``.

    None for an in-memory or read-only database, or one given with --database
    that lives elsewhere.
    """
    seamm_options = options.get("SEAMM", {}) if isinstance(options, dict) else {}
    if seamm_options.get("read_only"):
        return None
    db_file = seamm_options.get("database", "seamm.db") or "seamm.db"
    if ":memory:" in db_file:
        return None
    path = Path(db_file).expanduser()
    if not path.is_absolute():
        # Relative to the job directory, where the evaluator runs.
        path = Path(root) / path
    path = path.resolve()
    if path != (Path(root) / "seamm.db").resolve():
        return None
    return path


def archive_previous_database(root, options):
    """Move a previous run's job database into ``previous/<UTC time>/``.

    Only when the job database is ``<root>/seamm.db`` and it exists; the caller
    has decided not to resume from it (see :func:`plan_start`). The checkpoint
    mirror goes with it. Returns the directory it was moved to, or None.
    """
    root = Path(root)
    path = _job_database(options, root)
    if path is None or not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    destination = root / "previous" / stamp
    destination.mkdir(parents=True, exist_ok=True)
    for name in _DATABASE_FILES:
        source = root / name
        if source.exists():
            shutil.move(str(source), str(destination / name))
    return destination


def resume_requested(options):
    """Whether this run was asked to resume: --resume, or SEAMM_RESUME=1.

    The JobServer asks with the environment variable when it resubmits a job
    that was lost, so that an evaluator too old to know about resuming simply
    starts from the top instead of failing on an unknown option.
    """
    seamm_options = options.get("SEAMM", {}) if isinstance(options, dict) else {}
    if seamm_options.get("resume", False):
        return True
    value = os.environ.get(RESUME_ENVIRONMENT, "").strip().lower()
    return value in ("1", "yes", "true", "on")


def comparable_command_line(cmdline):
    """The command line as recorded in a checkpoint: without --resume."""
    return [arg for arg in cmdline if arg != "--resume"]


def plan_start(root, options, flowchart, cmdline):
    """Decide how a run starts: resume from the checkpoint, or from the top.

    Resuming is never the silent default for a rerun in place: the checkpoint
    cannot tell whether the files the flowchart reads have changed. A run that
    does not ask to resume sets a resumable checkpoint aside with the rest of the
    previous database, and says how to resume instead.

    Parameters
    ----------
    root : str or pathlib.Path
        The job directory.
    options : dict
        The parsed options.
    flowchart : seamm.Flowchart
        The flowchart to run.
    cmdline : [str]
        The command-line arguments.

    Returns
    -------
    dict
        ``resume``: the checkpoint to resume from, or None; ``checkpointing``:
        whether this run writes checkpoints (only for a job database in the job
        directory, writable and on disk); ``message``: what to tell the user, or
        None.
    """
    requested = resume_requested(options)
    path = _job_database(options, root)
    if path is None:
        message = None
        if requested:
            message = (
                "This job cannot resume: its database is read-only, in memory or "
                "outside the job directory, so it has no checkpoint. Running from "
                "the top."
            )
        return {"resume": None, "checkpointing": False, "message": message}

    checkpoint = seamm.read_checkpoint(path)
    ok, why = seamm.checkpoint.resumable(
        checkpoint,
        seamm.checkpoint.flowchart_fingerprint(flowchart),
        comparable_command_line(cmdline),
    )
    if ok:
        where = seamm.checkpoint.describe_position(checkpoint)
    if requested:
        if ok:
            message = (
                f"Resuming at {where}. The previous attempt ended in state "
                f"'{checkpoint['state']}'; its checkpoint was written "
                f"{checkpoint['written']}."
            )
            changed = seamm.checkpoint.changed_versions(checkpoint, flowchart)
            if len(changed) > 0:
                versions = ", ".join(f"{p} {a} -> {b}" for p, a, b in changed)
                message += (
                    " Note that the versions of some packages have changed since "
                    f"then: {versions}."
                )
            return {"resume": checkpoint, "checkpointing": True, "message": message}
        return {
            "resume": None,
            "checkpointing": True,
            "message": f"Asked to resume, but cannot: {why}. Running from the top.",
        }
    message = None
    if ok:
        message = (
            f"The previous run in this directory could have been resumed at {where}. "
            "Its database was set aside instead (rerun with --resume to continue "
            "from where it stopped)."
        )
    return {"resume": None, "checkpointing": True, "message": message}


def run_from_jobserver():
    """Helper routine to run from the JobServer.

    Gets the arguments from the command line.
    """
    job_id = sys.argv[1]
    wdir = sys.argv[2]
    db_path = sys.argv[3]
    cmdline = sys.argv[4:]

    with cd(wdir):
        try:
            run(
                job_id=job_id,
                wdir=wdir,
                db_path=db_path,
                in_jobserver=True,
                cmdline=cmdline,
            )
        except Exception as e:
            path = Path("job_data.json")
            if path.exists():
                with path.open("r") as fd:
                    fd.readline()
                    data = json.load(fd)
            else:
                data = {}

            data["state"] = "error"
            data["error type"] = type(e).__name__
            data["error message"] = traceback.format_exc()
            with path.open("w") as fd:
                fd.write(header_line)
                json.dump(data, fd, indent=3, sort_keys=True)
                fd.write("\n")
            raise


def run(
    job_id=None,
    wdir=None,
    # db_path is no longer used here (the JobServer now owns every terminal
    # status write -- see the finally block below), but stays accepted
    # since seamm_jobserver's _build_cmd() still passes it positionally on
    # the run_from_jobserver command line.
    db_path=None,
    setup_logging=True,
    in_jobserver=False,
    cmdline=None,
):
    """The standalone flowchart app"""
    if not in_jobserver and len(sys.argv) > 1:
        if sys.argv[1] == "--help" or sys.argv[1] == "-h":
            # Running run_flowchart by hand ...
            print("usage: run_flowchart <flowchart> [options]")
            print("")
            print("usually it is simpler to execute the flowchart file itself")
            exit()

        # Slice off 'run_flowchart' from the arguments, leaving the
        # flowchart as the thing being run.
        sys.argv = sys.argv[1:]

        filename = sys.argv[0]
    else:
        if wdir is None:
            filename = "flowchart.flow"
        else:
            filename = os.path.join(wdir, "flowchart.flow")

    if cmdline is None:
        cmdline = sys.argv[1:]

    # Set up the argument parser for this node.
    parser = seamm_util.seamm_parser()

    parser.epilog = textwrap.dedent("""
        The plug-ins in this flowchart are listed above.
        Options, if any, for plug-ins are placed after
        the name of the plug-in, e.g.:

           test.flow lammps-step --log-level DEBUG --np 4

        To get help for a plug-in, use --help or -h after the
        plug-in name. E.g.

           test.flow lammps-step --help
        """)
    parser.usage = "%(prog)s [options] plug-in [options] plug-in [options] ..."

    # How steps will execute simulation engines
    if "SEAMM_ENVIRONMENT" in os.environ:
        default = os.environ["SEAMM_ENVIRONMENT"]
    else:
        default = "local"
    parser.add_argument(
        "SEAMM",
        "--executor",
        group="job options",
        default=default,
        choices=seamm_exec.executors,
        help="The executor used to run simulation engines.",
    )

    parser.add_argument(
        "SEAMM",
        "--resume",
        group="job options",
        action="store_true",
        help=(
            "Resume a job that stopped part way, in its own directory, from its "
            "checkpoint. Without it a rerun in place starts from the top."
        ),
    )

    # Now we need to get the flowchart so that we can set up all the
    # parsers for the steps in order to provide appropriate help.
    if not os.path.exists(filename):
        raise FileNotFoundError(f"The flowchart '{filename}' does not exist.")

    logger.info(f"    reading in flowchart '{filename}'")
    flowchart = seamm.Flowchart(parser_name="SEAMM", directory=wdir)

    # Setup some useful parameters
    flowchart.in_jobserver = in_jobserver

    flowchart.read(filename)
    logger.info("   finished reading the flowchart")

    # Now traverse the flowchart, setting up the ids and parsers
    flowchart.set_ids()
    flowchart.create_parsers()

    # And handle the command-line arguments and ini file options.
    parser.parse_args(cmdline)
    logger.info("Parsed the command-line arguments")
    options = parser.get_options("SEAMM")

    # Whether to just run as-is, without getting a job_id, using the
    # datastore, etc.
    standalone = options["standalone"] or options["projects"] is None

    # Setup the logging
    if setup_logging:
        if "log_level" in options:
            logging.basicConfig(level=options["log_level"])

        # Set the log level for the plug-ins
        flowchart.set_log_level(parser.get_options())

    # Get the executor for tasks
    flowchart.executor = seamm_exec.get_executor(options["executor"])

    # Create the working directory where files, output, etc. go.
    # At the moment this is datastore/job_id

    if standalone:
        print("Running in standalone mode.")
        if wdir is None:
            wdir = os.getcwd()
    else:
        datastore = os.path.expanduser(options["datastore"])

        if job_id is None:
            if options["job_id_file"] is None:
                job_id_file = os.path.join(datastore, "job.id")
            else:
                job_id_file = options["job_id_file"]

            # Get the job_id from the file, creating the file if necessary
            job_id = get_job_id(job_id_file)
        if options["projects"] is None:
            projects = ["default"]
        else:
            projects = options["projects"]
        if wdir is None:
            # And put it all together
            wdir = os.path.abspath(
                os.path.join(
                    datastore, "projects", projects[0], "Job_{:06d}".format(job_id)
                )
            )

            if os.path.exists(wdir):
                if options["force"]:
                    shutil.rmtree(wdir)
                else:
                    msg = "Directory '{}' exists, use --force to overwrite".format(wdir)

                    logging.critical(msg)
                    sys.exit(msg)

            os.makedirs(wdir, exist_ok=False)

    logging.info("The working directory is '{}'".format(wdir))

    # Set up the root printer, and add handlers to print to the file
    # 'job.out' in the working directory and to stdout, as requested
    # in the options. Since all printers are children of the root
    # printer, all output at the right levels will flow here

    # Set up our formatter
    formatter = logging.Formatter(fmt="{message:s}", style="{")

    # A handler for stdout
    if not in_jobserver:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(seamm_util.printing.NORMAL)
        console_handler.setFormatter(formatter)
        printer.addHandler(console_handler)

    # Resume, or start from the top?
    with cd(wdir):
        plan = plan_start(
            wdir, parser.get_options(), flowchart, comparable_command_line(cmdline)
        )
    resuming = plan["resume"] is not None

    # A handler for the file
    # First remove the job.out file if it exists so we start fresh, unless
    # resuming, which carries on after a banner.
    job_file = Path(wdir) / "job.out"
    if resuming:
        with job_file.open("a") as fd:
            fd.write(
                "\n"
                + 79 * "="
                + "\n"
                + f"Resumed {datetime.now(timezone.utc).isoformat()}\n"
                + 79 * "="
                + "\n\n"
            )
    else:
        job_file.unlink(missing_ok=True)

    file_handler = logging.FileHandler(os.path.join(wdir, "job.out"))
    file_handler.setLevel(seamm_util.printing.NORMAL)
    file_handler.setFormatter(formatter)
    printer.addHandler(file_handler)

    # And ... finally ... run!
    printer.job(datetime.now().strftime("%A %Y.%m.%d %H:%M:%S %Z"))
    printer.job("Running in directory '{}'".format(wdir))

    flowchart_path = Path(wdir).resolve() / "flowchart.flow"
    path = Path(sys.argv[0]).resolve()

    # copy the flowchart to the root directory if it is not there already
    if not in_jobserver:
        if flowchart_path.exists() and path == flowchart_path:
            pass
        else:
            shutil.copy2(path, flowchart_path)

    # Make executable if it isn't
    permissions = flowchart_path.stat().st_mode
    if permissions & 0o100 == 0:
        flowchart_path.chmod(permissions | 0o110)

    # logger.info(f"    reading in flowchart '{flowchart_path}' -- 2")
    # flowchart = seamm.Flowchart(directory=wdir)
    # flowchart.read(flowchart_path)
    # logger.info("   finished reading the flowchart -- 2")

    # Change to the working directory and run the flowchart
    with cd(wdir):
        # Set up the initial metadata for the job.
        time_now = datetime.now(timezone.utc).isoformat()
        if in_jobserver or (resuming and Path("job_data.json").exists()):
            with open("job_data.json", "r") as fd:
                fd.readline()
                data = json.load(fd)
        else:
            if options["title"] != "":
                title = options["title"]
            else:
                title = flowchart.metadata["title"]
            if title == "":
                "untitled job"
            if "description" in options:
                description = options["description"]
            else:
                description = flowchart.metadata["description"]
            if description == "":
                description = "Run from the command-line."
            data = {
                "data_version": "1.0",
                "title": title,
                "working directory": wdir,
                "submitted time": time_now,
            }
        previous_start = data.get("start time")
        data.update(
            {
                "command line": cmdline,
                "flowchart_digest": flowchart.digest(),
                "flowchart_digest_strict": flowchart.digest(strict=True),
                "start time": time_now,
                "state": "started",
                "uuid": uuid.uuid4().hex,
                "~cpuinfo": cpuinfo.get_cpu_info(),
            }
        )
        if resuming:
            # Keep when the job first started; note each resume.
            if data.get("first start time") is None:
                data["first start time"] = previous_start or time_now
            data["resumed"] = [*data.get("resumed", []), time_now]
            for key in ("end time", "error type", "error message"):
                data.pop(key, None)
        if not in_jobserver and not standalone:
            import seamm_datastore

            if "projects" not in data:
                data["projects"] = projects
            data["datastore"] = datastore
            data["job id"] = job_id
            os.environ["SEAMM_JOB_ID"] = str(job_id)

            db = open_datastore(
                options["root"],
                datastore,
                timeout=options.get("database_timeout", 20.0),
            )

            pid = os.getpid()
            current_time = datetime.now(timezone.utc)
            with seamm_datastore.session_scope(
                db.Session, timeout=options.get("database_timeout", 20.0)
            ) as session:
                job = db.Job.create(
                    job_id,
                    str(flowchart_path),
                    project_names=data["projects"],
                    path=wdir,
                    title=title,
                    description=description,
                    submitted=current_time,
                    started=current_time,
                    parameters={"cmdline": [], "pid": pid},
                    status="started",
                )
                session.add(job)

            del db

        # Output the initial metadata for the job.
        with open("job_data.json", "w") as fd:
            fd.write(header_line)
            json.dump(data, fd, indent=3, sort_keys=True)
            fd.write("\n")

        t0 = time.time()
        pt0 = time.process_time()

        # And run the flowchart
        logger.info("Executing the flowchart")
        try:
            exec = ExecFlowchart(flowchart, cmdline=cmdline, plan=plan)
            exec.run(root=wdir, job_id=job_id)
            data["state"] = "finished"
        except Exception as e:
            data["state"] = "error"
            data["error type"] = type(e).__name__
            data["error message"] = traceback.format_exc()
            printer.job(traceback.format_exc())
        finally:
            # Wrap things up
            t1 = time.time()
            pt1 = time.process_time()
            data["end time"] = datetime.now(timezone.utc).isoformat()
            t = t1 - t0
            pt = pt1 - pt0
            data["elapsed time"] = t
            data["process time"] = pt

            with open("job_data.json", "w") as fd:
                fd.write(header_line)
                json.dump(data, fd, indent=3, sort_keys=True)
                fd.write("\n")

            printer.job(f"\nProcess time: {timedelta(seconds=pt)} ({pt:.3f} s)")
            printer.job(f"Elapsed time: {timedelta(seconds=t)} ({t:.3f} s)")

            # Under a JobServer (in_jobserver=True), nothing more to do here:
            # job_data.json above is the JobServer's sole source for this
            # job's terminal status -- it reads it back and writes the
            # datastore itself (see seamm_jobserver.jobserver's
            # _read_job_data_state/_finalize_job_status), rather than this
            # process writing to a datastore it may not even be able to
            # reach (true for a JobServer dispatching to a remote SLURM
            # cluster with no shared filesystem).
            if not in_jobserver and not standalone:
                import seamm_datastore

                # Let the datastore know that the job finished.
                # current_time = datetime.now(timezone.utc)

                # At the moment update takes weird numbers!
                now = datetime.now().astimezone()
                dt = now.utcoffset().total_seconds()
                current_time = (time.time() - dt) * 1000

                # Add to the database
                # N.B. Don't know how to remove 'pid' from the JSON properties column
                db = open_datastore(
                    options["root"],
                    datastore,
                    timeout=options.get("database_timeout", 20.0),
                )
                with seamm_datastore.session_scope(
                    db.Session, timeout=options.get("database_timeout", 20.0)
                ) as session:
                    job = db.Job.update(
                        job_id, finished=current_time, status=data["state"]
                    )
                del db
        printer.job(datetime.now().strftime("%A %Y.%m.%d %H:%M:%S %Z"))

    # Only after job_data.json and the datastore are written: exit non-zero for
    # a flowchart that failed, so batch scripts, pipelines and tasks see it
    # (seamm_exec#40).
    if data["state"] == "error":
        sys.exit(1)


def get_job_id(filename):
    """Get the next job id from the given file.

    This uses the fasteners module to provide locking so that
    only one job at a time can access the file, so that the job
    ids are unique and monotonically increasing.
    """

    filename = os.path.expanduser(filename)

    lock_file = filename + ".lock"
    lock = fasteners.InterProcessLock(lock_file)
    locked = lock.acquire(blocking=True, timeout=5)

    if locked:
        if not os.path.isfile(filename):
            job_id = 1
            with open(filename, "w") as fd:
                fd.write("!MolSSI job_id 1.0\n")
                fd.write("1\n")
            lock.release()
        else:
            with open(filename, "r+") as fd:
                line = fd.readline()
                pos = fd.tell()
                if line == "":
                    lock.release()
                    raise EOFError("job_id file '{}' is empty".format(filename))
                line = line.strip()
                match = re.fullmatch(r"!MolSSI job_id ([0-9]+(?:\.[0-9]+)*)", line)
                if match is None:
                    lock.release()
                    raise RuntimeError(
                        "The job_id file has an incorrect header: {}".format(line)
                    )
                line = fd.readline()
                if line == "":
                    lock.release()
                    raise EOFError("job_id file '{}' is truncated".format(filename))
                try:
                    job_id = int(line)
                except TypeError:
                    raise TypeError(
                        "The job_id in file '{}' is not an integer: {}".format(
                            filename, line
                        )
                    )
                job_id += 1
                fd.seek(pos)
                fd.write("{:d}\n".format(job_id))
    else:
        raise RuntimeError("Could not lock the job_id file '{}'".format(filename))

    return job_id


if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, "")
    run()
