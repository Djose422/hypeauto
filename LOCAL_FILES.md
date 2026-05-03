# Archivos NO versionados en /home/hypeauto

Documentación creada el 30/abr/2026 (sesión de fix del bug "Formulario no apareció tras validar PIN").

## Backups de main.js

- `main.js.bak.pre-deploy`        : original ANTES de los fixes de hoy
- `main.js.deployed.20260430`     : tras commit `ebd3b19` (primer fix)
- `main.js.bak.before-1f161a1`    : ANTES de commit `1f161a1` (optimización fail-fast)

Rollback rápido:
```bash
cp main.js.bak.pre-deploy main.js && systemctl restart hypeauto.service
```

## Scripts de diagnóstico (NO son parte del bot)

- `diagnose-parallel.js` : reproduce errores con N navegadores en paralelo.
  NO ejecutar mientras el bot esté activo. Antes:
  ```bash
  systemctl stop hypeauto.service
  cd /home/hypeauto && node diagnose-parallel.js <PIN_TEST> <GAME_ID> 100 5
  systemctl start hypeauto.service
  ```

- `diagnose-out/` : evidencias de fallos (screenshots, HTML, network). Vacía normalmente.

## docker-compose.yml

Tiene cambios uncommitted (BROWSER_COUNT, REDEEM_NAME, etc). Se preservan con `git stash` en deploys.
