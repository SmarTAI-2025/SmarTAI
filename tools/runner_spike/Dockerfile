FROM python:3.12-slim-bookworm

RUN python -m pip install --no-cache-dir "sympy==1.14.0"

RUN mkdir -p /input /work /opt/smartai-runner \
    && touch /input/request.json \
    && chmod 0444 /input/request.json \
    && chmod 0555 /input /opt/smartai-runner \
    && chown 65534:65534 /work \
    && chmod 0700 /work
COPY --chown=0:0 worker.py /opt/smartai-runner/worker.py
RUN chmod 0555 /opt/smartai-runner/worker.py

USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python", "-I", "/opt/smartai-runner/worker.py"]
