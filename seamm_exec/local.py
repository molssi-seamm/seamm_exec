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

        # Sift through the way we can find the executables.
        use_docker = False
        if "conda-environment" in config and config["conda-environment"] != "":
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
        elif "installation" in config and config["installation"] == "docker":
            # 3. Docker
            use_docker = True
        elif "installation" in config and config["installation"] == "modules":
            # 4. modules
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

        if use_docker:
            import docker

            client = docker.from_env()

            # See if this is running in Docker and adjust the path accordingly
            if (
                "SEAMM_ENVIRONMENT" in os.environ
                and os.environ["SEAMM_ENVIRONMENT"] == "docker"
            ):
                hostname = os.environ["HOSTNAME"]
                try:
                    this_container = client.containers.get(hostname)
                    mounts = this_container.attrs["Mounts"]
                    for mount in mounts:
                        if mount["Destination"] == "/home":
                            path = Path(mount["Source"]).joinpath(*directory.parts[2:])
                            break
                except Exception:
                    path = Path(directory)
            else:
                path = Path(directory)

            self.logger.debug(pprint.pformat(config, compact=True))

            # Replace any variables in the container name
            container = config["container"].format(**config, **ce)

            # See if there is a required platform
            platform = config.get("platform", None)

            if len(cmd) > 0:
                # Replace any variables in the command with values from the config file
                # and computational environment. Maybe nested.
                command = " ".join(cmd)
                tmp = command
                while True:
                    command = tmp.format(**config, **ce)
                    if tmp == command:
                        break
                    tmp = command

                # If running using Docker, we have to munge any paths in the command
                prefix = str(directory)
                command = command.replace(prefix, "/home")

                self.logger.debug(
                    f"""
                    result = client.containers.run(
                        command={command},
                        environment={env},
                        image={container},
                        platform={platform},
                        remove=True,
                        stderr=True,
                        stdout=True,
                        volumes=[f"{path}:/home"],
                        working_dir="/home",
                    )
                    """
                )

                result = client.containers.run(
                    command=command,
                    environment=env,
                    image=container,
                    platform=platform,
                    remove=True,
                    stderr=True,
                    stdout=True,
                    volumes=[f"{path}:/home"],
                    working_dir="/home",
                )
            else:
                self.logger.debug(
                    f"""
                    result = client.containers.run(
                        environment={env},
                        image={container},
                        platform={platform},
                        remove=True,
                        stderr=True,
                        stdout=True,
                        volumes=[f"{path}:/home"],
                        working_dir="/home",
                    )
                    """
                )

                result = client.containers.run(
                    environment=env,
                    image=container,
                    platform=platform,
                    remove=True,
                    stderr=True,
                    stdout=True,
                    volumes=[f"{path}:/home"],
                    working_dir="/home",
                )

            self.logger.debug("\n" + pprint.pformat(result))

            result = {}
        else:
            tmp_env = {**os.environ}
            tmp_env.update(env)
            self.logger.debug(
                f"Environment:\nCustom:\n{pprint.pformat(env)}\n"
                f"Full:\n {pprint.pformat(tmp_env)}"
            )

            p = subprocess.run(
                command,
                cwd=directory,
                env=tmp_env,
                input=input_data,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )

            self.logger.debug("Result from subprocess\n" + pprint.pformat(p))

            # capture the return code and output
            result = {
                "returncode": p.returncode,
                "stdout": p.stdout,
                "stderr": p.stderr,
            }

        return result
