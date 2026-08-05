"""Собрать архив CUDA-слоя, который приложение докачивает при первом запуске.

Запускать на машине, где стоят pip-пакеты NVIDIA:

    pip install --user nvidia-cublas-cu12 nvidia-cudnn-cu12
    python tools/build_cuda_layer.py

Скрипт находит `site-packages/nvidia/*/bin`, складывает оттуда все DLL в один
zip и печатает SHA-256 с размером — их надо прописать в `cuda_layer.LAYER_SHA256`
и `LAYER_SIZE_BYTES` перед публикацией архива в GitHub Releases.

Проверить слой, не выкладывая ничего в сеть:

    set SAYTYPE_CUDA_LAYER_URL=file:///C:/.../dist/saytype-cuda-....zip
    set SAYTYPE_CUDA_LAYER_SHA256=<из вывода скрипта>
"""

from __future__ import annotations

import argparse
import site
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from saytype import cuda_layer  # noqa: E402


def nvidia_bin_dirs() -> list[Path]:
    """Папки `nvidia/*/bin` во всех site-packages текущего интерпретатора."""
    roots: list[str] = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for sub in nvidia.iterdir():
            bin_dir = sub / "bin"
            key = str(bin_dir).lower()
            if bin_dir.is_dir() and key not in seen:
                seen.add(key)
                found.append(bin_dir)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o", "--out", default=f"dist/{cuda_layer.LAYER_FILENAME}",
        help="куда положить архив (по умолчанию dist/)",
    )
    parser.add_argument(
        "--minimal", action="store_true",
        help="только cuBLAS и загрузчик cuDNN — то, что реально грузится при "
             "транскрипции. Вдвое легче, но без страховки на случай, если "
             "CTranslate2 на чужой карте пойдёт в cuDNN-графы.",
    )
    args = parser.parse_args()

    sources = nvidia_bin_dirs()
    if not sources:
        print(
            "Не нашёл ни одной папки nvidia/*/bin. Поставьте пакеты:\n"
            "    pip install --user nvidia-cublas-cu12 nvidia-cudnn-cu12",
            file=sys.stderr,
        )
        return 1

    print("Источники:")
    for src in sources:
        dlls = list(src.glob("*.dll"))
        size = sum(p.stat().st_size for p in dlls)
        print(f"  {src}  ({len(dlls)} DLL, {size / 1e6:.0f} МБ)")

    out_path, digest, size = cuda_layer.pack_from_site_packages(
        Path(args.out), sources, with_cudnn=not args.minimal
    )

    missing = [d for d in cuda_layer.REQUIRED_DLLS
               if not any(d.lower() == n.lower() for n in _archive_names(out_path))]
    if missing:
        print(f"\nВНИМАНИЕ: в архиве нет {', '.join(missing)} — приложение его отвергнет.",
              file=sys.stderr)
        return 2

    print(f"\nГотово: {out_path}  ({size / 1e6:.0f} МБ)")
    print(f"SHA-256: {digest}")
    print("\nВпишите в src/saytype/cuda_layer.py:")
    print(f'    LAYER_SHA256 = "{digest}"')
    print(f"    LAYER_SIZE_BYTES = {size}")
    return 0


def _archive_names(path: Path) -> list[str]:
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return [Path(n).name for n in zf.namelist()]


if __name__ == "__main__":
    raise SystemExit(main())
