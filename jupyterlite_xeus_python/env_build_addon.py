"""a JupyterLite addon for creating the env for xeus-python"""
import json
import os
from pathlib import Path
import requests
import shutil
from subprocess import check_call, run, DEVNULL
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

import yaml

from traitlets import List, Unicode

from empack.file_packager import pack_environment
from empack.file_patterns import PkgFileFilter, pkg_file_filter_from_yaml

from jupyterlite.constants import (
    SHARE_LABEXTENSIONS,
    LAB_EXTENSIONS,
    JUPYTERLITE_JSON,
    UTF8,
    FEDERATED_EXTENSIONS,
)
from jupyterlite.addons.federated_extensions import (
    FederatedExtensionAddon,
    ENV_EXTENSIONS,
)

JUPYTERLITE_XEUS_PYTHON = "@jupyterlite/xeus-python-kernel"

# TODO Make this configurable
PYTHON_VERSION = "3.10"

CHANNELS = [
    "https://repo.mamba.pm/emscripten-forge",
    "https://repo.mamba.pm/conda-forge",
]
PLATFORM = "emscripten-32"

SILENT = dict(stdout=DEVNULL, stderr=DEVNULL)

try:
    from mamba.api import create as mamba_create

    MAMBA_PYTHON_AVAILABLE = True
except ImportError:
    MAMBA_PYTHON_AVAILABLE = False

try:
    check_call(["mamba", "--version"], **SILENT)
    MAMBA_AVAILABLE = True
except FileNotFoundError:
    MAMBA_AVAILABLE = False

try:
    check_call(["micromamba", "--version"], **SILENT)
    MICROMAMBA_AVAILABLE = True
except FileNotFoundError:
    MICROMAMBA_AVAILABLE = False

try:
    check_call(["conda", "--version"], **SILENT)
    CONDA_AVAILABLE = True
except FileNotFoundError:
    CONDA_AVAILABLE = False


class PackagesList(List):
    def from_string(self, s):
        return s.split(",")


