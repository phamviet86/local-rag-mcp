import hashlib
import json
import os
import platform
import shutil
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

PDFIUM_VERSION = "native-v7988"
ORT_VERSION = "1.27.0"
OCR_MODEL_REVISION = "oar-ocr-v0.7.0"

ARTIFACTS: Dict[Tuple[str, str], Dict[str, Tuple[str, str]]] = {
    ("linux", "x86_64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-linux-x64.tgz",
            "6248189e07bbc33cdeb31976c539a88614307c8a19f3276dbd018efbe5b4a2a2",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-linux-x64-1.27.0.tgz",
            "547e40a48f1fe73e3f812d7c88a948612c23f896b91e4e2ee1e232d7b468246f",
        ),
    },
    ("linux", "aarch64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-linux-arm64.tgz",
            "fb67e132abf9a816194bb40cbe1c9e1f6e3bfcaa0b0ef645bf2e6b9f155c8a73",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-linux-aarch64-1.27.0.tgz",
            "3e4d83ac06924a32a07b6d7f91ce6f852876153fc0bbdf931bf517a140bfbe48",
        ),
    },
    ("darwin", "arm64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-mac-arm64.tgz",
            "4168356c2e62ad5e79553e2e9162f5c99949759d90cb83876a50311f0c32b9b3",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-osx-arm64-1.27.0.tgz",
            "545e81c58152353acb0d1e8bd6ce4b62f830c0961f5b3acfedc790ffd76e477a",
        ),
    },
    ("windows", "amd64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-win-x64.tgz",
            "6f398552d8021a89078f64466557251a204999177287b876be49877eb8750d50",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-win-x64-1.27.0.zip",
            "c5c81710938e68079ff1a192b04897faabe4b43830d48f39f27ecd4e16138bfc",
        ),
    },
}


@dataclass(frozen=True)
class RuntimePaths:
    pdfium: Path
    ort: Path


class OCRRuntimeManager:
    def __init__(self, runtime_dir: Path, model_dir: Path):
        self.runtime_dir = runtime_dir
        self.model_dir = model_dir

    @staticmethod
    def platform_key() -> Tuple[str, str]:
        system = platform.system().lower()
        machine = platform.machine().lower()
        aliases = {"amd64": "x86_64"} if system != "windows" else {"x86_64": "amd64"}
        return system, aliases.get(machine, machine)

    def install(self) -> Dict[str, str]:
        key = self.platform_key()
        if key not in ARTIFACTS:
            raise RuntimeError(f"no pinned OCR runtime for {key[0]}/{key[1]}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        downloads = self.runtime_dir / "downloads"
        downloads.mkdir(exist_ok=True)
        for name, (url, expected) in ARTIFACTS[key].items():
            archive = downloads / url.rsplit("/", 1)[-1]
            if not archive.exists() or _sha256(archive) != expected:
                temporary = archive.with_suffix(archive.suffix + ".part")
                urllib.request.urlretrieve(url, temporary)
                if _sha256(temporary) != expected:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"checksum mismatch for {archive.name}")
                temporary.replace(archive)
            destination = self.runtime_dir / name
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir()
            _safe_extract(archive, destination)
        paths = self.paths()
        manifest = {
            "pdfium_version": PDFIUM_VERSION,
            "onnxruntime_version": ORT_VERSION,
            "ocr_model_revision": OCR_MODEL_REVISION,
            "pdfium": str(paths.pdfium),
            "ort": str(paths.ort),
        }
        (self.runtime_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def paths(self) -> RuntimePaths:
        pdfium_names = ("pdfium.dll", "libpdfium.dylib", "libpdfium.so")
        ort_names = ("onnxruntime.dll", "libonnxruntime.dylib", "libonnxruntime.so")
        pdfium = _find_library(self.runtime_dir / "pdfium", pdfium_names)
        ort = _find_library(self.runtime_dir / "ort", ort_names)
        return RuntimePaths(pdfium, ort)

    def configure(self) -> bool:
        try:
            paths = self.paths()
        except FileNotFoundError:
            return False
        os.environ["PDFIUM_LIB_PATH"] = str(paths.pdfium)
        os.environ["ORT_DYLIB_PATH"] = str(paths.ort)
        os.environ["PDF_INSPECTOR_MODEL_CACHE"] = str(self.model_dir)
        return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if any(not _within(destination, destination / member.filename) for member in members):
                raise RuntimeError("unsafe path in OCR runtime archive")
            source.extractall(destination)
    else:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if any(not _within(destination, destination / member.name) for member in members):
                raise RuntimeError("unsafe path in OCR runtime archive")
            for member in members:
                if member.issym():
                    link = (destination / member.name).parent / member.linkname
                    if not _within(destination, link):
                        raise RuntimeError("unsafe symbolic link in OCR runtime archive")
                elif member.islnk() and not _within(destination, destination / member.linkname):
                    raise RuntimeError("unsafe hard link in OCR runtime archive")
            source.extractall(destination)


def _within(root: Path, path: Path) -> bool:
    resolved_root, resolved = root.resolve(), path.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def _find_library(root: Path, names: Tuple[str, ...]) -> Path:
    for name in names:
        matches = list(root.rglob(name)) if root.exists() else []
        if matches:
            return matches[0].resolve()
    raise FileNotFoundError(f"runtime library not found below {root}")
