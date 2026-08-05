"""Собирает THIRD-PARTY-LICENSES.txt по фактическому составу сборки (T-318).

    python tools/collect_licenses.py                    # по составу build/saytype
    python tools/collect_licenses.py --build build/foo  # другая папка сборки

Список компонентов берётся из артефактов PyInstaller — `PYZ-*.toc` (что попало
в архив с байткодом) и `COLLECT-00.toc` (что легло файлами рядом с exe), — а не
из списка зависимостей. Разница принципиальная: зависимости описывают намерение,
TOC описывает то, что человек получил на руки. Ровно на этом расхождении
предыдущая версия NOTICE обещала одно, а поставка содержала другое.

Дальше по каждому компоненту читаются метаданные `importlib.metadata`: имя,
версия, лицензия, ссылка на проект и полный текст лицензии, если он лежит в
`*.dist-info`.

Папки сборки нет — запустите сначала `pyinstaller saytype.spec`.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata as md
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "licenses" / "THIRD-PARTY-LICENSES.txt"
DEFAULT_BUILD = REPO / "build" / "saytype"

# Не сторонние компоненты: сам проект и то, что даёт стандартная поставка Python.
OURS = {"saytype"}

# Верхнеуровневые имена, за которыми не стоит устанавливаемый дистрибутив:
# ресурсы, положенные спекой, и данные, которые пакеты кладут рядом с собой.
NOT_A_PACKAGE = {
    "assets", "licenses", "base_library.zip",
    "_sounddevice_data", "pywin32_system32",
    "numpy.libs", "scipy.libs",
}

# Компоненты не из PyPI: их лицензии в dist-info не найти, задаём руками.
MANUAL = {
    "tcl8": ("Tcl/Tk", "8.6", "Tcl/Tk License (BSD-style)", "https://www.tcl-lang.org/software/tcltk/license.html"),
    "_tcl_data": None,   # те же файлы, отдельной записи не нужно
    "_tk_data": None,
}


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def toc_entries(path: Path) -> list[str]:
    """Первые элементы кортежей TOC — имена модулей или относительные пути.

    Форма файла отличается от типа к типу: у `PYZ-*.toc` это пара
    ``(путь_к_архиву, [записи])``, у `COLLECT-*.toc` — кортеж из одного списка.
    Поэтому ищем в кортеже первый список, а не берём элемент по индексу.
    """
    data = ast.literal_eval(path.read_text(encoding="utf-8"))
    rows = data
    if isinstance(data, tuple):
        rows = next((item for item in data if isinstance(item, list)), [])
    return [row[0] for row in rows if row and isinstance(row[0], str)]


def top_level_names(build_dir: Path) -> set[str]:
    names: set[str] = set()
    tocs = sorted(build_dir.glob("PYZ-*.toc")) + sorted(build_dir.glob("COLLECT-*.toc"))
    if not tocs:
        raise SystemExit(
            f"В {build_dir} нет ни одного TOC. Сначала соберите: pyinstaller saytype.spec"
        )
    for toc in tocs:
        for entry in toc_entries(toc):
            head = re.split(r"[\\/.]", entry, maxsplit=1)[0]
            if head:
                names.add(head)
    return names


def resolve_dists(names: set[str]) -> tuple[list[md.Distribution], list[str]]:
    """Имена модулей → дистрибутивы. Вторым значением — что не опознали."""
    mapping = md.packages_distributions()
    found: dict[str, md.Distribution] = {}
    unknown: list[str] = []
    for name in sorted(names):
        if name in OURS or name in NOT_A_PACKAGE or name in MANUAL:
            continue
        dist_names = mapping.get(name)
        if not dist_names:
            # Модуль стандартной библиотеки распознаётся по расположению.
            try:
                spec = __import__(name)
                origin = getattr(spec, "__file__", "") or ""
            except Exception:
                origin = ""
            if "site-packages" not in origin.replace("\\", "/"):
                continue  # стандартная библиотека — не сторонний компонент
            unknown.append(name)
            continue
        for dist_name in dist_names:
            key = canon(dist_name)
            if key in found:
                continue
            try:
                found[key] = md.distribution(dist_name)
            except md.PackageNotFoundError:
                unknown.append(dist_name)
    return [found[k] for k in sorted(found)], unknown


LICENSE_FILE_RE = re.compile(r"(licen[cs]e|copying|notice)", re.IGNORECASE)


def license_text(dist: md.Distribution) -> str:
    chunks = []
    for f in dist.files or []:
        parts = f.parts
        if not any(p.endswith((".dist-info", ".egg-info")) for p in parts):
            continue
        if not LICENSE_FILE_RE.search(parts[-1]):
            continue
        if parts[-1].upper() in {"RECORD", "METADATA", "WHEEL"}:
            continue
        try:
            body = dist.read_text(str(f))
        except Exception:
            body = None
        if body and body.strip():
            chunks.append(f"--- {parts[-1]} ---\n{body.strip()}")
    return "\n\n".join(chunks)


def license_name(dist: md.Distribution) -> str:
    meta = dist.metadata
    value = meta.get("License-Expression") or meta.get("License") or ""
    if value and len(value) < 120 and "\n" not in value:
        return value.strip()
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(c.split(" :: ")[-1] for c in classifiers)
    return "см. текст ниже"


def project_url(dist: md.Distribution) -> str:
    meta = dist.metadata
    if meta.get("Home-page"):
        return meta["Home-page"]
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in {"homepage", "source", "repository", "source code"}:
            return url.strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, default=DEFAULT_BUILD,
                        help="папка сборки PyInstaller с файлами *.toc")
    args = parser.parse_args()

    names = top_level_names(args.build)
    dists, unknown = resolve_dists(names)
    manual = [v for v in (MANUAL.get(n) for n in sorted(names)) if v]

    lines = [
        "Сторонние компоненты и их лицензии",
        "=" * 70,
        "",
        "Список собран по фактическому составу сборки (tools/collect_licenses.py:",
        "TOC PyInstaller, а не список зависимостей). Само приложение",
        "распространяется под MIT — см. файл LICENSE.",
        "",
        f"Всего компонентов: {len(dists) + len(manual)}",
        "",
    ]
    for name, version, lic, url in manual:
        lines += ["=" * 70, f"{name} {version}", f"Лицензия: {lic}", f"Проект: {url}", "",
                  "(поставляется вместе с Python, текст — по ссылке выше)", ""]
    for dist in dists:
        lines += [
            "=" * 70,
            f"{dist.metadata['Name']} {dist.version}",
            f"Лицензия: {license_name(dist)}",
        ]
        url = project_url(dist)
        if url:
            lines.append(f"Проект: {url}")
        lines.append("")
        text = license_text(dist)
        lines.append(text if text else "(текст лицензии в пакете не поставляется)")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"{OUT}: {len(dists) + len(manual)} компонентов")
    if unknown:
        print("не опознаны (проверить руками): " + ", ".join(sorted(set(unknown))),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
