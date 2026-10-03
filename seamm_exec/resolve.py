# -*- coding: utf-8 -*-

"""Resolve a program on the machine that runs it.

A task names a *program* ("orca", "mopac", ...). Where it runs, the program's
configuration comes from that machine's ``<root>/<program>.ini`` (the section
for the executor, normally ``[local]``), and a plug-in may register a
*resolver* that adjusts the configuration, the command and the environment for
that machine and the task's share of it: ORCA must be invoked by its full path,
and a parallel run needs the OpenMPI it was built against on the paths.

Resolvers are entry points in the group ``org.molssi.seamm.exec.resolvers``,
named after the program::

    [project.entry-points."org.molssi.seamm.exec.resolvers"]
    orca = "orca_step.resolver:resolve"

with the signature ::

    def resolve(config, cmd, env, ce, root) -> (config, cmd, env)

``config`` is the program's ini section as a dict (already a copy), ``cmd`` the
task's command template (a list), ``env`` its extra environment, ``ce`` the
task's computational environment (``NTASKS``, ``MEM_PER_CPU``, ...) and
``root`` the SEAMM root holding the ini files. The command is formatted with
``config`` and ``ce`` afterwards, by the executor, exactly as before.

``Task.config`` is never sent to a remote back end (``transport = ssh``): it was
resolved on the evaluator's machine and names that machine's paths, so the
``TaskSet`` keeps such a task in the evaluator's own pool, with a warning. A
task without it is resolved where it runs, here. On the local transport
``Task.config``, when given, is used as it is, as in the ``LocalPool``.
"""

import configparser
import logging
from pathlib import Path

logger = logging.getLogger("seamm-exec")

RESOLVER_GROUP = "org.molssi.seamm.exec.resolvers"

_resolvers = None


def read_config(program, root, section="local"):
    """The ``[section]`` of ``<root>/<program>.ini`` as a dict, or None."""
    if root is None or not program:
        return None
    path = Path(root).expanduser() / f"{program}.ini"
    if not path.exists():
        return None
    full_config = configparser.ConfigParser()
    full_config.read(path)
    if section not in full_config:
        return None
    return dict(full_config.items(section))


def resolvers():
    """The registered resolvers, ``{program: callable}``."""
    global _resolvers
    if _resolvers is None:
        _resolvers = {}
        try:
            from importlib.metadata import entry_points

            try:
                eps = entry_points(group=RESOLVER_GROUP)
            except TypeError:  # Python < 3.10
                eps = entry_points().get(RESOLVER_GROUP, [])
            for ep in eps:
                try:
                    _resolvers[ep.name] = ep.load()
                except Exception:
                    logger.exception(f"Could not load the resolver for '{ep.name}'")
        except Exception:
            logger.exception("Could not read the program resolvers")
    return _resolvers


def register(program, function):
    """Register a resolver by hand (tests, or a program without a plug-in)."""
    resolvers()[program] = function


def has_resolver(program):
    """Whether ``program`` has a registered resolver."""
    return bool(program) and program in resolvers()


def available(program, root):
    """Whether ``program`` can run here without an ini file: its resolver says
    so through an optional ``available(root)`` attribute."""
    function = resolvers().get(program)
    check = getattr(function, "available", None)
    if check is None:
        return False
    try:
        return bool(check(root))
    except Exception:
        return False


def resolve(program, config, cmd, env, ce, root):
    """Apply the program's resolver, if it has one.

    Returns
    -------
    (dict, list, dict)
        The configuration, command and environment to run with.
    """
    config = dict(config or {})
    cmd = list(cmd)
    env = dict(env or {})
    function = resolvers().get(program)
    if function is None:
        return config, cmd, env
    result = function(config=config, cmd=cmd, env=env, ce=ce, root=root)
    if result is None:
        return config, cmd, env
    return result
