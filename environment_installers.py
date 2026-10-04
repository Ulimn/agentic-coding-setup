"""Docker's signed Ubuntu repository and a user-local Node tool bundle."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile

from tool_installers import InstallerError, ToolPlan, checksum_for, fetch_bytes, find_executable, local_bin, request, verify_executable


DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin")
DOCKER_CONFLICTS = ("docker.io", "docker-compose", "docker-compose-v2", "docker-doc", "docker-buildx", "podman-docker", "containerd", "runc")
DOCKER_SOURCE = "https://download.docker.com/linux/ubuntu"
DOCKER_KEY = Path("/etc/apt/keyrings/docker.asc")
DOCKER_REPOSITORY = Path("/etc/apt/sources.list.d/docker.sources")
NODE_COMMANDS = ("node", "npm", "pnpm", "yarn", "jest", "playwright")
NPM_PACKAGES = {"pnpm": "pnpm", "jest": "jest", "playwright": "@playwright/test"}


def run_checked(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallerError(f"Could not run {' '.join(command)}: {error}") from None
    if result.returncode != 0:
        detail = (getattr(result, "stderr", None) or "").strip()
        raise InstallerError(f"{' '.join(command)} failed (exit {result.returncode}). {detail} Resolve the error and rerun.")
    return result


def installed_packages() -> set[str]:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n", *DOCKER_PACKAGES, *DOCKER_CONFLICTS],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise InstallerError(f"Could not inspect Docker packages: {result.stderr.strip()}")
    return {line.split("\t")[0].split(":")[0] for line in result.stdout.splitlines() if line.endswith("\tinstalled")}


def docker_versions() -> None:
    verify_executable("/usr/bin/docker")
    for plugin in ("compose", "buildx"):
        result = run_checked(["/usr/bin/docker", plugin, "version"], capture_output=True, text=True, timeout=30)
        if not result.stdout.strip():
            raise InstallerError(f"Docker {plugin} did not report a version.")


def check_repository_file(path: Path, expected: bytes) -> None:
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file() or path.read_bytes() != expected):
        raise InstallerError(f"{path} already exists with different contents; it will not be overwritten. Review Docker's official repository setup before rerunning.")


def prepare_docker(arch: str) -> ToolPlan:
    for program in ("dpkg-query", "apt-get"):
        if shutil.which(program) is None:
            raise InstallerError(f"{program} is required to install Docker from its Ubuntu repository.")
    installed = installed_packages()
    login = "sudo docker info; sudo docker compose version (Docker group membership is unchanged)"
    if set(DOCKER_PACKAGES) <= installed:
        docker_versions()
        return ToolPlan("Docker with Compose", "docker", DOCKER_SOURCE, "docker", login, existing="/usr/bin/docker")
    conflicts = installed.intersection(DOCKER_CONFLICTS)
    if conflicts:
        raise InstallerError(f"Conflicting Docker packages: {', '.join(sorted(conflicts))}. Review/remove them manually using Docker's Ubuntu instructions; setup will not remove packages.")
    if shutil.which("sudo") is None:
        raise InstallerError("Docker installation requires sudo. Run setup as a normal user with sudo access in your VM.")
    if not Path("/run/systemd/system").is_dir():
        raise InstallerError("Docker Engine installation requires a VM running systemd. The disposable test container supports Node Tools, but cannot run this Docker service setup.")
    codename = platform.freedesktop_os_release().get("UBUNTU_CODENAME") or platform.freedesktop_os_release().get("VERSION_CODENAME", "")
    if codename not in ("jammy", "noble", "resolute"):
        raise InstallerError("Docker's official repository is supported here on Ubuntu 22.04, 24.04 and 26.04 only.")
    legacy = DOCKER_REPOSITORY.with_name("docker.list")
    if legacy.exists():
        raise InstallerError(f"Existing {legacy}: review the Docker repository configuration manually before adding docker.sources.")
    key = fetch_bytes(f"{DOCKER_SOURCE}/gpg")
    if not key.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        raise InstallerError("Docker's official repository key did not return a public PGP key.")
    repository = (
        f"Types: deb\nURIs: {DOCKER_SOURCE}\nSuites: {codename}\nComponents: stable\n"
        f"Architectures: {arch}\nSigned-By: {DOCKER_KEY}\n"
    )
    check_repository_file(DOCKER_KEY, key)
    check_repository_file(DOCKER_REPOSITORY, repository.encode())
    return ToolPlan("Docker with Compose", "docker", DOCKER_SOURCE, "docker", login,
                    script=key, packages=tuple(package for package in DOCKER_PACKAGES if package not in installed), repository=repository)


def install_docker(plan: ToolPlan) -> None:
    if plan.existing:
        docker_versions()
        print("Skipped: Docker Engine, Buildx and Compose are already installed and verified.")
        return
    # Recheck before privileged changes; don't remove conflicting packages.
    conflicts = installed_packages().intersection(DOCKER_CONFLICTS)
    if conflicts:
        raise InstallerError(f"Conflicting Docker packages appeared: {', '.join(sorted(conflicts))}; nothing was removed.")
    check_repository_file(DOCKER_KEY, plan.script)
    check_repository_file(DOCKER_REPOSITORY, plan.repository.encode())
    print("Installing Docker Engine, Buildx and Compose system-wide using sudo.")
    run_checked(["sudo", "-v"])
    with tempfile.TemporaryDirectory(prefix="vm-setup-docker-") as temporary:
        for destination, contents in ((DOCKER_KEY, plan.script), (DOCKER_REPOSITORY, plan.repository.encode())):
            source = Path(temporary) / destination.name
            source.write_bytes(contents)
            source.chmod(0o644)
            run_checked(["sudo", "install", "-m", "0755", "-d", str(destination.parent)])
            run_checked(["sudo", "cp", "--no-clobber", "--", str(source), str(destination)])
            check_repository_file(destination, contents)
    run_checked(["sudo", "apt-get", "update"])
    run_checked(["sudo", "apt-get", "install", "-y", *plan.packages])
    run_checked(["sudo", "systemctl", "enable", "--now", "docker"])
    docker_versions()
    run_checked(["sudo", "/usr/bin/docker", "info"], capture_output=True, text=True, timeout=30)
    print("Installed and verified: Docker Engine, Buildx and Compose. Use sudo docker; Docker group membership was not changed.")


def node_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{local_bin()}{os.pathsep}{environment.get('PATH', '')}"
    environment["COREPACK_ENABLE_PROJECT_SPEC"] = "0"
    environment["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    environment["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    return environment


def missing_node_commands() -> tuple[str, ...]:
    missing = []
    for command in NODE_COMMANDS:
        existing = find_executable(command)
        if existing:
            version = verify_executable(existing, env=node_environment())
            if command == "node":
                match = re.match(r"v(\d+)\.", version)
                if not match or int(match[1]) < 22:
                    raise InstallerError("Existing Node.js must be version 22 or newer for Node Tools. Upgrade it manually; setup will not overwrite it.")
        else:
            missing.append(command)
    return tuple(missing)


def prepare_node(arch: str) -> ToolPlan:
    missing = missing_node_commands()
    login = "playwright install --with-deps (optional: browsers and system dependencies; may require sudo)"
    source = "https://nodejs.org/dist/index.json"
    if not missing:
        return ToolPlan("Node Tools", "node", source, "node", login, existing=find_executable("node"))
    plan = ToolPlan("Node Tools", "node", source, "node", login, packages=missing)
    if "node" not in missing and "npm" not in missing:
        return plan
    try:
        releases = json.loads(fetch_bytes(source))
        latest = max((entry for entry in releases if entry.get("lts")), key=lambda entry: tuple(int(part) for part in entry["version"].lstrip("v").split(".")))
        version = latest["version"]
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise ValueError("unexpected Node.js version")
        filename = f"node-{version}-linux-{'x64' if arch == 'amd64' else 'arm64'}.tar.gz"
        archive_url = f"https://nodejs.org/dist/{version}/{filename}"
        checksum = checksum_for(filename, fetch_bytes(f"https://nodejs.org/dist/{version}/SHASUMS256.txt").decode())
        return ToolPlan("Node Tools", "node", source, "node", login, packages=missing, archive_url=archive_url, checksum=checksum)
    except (KeyError, ValueError, TypeError) as error:
        raise InstallerError(f"Could not resolve the official Node.js LTS release: {error}") from None


def unpack_node(archive: Path, destination: Path) -> None:
    # Copy only Node and bundled npm regular files. Archive symlinks are never
    # followed; npm launchers are explicitly created from known regular files.
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            parts = PurePosixPath(member.name).parts
            if ".." in parts or member.name.startswith("/"):
                raise InstallerError("Unsafe path in Node.js archive; nothing was published.")
            if len(parts) == 1 and member.isdir():
                continue
            if len(parts) < 2:
                raise InstallerError("Unexpected root file in Node.js archive.")
            relative = PurePosixPath(*parts[1:])
            selected = str(relative) == "bin/node" or str(relative).startswith("lib/node_modules/npm/")
            if not selected or member.isdir():
                continue
            if not member.isfile():
                raise InstallerError("Unexpected link or special file in Node.js/npm archive.")
            target = destination / str(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for file in ("bin/node", "lib/node_modules/npm/bin/npm-cli.js", "lib/node_modules/npm/bin/npx-cli.js"):
        if not (destination / file).is_file():
            raise InstallerError(f"Node.js archive is missing {file}.")


def install_node_runtime(plan: ToolPlan, missing: tuple[str, ...]) -> None:
    if not plan.archive_url:
        raise InstallerError("Node.js or npm disappeared since planning. Rerun to prepare its download.")
    root = Path.home() / ".local" / "share" / "vm-setup" / "node"
    root.mkdir(parents=True, exist_ok=True)
    # The owned runtime remains in place because local-bin launchers point to it.
    # On failure before publication, remove only this newly created directory.
    runtime = Path(tempfile.mkdtemp(prefix="runtime-", dir=root))
    published = False
    try:
        archive = runtime / "node.tar.gz"
        with request(plan.archive_url) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as source:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest != plan.checksum:
            raise InstallerError("Node.js checksum verification failed; nothing was published.")
        try:
            unpack_node(archive, runtime)
        except tarfile.TarError as error:
            raise InstallerError(f"Could not read the Node.js archive: {error}") from None
        archive.unlink()
        verify_executable(str(runtime / "bin/node"))
        local_bin().mkdir(parents=True, exist_ok=True)
        for command, target in (("node", "bin/node"), ("npm", "lib/node_modules/npm/bin/npm-cli.js"), ("npx", "lib/node_modules/npm/bin/npx-cli.js")):
            if command not in missing and command != "npx":
                continue
            if find_executable(command):
                continue
            try:
                (local_bin() / command).symlink_to(runtime / target)
                published = True
            except FileExistsError:
                raise InstallerError(f"{command} appeared during installation; it was not overwritten. Rerun to verify it.") from None
    finally:
        if not published:
            shutil.rmtree(runtime)


def install_node(plan: ToolPlan) -> None:
    missing = missing_node_commands()
    if not missing:
        print("Skipped: all six Node Tools commands are already installed and verified.")
        return
    print(f"Installing Node Tools: {', '.join(missing)} (user-local).")
    if "node" in missing or "npm" in missing:
        install_node_runtime(plan, missing)
    environment = node_environment()
    npm = find_executable("npm")
    if npm is None:
        raise InstallerError("npm was not found after installing Node.js.")
    # Explicit prefix and registry avoid root installs and project modifications.
    for command, package in NPM_PACKAGES.items():
        if find_executable(command):
            continue
        run_checked([npm, "install", "--global", "--prefix", str(Path.home() / ".local"), "--registry", "https://registry.npmjs.org", package], env=environment)
    if not find_executable("yarn"):
        corepack = find_executable("corepack")
        if corepack is None:
            prefix = Path.home() / ".local" / "share" / "vm-setup" / "corepack"
            managed = prefix / "bin" / "corepack"
            if not managed.is_file():
                run_checked([npm, "install", "--global", "--prefix", str(prefix), "--registry", "https://registry.npmjs.org", "corepack"], env=environment)
            if managed.is_file():
                corepack = str(managed)
        if corepack is None:
            raise InstallerError("Corepack was not found after installation.")
        verify_executable(corepack, env=environment)
        run_checked([corepack, "enable", "--install-directory", str(local_bin()), "yarn"], env=environment)
        run_checked([corepack, "install", "--global", "yarn@stable"], env=environment)
    if missing_node_commands():
        raise InstallerError("Node Tools verification failed: one or more commands are still missing.")
    print("Installed and verified: Node.js, npm, pnpm, Yarn, Jest and Playwright. Browser downloads remain manual.")
