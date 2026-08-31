"""Прочитать уже готовый отчёт VirusTotal по хешу или файлу — без повторной загрузки.

    python tools/check_virustotal.py Releases/saytype-app-win-Setup.exe
    python tools/check_virustotal.py 9613385a8eeff672714b180b5f77f9daa56a67507ae66349713010654c552380
    python tools/check_virustotal.py Releases/saytype-app-win-Setup.exe --engines

Для загрузки нового файла — upload_virustotal.py. Этот скрипт только читает:
пригодится свериться перед публикацией поста («антивирусы всё ещё молчат?»)
без траты минуты на повторный аплоад уже известного файла.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import requests

VT_ENV_PATH = Path(r"C:\Users\1\.claude\vt.env")
API_BASE = "https://www.virustotal.com/api/v3"
_HEX64 = set("0123456789abcdefABCDEF")


def _load_api_key() -> str:
    if not VT_ENV_PATH.exists():
        raise SystemExit(f"Нет файла с ключом: {VT_ENV_PATH}")
    text = VT_ENV_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VT_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise SystemExit(f"VT_API_KEY не найден или пуст в {VT_ENV_PATH}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_hash(target: str) -> str:
    """Аргумент — либо готовый sha256 (64 hex-символа), либо путь к файлу."""
    if len(target) == 64 and set(target) <= _HEX64:
        return target.lower()
    path = Path(target)
    if not path.exists():
        raise SystemExit(f"Не похоже на sha256 и не найдено как файл: {target}")
    return _sha256(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Путь к файлу или готовый sha256-хеш")
    parser.add_argument(
        "--engines", action="store_true",
        help="Показать движки, которые не сказали 'чисто' (malicious/suspicious)",
    )
    args = parser.parse_args()

    file_hash = _resolve_hash(args.target)
    api_key = _load_api_key()
    session = requests.Session()
    session.headers["x-apikey"] = api_key

    resp = session.get(f"{API_BASE}/files/{file_hash}")
    if resp.status_code == 404:
        print(
            f"Отчёта по хешу {file_hash} нет — файл ещё не загружался. "
            "Используйте upload_virustotal.py.",
            file=sys.stderr,
        )
        return 1
    resp.raise_for_status()
    data = resp.json()["data"]
    attrs = data["attributes"]
    stats = attrs.get("last_analysis_stats", {})

    print(f"sha256: {file_hash}")
    print(f"отчёт:  https://www.virustotal.com/gui/file/{file_hash}")
    print(
        f"вердикт: {stats.get('malicious', 0)} malicious / "
        f"{stats.get('suspicious', 0)} suspicious / "
        f"{stats.get('undetected', 0)} undetected / "
        f"{sum(stats.values()) if stats else 0} движков всего"
    )

    if args.engines:
        results = attrs.get("last_analysis_results", {})
        flagged = {
            name: r for name, r in results.items()
            if r.get("category") in ("malicious", "suspicious")
        }
        if not flagged:
            print("Ни один движок не пометил файл.")
        else:
            print(f"\nСработали ({len(flagged)}):")
            for name, r in sorted(flagged.items()):
                verdict = r.get("result") or r.get("category")
                print(f"  {name}: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
