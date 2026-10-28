"""Per-agent data-directory ownership; never moves legacy data implicitly."""

import json
import os
from pathlib import Path
import re


def validate_agent_id(agent_id):
    if not isinstance(agent_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", agent_id):
        raise ValueError("invalid agent identity")
    return agent_id


def ensure_agent_identity(data_dir, agent_id, *, adopt_existing=False):
    agent_id = validate_agent_id(agent_id)
    directory = Path(data_dir).expanduser().resolve()
    # A nested host can otherwise read another agent's memory/credentials even
    # though its own leaf marker is different. Do not follow marker symlinks.
    for parent in directory.parents:
        ancestor_marker = parent / '.agent-owner.json'
        if ancestor_marker.exists() or ancestor_marker.is_symlink():
            raise RuntimeError('Agent data directories cannot be nested.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / ".agent-owner.json"
    visited = 0
    for root, dirs, files in os.walk(directory, followlinks=False):
        visited += 1
        if visited > 10000:
            raise RuntimeError('Agent data ownership cannot be checked safely.')
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        if Path(root) != directory and ('.agent-owner.json' in files or '.agent-owner.json' in dirs):
            raise RuntimeError('Agent data directories cannot be nested.')
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, flags)
    except FileNotFoundError:
        if not adopt_existing and any(p.name != "server.lock" for p in directory.iterdir()):
            raise RuntimeError("Unowned data directory is not empty; use --adopt-existing-data explicitly to preserve and adopt it.")
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            return ensure_agent_identity(directory, agent_id, adopt_existing=adopt_existing)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"schema_version": 1, "agent_id": agent_id}, file)
            file.flush()
            os.fsync(file.fileno())
        return directory
    except OSError:
        raise RuntimeError("Agent data ownership marker cannot be safely read.") from None
    try:
        with os.fdopen(fd, encoding="utf-8") as file:
            value = json.load(file)
    except (ValueError, OSError, UnicodeError):
        raise RuntimeError("Agent data ownership marker is invalid.") from None
    if value != {"schema_version": 1, "agent_id": agent_id}:
        raise RuntimeError("Data directory belongs to a different Agent or has an unsupported marker.")
    return directory
