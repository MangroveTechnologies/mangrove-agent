"""Administrator installation of immutable software custody; never invoked by setup.

Requires existing dedicated custody and restricted agent OS users. Root writes
an installation and service definitions, but deliberately does not start a daemon
or authorize spending. Review the policy and start the service as administrator.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def service_definition(command, custody_user, custody_gid, state, socket_dir, platform):
    if platform == "linux":
        service = "\n".join([
            "[Unit]", "Description=Mangrove isolated wallet custody", "After=local-fs.target", "",
            "[Service]", f"User={custody_user}", f"Group={custody_gid}",
            "ExecStart=" + shlex.join(command), "Environment=PATH=/usr/bin:/bin", "UnsetEnvironment=PYTHONPATH PYTHONHOME LD_PRELOAD LD_LIBRARY_PATH",
            "NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=", "PrivateTmp=yes",
            "ProtectSystem=strict", "ProtectHome=yes", "PrivateDevices=yes", "RestrictSUIDSGID=yes",
            "RestrictAddressFamilies=AF_UNIX", "UMask=0077", "ReadWritePaths=" + shlex.join([str(state), str(socket_dir)]),
            *(["RuntimeDirectory=" + socket_dir.name, "RuntimeDirectoryMode=0755"] if socket_dir.parent == Path("/run") else []),
            "Restart=on-failure", "RestartSec=3", "", "[Install]", "WantedBy=multi-user.target", ""])
        return "mangrove-custody.service", service.encode()
    else:
        import plistlib
        definition = {"Label": "io.mangrove.custody", "UserName": custody_user,
                      "ProgramArguments": command, "RunAtLoad": True,
                      "EnvironmentVariables": {"PATH": "/usr/bin:/bin"}, "Umask": 0o077}
        return "io.mangrove.custody.plist", plistlib.dumps(definition)


def custody_application(example, catalog, state):
    """Pin service routing independently of the private runtime label 'custody'."""
    application = dict(example)
    environment = application.get("X402_MANGROVE_ENVIRONMENT") or catalog["default_environment"]["local"]
    if environment not in catalog["environments"]:
        raise ValueError("No reviewed MangroveAI environment for custody configuration")
    application.update(AUTH_ENABLED=True, API_KEYS="custody-service-no-http",
                       MANGROVE_API_KEY="", DB_PATH=str(state / "wallets.db"),
                       MASTER_KEY_PATH=str(state / "master.key"), KEYRING_SERVICE_NAME="mangrove-isolated-custody",
                       X402_MANGROVE_ENVIRONMENT=environment)
    application.pop("CUSTODY_SOCKET", None)
    application.pop("CUSTODY_UID", None)
    return application


def install(args):
    if os.name != "posix" or sys.platform not in {"linux", "darwin"} or os.geteuid() != 0:
        raise ValueError("A Linux or macOS host administrator must install isolated custody")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    from src.custody.transport import check_no_extended_acl
    custody, agent = pwd.getpwnam(args.custody_user), pwd.getpwnam(args.agent_user)
    os.umask(0o022)
    if custody.pw_uid == agent.pw_uid or min(custody.pw_uid, agent.pw_uid) <= 0:
        raise ValueError("Custody and agent must be distinct non-root OS users")
    privileged_groups = {"sudo", "wheel", "admin", "docker", "lxd", "incus"}
    agent_groups = {grp.getgrgid(agent.pw_gid).gr_name}
    agent_groups.update(group.gr_name for group in grp.getgrall() if agent.pw_name in group.gr_mem)
    if agent_groups & privileged_groups:
        raise ValueError("The agent user has administrative/container privileges; use a restricted agent OS user")
    sudo = shutil.which("sudo")
    if sudo and subprocess.run([sudo, "-l", "-U", agent.pw_name], capture_output=True, check=False).returncode == 0:
        raise ValueError("The agent user has sudo authority; remove it before installing isolated custody")
    destination = Path(args.destination)
    config_home = Path(args.config_home)
    state = Path(args.state_dir)
    socket_dir = Path(args.socket_dir)
    for path in (destination, config_home, state, socket_dir):
        if not path.is_absolute() or path.exists():
            raise ValueError("Installation paths must be new absolute paths: " + str(path))
        for parent in path.parents:
            if parent.exists() and (parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022):
                raise ValueError("Installation parent is not protected by root: " + str(parent))
            if parent.exists():
                check_no_extended_acl(parent)
    source = Path(__file__).resolve().parent.parent
    destination.mkdir(mode=0o755, parents=True)
    for directory in ("server", "scripts"):
        shutil.copytree(source / directory, destination / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*-config.json", "agent-data*"))
    # Example config is public and loaded only to generate the isolated service config.
    application = json.loads((source / "server/src/config/local-example-config.json").read_text())
    catalog = json.loads((source / "server/src/config/mangrove-endpoints.json").read_text())
    subprocess.run([args.python, "-m", "venv", str(destination / ".venv")], check=True)
    python = destination / ".venv/bin/python"
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(destination / "server/requirements.lock")], check=True)
    for path in destination.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
            check_no_extended_acl(path)
    config_home.mkdir(mode=0o755, parents=True)
    (config_home / "config").mkdir(mode=0o755)
    state.mkdir(mode=0o700, parents=True)
    os.chown(state, custody.pw_uid, custody.pw_gid)
    socket_dir.mkdir(mode=0o755, parents=True)
    os.chown(socket_dir, custody.pw_uid, custody.pw_gid)
    application = custody_application(application, catalog, state)
    (config_home / "config/custody-config.json").write_text(json.dumps(application, indent=2) + "\n")
    policy = {"custody_uid": custody.pw_uid, "agent_uid": agent.pw_uid,
              "state_dir": str(state), "socket_path": str(socket_dir / "agent.sock"),
              "policy": {"swap_rules": [], "x402": {}, "siwx_origins": [], "identity_chains": [], "identity_wallets": []}}
    policy_path = config_home / "policy.json"
    policy_path.write_text(json.dumps(policy, indent=2) + "\n")
    command = [str(python), "-I", str(destination / "scripts/custody-service.py"), "daemon", "--config", str(policy_path)]
    service_name, service_bytes = service_definition(command, custody.pw_name, custody.pw_gid, state, socket_dir, sys.platform)
    (config_home / service_name).write_bytes(service_bytes)
    for path in config_home.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    print("Installed immutable custody; no daemon started and all spending is denied by default.")
    print("Review " + str(policy_path) + " as administrator. Configure the agent with CUSTODY_SOCKET=" + str(socket_dir / "agent.sock") + ", CUSTODY_UID=" + str(custody.pw_uid))
    print("The agent OS identity must also lack root, sudo, container control, debugging access and any ability to replace service/runtime/policy files. Group/sudo checks cannot prove every host privilege path.")


def argument_parser(platform=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--custody-user", required=True)
    parser.add_argument("--agent-user", required=True)
    mac = (platform or sys.platform) == "darwin"
    parser.add_argument("--destination", default="/Library/MangroveCustody/app" if mac else "/opt/mangrove-custody")
    parser.add_argument("--config-home", default="/Library/MangroveCustody/configuration" if mac else "/etc/mangrove-custody")
    parser.add_argument("--state-dir", default="/Library/MangroveCustody/state" if mac else "/var/lib/mangrove-custody")
    parser.add_argument("--socket-dir", default="/Library/MangroveCustody/run" if mac else "/run/mangrove-custody")
    parser.add_argument("--python", default=sys.executable)
    return parser


if __name__ == "__main__":
    install(argument_parser().parse_args())
