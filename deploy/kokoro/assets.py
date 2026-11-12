"""Immutable model sources; all downloads live in the mounted Hugging Face cache."""

import importlib.util
import logging
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download

STANDARD_REPO = "hexgrad/Kokoro-82M"
STANDARD_REV = "f3ff3571791e39611d31c381e3a41a3af07b4987"
RUSSIAN_REPO = "zaakirio/kokoro-ru"
RUSSIAN_REV = "d649c57b239b18c4c384378127cbf01dba039bc1"
GERMAN_REPO = "kikiri-tts/kikiri-german-martin"
GERMAN_REV = "1e9dcd16ed48fda0a7a1f62e5e37130a5fdf10d9"
ACCENT_REPO = "ruaccent/accentuator"
ACCENT_REV = "b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855"
logger = logging.getLogger(__name__)


def download(repo: str, revision: str, patterns: list[str]) -> Path:
    # HF reuses immutable cached files; interrupted downloads resume on the next start.
    options = dict(
        repo_id=repo,
        revision=revision,
        allow_patterns=patterns,
        ignore_patterns=["**/big.onnx"],
        max_workers=4,
    )
    logger.info("Preparing pinned speech assets: %s@%s", repo, revision)
    return Path(snapshot_download(**options))


def prepare():
    standard = download(
        STANDARD_REPO,
        STANDARD_REV,
        ["config.json", "kokoro-v1_0.pth", "voices/*.pt", "README.md", "LICENSE*"],
    )
    russian = download(
        RUSSIAN_REPO,
        RUSSIAN_REV,
        [
            "config.json",
            "kokoro-config.json",
            "kokoro-ru-v2-base.pth",
            "voices/masha.pt",
            "voices/sveta.pt",
            "ru_g2p.py",
            "espeak-data/**",
            "README.md",
            "LICENSE*",
        ],
    )
    german = download(
        GERMAN_REPO,
        GERMAN_REV,
        ["kikiri_german_martin_ep10.pth", "voices/martin.pt", "README.md", "LICENSE*"],
    )
    accent = download(
        ACCENT_REPO,
        ACCENT_REV,
        [
            "dictionary/**",
            "nn/nn_accent/*",
            "nn/nn_omograph/turbo3.1/*",
            "nn/nn_stress_usage_predictor/*",
            "nn/nn_yo_homograph_resolver/*",
            "koziev/**",
        ],
    )
    import ruaccent

    # RUAccent defaults to package-local data. Link it to the pinned persistent snapshot,
    # so its legacy downloader is never invoked and no weights disappear on recreation.
    package = Path(ruaccent.__file__).parent
    for directory in ("dictionary", "nn"):
        link = package / directory
        target = accent / directory
        if link.is_symlink():
            if link.resolve() == target.resolve():
                continue
            link.unlink()
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
    # These Python helpers resolve __file__ to locate adjacent dictionaries. HF snapshot
    # files are symlinks into blobs, so materialize this small code/data tree together.
    helper = package / "koziev"
    if helper.is_symlink():
        helper.unlink()
    shutil.copytree(accent / "koziev", helper, dirs_exist_ok=True)
    spec = importlib.util.spec_from_file_location("kokoro_russian_g2p", russian / "ru_g2p.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (
        standard,
        russian,
        german,
        module.RuG2P(
            espeak_data=russian / "espeak-data", vocab_path=russian / "kokoro-config.json"
        ),
    )
