"""
build_exe.py — Genera el ejecutable EVOX Bot
Uso: python build_exe.py
Requiere: pip install pyinstaller pillow
"""
import subprocess
import sys
import os
import shutil

ARCHIVOS_EXTRA = [
    "control.html",
    "icon.png",
    "evox.png",
    "charlie.png",
    "susan.png",
]

def build():
    # Limpiar builds anteriores
    for carpeta in ["build", "dist", "__pycache__"]:
        if os.path.exists(carpeta):
            shutil.rmtree(carpeta)
            print(f"  Limpiando {carpeta}...")

    if os.path.exists("EVOX_Bot.spec"):
        os.remove("EVOX_Bot.spec")

    icon = "icon.png" if os.path.exists("icon.png") else ""

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",       # Genera una carpeta dist/EVOX_Bot/ con el exe adentro
        "--noconsole",    # Sin ventana CMD negra
        "--name", "EVOX_Bot",
    ]

    if icon:
        cmd += ["--icon", icon]

    cmd += ["bot_local.py"]

    print("Compilando EVOX Bot...")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print("\n❌ Error en la compilación")
        return

    # Copiar archivos extra a la carpeta del exe
    dest = os.path.join("dist", "EVOX_Bot")
    print("\nCopiando archivos al exe...")
    for archivo in ARCHIVOS_EXTRA:
        if os.path.exists(archivo):
            shutil.copy(archivo, os.path.join(dest, archivo))
            print(f"  ✅ {archivo}")
        else:
            print(f"  ⚠ No encontrado, se omite: {archivo}")

    print(f"""
✅ ¡Listo!

La carpeta a distribuir está en:
  dist\\EVOX_Bot\\

El usuario descarga ESA CARPETA completa y hace doble click en:
  dist\\EVOX_Bot\\EVOX_Bot.exe

Para distribuir: comprime la carpeta dist\\EVOX_Bot\\ en un .zip y compártela.
""")

if __name__ == "__main__":
    build()