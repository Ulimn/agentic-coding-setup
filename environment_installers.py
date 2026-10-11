"""Ubuntu package bundles, rootless Docker, and user-local Node tools."""

import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile

from tool_installers import InstallerError, ToolPlan, checksum_for, fetch_bytes, find_executable, local_bin, request, verify_executable


DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin", "docker-ce-rootless-extras", "uidmap", "dbus-user-session", "apparmor")
DOCKER_CONFLICTS = ("docker.io", "docker-compose", "docker-compose-v2", "docker-doc", "docker-buildx", "podman-docker", "containerd", "runc")
DOCKER_SOURCE = "https://download.docker.com/linux/ubuntu"
DOCKER_KEY = Path("/etc/apt/keyrings/docker.asc")
DOCKER_REPOSITORY = Path("/etc/apt/sources.list.d/docker.sources")
NODE_COMMANDS = ("node", "npm", "pnpm", "yarn", "jest", "playwright")
NPM_PACKAGES = {"pnpm": "pnpm", "jest": "jest", "playwright": "@playwright/test"}

APT_BUNDLES = {
    "Base dev tools": ("build-essential", "tmux", "git", "neovim", "htop", "btop",
                       "python3", "python3-dev", "python3-pip", "python3-venv"),
    "Java Tools": ("default-jdk", "maven", "gradle"),
}
VIM_ALIAS = "alias vim='nvim'"


def inspect_bundle_packages(packages: tuple[str, ...]) -> set[str]:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n", *packages],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallerError(f"Could not inspect Ubuntu packages: {error}") from None
    if result.returncode not in (0, 1):
        raise InstallerError(f"Could not inspect Ubuntu packages: {result.stderr.strip()}")
    return {line.split("\t")[0].split(":")[0] for line in result.stdout.splitlines()
            if line.endswith("\tinstalled")}


def vim_alias_present() -> bool:
    path = Path.home() / ".bash_aliases"
    if not os.path.lexists(path):
        return False
    if path.is_symlink() or not path.is_file():
        raise InstallerError(f"{path} is not a regular file; review it before adding the vim alias.")
    contents = path.read_text(encoding="utf-8")
    aliases = re.findall(r"^\s*alias\s+vim\s*=.*$", contents, re.MULTILINE)
    if any(line.strip() != VIM_ALIAS for line in aliases):
        raise InstallerError(f"{path} already defines a different vim alias; review it manually before rerunning.")
    return bool(aliases)


def prepare_apt_bundle(name: str) -> ToolPlan:
    for command in ("apt-get", "dpkg-query"):
        if shutil.which(command) is None:
            raise InstallerError(f"{command} is required for {name} on Ubuntu.")
    packages = APT_BUNDLES[name]
    installed = inspect_bundle_packages(packages)
    missing = tuple(package for package in packages if package not in installed)
    alias_ready = name != "Base dev tools" or vim_alias_present()
    if missing and shutil.which("sudo") is None:
        raise InstallerError(f"{name} requires sudo to install Ubuntu packages.")
    return ToolPlan(name, "", "configured Ubuntu APT repositories", "apt",
                    "source ~/.bash_aliases (Bash vim alias)" if name == "Base dev tools"
                    else "java -version; javac -version; mvn --version; gradle --version",
                    packages=missing, existing="Ubuntu packages" if not missing and alias_ready else None)


def install_apt_bundle(plan: ToolPlan) -> None:
    packages = APT_BUNDLES[plan.name]
    if plan.name == "Base dev tools":
        vim_alias_present()  # Refuse conflicts before installing packages.
    installed = inspect_bundle_packages(packages)
    missing = tuple(package for package in packages if package not in installed)
    if set(missing) - set(plan.packages):
        raise InstallerError(f"{plan.name} packages changed since planning. Rerun to prepare a new plan.")
    if missing:
        print(f"Installing {plan.name} system packages (sudo): {', '.join(missing)}")
        run_checked(["sudo", "-v"])
        run_checked(["sudo", "apt-get", "update"])
        run_checked(["sudo", "apt-get", "install", "-y", *missing])
    if not set(packages) <= inspect_bundle_packages(packages):
        raise InstallerError(f"{plan.name} package verification failed. Check APT output and rerun.")
    if plan.name == "Base dev tools" and not vim_alias_present():
        # Append rather than rewriting unrelated user shell configuration.
        with (Path.home() / ".bash_aliases").open("a", encoding="utf-8") as aliases:
            aliases.write(f"\n# Agentic Coding VM Setup: Neovim shortcut\n{VIM_ALIAS}\n")
        if not vim_alias_present():
            raise InstallerError("The Bash vim alias could not be verified.")
        print("Added Bash alias: vim -> nvim. Run: source ~/.bash_aliases")
    action = "Installed and verified" if missing else "Skipped installation; verified"
    print(f"{action}: {plan.name} Ubuntu packages.")


