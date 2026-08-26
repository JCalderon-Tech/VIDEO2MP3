# Tutorial — Video → MP3 Downloader

Guía paso a paso para instalar, ejecutar y usar la aplicación
(`video2mp3.py`). Es una app de escritorio PySide6 (Qt 6) que descarga una
cola de videos y los convierte a MP3 con `yt-dlp` + ffmpeg, con metadatos ID3
y carátula embebida.

> Nota: esta app se ejecuta con **Python**, no con `npx`. El comando
> `npx hyperframes skills update` pertenece a otra herramienta y no tiene
> relación con este proyecto.

---

## 1. Requisitos previos

| Requisito | Versión | Por qué |
|---|---|---|
| Python | **3.11+** (probado en 3.14) | yt-dlp y PySide6 6.11 lo exigen |
| ffmpeg | último estable | extracción de audio MP3 |
| Conexión a internet | — | descarga de videos |

### 1.1 Instalar ffmpeg (Windows)

```powershell
winget install ffmpeg
```

**Importante:** tras instalar ffmpeg con winget, el `PATH` puede no
refrescarse hasta reiniciar la sesión. Si la app no lo detecta, usá el botón
**"Ubicar ffmpeg.exe..."** de la ventana para indicar la carpeta manualmente
(normalmente `C:\Program Files\ffmpeg\bin`).

Alternativa: descargar de <https://ffmpeg.org/download.html> y agregar la
carpeta `bin` al `PATH`.

---

## 2. Instalación de la app

### 2.1 (Recomendado) Crear un entorno virtual

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2.2 Instalar dependencias

```powershell
pip install -r requirements.txt
```

Esto instala `yt-dlp` (motor de descarga) y `PySide6` (interfaz gráfica).

---

## 3. Ejecutar la aplicación

```powershell
python video2mp3.py
```

Se abre la ventana principal. Para salir: cerrar la ventana (si hay
descargas activas, pregunta confirmación y detiene el worker).

---

## 4. Uso paso a paso

1. **Añadir URLs**: pegá un enlace en el campo "URL del video" y presioná
   **"Añadir a la cola"** (o `Enter`). Repetí para varias URLs. Si pegás una
   playlist, se descarga completa.
   - URLs duplicadas → aviso "URL duplicada".
   - Si olvidás el `https://`, se agrega automáticamente.
2. **(Opcional) Carpeta destino**: "Cambiar..." elige dónde guardar los MP3
   (por defecto `~/Descargas_MP3`).
3. **Calidad**: selector junto a los botones (128 / 192 / 256 / 320 kbps;
   por defecto 320, el máximo de MP3).
4. **Iniciar**: **"▶ Iniciar descarga"**. La cola se procesa en orden y la
   tabla muestra estado, progreso, velocidad y tiempo restante.
5. **Cancelar en vuelo**: **"✕ Cancelar"** (`Ctrl+K`) aborta la descarga en
   curso; **"↻ Reintentar"** (`Ctrl+R`) reencola un ítem en Error/Cancelado.
6. **Log**: el panel inferior registra cada operación (`[+]` añadido,
   `[✓]` completado, `[x]` error, etc.).
7. **Importar masivo**: "Importar URLs..." carga un archivo `.txt` con una
   URL por línea (las líneas vacías y las que empiezan con `#` se ignoran).
8. **Actualizar yt-dlp**: "Actualizar yt-dlp" mantiene el motor al día
   (corrige errores 403/CVEs).
9. **Cambiar tema**: el botón "Cambiar tema" alterna claro/oscuro al instante;
   toda la interfaz (incluidos menús y dialogs) usa la paleta del tema activo.

### Atajos de teclado

| Acción | Atajo |
|---|---|
| Quitar seleccionado | `Delete` |
| Reintentar | `Ctrl+R` |
| Cancelar | `Ctrl+K` |
| Abrir carpeta destino | `Ctrl+O` |
| Importar URLs | `Ctrl+I` |
| Actualizar yt-dlp | `Ctrl+U` |

También hay mnemonics `Alt+…` en los botones (Añadir, Cambiar, ffmpeg,
Iniciar, Parar, Quitar, Reintentar, Cancelar, Abrir carpeta, Importar,
Actualizar, Tema).

---

## 5. Configuración persistente

Los ajustes se guardan en `~/.video2mp3.json`:

```json
{
  "output_dir": "",
  "bitrate": "320",
  "ffmpeg_location": "",
  "theme": "light",
  "geometry": ""
}
```

- `output_dir` vacío → se usa `~/Descargas_MP3`.
- `ffmpeg_location` vacío → se detecta en el `PATH`.
- `theme`: `"light"` o `"dark"`.
- `geometry`: posición y tamaño de la ventana (se guarda al cerrar).

---

## 6. Verificación y desarrollo (opcional)

Instalá las herramientas de dev:

```powershell
pip install -r requirements-dev.txt
```

Comprobaciones sin abrir la ventana:

```powershell
python -m py_compile video2mp3.py
python -m ruff check video2mp3.py
python -m mypy video2mp3.py
```

Pruebas funcionales y de temas (headless, con `QT_QPA_PLATFORM=offscreen`):

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python "%TEMP%\opencode\qa_pyside_test.py"     # 158 comprobaciones
python "%TEMP%\opencode\qa_theme_audit.py"     # 121 comprobaciones (contraste, temas)
```

---

## 7. Solución de problemas

| Problema | Solución |
|---|---|
| "ffmpeg no encontrado" | Instalá ffmpeg o usá "Ubicar ffmpeg.exe...". Tras `winget install ffmpeg`, reiniciá la sesión para refrescar el PATH |
| Error 403 / firma al descargar | Presioná "Actualizar yt-dlp" (el `player_client` android/tv/ios/web ya mitiga esto) |
| No se vuelve a descargar un MP3 que ya existe | `overwrites: False` protege contra sobrescritura (comportamiento intencional) |
| La descarga queda colgada | El timeout de red (30 s) evita bloqueos permanentes |
| Aparecen "datos" en el MP3 | La app embebe ID3 + carátula automáticamente; el nombre del archivo es el título del video |

---

## 8. Arquitectura (para desarrolladores)

- `QueueManager` — única orquestación: cola, worker thread, yt-dlp/ffmpeg,
  progreso. La GUI nunca llama a yt-dlp directamente.
- `DownloadItem` — modelo de datos con `uid` estable (identidad por uid,
  nunca por posición).
- `MainWindow` — capa de presentación PySide6; comunicación worker→UI vía
  callbacks + `log_queue` con refresco por `QTimer` (400 ms).
- `THEMES` — paleta de tokens centralizada; `get_stylesheet(theme)` genera el
  QSS global y se aplica con `QApplication.setStyleSheet`.

> Usá la herramienta solo para contenido que tengas derecho a descargar.
