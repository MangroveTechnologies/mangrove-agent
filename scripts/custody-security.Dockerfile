FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MANGROVE_SECURITY_E2E=1
RUN groupadd -g 22001 custody && useradd -u 22001 -g 22001 -M -s /usr/sbin/nologin custody \
 && groupadd -g 22002 agent && useradd -u 22002 -g 22002 -M -s /usr/sbin/nologin agent \
 && groupadd -g 22003 outsider && useradd -u 22003 -g 22003 -M -s /usr/sbin/nologin outsider
COPY server/requirements.lock /opt/requirements.lock
RUN pip install --no-cache-dir -r /opt/requirements.lock
WORKDIR /opt/mangrove
COPY server/src /opt/mangrove/server/src
COPY server/requirements.lock /opt/mangrove/server/requirements.lock
COPY scripts /opt/mangrove/scripts
RUN chmod -R go-w /opt/mangrove /usr/local
RUN python -I scripts/install-custody.py --custody-user custody --agent-user agent \
    --destination /opt/mangrove-installed --config-home /etc/mangrove-installed \
    --state-dir /var/lib/mangrove-installed --socket-dir /run/mangrove-installed
CMD ["python", "-I", "/opt/mangrove/scripts/custody_security_e2e.py"]
