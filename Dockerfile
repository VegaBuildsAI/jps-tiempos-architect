# JPS Tiempos Lab — Container para Railway / cualquier PaaS Docker
#
# Build local:
#   docker build -t jps-tiempos .
# Run local con volume:
#   docker run -p 7788:7788 -v $(pwd)/data:/data -e JPS_DATA_DIR=/data jps-tiempos
#
# En Railway: detecta este Dockerfile automáticamente. El volume se monta
# en /data (configurable via env JPS_DATA_DIR).

FROM python:3.12-slim

# UTF-8 para que los caracteres del dashboard se rendereen bien
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=es_ES.UTF-8 \
    LC_ALL=C.UTF-8 \
    JPS_DATA_DIR=/data \
    PORT=7788

WORKDIR /app

# El proyecto usa SOLO stdlib — sin requirements.txt
# (mantener consistente con el ethos del repo)

COPY jps_edge_tool.py \
     jps_accumulate.py \
     simulador.py \
     jps_server.py \
     jps_backtest.py \
     jps_predict.py \
     jps_reconcile.py \
     jps_bandit.py \
     jps_randomness_tests.py \
     jps_nist_sts.py \
     jps_diehard.py \
     dashboard.html \
     jps_console_v2.html \
     ./

COPY assets/ ./assets/

# Crear el directorio de datos por si el volume no está montado en first boot
RUN mkdir -p /data && chmod 755 /data

EXPOSE 7788

# Railway provee $PORT pero default a 7788 para correr local
CMD ["sh", "-c", "python3 jps_server.py ${PORT:-7788}"]
