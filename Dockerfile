# Pinned to 22.04 (jammy): Python 3.10 gets prebuilt TgCrypto/uvloop wheels
# and pip without the PEP 668 "externally-managed-environment" restriction
# (python3-pip on 24.04 refuses plain `pip3 install -r requirements.txt`).
FROM ubuntu:22.04

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 LANGUAGE=en_US:en TZ=Asia/Kolkata

WORKDIR /usr/src/app

# Everything — including mediainfo and its libzen/libmediainfo/tinyxml2
# dependencies — now comes from the Ubuntu archive. The old hand-picked
# .deb downloads were removed: three of those pool URLs are already 404
# (libzen0v5_0.4.40, libmediainfo0v5_22.12, libmediainfo-dev_22.12) and
# wget exits with code 8 on them, which broke the build.
RUN apt-get update && apt-get install -y \
    python3-pip \
    git \
    wget \
    ffmpeg \
    sox \
    libsox-fmt-mp3 \
    mediainfo \
    locales \
    megatools \
  && rm -rf /var/lib/apt/lists/*

RUN locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

COPY requirements.txt .

RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x start

CMD ["bash", "start"]