class XeusPythonEnv(FederatedExtensionAddon):

    __all__ = ["post_build"]

    xeus_python_version = Unicode().tag(
        config=True, description="The xeus-python version to use"
    )

    empack_config = Unicode(
        "https://raw.githubusercontent.com/emscripten-forge/recipes/main/empack_config.yaml",
        config=True,
        description="The path or URL to the empack config file",
    )

    packages = PackagesList([]).tag(
        config=True,
        description="A comma-separated list of packages to install in the xeus-python env",
    )

    @property
    def specs(self):
        """The package specs to install in the environment."""
        return [
            f"python={PYTHON_VERSION}",
            "xeus-python"
            if not self.xeus_python_version
            else f"xeus-python={self.xeus_python_version}",
            *self.packages,
        ]

    @property
    def prefix_path(self):
        """The environment prefix."""
        return Path(self.root_prefix) / "envs" / self.env_name

    def __init__(self, *args, **kwargs):
        super(XeusPythonEnv, self).__init__(*args, **kwargs)

        self.cwd = TemporaryDirectory()
        self.root_prefix = "/tmp/xeus-python-kernel"
        self.env_name = "xeus-python-kernel"

        # Cleanup tmp dir in case it's not empty
        shutil.rmtree(Path(self.root_prefix) / "envs", ignore_errors=True)
        Path(self.root_prefix).mkdir(parents=True, exist_ok=True)

        self.orig_config = os.environ.get("CONDARC")

    def post_build(self, manager):
        """yield a doit task to create the emscripten-32 env and grab anything we need from it"""
        # Install the jupyterlite-xeus-python ourselves
        for pkg_json in self.env_extensions(ENV_EXTENSIONS):
            pkg_data = json.loads(pkg_json.read_text(**UTF8))
            if pkg_data.get("name") == JUPYTERLITE_XEUS_PYTHON:
                yield from self.safe_copy_extension(pkg_json)

        # Bail early if there is no extra package to install
        if not self.packages and not self.xeus_python_version:
            return []

        # Create emscripten env with the given packages
        self.create_env()

        # Download env filter config
        empack_config_is_url = urlparse(self.empack_config).scheme in ("http", "https")
        if empack_config_is_url:
            empack_config_content = requests.get(self.empack_config).content
            pkg_file_filter = PkgFileFilter.parse_obj(
                yaml.safe_load(empack_config_content)
            )
        else:
            pkg_file_filter = pkg_file_filter_from_yaml(self.empack_config)

        # Pack the environment
        pack_environment(
            env_prefix=self.prefix_path,
            outname=Path(self.cwd.name) / "python_data",
            export_name="globalThis.Module",
            pkg_file_filter=pkg_file_filter,
            download_emsdk="latest",
        )

        # Find the federated extensions in the emscripten-env and install them
        root = self.prefix_path / SHARE_LABEXTENSIONS

        # Copy federated extensions found in the emscripten-env
        for pkg_json in self.env_extensions(root):
            yield from self.safe_copy_extension(pkg_json)

        # TODO Currently we're shamelessly overwriting the
        # python_data.{js,data} into the jupyterlite-xeus-python labextension.
        # We should really find a nicer way.
        # (make jupyterlite-xeus-python extension somewhat configurable?)
        dest = self.output_extensions / "@jupyterlite" / "xeus-python-kernel" / "static"

        for file in ["python_data.js", "python_data.data"]:
            yield dict(
                name=f"xeus:copy:{file}",
                actions=[(self.copy_one, [Path(self.cwd.name) / file, dest / file])],
            )

        for file in ["xpython_wasm.js", "xpython_wasm.wasm"]:
            yield dict(
                name=f"xeus:copy:{file}",
                actions=[
                    (
                        self.copy_one,
                        [
                            self.prefix_path / "bin" / file,
                            dest / file,
                        ],
                    )
                ],
            )

        jupyterlite_json = manager.output_dir / JUPYTERLITE_JSON
        lab_extensions_root = manager.output_dir / LAB_EXTENSIONS
        lab_extensions = self.env_extensions(lab_extensions_root)

        yield dict(
            name="patch:xeus",
            doc=f"ensure {JUPYTERLITE_JSON} includes the federated_extensions",
            file_dep=[*lab_extensions, jupyterlite_json],
            actions=[(self.patch_jupyterlite_json, [jupyterlite_json])],
        )

    def create_env(self):
        """Create the xeus-python emscripten-32 env with either mamba, micromamba or conda."""
        if MAMBA_PYTHON_AVAILABLE:
            mamba_create(
                env_name=self.env_name,
                base_prefix=self.root_prefix,
                specs=self.specs,
                channels=CHANNELS,
                target_platform=PLATFORM,
            )
            return

        channels = []
        for channel in CHANNELS:
            channels.extend(["-c", channel])

        if MAMBA_AVAILABLE:
            # Mamba needs the directory to exist already
            self.prefix_path.mkdir(parents=True, exist_ok=True)
            return self._create_env_with_config("mamba", channels)

        if MICROMAMBA_AVAILABLE:
            run(
                [
                    "micromamba",
                    "create",
                    "--yes",
                    "--root-prefix",
                    self.root_prefix,
                    "--name",
                    self.env_name,
                    f"--platform={PLATFORM}",
                    *channels,
                    *self.specs,
                ],
                cwd=self.cwd.name,
                check=True,
            )
            return

        if CONDA_AVAILABLE:
            return self._create_env_with_config("conda", channels)

        raise RuntimeError(
            """Failed to create the virtual environment for xeus-python,
            please make sure at least mamba, micromamba or conda is installed.
            """
        )

    def _create_env_with_config(self, conda, channels):
        run(
            [conda, "create", "--yes", "--prefix", self.prefix_path, *channels],
            cwd=self.cwd.name,
            check=True,
        )
        self._create_config()
        run(
            [
                conda,
                "install",
                "--yes",
                "--prefix",
                self.prefix_path,
                *channels,
                *self.specs,
            ],
            cwd=self.cwd.name,
            check=True,
        )

    def _create_config(self):
        with open(self.prefix_path / ".condarc", "w") as fobj:
            fobj.write(f"subdir: {PLATFORM}")
        os.environ["CONDARC"] = str(self.prefix_path / ".condarc")

    def safe_copy_extension(self, pkg_json):
        """Copy a labextension, and overwrite it
        if it's already in the output
        """
        pkg_path = pkg_json.parent
        stem = json.loads(pkg_json.read_text(**UTF8))["name"]
        dest = self.output_extensions / stem
        file_dep = [
            p
            for p in pkg_path.rglob("*")
            if not (p.is_dir() or self.is_ignored_sourcemap(p.name))
        ]

        yield dict(
            name=f"xeus:copy:ext:{stem}",
            file_dep=file_dep,
            actions=[(self.copy_one, [pkg_path, dest])],
        )

    def dedupe_federated_extensions(self, config):
        if FEDERATED_EXTENSIONS not in config:
            return

        named = {}

        # Making sure to dedupe extensions by keeping the most recent ones
        for ext in config[FEDERATED_EXTENSIONS]:
            if os.path.exists(self.output_extensions / ext["name"] / ext["load"]):
                named[ext["name"]] = ext

        config[FEDERATED_EXTENSIONS] = sorted(named.values(), key=lambda x: x["name"])

    def __del__(self):
        # Cleanup
        shutil.rmtree(Path(self.root_prefix) / "envs", ignore_errors=True)

        if self.orig_config is not None:
            os.environ["CONDARC"] = self.orig_config
        elif "CONDARC" in os.environ:
            del os.environ["CONDARC"]