def run_checked(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, check=False, **kwargs)
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
        check=False,
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


def docker_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def rootless_prerequisites() -> None:
    if os.getuid() == 0:
        raise InstallerError("Rootless Docker must be configured as a normal user, not root.")
    try:
        docker_group = grp.getgrnam("docker")
        docker_gid = docker_group.gr_gid
    except KeyError:
        docker_gid = None
    if docker_gid is not None and (docker_gid in {*os.getgroups(), os.getgid()} or docker_user() in docker_group.gr_mem):
        raise InstallerError("This session has Docker group access (VM root privileges). Remove that membership manually and log out/in before configuring rootless Docker.")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise InstallerError("Unset DOCKER_HOST and DOCKER_CONTEXT before setup so the rootless Docker context can be selected and verified.")
    user = docker_user()
    for filename in ("/etc/subuid", "/etc/subgid"):
        try:
            entries = Path(filename).read_text(encoding="utf-8").splitlines()
            valid = any(len(parts := line.split(":")) == 3
                        and parts[0] in (user, str(os.getuid()))
                        and parts[1].isdigit() and parts[2].isdigit()
                        and int(parts[2]) >= 65536 for line in entries)
        except OSError:
            valid = False
        if not valid:
            raise InstallerError(f"Rootless Docker requires a subordinate ID range of at least 65536 in {filename} for {user}. Ask your VM administrator to allocate a non-overlapping range, then rerun.")
    if not Path("/run/systemd/system").is_dir():
        raise InstallerError("Rootless Docker requires a VM running systemd, not the disposable test container.")
    run_checked(["systemctl", "--user", "show-environment"], capture_output=True, text=True, timeout=30)


def inspect_docker_command(command: list[str]) -> subprocess.CompletedProcess:
    # Missing contexts and inactive services use nonzero exit codes normally.
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallerError(f"Could not inspect Docker state: {error}. Resolve the error and rerun.") from None


def rootful_active() -> bool:
    for unit in ("docker.service", "docker.socket"):
        result = inspect_docker_command(["systemctl", "is-active", unit])
        if result.returncode == 0 or result.stdout.strip() in ("activating", "deactivating", "reloading"):
            return True
        if result.returncode not in (3, 4):
            raise InstallerError(f"Could not inspect {unit}. Check systemctl before rerunning.")
    return False


def require_rootful_stopped() -> None:
    if rootful_active():
        raise InstallerError("A system Docker service/socket is running. Stop any containers you want to preserve, then run: sudo systemctl disable --now docker.service docker.socket. Rerun setup afterward; existing rootful images/volumes are not migrated or deleted.")


def rootless_ready() -> bool:
    context = inspect_docker_command(["/usr/bin/docker", "context", "inspect", "rootless", "--format", "{{json .Endpoints.docker.Host}}"])
    if context.returncode != 0:
        return False
    try:
        endpoint = json.loads(context.stdout)
    except ValueError:
        raise InstallerError("The existing rootless Docker context is invalid; review it manually.") from None
    if endpoint != f"unix:///run/user/{os.getuid()}/docker.sock":
        raise InstallerError("The existing rootless Docker context points somewhere else; it will not be overwritten. Review it manually before rerunning.")
    result = inspect_docker_command(["/usr/bin/docker", "--context", "rootless", "info", "--format", "{{json .SecurityOptions}}"])
    if result.returncode != 0:
        return False
    try:
        options = json.loads(result.stdout)
    except ValueError:
        return False
    return isinstance(options, list) and "name=rootless" in options


def rootless_boot_ready() -> bool:
    user_service = inspect_docker_command(["systemctl", "--user", "is-enabled", "docker.service"])
    linger = run_checked(["loginctl", "show-user", docker_user(), "--property=Linger", "--value"], capture_output=True, text=True, timeout=30)
    for unit in ("docker.service", "docker.socket"):
        system_service = inspect_docker_command(["systemctl", "is-enabled", unit])
        if system_service.stdout.strip() not in ("disabled", "masked", "not-found"):
            return False
    return user_service.returncode == 0 and user_service.stdout.strip() == "enabled" and linger.stdout.strip() == "yes"


