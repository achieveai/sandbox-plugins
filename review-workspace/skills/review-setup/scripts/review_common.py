"""Shared, credential-free transport and recoverable local review lifecycle."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
import uuid


class ReviewError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class CleanupNotProvenError(ReviewError):
    """An owned child process may still be running; no fallback may proceed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def preflight() -> dict:
    if sys.version_info < (3, 10):
        raise ReviewError("Python 3.10 or newer is required; no pip packages are needed.")
    versions = {"python": sys.version.split()[0]}
    for command in ("git", "curl"):
        if not shutil.which(command):
            raise ReviewError(f"Required sandbox tool is not installed: {command}")
        result = subprocess.run([command, "--version"], text=True, capture_output=True, check=False)
        if result.returncode:
            raise ReviewError(f"Cannot execute required tool: {command}")
        versions[command] = result.stdout.splitlines()[0]
    return versions


def run_owned(args: list[str], *, timeout: float, term_grace: float = 2.0,
              kill_grace: float = 2.0, env: dict[str, str] | None = None,
              input_data: bytes | None = None) -> subprocess.CompletedProcess:
    """Run a command as the sole owner of a fresh POSIX session/process group.

    On timeout, only the process group this call created is ever signalled:
    first TERM, then (if the group is not independently proven empty) KILL.
    "Terminated" is never inferred from pipe EOF or the direct child being
    reaped alone -- a descendant that closes/redirects its inherited pipe is
    invisible to those checks, so every "terminated" outcome is confirmed
    with a killpg(pgid, 0) probe before being reported as such. Pipes are
    drained and the direct child is reaped before this function returns or
    raises. Every exit path funnels through a single reap/close step so a
    caller never leaks the child, its pipes, or an unreaped zombie, even if
    escalation itself raises partway through.

    If cleanup cannot be proven complete within the bounded windows, this
    raises ReviewError describing a blocked/error state -- it never claims
    the command was cleanly cancelled when that isn't verified.
    """
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError as exc:
        raise ReviewError(f"Could not start owned process ({type(exc).__name__}).") from None

    try:
        return _run_owned_body(process, timeout, term_grace, kill_grace, input_data)
    except ReviewError:
        raise
    except OSError as exc:
        # Any other OS-level failure across signalling/probing/draining/
        # reaping still becomes a clean ReviewError, matching the guarantee
        # a single subprocess.run(...) call used to give run_git's callers
        # (this operation now spans Popen, signalling, probing and reaping
        # instead of one call, so the guard has to span all of it too).
        raise ReviewError(f"Owned process operation could not complete ({type(exc).__name__}).") from None


def _run_owned_body(process: subprocess.Popen, timeout: float, term_grace: float,
                    kill_grace: float, input_data: bytes | None = None) -> subprocess.CompletedProcess:
    # start_new_session calls setsid() in the child, so its process group id
    # is expected to equal its own pid. Confirm this with a real syscall
    # instead of assuming it. The resulting pgid is the *only* value ever
    # used for a signal or a probe below -- it is captured once, here, and
    # never re-derived mid-flight, so escalation can never drift onto a
    # different (possibly pid-reused) group than the one we confirmed.
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        process.wait()
        raise ReviewError(
            "Owned process exited before its process group could be confirmed."
        ) from None
    if pgid != process.pid:
        process.kill()
        process.wait()
        raise ReviewError(
            "Owned process group id did not match its own pid after setsid(); "
            "refusing to signal an unexpected group."
        ) from None
    stdout = stderr = b""
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr = _escalate_owned_group(process, pgid, term_grace, kill_grace)
    finally:
        # Regardless of how we got here (including a blocked ReviewError
        # raised by escalation), make sure the direct child is reaped and
        # its pipe fds are closed; never leave a zombie or leaked FD.
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=kill_grace)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout, process.stderr, process.stdin):
            if stream is not None:
                stream.close()

    if timed_out:
        raise ReviewError(f"Command timed out after {timeout}s and its process group was terminated.")
    return subprocess.CompletedProcess(process.args, process.returncode,
                                       stdout.decode("utf-8", "surrogateescape"),
                                       stderr.decode("utf-8", "surrogateescape"))


def _signal_owned_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # The group is already gone; nothing left to signal.
    except PermissionError:
        # EPERM here means this pgid can no longer be safely proven ours to
        # signal (e.g. a narrow pid/pgid-reuse window) -- never treat that as
        # a successful signal or silently continue; surface it distinctly.
        raise CleanupNotProvenError(
            "Permission denied signalling the owned process group; treating "
            "as blocked rather than cancelled."
        ) from None



def _group_confirmed_empty(pgid: int, deadline: float) -> bool:
    """True only once killpg(pgid, 0) itself proves no member remains.

    Pipe EOF and a direct-child reap are not sufficient evidence that every
    process in the group has exited -- a descendant that never touches (or
    closes) the inherited pipe is invisible to those checks. A signal-0
    probe raises ProcessLookupError only when no process in the group
    remains; anything else means "not proven empty yet", bounded by
    `deadline` so this can never wait forever.
    """
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            raise CleanupNotProvenError(
                "Permission denied probing the owned process group for "
                "emptiness; treating as blocked rather than cancelled."
            ) from None
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)



def _drain(process: subprocess.Popen, grace: float) -> tuple[bytes, bytes, bool]:
    try:
        stdout, stderr = process.communicate(timeout=grace)
        return stdout, stderr, True
    except subprocess.TimeoutExpired:
        return b"", b"", False


def _escalate_owned_group(process: subprocess.Popen, pgid: int, term_grace: float,
                          kill_grace: float) -> tuple[bytes, bytes]:
    """Signal only this call's own process group; never anything unrelated.

    Never declares the group terminated on pipe EOF / direct-child reap
    alone: after TERM (and again after KILL), the direct child being reaped
    is necessary but not sufficient -- group emptiness is independently
    confirmed with `_group_confirmed_empty` before returning success.
    """
    _signal_owned_group(pgid, signal.SIGTERM)
    stdout, stderr, reaped = _drain(process, term_grace)
    if reaped and _group_confirmed_empty(pgid, time.monotonic() + term_grace):
        return stdout, stderr

    # Either the direct child (and/or pipe) didn't settle within term_grace,
    # or it did but the group isn't provably empty (e.g. a TERM-resistant
    # descendant that closed/redirected the inherited pipe) -- escalate to
    # KILL rather than trust pipe EOF alone.
    _signal_owned_group(pgid, signal.SIGKILL)
    if not reaped:
        stdout, stderr, reaped = _drain(process, kill_grace)
    if reaped and _group_confirmed_empty(pgid, time.monotonic() + kill_grace):
        return stdout, stderr

    raise CleanupNotProvenError(
        "Owned process group required SIGKILL and could not be independently "
        "confirmed empty within the bounded window; treating as blocked "
        "rather than cancelled."
    )



def run_git(repo: Path, args: list[str], check: bool = True, *,
            read_only: bool = False, input_data: bytes | None = None,
            timeout: float = 300) -> subprocess.CompletedProcess:
    # Do not echo stderr: remote helpers can include credential-bearing URLs.
    # Disable optional index refreshes for observations, including legacy reset
    # dry-run helpers. Explicit Git mutations still take their mandatory locks.
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = run_owned(["git", "--literal-pathspecs", "-c", "diff.external=", "-C", str(repo), *args],
                       timeout=timeout, env=environment, input_data=input_data)
    if check and result.returncode:
        operation = next((arg for arg in args if not arg.startswith("-")), "operation")
        raise ReviewError(f"Git {operation} failed (exit {result.returncode}); no state was forcibly cleared.")
    return result


