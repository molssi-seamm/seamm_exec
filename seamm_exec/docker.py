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
        # logger.setLevel(logging.DEBUG)

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
        if len(cmd) > 0:
            # Replace any strings in the cmd with those in the configuration
            command = " ".join(cmd)
            command = command.format(**config, **ce)

            self.logger.debug(
                f"""
                result = client.containers.run(
                    command={command},
                    environment={env},
                    image={config["container"]},
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
                image=config["container"],
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
                    image={config["container"]},
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
                image=config["container"],
                remove=True,
                stderr=True,
                stdout=True,
                volumes=[f"{path}:/home"],
                working_dir="/home",
            )

        self.logger.debug("\n" + pprint.pformat(result))

        return {}
