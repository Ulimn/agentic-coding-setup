"""Tool installers for Ubuntu VMs; account authentication remains manual."""

from dataclasses import dataclass, field
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zlib
from urllib.error import URLError
from urllib.request import Request, urlopen


class InstallerError(Exception):
    """A tool could not be prepared, installed, or verified."""


@dataclass(frozen=True)
class ToolPlan:
    name: str
    executable: str
    source: str
    method: str
    login: str
    existing: str | None = None
    script: bytes = field(default=b"", repr=False)
    archive_url: str = ""
    checksum: str | None = None
    packages: tuple[str, ...] = ()
    repository: str = ""


NATIVE_INSTALLERS = {
    "Codex": ("codex", "https://chatgpt.com/codex/install.sh", "sh", "codex (choose Sign in with Device Code)"),
    "Claude Code": ("claude", "https://claude.ai/install.sh", "bash", "claude"),
    "Antigravity-CLI": ("agy", "https://antigravity.google/cli/install.sh", "bash", "agy"),
}
RELEASE_INSTALLERS = {
    "Visual Studio Code Server": (
        "code", "https://update.code.visualstudio.com/api/update/cli-linux-{arch}/stable/latest",
        "code tunnel --disable-telemetry (accept the license and sign in when prompted)",
    ),
    "GitHub CLI": ("gh", "https://api.github.com/repos/cli/cli/releases/latest", "gh auth login"),
    "GitLab CLI": (
        "glab", "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases/permalink/latest",
        "glab auth login",
    ),
    "Forgejo CLI": (
        "fj", "https://codeberg.org/api/v1/repos/forgejo-contrib/forgejo-cli/releases/latest",
        "fj auth login",
    ),
}


def local_bin() -> Path:
    return Path.home() / ".local" / "bin"


def find_executable(name: str) -> str | None:
    existing = shutil.which(name)
    if existing:
        return existing
    candidate = local_bin() / name
    if os.path.lexists(candidate):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise InstallerError(f"{candidate} exists but is not executable. Resolve it before rerunning.")
        return str(candidate)
    return None


def request(url: str):
    if not url.startswith("https://"):
        raise InstallerError("Installer downloads must use HTTPS.")
    try:
        response = urlopen(Request(url, headers={"User-Agent": "agentic-coding-vm-setup"}), timeout=30)
        if not response.geturl().startswith("https://"):
            response.close()
            raise InstallerError("Installer download redirected to an insecure URL.")
        return response
    except (URLError, OSError) as error:
        raise InstallerError(f"Could not download {url}: {error}. Check connectivity and rerun.") from None


def fetch_bytes(url: str) -> bytes:
    limit = 2 * 1024 * 1024
    with request(url) as response:
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        data = response.read(limit + 1)
    if len(data) > limit:
        raise InstallerError(f"Unexpectedly large installer metadata: {url}")
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as decoded:
                data = decoded.read(limit + 1)
        except (OSError, EOFError, zlib.error):
            raise InstallerError(f"Invalid gzip installer metadata: {url}") from None
    elif encoding != "identity":
        raise InstallerError(f"Unsupported HTTP content encoding {encoding!r}: {url}")
    if len(data) > limit:
        raise InstallerError(f"Unexpectedly large installer metadata: {url}")
    return data