def run_git_read(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a Git observation with optional lock/index refreshes disabled."""
    return run_git(repo, args, check=check, read_only=True)


def bounded_fetch(repo: Path, remote: str, refs: list[str], *, depth: int,
                  max_depth: int = 1000000, deepen: bool = False) -> subprocess.CompletedProcess:
    """Fetch under the caller's lifecycle lock without shortening shared history.

    Servers lacking relative shallow fetch support use one temporary bounded
    acquisition store. Only local Git then updates the shared shallow boundary.
    No alternate object directory, hand-edited shallow file, or checkout is used.
    """
    if not isinstance(depth, int) or isinstance(depth, bool) or not 0 < depth <= max_depth:
        raise ReviewError("Bounded fetch depth is outside the configured history budget.")
    shallow = run_git(repo, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"
    boundary = f"--deepen={depth}" if shallow or deepen else f"--depth={depth}"
    args = ["-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch", "--no-tags", boundary, "--", remote, *refs]
    result = run_git(repo, args, check=False)
    if result.returncode == 0 or "fatal: Server does not support --deepen" not in result.stderr:
        return result
    return _fetch_via_bounded_store(repo, remote, refs, depth=depth, max_depth=max_depth)


def _fetch_via_bounded_store(repo: Path, remote: str, refs: list[str], *, depth: int,
                             max_depth: int) -> subprocess.CompletedProcess:
    deadline = time.monotonic() + 300

    def command(path: Path, args: list[str], *, check: bool = True):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewError("Bounded compatibility fetch exhausted its 300-second budget.")
        return run_git(path, args, check=check, timeout=remaining)

    common = Path(command(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip())
    # Include all refs and registered detached HEADs: local upload-pack must know
    # every old shallow root, not merely the newly requested divergent PR tip.
    roots = command(common, ["for-each-ref", "--format=%(objectname)"]).stdout.split()
    roots += [row["head"] for row in worktree_rows(common) if row.get("head")]
    roots = list(dict.fromkeys(validate_sha(sha) for sha in roots))
    if len(roots) + len(refs) > 256:
        raise ReviewError("Bounded compatibility fetch exceeds the 256-root limit.")
    edges_before = command(common, ["rev-list", "--all", "--parents"]).stdout.splitlines()
    # A total reachable-count bound overestimates every current root's path
    # length. Adding the requested deepen amount cannot under-fetch old roots.
    absolute_depth = max(depth, len(edges_before) + depth)
    if absolute_depth > max_depth:
        raise ReviewError("Server lacks relative-depth fetch and absolute acquisition exceeds the history budget.")
    remote_url = remote
    if remote == "origin":
        remote_url = command(common, ["config", "--get", "remote.origin.url"]).stdout.strip()
    # The URL is an already validated configured remote; never print it.
    object_bytes = sum(path.stat().st_size for path in (common / "objects" / "pack").glob("*.pack"))
    reserve = max(2 * 1024 ** 3, object_bytes * 2)
    if shutil.disk_usage(common).free < reserve:
        raise ReviewError("Insufficient temporary-pack headroom for bounded compatibility fetch.")
    temp = Path(tempfile.mkdtemp(prefix=".bounded-fetch-", dir=common.parent))
    cleanup = True
    try:
        command(temp, ["init", "--bare", "-q"])
        requested = list(dict.fromkeys(refs))
        specifiers = list(dict.fromkeys([*requested, *roots]))
        mappings = [f"{ref}:refs/heads/acquire-{index}" for index, ref in enumerate(specifiers)]
        acquisition = command(temp, ["-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch",
                                     "--no-tags", f"--depth={absolute_depth}", "--", remote_url, *mappings], check=False)
        if acquisition.returncode:
            return acquisition
        acquired_bytes = sum(path.stat().st_size for path in (temp / "objects" / "pack").glob("*.pack"))
        if shutil.disk_usage(common).free < max(2 * 1024 ** 3, acquired_bytes * 2):
            raise ReviewError("Insufficient headroom to import the bounded compatibility pack.")
        tips = [validate_sha(command(temp, ["rev-parse", f"refs/heads/acquire-{i}^{{commit}}"]).stdout.strip())
                for i in range(len(specifiers))]
        # Requested tips stay first so FETCH_HEAD continues to identify the
        # caller's baseline HEAD, not an arbitrary retained history root.
        tips = list(dict.fromkeys(tips))
        local_args = ["-c", "protocol.file.allow=always", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                      "fetch", "--no-tags", "--update-shallow"]
        imported = command(common, [*local_args, "--", temp.as_uri(), *tips], check=False)
        if imported.returncode:
            return imported
        expanded = command(common, [*local_args, f"--deepen={depth}", "--", temp.as_uri(), *tips], check=False)
        if expanded.returncode:
            return expanded
        after = {parts[0]: set(parts[1:]) for parts in
                 (line.split() for line in command(common, ["rev-list", "--all", "--parents"]).stdout.splitlines())}
        for line in edges_before:
            parts = line.split()
            if parts[0] not in after or not set(parts[1:]).issubset(after[parts[0]]):
                raise ReviewError("Bounded compatibility fetch failed retained-history preservation verification.")
        return expanded
    except CleanupNotProvenError:
        cleanup = False
        raise
    finally:
        # Only this invocation's owned disposable acquisition store is removed.
        # If process cleanup is unproven, preserve it for the surviving process.
        if cleanup:
            shutil.rmtree(temp)


def ensure_repository(path: Path, *, read_only: bool = False) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ReviewError("Expected a real Git checkout directory.")
    runner = run_git_read if read_only else run_git
    result = runner(path, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode or Path(result.stdout.strip()).resolve() != path.resolve():
        raise ReviewError("Git root mismatch; refusing an empty submodule or parent-repository fallback.")
    return path.resolve()


def discover_root(explicit: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).absolute()
    else:
        script = Path(__file__).resolve()
        # Inference is only valid for the in-repo layout <root>/.claude/skills/review-setup/scripts/.
        # Installed from a plugin cache, parents[4] is an unrelated directory: refuse instead.
        if len(script.parents) < 5 or script.parents[3].name != ".claude" or script.parents[2].name != "skills":
            raise ReviewError("--root is required: the review root cannot be inferred because this script is not "
                              "installed at <review-root>/.claude/skills/review-setup/scripts/.")
        root = script.parents[4]
    ensure_repository(root)
    if not (root / ".gitmodules").is_file() or not (root / "PRs").is_dir():
        raise ReviewError("Expected the canonical NOVA_reviews root with .gitmodules and PRs/.")
    return root.resolve()


def repo_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._]", "-", name.lower()).strip("-")
    if not slug or slug in (".", ".."):
        raise ReviewError("Repository name cannot form a safe review slug.")
    return slug


def validate_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ReviewError("Provider did not supply a full, valid commit ID.")
    return value.lower()


def parse_ado_url(url: str) -> dict:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ReviewError("Invalid Azure DevOps repository URL.") from None
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or port not in (None, 443):
        raise ReviewError("ADO repository URL must be HTTPS without credentials, query, or custom port.")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if any(part in ("", ".", "..") or "/" in part or "\\" in part or any(ord(c) < 32 for c in part) for part in parts):
        raise ReviewError("Unsafe Azure DevOps URL path.")
    host = (parsed.hostname or "").lower()
    if host == "dev.azure.com" and len(parts) == 4 and parts[2] == "_git":
        org, project, _, repository = parts
    elif host.endswith(".visualstudio.com"):
        org = host.removesuffix(".visualstudio.com")
        if parts and parts[0].lower() == "defaultcollection":
            parts = parts[1:]
        if len(parts) != 3 or parts[1] != "_git":
            raise ReviewError("Unsupported legacy Azure DevOps repository URL.")
        project, _, repository = parts
    else:
        raise ReviewError("Repository is not a recognized Azure DevOps HTTPS URL.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", org):
        raise ReviewError("Unsafe Azure DevOps organization name.")
    return {"org_url": f"https://dev.azure.com/{org.lower()}", "project": project, "repository": repository}


def canonical_ado_metadata_url(url: str, org_url: str) -> str:
    """Remove ADO's organization-name decoration from provider metadata only.

    Actual configured Git remotes still use strict parse_ado_url. Passwords,
    arbitrary usernames and cross-organization URLs are never accepted.
    """
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ReviewError("Invalid provider repository URL.") from None
    if parsed.scheme != "https" or parsed.password is not None or parsed.query or parsed.fragment or port not in (None, 443):
        raise ReviewError("Provider repository URL contains unsupported credentials or URL components.")
    host = (parsed.hostname or "").lower()
    if host != "dev.azure.com" and not host.endswith(".visualstudio.com"):
        raise ReviewError("Provider repository URL has an unexpected host.")
    clean = urlunsplit(("https", host, parsed.path, "", ""))
    coordinates = parse_ado_url(clean)
    if coordinates["org_url"].casefold() != org_url.rstrip("/").casefold():
        raise ReviewError("Provider repository URL points to a different organization.")
    organization = coordinates["org_url"].rsplit("/", 1)[1]
    if parsed.username is not None and unquote(parsed.username).casefold() != organization.casefold():
        raise ReviewError("Provider repository URL contains an unsupported username.")
    return (coordinates["org_url"] + "/" + quote(coordinates["project"], safe="")
            + "/_git/" + quote(coordinates["repository"], safe=""))


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or any(ord(c) < 32 for c in relative):
        raise ReviewError("Unsafe managed path.")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(p in (".", "..", ".git") for p in rel.parts):
        raise ReviewError("Managed paths must remain beneath their recorded root.")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ReviewError("Symlink in managed path; refusing to follow it.")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ReviewError("Managed path escapes its root.")
    return path


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReviewError("Expected a regular artifact file.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ReviewError("Refusing symlink state file.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_new_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ReviewError("Refusing symlink artifact.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".artifact-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # Atomic publication, refusing existing paths.
        except FileExistsError:
            raise ReviewError("Artifact already exists; previous runs are never overwritten.") from None
    finally:
        os.unlink(temporary)


def write_new_json(path: Path, value: dict) -> None:
    write_new_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


class AdoClient:
    """Read-only ADO queries through curl's inherited sandbox proxy/CA environment."""
    def __init__(self, org_url: str, *, timeout: int = 30, retries: int = 2):
        parsed = urlsplit(org_url)
        if parsed.scheme != "https" or parsed.hostname != "dev.azure.com" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ReviewError("ADO organization must be https://dev.azure.com/<organization>.")
        org = parsed.path.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", org) or parsed.port not in (None, 443):
            raise ReviewError("Invalid Azure DevOps organization.")
        self.org_url = f"https://dev.azure.com/{org.lower()}"
        self.timeout, self.retries = timeout, retries
        self.last_headers: dict[str, str] = {}

    def request(self, method: str, path: str, body=None):
        method = method.upper()
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or path.startswith("//") or "\\" in path:
            raise ReviewError("ADO request must use an organization-relative path.")
        if any(unquote(part) in (".", "..") or "/" in unquote(part) or "\\" in unquote(part)
               or any(ord(c) < 32 for c in unquote(part)) for part in parsed.path.split("/")):
            raise ReviewError("Unsafe ADO request path.")
        if method not in ("GET", "POST") or (method == "POST" and not parsed.path.lower().rstrip("/").endswith(("/workitemsbatch", "/pullrequestquery", "/wiql"))):
            raise ReviewError("Only read-only ADO query operations are allowed.")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("api-version", "7.1")
        url = self.org_url + "/" + parsed.path.lstrip("/") + "?" + urlencode(query)
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries + 1):
            with tempfile.TemporaryDirectory(prefix="review-ado-") as directory:
                headers = Path(directory) / "headers"
                output = Path(directory) / "body"
                args = ["curl", "--disable", "--silent", "--show-error", "--proto", "=https",
                        "--max-time", str(self.timeout), "--request", method, "--dump-header", str(headers),
                        "--output", str(output), "--write-out", "%{http_code}", "--header", "Accept: application/json"]
                if payload is not None:
                    args += ["--header", "Content-Type: application/json", "--data-binary", "@-"]
                args += [url]
                try:
                    result = subprocess.run(args, input=payload, capture_output=True, timeout=self.timeout + 5)
                except (OSError, subprocess.TimeoutExpired):
                    raise ReviewError("ADO transport failed; check sandbox proxy connectivity.") from None
                if result.returncode:
                    raise ReviewError(f"ADO transport failed (curl exit {result.returncode}); no response or credentials logged.")
                try:
                    status = int(result.stdout)
                except ValueError:
                    raise ReviewError("ADO transport returned an invalid HTTP status.") from None
                self.last_headers = {}
                if headers.exists():
                    for line in headers.read_text(encoding="iso-8859-1").splitlines():
                        key, sep, value = line.partition(":")
                        if sep and key.lower() in ("x-ms-continuationtoken", "retry-after"):
                            self.last_headers[key.lower()] = value.strip()
                if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    delay = self.last_headers.get("retry-after", "")
                    time.sleep(min(30, int(delay)) if delay.isdigit() else min(2 ** attempt, 8))
                    continue
                if not 200 <= status < 300:
                    raise ReviewError(f"ADO query failed (HTTP {status}); the resource may be inaccessible or unavailable.")
                try:
                    return json.loads(output.read_bytes())
                except (ValueError, OSError):
                    raise ReviewError("ADO returned a non-JSON response.") from None
        raise ReviewError("ADO request retry limit reached.")

    def get_pages(self, path: str) -> list:
        values, seen = [], set()
        for _ in range(1000):
            result = self.request("GET", path)
            if isinstance(result, list):
                return values + result
            if not isinstance(result, dict) or not isinstance(result.get("value"), list):
                raise ReviewError("ADO list response is missing its value array.")
            values.extend(result["value"])
            token = self.last_headers.get("x-ms-continuationtoken")
            if not token:
                return values
            if token in seen:
                raise ReviewError("ADO repeated a continuation token; refusing an incomplete list.")
            seen.add(token)
            parsed = urlsplit(path)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query["continuationToken"] = token
            path = urlunsplit(("", "", parsed.path, urlencode(query), ""))
        raise ReviewError("ADO pagination exceeded the safety limit.")


