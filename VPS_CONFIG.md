# VPS Configuración – Optimizaciones aplicadas el 1/may/2026

Servidor: **root@187.124.230.146** (Ubuntu 24.04, kernel 6.8.0-111).

Documentación de TODOS los archivos creados/modificados durante la sesión de
optimización. Útil para auditar, replicar en otro servidor, o revertir.

---

## 📁 Archivos NUEVOS (creados desde cero)

### 1. Swap

| Path | Propósito |
|---|---|
| `/swapfile` | Archivo binario de 4 GB usado como swap. Persistente en `/etc/fstab`. |
| `/etc/sysctl.d/98-swap.conf` | `vm.swappiness=10` y `vm.vfs_cache_pressure=50` |

**Línea añadida a `/etc/fstab`:**
```
/swapfile none swap sw 0 0
```

**Revertir:**
```bash
swapoff /swapfile && rm /swapfile /etc/sysctl.d/98-swap.conf
sed -i '/^\/swapfile/d' /etc/fstab
```

---

### 2. Tuning de red + BBR

| Path | Propósito |
|---|---|
| `/etc/sysctl.d/99-tuning.conf` | BBR, qdisc fq, backlogs, keepalive, TFO, MTU probing |
| `/etc/modules-load.d/bbr.conf` | Carga `tcp_bbr` al arranque |

**Contenido `/etc/sysctl.d/99-tuning.conf`:**
```ini
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_max_syn_backlog = 8192
net.core.netdev_max_backlog = 16384
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 6
net.ipv4.tcp_fin_timeout = 15
net.ipv4.ip_local_port_range = 10240 65535
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_fastopen = 3
```

**Revertir:**
```bash
rm /etc/sysctl.d/99-tuning.conf /etc/modules-load.d/bbr.conf
sysctl --system
```

---

### 3. PostgreSQL 16 — tuning para 8 GB RAM / 2 CPU / SSD

| Path | Propósito |
|---|---|
| `/etc/postgresql/16/main/conf.d/99-tuning.conf` | shared_buffers, work_mem, random_page_cost, etc. |

**Línea añadida a `/etc/postgresql/16/main/postgresql.conf`** (si no existía):
```
include_dir = 'conf.d'
```

**Contenido `/etc/postgresql/16/main/conf.d/99-tuning.conf`:**
```ini
shared_buffers = 1GB
effective_cache_size = 5GB
work_mem = 16MB
maintenance_work_mem = 256MB
wal_buffers = 16MB
random_page_cost = 1.1
effective_io_concurrency = 200
checkpoint_completion_target = 0.9
max_wal_size = 2GB
min_wal_size = 256MB
max_worker_processes = 4
max_parallel_workers = 2
max_parallel_workers_per_gather = 1
max_parallel_maintenance_workers = 1
log_min_duration_statement = 1000
```

**Revertir:**
```bash
rm /etc/postgresql/16/main/conf.d/99-tuning.conf
systemctl restart postgresql@16-main
```

---

### 4. Nginx — gzip extendido + open_file_cache + proxy buffers

| Path | Propósito |
|---|---|
| `/etc/nginx/conf.d/99-tuning.conf` | gzip_types, open_file_cache, tcp_nodelay, proxy buffers, server_tokens off |

**Revertir:**
```bash
rm /etc/nginx/conf.d/99-tuning.conf
nginx -t && systemctl reload nginx
```

---

### 5. systemd drop-ins — LimitNOFILE para apps

Aplicado a 4 servicios. Mismo contenido en cada uno:

| Path |
|---|
| `/etc/systemd/system/hypeauto.service.d/limits.conf` |
| `/etc/systemd/system/jadhstore.service.d/limits.conf` |
| `/etc/systemd/system/netease-api.service.d/limits.conf` |
| `/etc/systemd/system/netease-worker.service.d/limits.conf` |

**Contenido:**
```ini
[Service]
LimitNOFILE=65536
LimitNPROC=8192
```

**Revertir un servicio (ejemplo hypeauto):**
```bash
rm /etc/systemd/system/hypeauto.service.d/limits.conf
systemctl daemon-reload && systemctl restart hypeauto
```

---

## ✏️ Archivos MODIFICADOS

### `/etc/nginx/nginx.conf`
- `worker_connections 768;` → **`worker_connections 4096;`**

### `/etc/nginx/sites-enabled/jadhstore` y `/etc/nginx/sites-enabled/netease-bot`
- `listen 443 ssl;` → **`listen 443 ssl http2;`** (HTTP/2 activado)

### `/etc/systemd/system/jadhstore.service`
ExecStart cambiado (sesión previa, mismo día):
```diff
- ExecStart=...gunicorn --bind 127.0.0.1:8000 --threads 4 --timeout 120 app:app
+ ExecStart=...gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 16 --timeout 90 --graceful-timeout 30 --keep-alive 5 app:app
```

---

## 🚫 Servicios DESACTIVADOS

ClamAV (no era usado por ninguna app — liberó ~1 GB RAM):
```bash
systemctl stop    clamav-daemon clamav-freshclam clamav-daemon.socket
systemctl disable clamav-daemon clamav-freshclam clamav-daemon.socket
```

**Reactivar si alguna vez fuera necesario:**
```bash
systemctl enable --now clamav-daemon clamav-freshclam
```

Los paquetes `clamav` y `clamav-daemon` siguen instalados (no se purgaron).

---

## 📦 Paquetes eliminados (apt autoremove)

52 librerías multimedia huérfanas (libdvdnav4, libfaad2, libgssdp-1.6-0,
libpipewire-0.3-0t64, libsdl2-2.0-0, libxss1, etc.). Ninguna usada por el stack.

---

## 💾 Backups disponibles en el VPS

```
/root/backups/20260501/
├── jadhstore.service.bak                # gunicorn original (4 threads)
├── kernel.before.txt                    # 6.8.0-90 (antes del upgrade)
├── packages.before.txt                  # snapshot dpkg -l (1066 paquetes)
├── packages-count.before.txt
├── services.before.txt                  # systemctl list-units running
├── upgradable.before.txt                # 46 updates pendientes pre-fase1
└── nginx/
    ├── nginx.conf.bak
    ├── jadhstore.bak
    └── netease-bot.bak
```

---

## 📊 Resultado consolidado

| Métrica | Antes | Después |
|---|---|---|
| RAM en uso | 2.2 GB | **1.3 GB** |
| Swap | 0 B | **4 GB disponible** |
| Kernel | 6.8.0-90 | **6.8.0-111** |
| TCP congestion | cubic | **bbr** |
| HTTP version (jadh.shop) | 1.1 | **2** |
| jadh.shop latencia | ~80 ms | **~25 ms** |
| Postgres `shared_buffers` | 128 MB | **1 GB** |
| Postgres `random_page_cost` | 4 (HDD) | **1.1 (SSD)** |
| ClamAV RAM | ~990 MB | **0** |
| nginx `worker_connections` | 768 | **4096** |
| jadhstore gunicorn | 4 threads | **1 worker × 16 threads** |
