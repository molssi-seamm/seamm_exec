# -*- coding: utf-8 -*-

"""The Docker object executes a task using a Docker container"""

import logging
from pathlib import Path
import pprint
import os

try:
    import docker
except Exception:
    print(
        "The docker API is not installed. Please install it from PyPi or Conda, e.g.\n"
        "\n    pip install docker\n"
        "or\n"
        "      conda install docker-py"
    )
    raise

from .base import Base

logger = logging.getLogger("seamm-exec")


class Docker(Base):
    def __init__(self, logger=logger):
        super().__init__(logger=logger)

    @property
    def name(self):
        """The name of this type of executor."""
        return "docker"

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
        """Execute a command using a docker container..

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


        The equivalent commandline commmand is::

            docker run --rm -v $PWD:/home -w /home psaxe/mopac [mopac input_file]

        For this container the input filename defaults to "mopac.dat" so we do not need
        to add it.
        """
        # self.logger.setLevel(logging.DEBUG)

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
            except Exception as e:
                self.logger.error(
                    f"An error occurred in docker.py finding the mounts: {str(e)}"
                )
                raise

            paths = []
            no_home = True
            for mount in mounts:
                self.logger.debug(f"{mount=}")
                if mount["Destination"] == "/home":
                    no_home = False
                    path = Path(mount["Source"]).joinpath(*directory.parts[2:])
                    paths.append(f"{path}:/home")
                elif mount["Destination"] == "/root/SEAMM":
                    seamm_path = Path(mount["Source"])
                    paths.append(f"{seamm_path}:/root/SEAMM")
            if len(paths) == 0:
                raise RuntimeError(
                    "Neither /home or /root/SEAMM were in the mounts for the container!"
                    "\n{mounts=}"
                )
            if no_home:
                path = seamm_path.joinpath(*directory.parts[3:])
                paths.append(f"{path}:/home")
        else:
            # Not in a Docker container, so use the path as is
            paths = [f"{Path(directory)}:/home"]

        self.logger.debug(f"path = {path}\n")

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

            if "SEAMM_DEBUG_DOCKER" in os.environ:
                with open(directory / "docker.txt", "a") as fd:
                    print(
                        f"""
                        result = client.containers.run(
                            command={command},
                            environment={env},
                            image={container},
                            platform={platform},
                            remove=True,
                            stderr=True,
                            stdout=True,
                            volumes={paths},
                            working_dir="/home",
                        )
                        """,
                        file=fd,
                        flush=True,
                    )
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
                    volumes={paths},
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
                volumes=paths,
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
                    volumes={paths},
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
                volumes=paths,
                working_dir="/home",
            )

        self.logger.debug("\n" + pprint.pformat(result))

        return {}
