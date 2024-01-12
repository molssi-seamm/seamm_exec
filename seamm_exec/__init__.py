"""Classes to execute background codes for SEAMM"""

# Add imports here
from .computational_environment import computational_environment  # noqa: F401
from .exec_flowchart import run  # noqa: F401
from .exec_flowchart import run_from_jobserver  # noqa: F401
from .local import Local  # noqa: F401
from .docker import Docker  # noqa: F401

# Handle versioneer
from ._version import get_versions

__author__ = """Paul Saxe"""
__email__ = "psaxe@molssi.org"
versions = get_versions()
__version__ = versions["version"]
__git_revision__ = versions["full-revisionid"]
del get_versions, versions

# List of executors corresponding to imports above.
executors = ["local", "docker"]


def get_executor(executor):
    """Return an object of the executor requested.

    Parameters
    ----------
    executor : str
        The name of the executor.

    Returns
    -------
    instance of executor
    """
    if executor.lower() == "local":
        return Local()
    elif executor.lower() == "docker":
        return Docker()
    else:
        raise RuntimeError(f"Don't recognize executor '{executor}'.")
