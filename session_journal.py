#!/usr/bin/python3
"""Mirror Codex turns into the branch-specific sessions_codex journal."""

import datetime
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from _wt_config import BASE_BRANCH, FEATURE_PREFIX, TICKET_PREFIX, setting


MAX_CONTEXT_MESSAGES = 20


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def git_value(cwd, *arguments):
    process = subprocess.run(
        ["git", "-C", cwd] + list(arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    return process.stdout.strip()


def journal_location(cwd):
    root = pathlib.Path(git_value(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve().parent
    branch = git_value(cwd, "branch", "--show-current")
    if not branch:
        raise RuntimeError("impossible de creer un journal sans branche Git courante")

    if branch.startswith((FEATURE_PREFIX, TICKET_PREFIX)):
        journal_name = branch.split("/", 1)[1]
    elif branch == BASE_BRANCH:
        journal_name = branch
    else:
        raise RuntimeError("branche hors convention : préciser le journal")
    if "/" in journal_name or journal_name in (".", ".."):
        raise RuntimeError("nom de journal invalide")
    return root, branch, root / setting("JOURNAL_DIR", "sessions_codex") / (journal_name + ".jsont")


def new_journal(branch, timestamp):
    return {
        "branch": branch,
        "created_at": timestamp,
        "updated_at": timestamp,
        "messages": [],
    }


def load_journal(path, branch, timestamp):
    if not path.exists():
        return new_journal(branch, timestamp)

    with path.open("r", encoding="utf-8") as stream:
        journal = json.load(stream)
    if not isinstance(journal, dict) or not isinstance(journal.get("messages"), list):
        raise ValueError("le journal existant ne contient pas un tableau messages valide")
    if journal.get("branch") != branch:
        raise ValueError("le journal existant appartient a une autre branche")
    return journal


def append_once(journal, message):
    identity = (message.get("session_id"), message.get("turn_id"), message.get("role"))
    for existing in journal["messages"]:
        existing_identity = (
            existing.get("session_id"),
            existing.get("turn_id"),
            existing.get("role"),
        )
        if identity == existing_identity:
            return False
    journal["messages"].append(message)
    return True


def write_atomic(path, journal):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(journal, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def context_output(path, journal):
    messages = journal["messages"][-MAX_CONTEXT_MESSAGES:]
    if not messages:
        return None
    context = {
        "journal": str(path),
        "branch": journal["branch"],
        "recent_messages": messages,
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Reprends la session projet existante ci-dessous et continue a alimenter "
                "son journal automatiquement :\n" + json.dumps(context, ensure_ascii=False)
            ),
        }
    }


def process_event(payload):
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "UserPromptSubmit", "Stop"):
        raise ValueError("evenement Codex non pris en charge: %r" % event)

    # Les sous-agents ne doivent pas polluer le journal de la conversation principale.
    if payload.get("agent_id") or payload.get("agent_type"):
        return None

    cwd = payload.get("cwd")
    if not cwd:
        raise ValueError("le hook Codex n'a pas fourni cwd")

    root, branch, path = journal_location(cwd)
    # Le lanceur crée un journal central même pour une tâche jamais ouverte.
    # Ne pas recopier automatiquement les journaux éventuellement présents ailleurs.
    lock_path = pathlib.Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now()

    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        journal = load_journal(path, branch, timestamp)

        if event == "UserPromptSubmit":
            content = payload.get("prompt")
            if not isinstance(content, str):
                raise ValueError("le hook UserPromptSubmit n'a pas fourni prompt")
            append_once(
                journal,
                {
                    "timestamp": timestamp,
                    "role": "user",
                    "content": content,
                    "session_id": payload.get("session_id"),
                    "turn_id": payload.get("turn_id"),
                },
            )
        elif event == "Stop":
            content = payload.get("last_assistant_message")
            if content:
                append_once(
                    journal,
                    {
                        "timestamp": timestamp,
                        "role": "assistant",
                        "content": content,
                        "session_id": payload.get("session_id"),
                        "turn_id": payload.get("turn_id"),
                    },
                )

        transcript = payload.get("transcript_path")
        if event == "SessionStart" and transcript:
            transcript_path = pathlib.Path(transcript).resolve()
            if transcript_path.is_file():
                alias = path.with_suffix(".jsonl")
                # Conserver les transcripts natifs, remplacer uniquement notre lien.
                if alias.exists() and not alias.is_symlink():
                    raise RuntimeError("alias de reprise occupé par un fichier ordinaire")
                temp_alias = alias.with_name(alias.name + ".link-" + str(os.getpid()))
                temp_alias.symlink_to(transcript_path)
                os.replace(temp_alias, alias)
        journal["updated_at"] = timestamp
        write_atomic(path, journal)
        return context_output(path, journal) if event == "SessionStart" else None


def main():
    try:
        payload = json.load(sys.stdin)
        output = process_event(payload)
        if output:
            json.dump(output, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
    except Exception as error:
        sys.stderr.write("[worktree][codex] echec du journal de session: %s\n" % error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
