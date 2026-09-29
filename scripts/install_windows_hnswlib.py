"""
Windows-only setup helper: installs the chroma-hnswlib C extension into .venv.

chromadb's default Rust bindings crash with an access violation on native Windows
(upstream bug: https://github.com/chroma-core/chroma/issues/6052). app/retrieval/stores.py
works around this by forcing chromadb's legacy SegmentAPI on win32, which needs
chroma-hnswlib==0.7.6 exactly (chromadb's pinned version) -- it has no Windows wheel on
PyPI (only an alpha, 0.7.6a9, which corrupts memory once the index grows past its first
batch), so this pulls the real conda-forge build instead.

Usage:  pip install zstandard  &&  python scripts/install_windows_hnswlib.py
"""
import io
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import zstandard

PKG = "chroma-hnswlib-0.7.6-py312hbaa7e33_1.conda"
URL = f"https://conda.anaconda.org/conda-forge/win-64/{PKG}"


def main() -> None:
    if sys.platform != "win32":
        sys.exit("This script is only needed on Windows; skip it on Linux/Docker.")

    work = Path(".hnswlib_tmp")
    work.mkdir(exist_ok=True)
    conda_pkg = work / PKG
    print(f"downloading {URL}")
    urllib.request.urlretrieve(URL, conda_pkg)

    with zipfile.ZipFile(conda_pkg) as zf:
        zf.extractall(work)
    pkg_tar_zst = next(work.glob("pkg-*.tar.zst"))
    tar_bytes = zstandard.ZstdDecompressor().decompress(pkg_tar_zst.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        tf.extractall(work / "out")

    site_packages = Path(".venv/Lib/site-packages")
    src_dir = work / "out" / "Lib" / "site-packages"
    for item in src_dir.iterdir():
        dest = site_packages / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy(item, dest)
        print(f"installed {dest}")

    shutil.rmtree(work)
    print("done -- verify with: python -c \"import hnswlib; print(hnswlib.Index.file_handle_count)\"")


if __name__ == "__main__":
    main()
