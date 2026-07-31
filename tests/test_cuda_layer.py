"""Тесты докачиваемого CUDA-слоя (T-261).

Главное, что здесь проверяется, — возобновление закачки. Слой весит сотни
мегабайт, обрыв на нестабильной сети это норма, и «начать заново» после каждого
обрыва означает, что на плохом канале слой не поставится никогда.

Сервер поднимается локальный, с поддержкой заголовка Range; настоящие DLL
не нужны — достаточно zip той же формы.
"""

from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from iwhisper import cuda_layer


def _make_archive(payload_size: int = 300_000) -> bytes:
    """Zip той же раскладки, что настоящий слой: bin/<нужные DLL>."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in cuda_layer.REQUIRED_DLLS:
            zf.writestr(f"bin/{name}", b"\x4d\x5a" + b"x" * payload_size)
        zf.writestr("bin/readme.txt", "не DLL — распаковщик должен это пропустить")
    return buf.getvalue()


class _RangeServer(ThreadingHTTPServer):
    daemon_threads = True
    body = b""
    cut_after: int | None = None  # оборвать ответ после N байт (имитация обрыва)
    served_ranges: list[tuple[int, int]] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # тишина в выводе тестов
        pass

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        body = self.server.body
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
        chunk = body[start:]
        self.server.served_ranges.append((start, len(chunk)))
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        cut = self.server.cut_after
        self.wfile.write(chunk[:cut] if cut is not None else chunk)


@pytest.fixture()
def server():
    srv = _RangeServer(("127.0.0.1", 0), _Handler)
    srv.served_ranges = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    """Профиль пользователя — во временную папку, а не в настоящий LOCALAPPDATA."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv(cuda_layer.ENV_SHA256, raising=False)
    return tmp_path


def _url(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/layer.zip"


def test_download_resumes_after_break(server, profile_home, monkeypatch):
    """Оборванная закачка продолжается с места обрыва, а не с нуля."""
    server.body = _make_archive()
    digest = hashlib.sha256(server.body).hexdigest()
    monkeypatch.setattr(cuda_layer, "LAYER_SHA256", digest)

    server.cut_after = 50_000  # первый заход обрывается на 50 КБ
    with pytest.raises(cuda_layer.LayerError):
        cuda_layer.download(url=_url(server))

    part = cuda_layer._download_dir() / (cuda_layer.LAYER_FILENAME + ".part")
    assert part.exists() and part.stat().st_size == 50_000, "недокачанное должно остаться"

    server.cut_after = None  # сеть починилась
    ready = cuda_layer.download(url=_url(server))

    assert ready.read_bytes() == server.body
    assert server.served_ranges[-1][0] == 50_000, "докачка должна пойти с 50 КБ, а не с нуля"


def test_bad_checksum_is_rejected(server, profile_home, monkeypatch):
    """Подменённый или битый архив не должен превратиться в «CUDA установлена»."""
    server.body = _make_archive()
    monkeypatch.setattr(cuda_layer, "LAYER_SHA256", "0" * 64)

    with pytest.raises(cuda_layer.LayerError):
        cuda_layer.download(url=_url(server))

    part = cuda_layer._download_dir() / (cuda_layer.LAYER_FILENAME + ".part")
    assert not part.exists(), "битую закачку надо выбрасывать, иначе докачка её увековечит"


def test_cancel_keeps_progress(server, profile_home, monkeypatch):
    """Отмена сохраняет скачанное: следующий заход продолжит, а не начнёт заново."""
    server.body = _make_archive()
    monkeypatch.setattr(cuda_layer, "LAYER_SHA256", "")

    with pytest.raises(cuda_layer.LayerError):
        cuda_layer.download(url=_url(server), should_cancel=lambda: True)

    part = cuda_layer._download_dir() / (cuda_layer.LAYER_FILENAME + ".part")
    assert part.exists() and part.stat().st_size > 0


def test_install_unpacks_flat_and_marks_version(server, profile_home, tmp_path):
    """Распаковка кладёт DLL плоско, ставит маркер и не тащит мусор из архива."""
    archive = tmp_path / "layer.zip"
    archive.write_bytes(_make_archive())

    target = cuda_layer.install(archive)

    names = {p.name for p in target.glob("*")}
    assert set(cuda_layer.REQUIRED_DLLS) <= names
    assert "readme.txt" not in names, "в bin/ должны попадать только DLL"
    assert cuda_layer.installed_version() == cuda_layer.LAYER_VERSION
    assert cuda_layer.is_installed()


def test_incomplete_archive_is_refused(profile_home, tmp_path):
    """Архив без нужных DLL отвергается до подмены рабочей папки."""
    archive = tmp_path / "wrong.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bin/something_else.dll", b"MZ")

    with pytest.raises(cuda_layer.LayerError):
        cuda_layer.install(archive)
    assert not cuda_layer.is_installed()


def test_installed_version_survives_manual_cleanup(profile_home, tmp_path):
    """Пользователь удалил DLL руками — маркер не должен врать, что слой на месте."""
    archive = tmp_path / "layer.zip"
    archive.write_bytes(_make_archive())
    cuda_layer.install(archive)

    for dll in cuda_layer.bin_dir().glob("*.dll"):
        dll.unlink()

    assert cuda_layer.installed_version() is None
    assert not cuda_layer.is_installed()
