# -*- coding: utf-8 -*-

"""The Local object does what it name implies: it executes, or
runs, an executable locally."""

import logging
import os
import pprint
import subprocess

from .base import Base

logger = logging.getLogger("seamm-exec")


class Local(Base):
    def __init__(self, logger=logger):
        super().__init__(logger=logger)

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
        # logger.setLevel(logging.DEBUG)

        # Replace any strings in the cmd with those in the configuration
        self.logger.debug(
            "Config:\n"
            + pprint.pformat(config, compact=True)
            + "\nComputational environment:\n"
            + pprint.pformat(ce, compact=True)
        )
        command = " ".join(cmd)

        # Sift through the way we can find the executables.
        # 1. Conda
        if "conda-environment" in config and config["conda-environment"] != "":
            command = "conda run -n {conda-environment} " + command

        # 2. modules
        modules = ""
        if "GPUS" in ce:
            if "gpu_modules" in config and config["gpu_modules"] != "":
                modules = config["gpu_modules"]
        else:
            if "modules" in config:
                modules = config["modules"]
        if len(modules) > 0:
            # Use modules to get the executables
            command = f"module load {modules}\n" + command

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

        p = subprocess.run(
            command,
            input=input_data,
            env=tmp_env,
            cwd=directory,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=shell,
        )

        self.logger.debug("Result from subprocess\n" + pprint.pformat(p))

        # capture the return code and output
        result = {
            "returncode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }

        return result
