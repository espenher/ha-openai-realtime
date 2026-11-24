# Home Assistant add-on image.
#
# Uses the Debian (glibc) base image instead of Alpine (musl). Heavy Python
# dependencies pulled in transitively by pipecat (llvmlite, numba, ...) ship
# prebuilt manylinux wheels for glibc, so they install without compiling
# against a specific LLVM. The previous Alpine build compiled llvmlite from
# source and broke once it resolved to llvmlite 0.47 (which requires LLVM 20)
# while the image only provided LLVM 15.
#
# Override the base per-arch via build.yaml (build_from) / --build-arg BUILD_FROM.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm
FROM ${BUILD_FROM}

ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime deps + minimal build insurance. Most Python deps install from wheels;
# build-essential/python3-dev only cover the rare source-only package.
# ffmpeg + libsndfile1 cover audio decoding/encoding paths in pipecat.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        build-essential \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# The dependencies from pyproject (PEP 517 via poetry-core). No poetry.lock is
# used, matching upstream behaviour; on glibc this resolves to wheels and needs
# no native LLVM/cmake build.
# Only pyproject.toml goes in here, with an empty stand-in for the app package
# (poetry wants to see it), and the stand-in is uninstalled again: so this big
# layer is only rebuilt when the dependencies change. With the app's code in it,
# every update reinstalled everything, and Docker's build cache kept each old
# copy (6 GB of it by 2026-10-24, filling Home Assistant's disk).
COPY pyproject.toml /tmp/app_build/pyproject.toml
RUN mkdir -p /tmp/app_build/app && touch /tmp/app_build/app/__init__.py \
    && pip3 install /tmp/app_build \
    && pip3 uninstall -y openai-realtime-voice-agent \
    && rm -rf /tmp/app_build

# The voice model active mode knows Espen by (3D-Speaker CAM++, Apache 2.0, 28 MB).
RUN mkdir -p /models \
    && curl -fsSL -o /models/speaker.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx \
    && echo "98daf9bcf1c8abf3db374fec4c27a5eda7c58705bb3f180d057b085a72a88bb5  /models/speaker.onnx" | sha256sum -c -

# App code (kept at /app for `python3 -m app.main` and easy inspection)
COPY app/ /app/

# s6-overlay / bashio run script
COPY root/ /
RUN chmod a+x /run.sh

WORKDIR /
CMD ["/run.sh"]
