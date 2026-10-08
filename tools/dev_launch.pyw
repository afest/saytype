"""Ярлык dev-копии → tools/dev_run.ps1 без вспышки консоли.

`powershell -WindowStyle Hidden` прячет окно уже после создания: на Windows 11
с Терминалом по умолчанию оно успевает мелькнуть (~0,4 с, замер 05.10.2026).
pythonw своей консоли не имеет, а CREATE_NO_WINDOW не даёт завести её PowerShell.

Ярлык: pythonw.exe "<repo>\\tools\\dev_launch.pyw" [аргументы dev_run.ps1]
"""
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

here = Path(__file__).resolve().parent
subprocess.Popen(
    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(here / "dev_run.ps1"), *sys.argv[1:]],
    cwd=str(here.parent),
    creationflags=CREATE_NO_WINDOW,
)