def read_submodules(root: Path, *, read_only: bool = False) -> list[dict]:
    config = root / ".gitmodules"
    if config.is_symlink():
        raise ReviewError("Refusing symlink .gitmodules.")
    runner = run_git_read if read_only else run_git
    result = runner(root, ["config", "--file", str(config), "--get-regexp", r"^submodule\..*\.path$"])
    modules = []
    for line in result.stdout.splitlines():
        key, _, relative = line.partition(" ")
        safe_path(root, relative)
        if not relative.startswith("repos/") or len(PurePosixPath(relative).parts) != 2:
            raise ReviewError("Source submodules must be direct children of repos/.")
        url = runner(root, ["config", "--file", str(config), "--get", key[:-4] + "url"]).stdout.strip()
        coordinates = parse_ado_url(url)
        modules.append({"name": key[len("submodule."):-len(".path")], "path": relative, "url": url, **coordinates})
    if not modules:
        raise ReviewError("No configured Azure DevOps source submodules found.")
    return modules


def git_status(repo: Path, *, read_only: bool = False) -> list[tuple[str, str]]:
    runner = run_git_read if read_only else run_git
    raw = runner(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout
    records, entries, index = raw.split("\0"), [], 0
    while index < len(records) and records[index]:
        row = records[index]
        code, path = row[:2], row[3:]
        entries.append((code, path))
        index += 1
        if "R" in code or "C" in code:
            if index < len(records) and records[index]:
                entries.append((code, records[index]))
                index += 1
    return entries


def current_ref(repo: Path, *, read_only: bool = False) -> str | None:
    runner = run_git_read if read_only else run_git
    result = runner(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def worktree_rows(repo: Path, *, read_only: bool = False) -> list[dict]:
    """Parse `git worktree list --porcelain -z` into structured rows.

    Each row carries at least a resolved absolute "worktree" path and a
    "detached" bool; "head" (sha) and "branch" (short name, when checked out)
    are included when Git reports them. Shared by `ReviewState.registered()`
    (the NOVA root's own worktree registry) and `SourcePool` (each owned bare
    source store's separate worktree registry) -- these are two distinct
    registries against two different repositories, not the same list twice.
    """
    runner = run_git_read if read_only else run_git
    raw = runner(repo, ["worktree", "list", "--porcelain", "-z"]).stdout
    rows: list[dict] = []
    current: dict | None = None
    for record in raw.split("\0"):
        if record.startswith("worktree "):
            if current is not None:
                rows.append(current)
            current = {"worktree": Path(record[9:]).resolve(), "detached": False}
        elif current is None:
            continue
        elif record.startswith("HEAD "):
            current["head"] = record[5:]
        elif record == "detached":
            current["detached"] = True
        elif record.startswith("branch "):
            current["branch"] = record[7:].removeprefix("refs/heads/")
    if current is not None:
        rows.append(current)
    return rows


def assert_no_git_operation(repo: Path) -> None:
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock"):
        resolved = run_git(repo, ["rev-parse", "--git-path", name]).stdout.strip()
        path = Path(resolved)
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            raise ReviewError("An existing Git operation or index lock must be resolved first.")


def inspect_cleanup_tree(repo: Path, *, protected: Path | None = None) -> list[str]:
    """Inventory without following links; nested repositories are never cleanup targets."""
    assert_no_git_operation(repo)
    paths = []
    for parent, dirs, files in os.walk(repo, followlinks=False):
        parent = Path(parent)
        for name in list(dirs) + files:
            path = parent / name
            if path == repo / ".git" or path == protected:
                if name in dirs:
                    dirs.remove(name)
                continue
            if name == ".git" or (path.is_dir() and not path.is_symlink()
                                  and (path / "HEAD").is_file() and (path / "objects").is_dir()):
                raise ReviewError("Unexpected nested Git repository; refusing slot cleanup.")
            if path.is_symlink() and name in dirs:
                dirs.remove(name)
            paths.append(path.relative_to(repo).as_posix())
    return paths


def cleanup_worktree(repo: Path, expected_head: str, *, protected: str | None = None) -> None:
    """Discard only one prevalidated checkout; never recurse into a source gitlink."""
    if run_git_read(repo, ["rev-parse", "HEAD"]).stdout.strip() != expected_head:
        raise ReviewError("Checkout HEAD changed after cleanup intent.")
    inspect_cleanup_tree(repo, protected=safe_path(repo, protected) if protected else None)
    run_git(repo, ["-c", "submodule.recurse=false", "restore", "--source=HEAD",
                   "--staged", "--worktree", "--", "."])
    args = ["clean", "-f", "-d", "-x"]
    if protected:
        args += ["-e", f"/{protected}/"]
    run_git(repo, [*args, "--", "."])


def subtree_files(root: Path, relative: str) -> dict[str, str]:
    directory = safe_path(root, relative)
    files = {}
    if not directory.exists():
        return files
    for parent, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            path = Path(parent) / name
            rel = path.relative_to(root).as_posix()
            safe_path(root, rel)
            if path.is_file():
                files[rel] = file_hash(path)
    return files


class ReviewState:
    """A script-owned slot registry. Root worktree and unknown paths are never reset."""
    def __init__(self, root: Path, *, read_only: bool = False):
        self.read_only = read_only
        self.root = ensure_repository(root, read_only=read_only)
        runner = run_git_read if read_only else run_git
        common = runner(root, ["rev-parse", "--git-common-dir"]).stdout.strip()
        self.common = (root / common).resolve() if not Path(common).is_absolute() else Path(common).resolve()
        self.directory = self.common / "review-setup"
        if self.directory.is_symlink():
            raise ReviewError("Refusing symlink review state directory.")
        self.identity = hashlib.sha256(str(self.common).encode()).hexdigest()

    @contextmanager
    def lock(self):
        # The global allocation lock also serializes manifest/branch transitions;
        # OS locks release after a crash, reservations remain until explicit reset.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("slots", "history", "pending"):
            if (self.directory / name).is_symlink():
                raise ReviewError("Refusing symlink review state subdirectory.")
        lockpath = self.directory / "allocation.lock"
        if lockpath.is_symlink():
            raise ReviewError("Refusing symlink lock file.")
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReviewError("Another review setup/reset operation is running.") from None
            yield
        finally:
            os.close(fd)

    def manifest_path(self, slot: int) -> Path:
        if not isinstance(slot, int) or slot < 0 or slot > 9999:
            raise ReviewError("Slot must be an integer from 0 through 9999.")
        return self.directory / "slots" / f"{slot}.json"

    def pool_run_path(self, repo_key: str, slot: int) -> Path:
        if not re.fullmatch(r"[a-z0-9._-]+", repo_key) or repo_key in (".", ".."):
            raise ReviewError("Invalid pool repository key.")
        SourcePool._validate_slots([slot])
        return self.directory / "pool-runs" / f"{repo_key}-{slot}.json"

    def save_pool_run(self, manifest: dict) -> None:
        repo_key = manifest.get("repo_key")
        slot = manifest.get("slot_number")
        path = self.pool_run_path(repo_key, slot)
        manifest["updated_at"] = utc_now()
        atomic_json(path, manifest)
        event = {
            "time": manifest["updated_at"],
            "event": "pool-run-state",
            "slot": manifest["slot_id"],
            "run_id": manifest["run_id"],
            "status": manifest["status"],
        }
        log = self.directory / "events.jsonl"
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def load_pool_run(self, repo_key: str, slot: int) -> dict | None:
        path = self.pool_run_path(repo_key, slot)
        if path.is_symlink() or path.parent.is_symlink():
            raise ReviewError("Refusing symlink pool-run state file.")
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ReviewError("Pool-run manifest is unreadable.") from None
        if (
            value.get("schema_version") != 2
            or value.get("identity") != self.identity
            or value.get("repo_key") != repo_key
            or value.get("slot_number") != slot
        ):
            raise ReviewError("Pool-run manifest has incompatible identity or schema.")
        return value

    def all_pool_runs(self) -> list[dict]:
        directory = self.directory / "pool-runs"
        if directory.is_symlink():
            raise ReviewError("Refusing symlink pool-run registry.")
        if not directory.exists():
            return []
        rows = []
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ReviewError("Pool-run manifest is unreadable.") from None
            if value.get("schema_version") != 2 or value.get("identity") != self.identity:
                raise ReviewError("Pool-run manifest has incompatible identity or schema.")
            rows.append(value)
        return rows

    def load(self, slot: int) -> dict:
        path = self.manifest_path(slot)
        if path.is_symlink() or path.parent.is_symlink():
            raise ReviewError("Refusing symlink state file.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ReviewError("No readable managed run exists for that slot.") from None
        if value.get("identity") != self.identity or value.get("slot") != slot or value.get("schema_version") != 1:
            raise ReviewError("Run manifest belongs to a different Git root or has an unsupported schema.")
        expected = f".worktrees/review-slot-{slot}"
        if value.get("slot_path") != expected:
            raise ReviewError("Run manifest contains an unexpected slot path.")
        safe_path(self.root, expected)
        return value

    def save(self, manifest: dict) -> None:
        if manifest.get("schema_version") == 2:
            self.save_pool_run(manifest)
            return
        manifest["updated_at"] = utc_now()
        atomic_json(self.manifest_path(manifest["slot"]), manifest)
        self.directory.mkdir(parents=True, exist_ok=True)
        event = {"time": manifest["updated_at"], "event": "state", "slot": manifest["slot"],
                 "run_id": manifest["run_id"], "status": manifest["status"]}
        log = self.directory / "events.jsonl"
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def slot_path(self, manifest: dict) -> Path:
        return safe_path(self.root, manifest["slot_path"])

    def all_runs(self) -> list[dict]:
        directory = self.directory / "slots"
        if directory.is_symlink():
            raise ReviewError("Refusing symlink slot registry.")
        return [self.load(int(path.stem)) for path in sorted(directory.glob("*.json"))]

    def registered(self, *, read_only: bool = False) -> dict[Path, str | None]:
        return {
            row["worktree"]: row.get("branch")
            for row in worktree_rows(self.root, read_only=read_only)
        }

    def check_slot(self, manifest: dict, *, allow_detached: bool = False) -> Path:
        slot = self.slot_path(manifest)
        ensure_repository(slot)
        if slot not in self.registered():
            raise ReviewError("Slot is not registered in the canonical repository.")
        common = run_git(slot, ["rev-parse", "--git-common-dir"]).stdout.strip()
        if (slot / common).resolve() != self.common:
            raise ReviewError("Slot belongs to a different repository or mount namespace.")
        assert_no_git_operation(slot)
        expected = manifest.get("archive_branch") if manifest.get("status") in ("renamed", "restoring") and manifest.get("archive_branch") else manifest["branch"]
        active = current_ref(slot)
        rename_recovery = (manifest.get("status") == "renaming" and active == manifest.get("archive_branch")
                           and run_git(slot, ["rev-parse", "HEAD"]).stdout.strip() == manifest.get("checkpoint_sha"))
        if active != expected and not rename_recovery and not (allow_detached and active is None):
            raise ReviewError("Slot branch changed outside this run; refusing to alter it.")
        return slot

    def validate_run_changes(self, manifest: dict, *, archive: bool = False) -> list[str]:
        slot = self.check_slot(manifest)
        return self._validate_run_changes_in_slot(slot, manifest, archive=archive)


    def validate_pool_run_changes(self, manifest: dict, *, archive: bool = False) -> list[str]:
        if manifest.get("schema_version") != 2:
            raise ReviewError("Expected a schema-2 pooled run manifest.")
        slot = safe_path(self.root, manifest["slot_path"])
        ensure_repository(slot)
        if slot not in self.registered():
            raise ReviewError("Pooled slot is not registered in the canonical repository.")
        common = run_git(
            slot, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
        if Path(common).resolve() != self.common:
            raise ReviewError("Pooled slot belongs to a different repository or mount namespace.")
        assert_no_git_operation(slot)
        expected_ref = (
            manifest.get("archive_branch")
            if manifest.get("reset_phase") == "branch-archived"
            else manifest["branch"]
        )
        if current_ref(slot) != expected_ref:
            raise ReviewError("Pooled slot branch changed outside this run.")
        return self._validate_run_changes_in_slot(slot, manifest, archive=archive)


    def _validate_run_changes_in_slot(
        self, slot: Path, manifest: dict, *, archive: bool
    ) -> list[str]:
        expected_tip = manifest.get("checkpoint_sha") or manifest["branch_start"]
        if run_git(slot, ["rev-parse", "HEAD"]).stdout.strip() != expected_tip:
            raise ReviewError("Review branch has new commits outside the recorded run; preserve them manually before reset.")
        files = []
        for code, relative in git_status(slot):
            safe_path(slot, relative)
            if relative == manifest["submodule"]["path"]:
                continue
            if not relative.startswith(manifest["pr_dir"] + "/"):
                raise ReviewError("Unrelated slot changes exist; nothing was discarded or staged.")
            if relative in manifest.get("baseline_artifacts", {}):
                raise ReviewError("An artifact from a prior round was modified; refusing to overwrite its history.")
            known_artifacts = dict(manifest.get("artifacts", {}))
            if manifest.get("status") == "publishing":
                known_artifacts.update(manifest.get("planned_artifacts", {}))
            if not archive and (relative not in known_artifacts or not safe_path(slot, relative).is_file() or file_hash(safe_path(slot, relative)) != known_artifacts[relative]):
                raise ReviewError("A generated artifact changed or new user output exists; archive it instead of discarding.")
            if code[0] not in (" ", "?") and not archive:
                raise ReviewError("Staged review output requires archive mode.")
            files.append(relative)
        source = safe_path(slot, manifest["submodule"]["path"])
        if (source / ".git").exists():
            ensure_repository(source)
            if git_status(source):
                raise ReviewError("Source submodule contains changes; no source code is automatically committed or reset.")
            if not manifest.get("source_baseline") and run_git(source, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]).stdout:
                raise ReviewError("Source checkout contains ignored local data; reset must preserve it.")
            if manifest.get("source_prepared") and run_git(source, ["rev-parse", "HEAD"]).stdout.strip() != manifest["snapshot"]["head_sha"]:
                raise ReviewError("Source HEAD changed after setup; refusing reset.")
        return sorted(set(files))


    def reserve(self, snapshot: dict, module: dict, *, slot: int | None = None) -> dict:
        key = f"{repo_slug(snapshot['repository']['name'])}-{int(snapshot['pr_number'])}"
        branch = f"review/{key}"
        if run_git(self.root, ["check-ref-format", "--branch", branch], check=False).returncode:
            raise ReviewError("Invalid review branch name.")
        if slot is not None:
            self.manifest_path(slot)
        runs = self.all_runs()
        for old in runs:
            if old["key"] == key and old["status"] not in ("archived", "discarded"):
                if slot is not None and old["slot"] != slot:
                    raise ReviewError(f"PR already owns slot {old['slot']}; reset it before choosing another slot.")
                if old["snapshot"].get("snapshot_fingerprint") != snapshot.get("snapshot_fingerprint"):
                    raise ReviewError("PR snapshot changed; archive/reset the prior run before preparing a new snapshot.", 3)
                if old["status"] == "reserving":
                    return self._finish_reserve(old)
                self.validate_run_changes(old)
                return old
        active = {r["slot"] for r in runs if r["status"] not in ("archived", "discarded")}
        if slot is None:
            slot = next((n for n in range(10000) if n not in active), None)
            if slot is None:
                raise ReviewError("No free review slot.")
        if slot in active:
            raise ReviewError("Requested slot is reserved for another review.")
        path = safe_path(self.root, f".worktrees/review-slot-{slot}")
        previous = next((r for r in runs if r["slot"] == slot), None)
        if path.exists() and previous is None:
            raise ReviewError("Existing slot directory is not owned by these scripts.")
        registered = self.registered()
        owners = [p for p, name in registered.items() if name == branch]
        if owners and owners != [path]:
            raise ReviewError("Review branch is checked out in another worktree.")
        baseline = run_git(self.root, ["rev-parse", "HEAD"]).stdout.strip()
        start_ref = None
        if previous:
            ensure_repository(path)
            if path not in registered or git_status(path):
                raise ReviewError("Previously released slot is not clean and registered.")
            baseline = run_git(path, ["rev-parse", "HEAD"]).stdout.strip()
            start_ref = current_ref(path)
        branch_result = run_git(self.root, ["rev-parse", "--verify", f"refs/heads/{branch}"], check=False)
        branch_start = branch_result.stdout.strip() if branch_result.returncode == 0 else baseline
        manifest = {"schema_version": 1, "identity": self.identity, "slot": slot,
                    "slot_path": path.relative_to(self.root).as_posix(), "key": key, "branch": branch,
                    "branch_existed": branch_result.returncode == 0, "branch_start": branch_start,
                    "baseline_sha": baseline, "baseline_ref": start_ref, "submodule": module,
                    "snapshot": snapshot, "pr_dir": f"PRs/{key}", "status": "reserving",
                    "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8],
                    "created_at": utc_now(), "artifacts": {}, "baseline_artifacts": {},
                    "source_prepared": False, "source_baseline": None}
        self.save(manifest)
        return self._finish_reserve(manifest)

    def _finish_reserve(self, manifest: dict) -> dict:
        path = self.slot_path(manifest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            exclude = self.common / "info/exclude"
            if exclude.is_symlink() or exclude.parent.is_symlink():
                raise ReviewError("Refusing symlink Git exclusion file.")
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if "/.worktrees/" not in existing.splitlines():
                exclude.parent.mkdir(exist_ok=True)
                with exclude.open("a", encoding="utf-8") as stream:
                    stream.write(("\n" if existing and not existing.endswith("\n") else "") + "/.worktrees/\n")
            run_git(self.root, ["worktree", "add", "--detach", str(path), manifest["baseline_sha"]])
        ensure_repository(path)
        if path not in self.registered() or git_status(path):
            raise ReviewError("Interrupted slot reservation is not clean and registered.")
        branch = manifest["branch"]
        exists = run_git(self.root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0
        if exists:
            actual = run_git(self.root, ["rev-parse", f"refs/heads/{branch}"]).stdout.strip()
            if actual != manifest["branch_start"]:
                raise ReviewError("Review ref changed during preparation.")
            run_git(path, ["checkout", branch])
        else:
            run_git(path, ["checkout", "-b", branch, manifest["branch_start"]])
        manifest["baseline_artifacts"] = subtree_files(path, manifest["pr_dir"])
        source = safe_path(path, manifest["submodule"]["path"])
        if (source / ".git").exists():
            ensure_repository(source)
            if git_status(source):
                raise ReviewError("Source submodule is not clean.")
            manifest["source_baseline"] = {"head": run_git(source, ["rev-parse", "HEAD"]).stdout.strip(), "ref": current_ref(source)}
        manifest["status"] = "reserved"
        self.save(manifest)
        return manifest


class SourcePool:
    """A shared, repo-scoped warm-slot pool of nested source checkouts.

    Six numbered outer worktrees per source repo (`.worktrees/{Repo}-{slot}`,
    slots 0..5) each host a detached nested source worktree at
    `repos/{Repo}`, all backed by exactly one bare object/common directory
    the pool owns: `review-setup/source-stores/{repo-key}.git`, under the
    NOVA store's own Git common directory (`state.common`). This is a
    separate, additive namespace from `ReviewState`'s per-PR
    `slots/<n>.json` schema-1 manifests -- `SourcePool` never reads or
    writes under `review-setup/slots/`, and `ReviewState` never reads or
    writes the pool's own state (`review-setup/pool/{repo-key}.json`).

    The module identity is resolved only from a `read_submodules()`-
    validated `.gitmodules` entry; a caller-supplied dict that does not
    exactly match a real entry is rejected before any Git call is made.

    Callers must hold `ReviewState.lock()` for the duration of any mutating
    call (`warm`, `refresh`), exactly like `ReviewState.reserve()` --
    `SourcePool` does not re-acquire or nest that lock itself.

    No `git submodule update`/`deinit` is ever used, no alternates file is
    ever created, no existing worktree is ever moved, and no forced
    cleanup (`--force`, `clean -f`, `reset --hard`) is ever issued against a
    slot this call did not just create. Unknown, foreign, legacy, occupied,
    dirty, staged, untracked, and ignored state is preserved: incompatible
    paths are refused with `ReviewError`, never deleted or overwritten.
    """

    SLOT_COUNT = 6

    def __init__(self, state: ReviewState, module: dict, *, reserve_bytes: int = 0):
        self.state = state
        known = {
            entry["path"]: entry
            for entry in read_submodules(state.root, read_only=state.read_only)
        }
        entry = known.get(module.get("path"))
        if not entry or entry["name"] != module.get("name") or entry["url"] != module.get("url"):
            raise ReviewError("Source module is not a validated .gitmodules submodule entry.")
        self.module = entry
        self.reserve_bytes = int(reserve_bytes)
        self.repo_key = repo_slug(self.module["name"])
        self.repo_name = PurePosixPath(self.module["path"]).name
        if not self.repo_name or self.repo_name in (".", ".."):
            raise ReviewError("Source module path must resolve to a real repository name.")
        self.directory = state.common / "review-setup" / "pool"
        self.store = state.common / "review-setup" / "source-stores" / f"{self.repo_key}.git"
        self.state_path = self.directory / f"{self.repo_key}.json"
        self._baseline_cache: str | None = None

    # -- slot naming -----------------------------------------------------

    def slot_name(self, slot: int) -> str:
        return f"{self.repo_name}-{slot}"

    def slot_relative_path(self, slot: int) -> str:
        return f".worktrees/{self.slot_name(slot)}"

    @staticmethod
    def _validate_slots(slots) -> list[int]:
        # Validate the complete requested slot set before any mutation: a
        # mixed request such as [0, 6] must be fully rejected, not partially
        # applied. `bool` is an `int` subclass in Python, so it is excluded
        # explicitly rather than accepted as 0/1.
        numbers = []
        for value in slots:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ReviewError("Pool slot numbers must be plain integers.")
            if value < 0 or value >= SourcePool.SLOT_COUNT:
                raise ReviewError(
                    f"Pool slot {value} is outside the supported range 0-{SourcePool.SLOT_COUNT - 1}."
                )
            numbers.append(value)
        seen: set[int] = set()
        ordered = []
        for value in numbers:
            if value not in seen:
                seen.add(value)
                ordered.append(value)
        return sorted(ordered)

    # -- owned bare store --------------------------------------------------

    def _ensure_store(self) -> None:
        parent = self.store.parent
        if parent.is_symlink():
            raise ReviewError("Refusing symlink source-store parent directory.")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.store.is_symlink():
            raise ReviewError("Refusing symlink source store.")
        if self.store.exists():
            result = run_git(self.store, ["rev-parse", "--is-bare-repository"], check=False)
            if result.returncode or result.stdout.strip() != "true":
                raise ReviewError("Existing source-store path is not a compatible bare repository.")
        else:
            run_git(parent, ["init", "--bare", "-q", str(self.store)])
        origin = run_git(self.store, ["remote", "get-url", "origin"], check=False)
        if origin.returncode:
            run_git(self.store, ["remote", "add", "origin", self.module["url"]])
        elif parse_ado_url(origin.stdout.strip()) != parse_ado_url(self.module["url"]):
            raise ReviewError("Source-store origin does not match the configured repository.")

    def _read_state(self) -> dict:
        if self.state_path.is_symlink() or self.state_path.parent.is_symlink():
            raise ReviewError("Refusing symlink pool state file.")
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ReviewError("Existing pool state is unreadable; refusing to replace it.") from None
        if not isinstance(value, dict) or value.get("repo_key") != self.repo_key:
            raise ReviewError("Pool state belongs to a different source repository.")
        return value


    def _write_state(self, updates: dict) -> dict:
        value = self._read_state()
        value.update(updates)
        value.update(
            {
                "schema_version": 1,
                "repo_key": self.repo_key,
                "repo_name": self.repo_name,
                "module_path": self.module["path"],
                "baseline_ref": f"refs/review-setup/baselines/{self.repo_key}",
            }
        )
        atomic_json(self.state_path, value)
        return value


    def cached_baseline_sha(self) -> str | None:
        """The last independently-verified baseline, without fetching."""
        value = self._read_state()
        sha = value.get("baseline_sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None
        if value.get("repo_key") != self.repo_key:
            return None
        if not self.store.exists() or self.store.is_symlink():
            return None
        baseline_ref = value.get("baseline_ref")
        expected_ref = f"refs/review-setup/baselines/{self.repo_key}"
        if baseline_ref != expected_ref:
            return None
        actual = run_git(
            self.store, ["rev-parse", "--verify", expected_ref], check=False
        )
        if actual.returncode or actual.stdout.strip() != sha:
            return None
        return sha


    def publish_baseline(self, sha: str, *, depth: int = 1000) -> None:
        sha = validate_sha(sha)
        if run_git(self.store, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode:
            raise ReviewError("Cannot publish a missing source-pool baseline commit.")
        baseline_ref = f"refs/review-setup/baselines/{self.repo_key}"
        run_git(self.store, ["update-ref", baseline_ref, sha])
        if run_git(self.store, ["rev-parse", baseline_ref]).stdout.strip() != sha:
            raise ReviewError("Source-pool baseline ref verification failed.")
        self._baseline_cache = sha
        self._write_state(
            {"baseline_sha": sha, "verified_at": utc_now(), "fetch_depth": depth}
        )


    def cached_baseline_sha_read_only(self) -> str | None:
        """Read the cached baseline without invoking Git or changing state."""
        return self.cached_baseline_sha()

    def refresh(self, *, depth: int = 1000, persist: bool = True) -> str:
        """Fetch a bounded default-branch snapshot without shrinking history."""
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
            raise ReviewError("Source-pool fetch depth must be a positive integer.")
        self._ensure_store()
        result = bounded_fetch(self.store, self.module["url"], ["HEAD"], depth=depth)
        if result.returncode:
            raise ReviewError(f"Bounded source-pool baseline fetch failed (exit {result.returncode}).")
        sha = validate_sha(run_git(self.store, ["rev-parse", "FETCH_HEAD"]).stdout.strip())
        if persist:
            self.publish_baseline(sha, depth=depth)
        return sha


    def _verified_baseline(self) -> str:
        # Repeated warm() calls against an already-verified cached baseline
        # reuse it and never re-fetch: first this pool instance's own
        # in-memory cache, then the persisted cache -- but only if the store
        # actually already has that exact object (never trust a cache entry
        # for a store that could have been recreated since).
        if self._baseline_cache is not None:
            return self._baseline_cache
        cached = self.cached_baseline_sha()
        if cached is not None and self.store.exists():
            check = run_git(self.store, ["cat-file", "-e", cached], check=False)
            if check.returncode == 0:
                self._baseline_cache = cached
                return cached
        return self.refresh()

    # -- warming -----------------------------------------------------------

    def warm(self, slots) -> list[dict]:
        numbers = self._validate_slots(slots)  # raises before any mutation
        if self.reserve_bytes:
            usage = shutil.disk_usage(self.state.common)
            if usage.free < self.reserve_bytes:
                raise ReviewError("Not enough free disk space reserved for the warm-pool operation.")
        root_head = validate_sha(run_git(self.state.root, ["rev-parse", "HEAD"]).stdout.strip())
        source_baseline = self._verified_baseline()
        return [self._warm_one(number, root_head, source_baseline) for number in numbers]

    def _warm_one(self, slot: int, root_head: str, source_baseline: str) -> dict:
        outer = safe_path(self.state.root, self.slot_relative_path(slot))
        nested = safe_path(self.state.root, f"{self.slot_relative_path(slot)}/{self.module['path']}")
        self._ensure_outer(outer, root_head)
        self._ensure_nested(nested, source_baseline)
        if current_ref(nested) is not None:
            raise ReviewError("Nested source worktree must remain detached.")
        if git_status(nested):
            raise ReviewError("Nested source worktree is not clean.")
        outer_status = git_status(outer)
        if any(
            path != self.module["path"] or code[0] != " "
            for code, path in outer_status
        ):
            raise ReviewError(
                "Pool outer worktree contains changes beyond its expected gitlink delta."
            )
        head = validate_sha(run_git(nested, ["rev-parse", "HEAD"]).stdout.strip())
        if head != source_baseline:
            raise ReviewError("Nested source worktree is not pinned to the verified baseline.")
        return {
            "slot": self.slot_name(slot),
            "slot_path": outer.relative_to(self.state.root).as_posix(),
            "baseline_sha": source_baseline,
            "root_head": root_head,
            "head_sha": head,
        }

    def _ensure_outer(self, outer: Path, root_head: str) -> None:
        if outer.is_symlink():
            raise ReviewError("Refusing symlink pool worktree target.")
        registered = self.state.registered()
        if outer.exists():
            ensure_repository(outer)
            if outer not in registered:
                raise ReviewError(
                    "Pool slot directory exists but is not a registered worktree; "
                    "refusing to adopt foreign or unmanaged state."
                )
            common = run_git(outer, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
            if Path(common).resolve() != self.state.common:
                raise ReviewError("Pool slot belongs to a different repository or mount namespace.")
            assert_no_git_operation(outer)
            if current_ref(outer) is not None:
                raise ReviewError(
                    "Pool slot outer worktree is on a branch; refusing to adopt foreign state."
                )
            return
        if outer.parent.is_symlink():
            raise ReviewError("Refusing symlink pool worktree parent directory.")
        outer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_git(self.state.root, ["worktree", "add", "--detach", str(outer), root_head])

    def _ensure_nested(self, nested: Path, source_baseline: str) -> None:
        if nested.is_symlink():
            raise ReviewError("Refusing symlink nested source worktree target.")
        if (nested / ".git").exists():
            ensure_repository(nested)
            common = run_git(nested, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
            if Path(common).resolve() != self.store.resolve():
                raise ReviewError(
                    "Nested source worktree is bound to a different store; refusing to adopt it."
                )
            owned = {row["worktree"] for row in worktree_rows(self.store)}
            if nested.resolve() not in owned:
                raise ReviewError(
                    "Nested source path is a Git checkout but not a registered worktree of the owned store."
                )
            assert_no_git_operation(nested)
            return
        if not nested.exists():
            raise ReviewError(
                "Expected the gitlink placeholder directory created by the outer worktree checkout; "
                "the outer worktree may be foreign or damaged."
            )
        if any(nested.iterdir()):
            raise ReviewError("Nested source path is occupied by unmanaged content; refusing to overwrite it.")
        run_git(self.store, ["worktree", "add", "--detach", str(nested), source_baseline])


    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id
        ):
            raise ReviewError("Run id cannot form a safe owned retention ref.")
        return run_id


    def retention_namespace(self, run_id: str) -> str:
        return f"refs/review-setup/retain/{self.repo_key}/{self._validate_run_id(run_id)}"


    def retention_ref(self, run_id: str, kind: str) -> str:
        if kind not in ("head", "base", "target", "baseline", "archive"):
            raise ReviewError("Unsupported retention-ref kind.")
        return f"{self.retention_namespace(run_id)}/{kind}"


    def retain(self, run_id: str, commits: dict[str, str]) -> dict[str, str]:
        """Pin exact owned commits before any checkout can move away from them."""
        if not self.store.exists() or self.store.is_symlink():
            raise ReviewError("Owned source store is absent or unsafe.")
        bare = run_git(self.store, ["rev-parse", "--is-bare-repository"], check=False)
        if bare.returncode or bare.stdout.strip() != "true":
            raise ReviewError("Owned source store is not a compatible bare repository.")
        required = ("head", "base", "target", "baseline")
        if any(kind not in commits for kind in required):
            raise ReviewError("Retention requires exact head, base, target, and baseline commits.")
        values = {kind: validate_sha(sha) for kind, sha in commits.items()}
        for kind, sha in values.items():
            if run_git(
                self.store, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False
            ).returncode:
                raise ReviewError(f"Cannot retain missing {kind} commit in the owned source store.")
        commands = ["start\n"]
        for kind, sha in values.items():
            ref = self.retention_ref(run_id, kind)
            current = run_git(self.store, ["rev-parse", "--verify", ref], check=False)
            if current.returncode == 0 and current.stdout.strip() != sha:
                raise ReviewError("An owned retention ref already points to a different commit.")
            previous = current.stdout.strip() if current.returncode == 0 else "0" * 40
            commands.append(f"update {ref} {sha} {previous}\n")
        commands.extend(("prepare\n", "commit\n"))
        run_git(self.store, ["update-ref", "--stdin"], input_data="".join(commands).encode("ascii"))
        for kind, sha in values.items():
            if run_git(self.store, ["rev-parse", self.retention_ref(run_id, kind)]).stdout.strip() != sha:
                raise ReviewError("Retention-ref verification failed.")
        return values


    def drop_retention(self, run_id: str, *, expected: dict[str, str]) -> None:
        """Delete only this discarded run's refs, guarded by exact old SHAs."""
        commands = ["start\n"]
        for kind, raw_sha in expected.items():
            sha = validate_sha(raw_sha)
            ref = self.retention_ref(run_id, kind)
            current = run_git(self.store, ["rev-parse", "--verify", ref], check=False)
            if current.returncode:
                continue
            if current.stdout.strip() != sha:
                raise ReviewError("Retention ref moved; refusing to delete a different object owner.")
            commands.append(f"delete {ref} {sha}\n")
        commands.extend(("prepare\n", "commit\n"))
        run_git(self.store, ["update-ref", "--stdin"], input_data="".join(commands).encode("ascii"))


    def _source_for_slot(self, slot: int, *, expected_branch: str | None = None) -> Path:
        number = self._validate_slots([slot])[0]
        outer = safe_path(self.state.root, self.slot_relative_path(number))
        nested = safe_path(
            self.state.root, f"{self.slot_relative_path(number)}/{self.module['path']}"
        )
        ensure_repository(outer)
        if outer not in self.state.registered():
            raise ReviewError("Managed pool slot is not a registered outer worktree.")
        active = current_ref(outer)
        if active is not None and active != expected_branch:
            raise ReviewError("Managed pool slot is on an unexpected branch.")
        ensure_repository(nested)
        common = run_git(
            nested, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
        if Path(common).resolve() != self.store.resolve():
            raise ReviewError("Nested source worktree is not owned by this source pool.")
        if nested.resolve() not in {row["worktree"] for row in worktree_rows(self.store)}:
            raise ReviewError("Nested source worktree is not registered in the owned source store.")
        assert_no_git_operation(nested)
        return nested


    def reset_source(
        self,
        slot: int,
        run_id: str,
        retained: dict[str, str],
        *,
        refresh: bool = True,
        expected_branch: str | None = None,
    ) -> dict:
        """Return a clean pooled source to a verified warm baseline without deinit."""
        source = self._source_for_slot(slot, expected_branch=expected_branch)
        if git_status(source):
            raise ReviewError("Source checkout changed during reset; leaving it untouched.")
        ignored = run_git(
            source, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
        ).stdout
        if ignored:
            raise ReviewError("Source checkout has ignored local data; leaving it untouched.")
        retained_values = self.retain(run_id, retained)
        warning = None
        baseline = None
        if refresh:
            try:
                baseline = self.refresh(persist=False)
            except CleanupNotProvenError:
                raise
            except ReviewError:
                baseline = self.cached_baseline_sha()
                if baseline is None or not self.store.exists() or run_git(
                    self.store, ["cat-file", "-e", f"{baseline}^{{commit}}"], check=False
                ).returncode:
                    raise ReviewError(
                        "Baseline refresh failed and no verified cached baseline is available; "
                        "the pooled reset remains recoverable and blocked."
                    ) from None
                warning = "Baseline refresh failed; reused the verified cached baseline."
        else:
            baseline = self.cached_baseline_sha()
            if baseline is None or run_git(
                self.store, ["cat-file", "-e", f"{baseline}^{{commit}}"], check=False
            ).returncode:
                raise ReviewError("No verified cached warm baseline is available.")
        run_git(source, ["checkout", "--detach", validate_sha(baseline)])
        if git_status(source) or current_ref(source) is not None:
            raise ReviewError("Pooled source did not return to a clean detached baseline.")
        if run_git(source, ["rev-parse", "HEAD"]).stdout.strip() != baseline:
            raise ReviewError("Pooled source baseline verification failed.")
        self.publish_baseline(baseline)
        return {
            "baseline_sha": baseline,
            "warning": warning,
            "retained": retained_values,
        }


    @contextmanager
    def store_lock(self):
        """Acquire after ReviewState.lock(); every mutating caller uses this order."""
        lock_dir = self.state.common / "review-setup" / "source-locks"
        if lock_dir.is_symlink():
            raise ReviewError("Refusing symlink source-lock directory.")
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = lock_dir / f"{self.repo_key}.lock"
        if path.is_symlink():
            raise ReviewError("Refusing symlink source-store lock.")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReviewError("Another operation owns this source store.") from None
            yield
        finally:
            os.close(fd)


    def _retained_commits(self) -> list[str]:
        if not self.store.exists():
            return []
        prefix = f"refs/review-setup/retain/{self.repo_key}/"
        output = run_git(
            self.store, ["for-each-ref", "--format=%(objectname)", prefix]
        ).stdout.splitlines()
        return [validate_sha(value) for value in output if value]


    def maintain(self, *, now: str | None = None, reserve_bytes: int = 5 * 1024**3) -> dict:
        """Run safe foreground maintenance on an owned idle store at most daily."""
        if not self.store.exists() or self.store.is_symlink():
            return {"status": "skipped-absent"}
        timestamp = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ReviewError("Maintenance time must include a timezone.")
        value = self._read_state()
        last_raw = value.get("maintenance_at")
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
            except ValueError:
                raise ReviewError("Pool maintenance timestamp is invalid.") from None
            if last.tzinfo is None:
                raise ReviewError("Pool maintenance timestamp must include a timezone.")
            if (timestamp - last).total_seconds() < 24 * 60 * 60:
                return {"status": "skipped-cadence", "maintenance_at": last_raw}
        for path in (self.store, self.state.root):
            if shutil.disk_usage(path).free < reserve_bytes:
                return {"status": "skipped-headroom", "path": str(path)}
        retained = self._retained_commits()
        with self.store_lock():
            # Revalidate cadence and headroom under the per-store lock. The
            # caller already owns ReviewState.lock(), establishing the fixed
            # global-then-store lock order used by every maintenance path.
            locked_value = self._read_state()
            locked_last_raw = locked_value.get("maintenance_at")
            if locked_last_raw:
                try:
                    locked_last = datetime.fromisoformat(locked_last_raw)
                except ValueError:
                    raise ReviewError("Pool maintenance timestamp is invalid.") from None
                if locked_last.tzinfo is None:
                    raise ReviewError("Pool maintenance timestamp must include a timezone.")
                if (timestamp - locked_last).total_seconds() < 24 * 60 * 60:
                    return {
                        "status": "skipped-cadence",
                        "maintenance_at": locked_last_raw,
                    }
            bare = run_git(self.store, ["rev-parse", "--is-bare-repository"], check=False)
            origin = run_git(self.store, ["config", "--get", "remote.origin.url"], check=False)
            if bare.returncode or bare.stdout.strip() != "true" or origin.returncode or origin.stdout.strip() != self.module["url"]:
                return {"status": "skipped-unowned"}
            if any(run.get("repo_key") == self.repo_key and run.get("status") not in ("archived", "discarded")
                   for run in self.state.all_pool_runs()):
                return {"status": "skipped-active-review"}
            checkouts = []
            expected_sources = {
                safe_path(self.state.root, f"{self.slot_relative_path(slot)}/{self.module['path']}"): slot
                for slot in range(self.SLOT_COUNT)
            }
            for row in worktree_rows(self.store):
                path = row["worktree"]
                if path == self.store.resolve():
                    continue
                if path not in expected_sources:
                    return {"status": "skipped-unowned-checkout"}
                number = expected_sources[path]
                source = self._source_for_slot(number)
                outer = safe_path(self.state.root, self.slot_relative_path(number))
                if current_ref(source) is not None or git_status(source) or run_git(source, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]).stdout:
                    return {"status": "skipped-busy-checkout"}
                if any(relative != self.module["path"] or code[0] != " " for code, relative in git_status(outer)):
                    return {"status": "skipped-busy-checkout"}
                assert_no_git_operation(outer)
                checkouts.extend((outer, source))
            objects = self.store / "objects"
            temporary_bytes = sum(path.stat().st_size for path in objects.rglob("*") if path.is_file())
            for path in (self.store, self.state.root, *checkouts):
                if shutil.disk_usage(path).free < reserve_bytes + temporary_bytes:
                    return {"status": "skipped-headroom", "path": str(path)}
            retained = self._retained_commits()
            baseline = self.cached_baseline_sha()
            if baseline is None:
                return {"status": "skipped-unverified-baseline"}
            retained.append(baseline)
            for sha in retained:
                run_git(self.store, ["cat-file", "-e", f"{sha}^{{commit}}"])
            # Deliberately avoid `git gc`: it can expire reflogs and prune
            # worktree metadata as implicit side effects. Repack reachable
            # objects, then prune only loose objects older than the normal
            # two-week grace period.
            run_git(self.store, ["repack", "-d", "-l"])
            run_git(self.store, ["prune", "--expire=2.weeks.ago"])
            for sha in retained:
                if run_git(
                    self.store, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False
                ).returncode:
                    raise ReviewError("Maintenance could not verify a retained source commit.")
            completed = timestamp.astimezone(timezone.utc).isoformat()
            self._write_state({"maintenance_at": completed})
        return {"status": "completed", "maintenance_at": completed}

