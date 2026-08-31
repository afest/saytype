"""Загрузить файл на VirusTotal и дождаться готового отчёта.

Ключ — в C:\\Users\\1\\.claude\\vt.env (VT_API_KEY=...), вне репозитория и вне
переменных окружения по умолчанию: читаем файл напрямую, чтобы не требовать
от вызывающего ничего, кроме самого запуска.

    python tools/upload_virustotal.py Releases/saytype-app-win-Setup.exe
    python tools/upload_virustotal.py Releases/saytype-app-win-Setup.exe --json

Каждый релиз проверяется заново — отчёт привязан к SHA-256 конкретного файла,
а не к версии. Через веб форму загрузка зацикливается на капче при включённом
VPN (замечено на этой машине), поэтому только API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

VT_ENV_PATH = Path(r"C:\Users\1\.claude\vt.env")
API_BASE = "https://www.virustotal.com/api/v3"
POLL_INTERVAL_SEC = 15
POLL_TIMEOUT_SEC = 300


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


def _existing_report(session: requests.Session, file_hash: str) -> dict | None:
    """Файл с этим хешем уже сканировали раньше — не грузить заново."""
    resp = session.get(f"{API_BASE}/files/{file_hash}")
    if resp.status_code == 200:
        return resp.json()
    return None


def upload_and_wait(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Файл не найден: {path}")

    api_key = _load_api_key()
    session = requests.Session()
    session.headers["x-apikey"] = api_key

    file_hash = _sha256(path)
    print(f"[virustotal] {path.name}: sha256 {file_hash}", file=sys.stderr)

    existing = _existing_report(session, file_hash)
    if existing is not None:
        print("[virustotal] отчёт уже есть — повторную загрузку пропускаю", file=sys.stderr)
        return existing

    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"[virustotal] загружаю ({size_mb:.1f} МБ)…", file=sys.stderr)

    # Файлы > 32 МБ VirusTotal просит грузить через отдельный upload_url,
    # обычные POST на /files режет лимитом.
    upload_url = f"{API_BASE}/files"
    if size_mb > 32:
        resp = session.get(f"{API_BASE}/files/upload_url")
        resp.raise_for_status()
        upload_url = resp.json()["data"]

    with path.open("rb") as f:
        resp = session.post(upload_url, files={"file": (path.name, f)})
    resp.raise_for_status()
    analysis_id = resp.json()["data"]["id"]

    print("[virustotal] анализ запущен, жду результат…", file=sys.stderr)
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        resp = session.get(f"{API_BASE}/analyses/{analysis_id}")
        resp.raise_for_status()
        status = resp.json()["data"]["attributes"]["status"]
        if status == "completed":
            break
        time.sleep(POLL_INTERVAL_SEC)
    else:
        raise SystemExit(f"Анализ не завершился за {POLL_TIMEOUT_SEC} сек — проверьте позже вручную.")

    report = _existing_report(session, file_hash)
    if report is None:
        raise SystemExit("Анализ завершён, но отчёт по хешу не найден — странно, проверьте вручную.")
    return report


def summarize(report: dict) -> dict:
    attrs = report["data"]["attributes"]
    stats = attrs.get("last_analysis_stats", {})
    file_hash = attrs.get("sha256", "")
    return {
        "sha256": file_hash,
        "url": f"https://www.virustotal.com/gui/file/{file_hash}",
        "malicious": stats.get("malicious", 0),
        "suspicious": stats.get("suspicious", 0),
        "undetected": stats.get("undetected", 0),
        "total_engines": sum(stats.values()) if stats else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Путь к файлу (Setup.exe / Portable.zip)")
    parser.add_argument("--json", action="store_true", help="Вывести сводку как JSON")
    args = parser.parse_args()

    report = upload_and_wait(args.path)
    summary = summarize(report)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"sha256: {summary['sha256']}")
        print(f"отчёт:  {summary['url']}")
        print(
            f"вердикт: {summary['malicious']} malicious / "
            f"{summary['suspicious']} suspicious / "
            f"{summary['total_engines']} движков всего"
        )
        if summary["malicious"] > 0:
            print("⚠ есть срабатывания malicious — проверить движки поимённо через check_virustotal.py", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
