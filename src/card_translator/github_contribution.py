from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from card_translator.project_documents import missing_document_translations
from card_translator.project_publication import publication_status

_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _run(
    command: list[str], *, cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "Command failed"
        raise ValueError(message)
    return result


def _github_repository(value: str) -> tuple[str, str] | None:
    match = _GITHUB_REPOSITORY.fullmatch(value.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def _contribution_scope(scenario: dict[str, Any], scope: str | None) -> str:
    if scenario.get("source_only"):
        return "extraction"
    resolved = scope or "translation"
    if resolved not in {"translation", "extraction"}:
        raise ValueError("Unknown contribution scope")
    return resolved


def _branch_name(scenario: dict[str, Any], scope: str) -> str:
    language = (
        "source"
        if scope == "extraction"
        else str(scenario.get("storage_language") or "translation")
    )
    raw = f"contribution/{scenario['game']}-{scenario['slug']}-{language}"
    return re.sub(r"[^A-Za-z0-9._/-]+", "-", raw).strip("-./")


def _git_status_paths(repository_root: Path) -> set[str]:
    result = _run(
        [
            "git", "-c", "core.quotepath=false", "status", "--porcelain=v1", "-z",
            "--untracked-files=all",
        ],
        cwd=repository_root,
    )
    records = result.stdout.split("\0")
    changed: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        state = record[:2]
        changed.add(record[3:])
        if "R" in state or "C" in state:
            if index < len(records) and records[index]:
                changed.add(records[index])
            index += 1
    return changed


def pull_request_status(
    scenario: dict[str, Any],
    repository_root: Path,
    *,
    upstream_url: str,
    base_branch: str,
    contribution_scope: str | None = None,
    include_extractions: bool = False,
) -> dict[str, Any]:
    scope = _contribution_scope(scenario, contribution_scope)
    publication = publication_status(scenario, repository_root)
    repository = _github_repository(upstream_url)
    git_installed = shutil.which("git") is not None
    git_repository = (repository_root / ".git").exists()
    all_changes: set[str] = set()
    staged_changes: list[str] = []
    if git_installed and git_repository:
        try:
            all_changes = _git_status_paths(repository_root)
            staged_changes = [
                path
                for path in _run(
                    ["git", "diff", "--cached", "--name-only"],
                    cwd=repository_root,
                ).stdout.splitlines()
                if path
            ]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    scoped_changes = set(publication["repository_changed_files"])
    language = str(scenario.get("storage_language") or "")
    translation_marker = f"/translations/{language}/"
    translation_changes = {
        path for path in scoped_changes if translation_marker in f"/{path}"
    }
    extraction_changes = scoped_changes - translation_changes
    selected_changes = (
        translation_changes | extraction_changes
        if scope == "translation" and include_extractions
        else translation_changes
        if scope == "translation"
        else extraction_changes
    )
    unrelated_changes = all_changes - scoped_changes
    missing_documents = missing_document_translations(scenario)
    can_prepare = bool(
        not scenario.get("private_only")
        and git_installed
        and git_repository
        and selected_changes
        and not staged_changes
    )
    return {
        **publication,
        "upstream_url": upstream_url,
        "upstream_repository": "/".join(repository) if repository else "",
        "base_branch": base_branch,
        "branch_name": _branch_name(scenario, scope),
        "contribution_scope": scope,
        "include_extractions": include_extractions,
        "selected_files": selected_changes,
        "translation_changed_files": translation_changes,
        "translation_changed_file_count": len(translation_changes),
        "extraction_changed_files": extraction_changes,
        "extraction_changed_file_count": len(extraction_changes),
        "can_prepare_translation": bool(
            not scenario.get("private_only")
            and git_installed
            and git_repository
            and translation_changes
            and not staged_changes
        ),
        "can_prepare_extraction": bool(
            not scenario.get("private_only")
            and git_installed
            and git_repository
            and extraction_changes
            and not staged_changes
        ),
        "git_installed": git_installed,
        "git_repository": git_repository,
        "gh_installed": shutil.which("gh") is not None,
        "unrelated_changes": sorted(unrelated_changes),
        "staged_changes": staged_changes,
        "missing_document_translations": missing_documents,
        "missing_document_count": len(missing_documents),
        "can_prepare": can_prepare,
        "can_submit": bool(can_prepare and repository),
    }


def _set_remote(repository_root: Path, name: str, url: str) -> None:
    existing = subprocess.run(
        ["git", "remote", "get-url", name],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        _run(["git", "remote", "set-url", name, url], cwd=repository_root)
    else:
        _run(["git", "remote", "add", name, url], cwd=repository_root)


def _fork_details_with_gh(
    repository_root: Path, *, upstream_url: str
) -> tuple[str, str]:
    repository = _github_repository(upstream_url)
    if repository is None:
        raise ValueError("The shared project source must be a GitHub repository")
    owner, name = repository
    _run(
        ["gh", "repo", "fork", f"{owner}/{name}", "--clone=false"],
        cwd=repository_root,
        timeout=180,
    )
    login = _run(
        ["gh", "api", "user", "--jq", ".login"], cwd=repository_root
    ).stdout.strip()
    if not login:
        raise ValueError("GitHub CLI did not return the signed-in user")
    protocol = _run(
        ["gh", "config", "get", "git_protocol"], cwd=repository_root
    ).stdout.strip()
    fork_url = (
        f"git@github.com:{login}/{name}.git"
        if protocol == "ssh"
        else f"https://github.com/{login}/{name}.git"
    )
    return login, fork_url


def create_pull_request(
    scenario: dict[str, Any],
    repository_root: Path,
    *,
    upstream_url: str,
    base_branch: str,
    method: str,
    fork_url: str = "",
    contribution_scope: str | None = None,
    include_extractions: bool = False,
) -> dict[str, Any]:
    status = pull_request_status(
        scenario,
        repository_root,
        upstream_url=upstream_url,
        base_branch=base_branch,
        contribution_scope=contribution_scope,
        include_extractions=include_extractions,
    )
    if scenario.get("private_only"):
        raise ValueError("Private-only scenarios cannot be submitted")
    if status["staged_changes"]:
        raise ValueError("The repository already contains staged changes")
    if not status["can_prepare"]:
        raise ValueError("This contribution is not ready for a pull request")
    if method not in {"gh", "manual"}:
        raise ValueError("Unknown pull request method")
    upstream = _github_repository(upstream_url)
    if upstream is None:
        raise ValueError("The shared project source must be a GitHub repository")
    fork = _github_repository(fork_url) if method == "manual" else None
    if method == "manual" and fork is None:
        raise ValueError("Enter the GitHub URL of your fork")
    if method == "gh" and not status["gh_installed"]:
        raise ValueError("GitHub CLI is not installed")

    current_branch = _run(
        ["git", "branch", "--show-current"], cwd=repository_root
    ).stdout.strip()
    if current_branch != base_branch:
        raise ValueError(f"Switch to the {base_branch} branch before creating a pull request")
    staged = _run(
        ["git", "diff", "--cached", "--name-only"], cwd=repository_root
    ).stdout.strip()
    if staged:
        raise ValueError("The repository already contains staged changes")

    branch = status["branch_name"]
    existing_branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repository_root,
        check=False,
    ).returncode == 0
    if existing_branch:
        raise ValueError(f"The local branch {branch} already exists")

    scope = status["contribution_scope"]
    title = (
        f"Add card data extraction for {scenario['title']}"
        if scope == "extraction"
        else f"Add {str(scenario['storage_language']).upper()} translation for {scenario['title']}"
    )
    contribution_name = (
        "card-data-extraction"
        if scope == "extraction"
        else "translation-with-card-data"
        if status["include_extractions"]
        else "translation"
    )
    body = (
        "Created with Unspeakable Translations.\n\n"
        f"Scenario: `{scenario['game']}/{scenario['slug']}`\n"
        f"Contribution: `{contribution_name}`\n"
        f"Workspace: `{'source' if scope == 'extraction' else scenario['storage_language']}`"
    )
    _run(["git", "switch", "-c", branch], cwd=repository_root)
    try:
        _run(
            ["git", "add", "--", *sorted(status["selected_files"])],
            cwd=repository_root,
        )
        _run(["git", "commit", "-m", title], cwd=repository_root)
        if method == "gh":
            fork_owner, resolved_fork_url = _fork_details_with_gh(
                repository_root, upstream_url=upstream_url
            )
        else:
            assert fork is not None
            fork_owner, _ = fork
            resolved_fork_url = fork_url
        _set_remote(repository_root, "contribution-fork", resolved_fork_url)
        _run(
            ["git", "push", "--set-upstream", "contribution-fork", branch],
            cwd=repository_root,
            timeout=180,
        )
    except Exception:
        # Nothing outside the selected scenario was staged, so restoring the
        # original branch safely returns the contribution to its pre-submit state.
        _run(["git", "reset", "--mixed", base_branch], cwd=repository_root)
        _run(["git", "switch", base_branch], cwd=repository_root)
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        raise

    owner, repository_name = upstream
    compare_url = (
        f"https://github.com/{owner}/{repository_name}/compare/"
        f"{quote(base_branch, safe='')}...{quote(fork_owner, safe='')}:"
        f"{quote(branch, safe='/')}?expand=1"
    )
    if method == "manual":
        return {"created": False, "url": compare_url, "branch": branch}

    try:
        result = _run(
            [
                "gh", "pr", "create",
                "--repo", f"{owner}/{repository_name}",
                "--base", base_branch,
                "--head", f"{fork_owner}:{branch}",
                "--title", title,
                "--body", body,
            ],
            cwd=repository_root,
            timeout=180,
        )
    except ValueError as exc:
        return {
            "created": False,
            "url": compare_url,
            "branch": branch,
            "warning": str(exc),
        }
    return {"created": True, "url": result.stdout.strip(), "branch": branch}
