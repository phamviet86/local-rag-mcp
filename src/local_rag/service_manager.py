from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class AutoIndexService:
    LABEL = "io.local-rag-mcp.watch"

    def __init__(
        self,
        data_home: Path,
        *,
        user_home: Path | None = None,
        system: str | None = None,
        executable: str | None = None,
    ):
        self.data_home = data_home.expanduser().resolve()
        self.user_home = (user_home or Path.home()).expanduser().resolve()
        self.system = (system or platform.system()).lower()
        self.executable = executable or shutil.which("local-rag-mcp") or "local-rag-mcp"
        if self.system not in {"darwin", "linux"}:
            raise RuntimeError("auto-index service is supported on macOS and Linux")

    @property
    def unit_path(self) -> Path:
        if self.system == "darwin":
            return self.user_home / "Library" / "LaunchAgents" / f"{self.LABEL}.plist"
        return self.user_home / ".config" / "systemd" / "user" / "local-rag-mcp.service"

    def install(self) -> dict[str, Any]:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        if self.system == "darwin":
            payload = {
                "Label": self.LABEL,
                "ProgramArguments": [self.executable, "--home", str(self.data_home), "serve"],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Background",
                "StandardOutPath": str(self.data_home / "service.log"),
                "StandardErrorPath": str(self.data_home / "service-error.log"),
            }
            with self.unit_path.open("wb") as stream:
                plistlib.dump(payload, stream, sort_keys=True)
        else:
            self.unit_path.write_text(
                "\n".join(
                    [
                        "[Unit]",
                        "Description=local-rag-mcp continuous indexing",
                        "After=network-online.target",
                        "",
                        "[Service]",
                        (
                            f"ExecStart={_systemd_quote(self.executable)} --home "
                            f"{_systemd_quote(str(self.data_home))} serve"
                        ),
                        "Restart=on-failure",
                        "RestartSec=5",
                        "",
                        "[Install]",
                        "WantedBy=default.target",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self._run(["systemctl", "--user", "daemon-reload"])
        return {"installed": True, "active": False, "unit": str(self.unit_path)}

    def status(self) -> dict[str, Any]:
        installed = self.unit_path.is_file()
        if not installed:
            return {"installed": False, "active": False, "unit": str(self.unit_path)}
        if self.system == "darwin":
            command = ["launchctl", "print", f"gui/{os.getuid()}/{self.LABEL}"]
        else:
            command = ["systemctl", "--user", "is-active", "local-rag-mcp.service"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        return {"installed": True, "active": result.returncode == 0, "unit": str(self.unit_path)}

    def start(self) -> dict[str, Any]:
        if not self.unit_path.is_file():
            raise RuntimeError("service is not installed; run 'local-rag-mcp service install'")
        current = self.status()
        if current["active"]:
            return current
        if self.system == "darwin":
            self._run(
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{os.getuid()}",
                    str(self.unit_path),
                ]
            )
        else:
            self._run(["systemctl", "--user", "enable", "--now", "local-rag-mcp.service"])
        result = self.status()
        if not result["active"]:
            raise RuntimeError(
                "service command completed but the service is inactive; inspect the local "
                "service status and logs"
            )
        return result

    def stop(self) -> dict[str, Any]:
        if not self.status()["active"]:
            result = self.status()
            result["active"] = False
            return result
        if self.system == "darwin":
            self._run(["launchctl", "bootout", f"gui/{os.getuid()}/{self.LABEL}"])
        else:
            self._run(["systemctl", "--user", "disable", "--now", "local-rag-mcp.service"])
        result = self.status()
        result["active"] = False
        return result

    def uninstall(self) -> dict[str, Any]:
        if self.unit_path.exists():
            self.stop()
            self.unit_path.unlink()
        if self.system == "linux":
            self._run(["systemctl", "--user", "daemon-reload"])
        return {"installed": False, "active": False, "unit": str(self.unit_path)}

    def _run(self, command: list[str]) -> None:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            message = self._sanitized_output(result.stderr or result.stdout)
            action = " ".join(command[:2])
            hint = ""
            if self.system == "darwin" and command[1:2] == ["bootstrap"]:
                hint = (
                    " Validate the plist with plutil -lint and inspect the launchd unified log; "
                    "the service was not started."
                )
            raise RuntimeError(
                f"{action} failed (exit {result.returncode}): {message}.{hint}".strip()
            )

    def _sanitized_output(self, value: str) -> str:
        message = " ".join(value.split()) or "service command failed"
        message = message.replace(str(self.data_home), "$LOCAL_RAG_HOME")
        message = message.replace(str(self.user_home), "~")
        message = re.sub(r"(?<!\w)/(?:[^\s:]+/)*[^\s:]+", "<path>", message)
        return message[:240]


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
