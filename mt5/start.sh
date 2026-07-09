#!/bin/bash
# Fixed start.sh — pins mt5linux/rpyc/plumbum so the CLI (-w) works.

mt5file='/config/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe'
WINEPREFIX='/config/.wine'
WINEDEBUG='-all'
wine_executable="wine"
metatrader_version="5.0.36"
mt5server_port="8001"
MT5_CMD_OPTIONS="${MT5_CMD_OPTIONS:-}"
mono_url="https://dl.winehq.org/wine/wine-mono/10.3.0/wine-mono-10.3.0-x86.msi"
python_url="https://www.python.org/ftp/python/3.9.13/python-3.9.13.exe"
mt5setup_url="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"

show_message() { echo "$1"; }

check_dependency() {
    if ! command -v $1 &> /dev/null; then
        echo "$1 is not installed. Please install it to continue."
        exit 1
    fi
}

check_dependency "curl"
check_dependency "$wine_executable"

# [1/7] Mono
if [ ! -e "/config/.wine/drive_c/windows/mono" ]; then
    show_message "[1/7] Downloading and installing Mono..."
    curl -o /config/.wine/drive_c/mono.msi $mono_url
    WINEDLLOVERRIDES=mscoree=d $wine_executable msiexec /i /config/.wine/drive_c/mono.msi /qn
    rm /config/.wine/drive_c/mono.msi
else
    show_message "[1/7] Mono is already installed."
fi

# [2-3/7] MetaTrader 5
if [ -e "$mt5file" ]; then
    show_message "[2/7] MT5 already installed."
else
    show_message "[2/7] Installing MT5..."
    $wine_executable reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d "win10" /f
    curl -o /config/.wine/drive_c/mt5setup.exe $mt5setup_url
    $wine_executable "/config/.wine/drive_c/mt5setup.exe" "/auto" &
    wait
    rm -f /config/.wine/drive_c/mt5setup.exe
fi

# [4/7] Run MT5
if [ -e "$mt5file" ]; then
    show_message "[4/7] Running MT5..."
    $wine_executable "$mt5file" $MT5_CMD_OPTIONS &
else
    show_message "[4/7] MT5 not installed. Cannot run."
fi

# [5/7] Python in Wine
if ! $wine_executable python --version 2>/dev/null; then
    show_message "[5/7] Installing Python in Wine..."
    curl -L $python_url -o /tmp/python-installer.exe
    $wine_executable /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
    rm /tmp/python-installer.exe
fi

# [6/7] PINNED packages — Windows/Wine side
show_message "[6/7] Installing pinned Python libraries (Wine)..."
$wine_executable python -m pip install --upgrade --no-cache-dir pip
$wine_executable python -m pip install --no-cache-dir \
    MetaTrader5==$metatrader_version \
    mt5linux==0.1.9 \
    "numpy<2" \
    python-dateutil

# [6/7] PINNED packages — Linux side (force correct versions even if wrong ones exist)
show_message "[6/7] Installing pinned Python libraries (Linux)..."
pip install --break-system-packages --no-cache-dir --force-reinstall --no-deps \
    mt5linux==0.1.9 rpyc==5.2.3 plumbum==1.7.0
pip install --break-system-packages --no-cache-dir numpy==1.26.4 pyxdg

# [7/7] Start the RPyC server
show_message "[7/7] Starting the mt5linux server..."
python3 -m mt5linux --host 0.0.0.0 -p $mt5server_port -w $wine_executable python.exe &

sleep 5
if ss -tuln | grep ":$mt5server_port" > /dev/null; then
    show_message "[7/7] mt5linux server running on port $mt5server_port."
else
    show_message "[7/7] FAILED to start mt5linux server on port $mt5server_port."
fi
