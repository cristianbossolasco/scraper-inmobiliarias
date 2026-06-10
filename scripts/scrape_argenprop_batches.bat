@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0\.."

set SOURCE=argenprop
set FIRST_PAGE=46
set LAST_PAGE=91
set PAGES_PER_BATCH=3
set MAX_LISTINGS=80
set MIN_DELAY=8
set MAX_DELAY=15
set REQUEST_TIMEOUT=30
set GEOCODE_LIMIT=0

echo.
echo Barrido por tandas de %SOURCE%
echo Paginas: %FIRST_PAGE% a %LAST_PAGE%
echo Tanda: %PAGES_PER_BATCH% paginas, max %MAX_LISTINGS% fichas
echo Espera aleatoria entre tandas: %MIN_DELAY% a %MAX_DELAY% segundos
echo Geocodificacion durante scraping: desactivada
echo.

if exist ".scrape.lock" (
    echo Ya existe .scrape.lock.
    echo Esto normalmente significa que hay un scraping en curso o quedo un lock viejo.
    echo Si estas seguro de que no hay ningun scraping corriendo, borralo con:
    echo del .scrape.lock
    echo.
    exit /b 1
)

for /L %%P in (%FIRST_PAGE%,%PAGES_PER_BATCH%,%LAST_PAGE%) do (
    set START_PAGE=%%P
    set /A END_PAGE=%%P + %PAGES_PER_BATCH% - 1
    if !END_PAGE! GTR %LAST_PAGE% set END_PAGE=%LAST_PAGE%

    echo ============================================================
    echo Tanda paginas !START_PAGE! a !END_PAGE!
    echo ============================================================

    python manage.py scrape --source %SOURCE% --start-page !START_PAGE! --max-pages %PAGES_PER_BATCH% --max-listings %MAX_LISTINGS% --geocode-limit %GEOCODE_LIMIT% --request-timeout %REQUEST_TIMEOUT%

    if errorlevel 1 (
        echo.
        echo La tanda iniciada en pagina !START_PAGE! fallo o quedo parcial.
        echo Reanudar luego con:
        echo python manage.py scrape --source %SOURCE% --start-page !START_PAGE! --max-pages %PAGES_PER_BATCH% --max-listings %MAX_LISTINGS% --geocode-limit %GEOCODE_LIMIT% --request-timeout %REQUEST_TIMEOUT%
        echo.
        exit /b 1
    )

    if !END_PAGE! LSS %LAST_PAGE% (
        set /A RANGE=%MAX_DELAY% - %MIN_DELAY% + 1
        set /A WAIT_SECONDS=%MIN_DELAY% + (!RANDOM! %% !RANGE!)
        echo.
        echo Esperando !WAIT_SECONDS! segundos antes de la siguiente tanda...
        timeout /t !WAIT_SECONDS! /nobreak
    )
)

echo.
echo Barrido finalizado.
exit /b 0
