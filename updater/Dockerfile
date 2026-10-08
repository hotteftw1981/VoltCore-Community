FROM docker:28-cli

RUN apk add --no-cache python3 docker-cli-compose

ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CONFIG_HOME=/tmp/.config
WORKDIR /opt/voltcore-updater

COPY updater.py /opt/voltcore-updater/updater.py

EXPOSE 9011
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 CMD ["python3","-c","import urllib.request; urllib.request.urlopen('http://127.0.0.1:9011/health', timeout=2).read()"]

CMD ["python3","/opt/voltcore-updater/updater.py"]
