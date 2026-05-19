#  natagaming

 —optimiza tu sistema automáticamente al detectar juegos y lo restaura al salir. (precausion, no ejecutar con permisos sudo.)

## ¿Qué hace?

en el archivo de configuracion setecta los archivos y ventanas especificas que deberia correr con el modo de optimizacion, luego: 

- Desactiva blur, sombras y animaciones en Hyprland para liberar GPU
- Cambia el perfil de CPU a rendimiento máximo (`performance` governor o `powerprofilesctl`)
- Pausa Spotify automáticamente (y lo reanuda al salir)
- Lanza música de fondo con mpv (opcional)
- Oculta Waybar para ganar espacio en pantalla
- Restaura todo al cerrar el juego

Todo sin tocar nada manualmente.

---

## Requisitos

- Hyprland
- Python 3.11+
- `hyprctl`
- `notify-send`
- `mpv` (si `ENABLE_MPV=1`)
- `playerctl` (si `ENABLE_SPOTIFY_PAUSE=1`)
- `powerprofilesctl` o `cpupower` (si `ENABLE_CPU_GOVERNOR=1`)

En Arch/Manjaro:

```bash
sudo pacman -S python mpv playerctl libnotify power-profiles-daemon
```

---

## Instalación

```bash
# 1. Copia el script
cp natagaming.py ~/.local/bin/natagaming.py
chmod +x ~/.local/bin/natagaming.py

# 2. Copia el servicio systemd
mkdir -p ~/.config/systemd/user
cp natagaming.service ~/.config/systemd/user/

# 3. Copia y edita la config
cp natagaming.conf.example ~/.config/natagaming.conf
nano ~/.config/natagaming.conf

# 4. Activa e inicia el servicio
systemctl --user enable --now natagaming
```

---

## Configuración

El archivo de config se crea automáticamente en `~/.config/natagaming.conf` si no existe.

```ini
# Nivel de log: debug | info | warn | error
LOG_LEVEL=info

# Resolución y refresco para gamescope
GAMESCOPE_RES=1920x1080
GAMESCOPE_HZ=144

# Música de fondo con mpv
ENABLE_MPV=1
PLAYLIST=https://www.youtube.com/watch?v=...

# Integraciones
ENABLE_WAYBAR=1
ENABLE_SPOTIFY_PAUSE=1
ENABLE_CPU_GOVERNOR=1

# Proton (dejar vacío para autodetectar)
PROTON_PATH=
STEAM_COMPAT_DATA_PATH=/home/USER/.steam/root/steamapps/compatdata
STEAM_COMPAT_CLIENT_INSTALL_PATH=/home/USER/.steam/root

# Ventanas que activan modo gaming (regex POSIX, pipe-separated)
GAMING_WINDOW_CLASSES=steam_app_[0-9]+|cs2|hl2_linux|Minecraft|heroic|lutris|wine|Lunar Client.*|Roblox|[Ss]ober

# Ventanas que NUNCA activan modo gaming
IGNORE_WINDOW_CLASSES=firefox|Brave-browser|google-chrome|chromium|mpv|vlc|obs|discord|Spotify

# Apps para lanzar en TTY dedicada
# Formato: nombre:tty:comando
TTY_APPS=
```

> **Nota:** el parser no expande `$HOME`. Usa rutas absolutas.

> **Nota:** los arrays usan `|` como separador, no espacios ni comas.

---

## Modos de uso

### `auto` (por defecto)
Corre como daemon y escucha eventos de Hyprland. Se activa solo cuando detecta una ventana de juego.

```bash
natagaming.py
# o explícito:
natagaming.py auto
```

### `steam`
Lanza un juego de Steam por su App ID y entra en modo gaming.

```bash
natagaming.py steam 730        # CS2
natagaming.py steam 570        # Dota 2
```

### `run`
Lanza cualquier ejecutable nativo con modo gaming activo.

```bash
natagaming.py run /usr/bin/mijuego
```

### `wine`
Lanza un ejecutable `.exe` con Wine y activa modo gaming.

```bash
natagaming.py wine "/home/USER/Games/MiJuego/game.exe"
```

### `proton`
Lanza un ejecutable `.exe` con Proton (autodetecta la versión más reciente instalada).

```bash
natagaming.py proton "/home/USER/Games/MiJuego/game.exe"
```

### `gamescope`
Lanza un ejecutable dentro de Gamescope con la resolución y Hz configurados.

```bash
natagaming.py gamescope /usr/bin/mijuego
```

### `tty`
Lanza las apps definidas en `TTY_APPS` en TTYs dedicadas y entra en modo auto.

```bash
natagaming.py tty
```

---

## Gestión del servicio

```bash
# Estado
systemctl --user status natagaming

# Iniciar / detener / reiniciar
systemctl --user start natagaming
systemctl --user stop natagaming
systemctl --user restart natagaming

# Recargar config en caliente (sin reiniciar)
systemctl --user kill -s SIGHUP natagaming

# Ver logs en vivo
journalctl --user -u natagaming -f
```

---

## Detección automática de juegos

El daemon escucha el socket de Hyprland en tiempo real. Cuando una ventana cuya clase coincide con `GAMING_WINDOW_CLASSES` queda activa, entra en modo gaming. Al cerrarla o cambiar a otra ventana que no sea juego, restaura todo automáticamente.

Usa expresiones regulares POSIX, por lo que puedes usar patrones como:
- `steam_app_[0-9]+` — cualquier juego de Steam
- `Lunar Client.*` — Lunar Client con cualquier versión
- `[Ss]ober` — Sober con mayúscula o minúscula

---

## Notas

- Solo funciona dentro de una sesión Hyprland activa.
- Solo puede correr una instancia a la vez (lock file en `$XDG_RUNTIME_DIR/natagaming/`).
- El estado de Hyprland (blur, sombras, animaciones) se guarda antes de entrar al modo gaming y se restaura al salir, incluso si el daemon se detiene inesperadamente.
- En cada boot, el `ExecStartPre` espera a que el socket de Hyprland esté disponible antes de iniciar. (esto puede generar logs de fallos por inicio lento, sin embargo funciona al 2do o tercer intento.)

## Problables bugs.
- Como el script limita a otros programas siempre que es ejecutado, este mismo puede llegar a crashear los mismos si estos consumen una ccantidad considerable de recursos. !Es muy importante cerrar archuvos importantes o de mucho peso, inclusive navegadores que consuman mucho para evitar perdida de datos o bugs!

- Si tu sesion crashea con el script activo (por cualquier razon) este ultimo puede dejar apagadas sombras, animaciones, blur, etc de forma semi-permanente.para arreglarlo estan los siguientes comandos

## Arreglar entorno grafico en caso de crasheo
ejecuta el siguiente comando:

sed -i 's|ejecuta el siguiente comando:\n\nsed.*README.md|ejecuta el siguiente comando:\n\n```bash\nhyprctl --batch "keyword animations:enabled 1 ; keyword decoration:blur:enabled 1 ; keyword decoration:drop_shadow 1"\n```\n\nO reinicia el daemon:\n\n```bash\nsystemctl --user restart natagaming\n```|' ~/natagaming/README.md

---

## Licencia

GPL v3