def prepare_docker(arch: str) -> ToolPlan:
    for program in ("dpkg-query", "apt-get", "systemctl", "loginctl"):
        if shutil.which(program) is None:
            raise InstallerError(f"{program} is required to install rootless Docker in an Ubuntu VM.")
    installed = installed_packages()
    conflicts = installed.intersection(DOCKER_CONFLICTS)
    if conflicts:
        raise InstallerError(f"Conflicting Docker packages: {', '.join(sorted(conflicts))}. Review/remove them manually using Docker's Ubuntu instructions; setup will not remove packages.")
    rootless_prerequisites()
    require_rootful_stopped()
    login = "docker info; docker compose version (rootless; no sudo needed)"
    if set(DOCKER_PACKAGES) <= installed:
        docker_versions()
        if rootless_ready() and rootless_boot_ready():
            return ToolPlan("Docker with Compose", "docker", DOCKER_SOURCE, "docker", login, existing="rootless Docker context")
    if shutil.which("sudo") is None:
        raise InstallerError("Rootless Docker setup requires sudo for packages and boot persistence; run as a normal user with sudo access.")
    release = platform.freedesktop_os_release()
    codename = release.get("UBUNTU_CODENAME") or release.get("VERSION_CODENAME", "")
    if codename not in ("jammy", "noble", "resolute"):
        raise InstallerError("Docker's official repository is supported here on Ubuntu 22.04, 24.04 and 26.04 only.")
    packages = tuple(package for package in DOCKER_PACKAGES if package not in installed)
    key, repository = b"", ""
    if packages:
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
                    script=key, packages=packages, repository=repository)


def install_docker(plan: ToolPlan) -> None:
    rootless_prerequisites()
    require_rootful_stopped()
    if plan.existing:
        docker_versions()
        if not rootless_ready() or not rootless_boot_ready():
            raise InstallerError("Rootless Docker changed since planning. Rerun setup to repair it.")
        run_checked(["/usr/bin/docker", "context", "use", "rootless"])
        print("Skipped: rootless Docker, Buildx and Compose are verified; selected the rootless context.")
        return
    conflicts = installed_packages().intersection(DOCKER_CONFLICTS)
    if conflicts:
        raise InstallerError(f"Conflicting Docker packages appeared: {', '.join(sorted(conflicts))}; nothing was removed.")
    print("Configuring rootless Docker; sudo is used only for packages, disabling system Docker and boot persistence.")
    run_checked(["sudo", "-v"])
    if plan.packages:
        check_repository_file(DOCKER_KEY, plan.script)
        check_repository_file(DOCKER_REPOSITORY, plan.repository.encode())
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
    # Packages can automatically start system Docker. Disable it before rootless
    # setup; pre-existing running daemons were refused before any installation.
    run_checked(["sudo", "systemctl", "disable", "--now", "docker.service", "docker.socket"])
    require_rootful_stopped()
    run_checked(["/usr/bin/dockerd-rootless-setuptool.sh", "check"])
    run_checked(["/usr/bin/dockerd-rootless-setuptool.sh", "install"])
    run_checked(["systemctl", "--user", "enable", "--now", "docker.service"])
    run_checked(["sudo", "loginctl", "enable-linger", docker_user()])
    run_checked(["/usr/bin/docker", "context", "use", "rootless"])
    docker_versions()
    if not rootless_ready() or not rootless_boot_ready():
        raise InstallerError("Docker rootless mode or boot persistence could not be verified. Check systemctl --user status docker and rerun.")
    print("Installed and verified: rootless Docker, Buildx and Compose. Use docker and docker compose without sudo.")


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


def install_node_commands(plan: ToolPlan) -> None:
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
    print("Installed and verified: Node.js, npm, pnpm, Yarn, Jest and Playwright.")



def install_node(plan: ToolPlan) -> None:
    install_node_commands(plan)
    if not plan.playwright_chromium:
        return
    playwright = find_executable("playwright")
    if playwright is None:
        raise InstallerError("Playwright disappeared before Chromium installation. Rerun setup.")
    environment = node_environment()
    environment.pop("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", None)
    print("Installing Playwright Chromium for the current user and its Ubuntu libraries (sudo).")
    run_checked([playwright, "install", "--with-deps", "chromium"], env=environment)
    run_checked([playwright, "install-deps", "--dry-run", "chromium"],
                env=environment, capture_output=True, text=True, timeout=120)
    print("Installed: Playwright Chromium. Verified: Chromium system dependencies.")
