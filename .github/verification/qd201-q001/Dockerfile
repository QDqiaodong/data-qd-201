FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb xterm ffmpeg xdotool python3 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN python3 -c 'import sys; assert sys.implementation.name == "cpython" and sys.version_info[:2] == (3,12)'
COPY record_session.py /opt/verification/record_session.py
ENTRYPOINT ["python3", "-B", "/opt/verification/record_session.py"]
