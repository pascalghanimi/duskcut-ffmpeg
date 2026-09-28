#!/bin/bash
set -euo pipefail
cd /work/source/FFmpeg-c867e135494d97b27158d17837d254bbbb8d4f7b
mkdir -p /work/prototype-out
./configure --target-os=mingw32 --arch=x86_64 --enable-cross-compile \
  --cross-prefix=x86_64-w64-mingw32- --cc="$CC" --cxx="$CXX" --ar="$AR" --nm="$NM" --ranlib="$RANLIB" \
  --disable-autodetect --disable-doc --disable-debug --disable-ffplay --disable-network \
  --disable-everything --enable-ffmpeg --enable-ffprobe --enable-mediafoundation --enable-d3d11va \
  --enable-protocol=file,pipe --enable-demuxer=mov,matroska,aac,mpegts,h264,hevc,wav,rawvideo \
  --enable-muxer=mov,mp4,matroska,rawvideo,wav,null,framemd5,hash,md5,adts \
  --enable-decoder=aac_mf,aac_latm_mf,h264_mf,hevc_mf,pcm_s16le,pcm_f32le,rawvideo \
  --enable-encoder=h264_mf,hevc_mf,aac_mf,rawvideo,pcm_s16le,pcm_f32le \
  --enable-parser=h264,hevc,aac,aac_latm \
  --enable-filter=anull,null,format,aformat,aresample,scale,ashowinfo,showinfo,trim,atrim,setpts,asetpts \
  --enable-swscale --enable-swresample --extra-ldflags='-static' \
  --extra-version=duskcut-windows-prototype
make -j"$(nproc)" > /work/prototype-out/compile.log 2>&1
cp ffmpeg.exe ffprobe.exe config.h config_components.h /work/prototype-out/
x86_64-w64-mingw32-objdump -p ffmpeg.exe > /work/prototype-out/pe-imports.txt
