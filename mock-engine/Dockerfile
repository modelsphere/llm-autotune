# Any image with python3 works (the mock is stdlib-only, python 3.8+).
# Override on boxes that can't reach docker hub:
#   docker build --build-arg BASE_IMAGE=<local python image> ...
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
WORKDIR /opt/mock
COPY mock_engine.py ./
COPY sglang ./sglang
COPY vllm /usr/local/bin/vllm
RUN chmod +x /usr/local/bin/vllm
ENV PYTHONPATH=/opt/mock
# No ENTRYPOINT: the autotune driver supplies one (python3 or vllm), exactly
# as it does for real engine images.