def ubuntu_arch() -> str:
    if platform.system() != "Linux":
        raise InstallerError("Tool installation currently supports Ubuntu 22.04+ only.")
    values = dict(
        line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    distro = values.get("ID", "").strip('"')
    version = values.get("VERSION_ID", "").strip('"')
    if distro != "ubuntu" or not re.fullmatch(r"\d+\.\d+", version) or float(version) < 22.04:
        raise InstallerError("Tool installation currently supports Ubuntu 22.04+ only.")
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if architecture is None:
        raise InstallerError("Tool installation requires an x86_64 or aarch64 Ubuntu VM.")
    return architecture


def verify_executable(executable: str, *, env: dict[str, str] | None = None) -> str:
    version_argument = "version" if Path(executable).name == "fj" else "--version"
    try:
        result = subprocess.run([executable, version_argument], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallerError(f"Could not verify {executable}: {error}") from None
    lines = result.stdout.strip().splitlines()
    # fj can print a keys-file notice before its version, even without login.
    version = next((line for line in lines if line.startswith("fj v")), "") if version_argument == "version" else (lines[0] if lines else "")
    if result.returncode != 0 or not version:
        detail = result.stderr.strip() or "No version was reported."
        raise InstallerError(f"{executable} failed its version check: {detail} Resolve the installation and rerun.")
    return version


def checksum_for(name: str, manifest: str) -> str:
    for line in manifest.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip(" *") == name and re.fullmatch(r"[a-fA-F0-9]{64}", parts[0]):
            return parts[0].lower()
    raise InstallerError(f"No SHA-256 checksum was published for {name}.")


def release_plan(name: str, arch: str) -> ToolPlan:
    executable, source, login = RELEASE_INSTALLERS[name]
    if name == "Visual Studio Code Server":
        source = source.format(arch="x64" if arch == "amd64" else "arm64")
    try:
        release = json.loads(fetch_bytes(source))
        if name == "Visual Studio Code Server":
            checksum = release["sha256hash"]
            if not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
                raise ValueError("invalid download checksum")
            return ToolPlan(name, executable, source, "archive", login,
                            archive_url=release["url"], checksum=checksum.lower())
        version = release["tag_name"].lstrip("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("unexpected release version")
        assets = release["assets"]
        if name == "GitLab CLI":
            assets = assets["links"]
        asset_name = {
            "GitHub CLI": f"gh_{version}_linux_{arch}.tar.gz",
            "GitLab CLI": f"glab_{version}_linux_{arch}.tar.gz",
            "Forgejo CLI": f"forgejo-cli-{'x86_64' if arch == 'amd64' else 'aarch64'}-linux.tar.gz",
        }[name]
        asset = next(item for item in assets if item["name"] == asset_name)
        asset_url = asset.get("browser_download_url") or asset.get("direct_asset_url") or asset["url"]
        checksum = None
        if name != "Forgejo CLI":
            checksum_name = f"gh_{version}_checksums.txt" if name == "GitHub CLI" else "checksums.txt"
            manifest = next(item for item in assets if item["name"] == checksum_name)
            manifest_url = manifest.get("browser_download_url") or manifest.get("direct_asset_url") or manifest["url"]
            checksum = checksum_for(asset_name, fetch_bytes(manifest_url).decode())
        return ToolPlan(name, executable, source, "archive", login, archive_url=asset_url, checksum=checksum)
    except (KeyError, ValueError, StopIteration, TypeError) as error:
        raise InstallerError(f"Could not resolve {name}'s official release: {error}") from None


def prepare_tools(names: tuple[str, ...]) -> tuple[ToolPlan, ...]:
    if not names:
        return ()
    arch = ubuntu_arch()
    plans = []
    for name in names:
        print(f"Checking: {name}")
        if name in ("Docker with Compose", "Node Tools"):
            from environment_installers import prepare_docker, prepare_node
            plans.append(prepare_docker(arch) if name == "Docker with Compose" else prepare_node(arch))
            continue
        if name not in NATIVE_INSTALLERS and name not in RELEASE_INSTALLERS:
            raise InstallerError(f"{name} needs its installation source configured before it can be installed.")
        if name in NATIVE_INSTALLERS:
            executable, source, shell, login = NATIVE_INSTALLERS[name]
        else:
            executable, source, login = RELEASE_INSTALLERS[name]
            shell = "archive"
        existing = find_executable(executable)
        if existing:
            verify_executable(existing)
            plans.append(ToolPlan(name, executable, source, shell, login, existing=existing))
        elif name in NATIVE_INSTALLERS:
            for program in (shell, "curl", "tar", "sha256sum", "sha512sum"):
                if shutil.which(program) is None:
                    raise InstallerError(f"{program} is required. On Ubuntu, run: sudo apt install curl tar coreutils bash")
            script = fetch_bytes(source)
            if not script.startswith(b"#!/"):
                raise InstallerError(f"{source} did not return an installer script.")
            plans.append(ToolPlan(name, executable, source, shell, login, script=script))
        else:
            plans.append(release_plan(name, arch))
    return tuple(plans)


def describe_tool(plan: ToolPlan) -> str:
    if plan.existing:
        return f"  Keep {plan.name}: verified existing installation at {plan.existing}"
    if plan.method == "docker":
        return f"  Configure rootless Docker, Buildx and Compose from {plan.source} (sudo for packages; disable system Docker; enable/start user Docker service and boot persistence; Docker group unchanged; use Docker without sudo)."
    if plan.method == "node":
        return f"  Complete Node Tools: {', '.join(plan.packages)} (user-local; Node.js from nodejs.org, packages from npm registry; Yarn via Corepack; browser downloads remain manual)."
    if plan.method == "archive":
        return f"  Install {plan.name} into {local_bin()} from {plan.archive_url}"
    return f"  Install {plan.name} using its official native installer: {plan.source} (user-local)"


def install_archive(plan: ToolPlan, directory: Path) -> None:
    archive = directory / "download.tar.gz"
    with request(plan.archive_url) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    if plan.checksum:
        digest = hashlib.sha256()
        with archive.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != plan.checksum:
            raise InstallerError(f"{plan.name} checksum verification failed; nothing was installed.")
    executable = directory / plan.executable
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            candidates = [item for item in bundle.getmembers() if PurePosixPath(item.name).name == plan.executable]
            if len(candidates) != 1 or not candidates[0].isfile():
                raise InstallerError(f"{plan.name} archive did not contain exactly one regular executable.")
            with bundle.extractfile(candidates[0]) as source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
    except tarfile.TarError as error:
        raise InstallerError(f"Could not read {plan.name} archive: {error}") from None
    executable.chmod(0o755)
    verify_executable(str(executable))
    local_bin().mkdir(parents=True, exist_ok=True)
    # Exclusive publication preserves files created during download as well.
    try:
        os.link(executable, local_bin() / plan.executable)
    except FileExistsError:
        raise InstallerError(f"{local_bin() / plan.executable} appeared during installation; it was not overwritten.") from None


def install_tools(plans: tuple[ToolPlan, ...]) -> None:
    for plan in plans:
        if plan.method in ("docker", "node"):
            from environment_installers import install_docker, install_node
            (install_docker if plan.method == "docker" else install_node)(plan)
            continue
        path = plan.existing or find_executable(plan.executable)
        if path:
            version = verify_executable(path)
            print(f"Skipped: {plan.name} is already installed ({version}).")
        else:
            print(f"Installing: {plan.name}")
            local_bin().mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="vm-setup-", dir=local_bin()) as directory:
                directory = Path(directory)
                if plan.method == "archive":
                    install_archive(plan, directory)
                else:
                    script = directory / "install.sh"
                    script.write_bytes(plan.script)
                    environment = os.environ.copy()
                    environment["PATH"] = f"{local_bin()}{os.pathsep}{environment.get('PATH', '')}"
                    if plan.name == "Codex":
                        environment["CODEX_NON_INTERACTIVE"] = "1"
                    command = [plan.method, str(script)]
                    if plan.name == "Claude Code":
                        command.append("stable")
                    result = subprocess.run(command, env=environment)
                    if result.returncode != 0:
                        raise InstallerError(f"{plan.name} installer failed (exit {result.returncode}). Check its output and rerun.")
            path = find_executable(plan.executable)
            if path is None:
                raise InstallerError(f"{plan.name} installer finished but {plan.executable} was not found.")
            print(f"Installed and verified: {plan.name} ({verify_executable(path)}).")
    if plans:
        print(f"\nFor commands in your current shell, run: export PATH={shlex.quote(str(local_bin()))}:\"$PATH\"")
        print("Manual account login and next steps:")
        for plan in plans:
            print(f"  {plan.name}: {plan.login}")
        if any(plan.name == "Codex" for plan in plans):
            heading = "Codex login on this VM"
            if sys.stdout.isatty():
                heading = f"\033[1;31m{heading}\033[0m"
            print(f"\n{heading}")
            print("Enable device code login in ChatGPT security settings (or workspace permissions).")
            print("Then run codex and choose Sign in with Device Code in the initial login menu.")
            print("Open the printed link in your browser and enter the one-time code; no localhost callback is needed.")
            print("If device code login is unavailable, see README.md for SSH callback port forwarding.")
