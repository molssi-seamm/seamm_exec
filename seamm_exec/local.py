# -*- coding: utf-8 -*-

"""The Local object does what it name implies: it executes, or
runs, an executable locally."""

import logging
import os
from pathlib import Path
import pprint
import subprocess

from .base import Base

logger = logging.getLogger("seamm-exec")


class Local(Base):
    def __init__(self, logger=logger):
        super().__init__(logger=logger)
        # logger.setLevel(logging.DEBUG)

    @property
    def name(self):
        """The name of this type of executor."""
        return "local"

    def exec(
        self,
        config,
        cmd=[],
        directory=None,
        input_data=None,
        env={},
        shell=False,
        ce={},
    ):
        """Execute a command directly on the current machine.

        Parameters
        ----------
        config : dict(str: any)
            The configuration for the code to run
        cmd : [str]
            The command as a list of words.
        directory : str or Path
            The directory for the tasks files.
        input_data : str
            Data to be redirected to the stdin of the process.
        env : {str: str}
            Dictionary of environment variables to pass to the execution environment
        shell : bool = False
            Whether to use the shell when launching task
        ce : dict(str, str or int)
            Description of the computational enviroment

        Returns
        -------
        {str: str}
            Dictionary with stdout, stderr, returncode, etc.
        """
        # Replace any strings in the cmd with those in the configuration
        self.logger.debug(
            "Config:\n"
            + pprint.pformat(config, compact=True)
            + "\nComputational environment:\n"
            + pprint.pformat(ce, compact=True)
        )
        command = " ".join(cmd)

        # Docker was removed in 2026.10.6.2: say so, rather than run the code
        # as a local installation and fail later with "command not found"
        if config.get("installation") == "docker":
            raise RuntimeError(
                "Docker support was removed in seamm-exec 2026.10.6.2: set "
                "'installation' to local, conda or modules in this code's "
                "<program>.ini."
            )

        # Sift through the way we can find the executables.
        shell_exe = None
        if "installation" in config and config["installation"] == "conda":
            # 1. Conda
            # May be the name of the environment or the path to the environment

            if "CONDA_EXE" in os.environ:
                conda = os.environ["CONDA_EXE"]
            elif "conda" in config:
                conda = config["conda"]
            else:
                conda = "conda"

            environment = config["conda-environment"]
            if environment[0] == "~":
                environment = str(Path(environment).expanduser())
                command = f"'{conda}' run --live-stream -p '{environment}' " + command
            elif Path(environment).is_absolute():
                command = f"'{conda}' run --live-stream -p '{environment}' " + command
            else:
                command = f"'{conda}' run --live-stream -n '{environment}' " + command
        elif "installation" in config and config["installation"] == "local":
            # 2. local installation
            pass
        elif "installation" in config and config["installation"] == "modules":
            # 3. modules
            modules = ""
            if "NGPUS" in ce:
                if "gpu_modules" in config and config["gpu_modules"] != "":
                    modules = config["gpu_modules"]
            else:
                if "modules" in config:
                    modules = config["modules"]
            if len(modules) > 0:
                # Use modules to get the executables
                command = f"module load {modules}\n" + command

            # Sort out the shell ... dash does not work with modules
            if "shell" in config:
                shell_exe = config["shell"]
            else:
                shell_exe = "/bin/bash"

        # Replace any variables in the command with values from the config file
        # and computational environment. Maybe nested.
        tmp = command
        while True:
            command = tmp.format(**config, **ce)
            if tmp == command:
                break
            tmp = command

        self.logger.debug(f"command=\n{command}")

        tmp_env = {**os.environ}
        tmp_env.update(env)
        self.logger.debug(
            f"Environment:\nCustom:\n{pprint.pformat(env)}\n"
            f"Full:\n {pprint.pformat(tmp_env)}"
        )

        hooks = getattr(getattr(self, "_task_context", None), "hooks", None)
        if hooks is None:
            p = subprocess.run(
                command,
                cwd=directory,
                env=tmp_env,
                input=input_data,
                shell=shell,
                executable=shell_exe,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        else:
            # Under a concurrent LocalPool: run in a new session so the pool
            # can kill the whole process tree, and report the pid.
            proc = subprocess.Popen(
                command,
                cwd=directory,
                env=tmp_env,
                stdin=subprocess.PIPE if input_data is not None else None,
                shell=shell,
                executable=shell_exe,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                start_new_session=True,
            )
            hooks.started(proc)
            try:
                stdout, stderr = proc.communicate(input_data)
            finally:
                hooks.finished(proc)
            p = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)

        self.logger.debug("Result from subprocess\n" + pprint.pformat(p))

        # capture the return code and output
        result = {
            "returncode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }

        return result
