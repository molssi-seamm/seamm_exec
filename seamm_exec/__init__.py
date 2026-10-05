"""Classes to execute background codes for SEAMM"""

# Add imports here
from .computational_environment import computational_environment  # noqa: F401
from .exec_flowchart import run  # noqa: F401
from .exec_flowchart import run_from_jobserver  # noqa: F401
from .local import Local  # noqa: F401
from .docker import Docker  # noqa: F401
from .tasks import Resources, Task, TaskResult, TaskSet, TaskBackend  # noqa: F401
from .tasks import run_task  # noqa: F401
from .timing import machine_class, record_task_timing  # noqa: F401
from .local_pool import LocalPool  # noqa: F401
from .scheduler_backend import SchedulerBackend  # noqa: F401
from .targets import find_target, write_target  # noqa: F401
from .evaluator import (  # noqa: F401
    AnalysisError,
    Evaluator,
    EvaluatorResult,
    Geometry,
    check_properties,
    choose_path,
    mdi_method_and_basis,
    structure_data,
)
from ._version import __version__  # noqa: F401

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
