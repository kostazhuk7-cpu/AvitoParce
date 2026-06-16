@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ====================================
echo   Avito Parser - Автоустановка
echo ====================================
echo.

:: Проверка Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден. Установите Python 3.10+ с python.org
    pause
    exit /b 1
)

:: Установка зависимостей
echo [1/3] Установка пакетов...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo Предупреждение: некоторые пакеты не установились
)

:: Установка Playwright браузеров
echo [2/3] Установка Chromium для Playwright...
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo [ОШИБКА] Не удалось установить Chromium
    pause
    exit /b 1
)

:: Запуск
echo [3/3] Запуск сервера...
echo.
start "Avito Parser" python avito_parser\web_app.py
timeout /t 4 /nobreak >nul
start http://localhost:8765
echo.
echo  Сервер: http://localhost:8765
echo  Закройте это окно для остановки.
echo.
pause
