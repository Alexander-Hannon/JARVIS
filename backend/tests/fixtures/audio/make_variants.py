"""Transcode the real speech WAV into the containers browsers actually record:
WebM/Opus (Chromium, Firefox) and MP4/AAC (iOS Safari). Uses PyAV's bundled FFmpeg."""
from pathlib import Path

import av

HERE = Path(__file__).parent


def transcode(src: Path, dst: Path, fmt: str, codec: str, rate: int) -> None:
    with av.open(str(src)) as inp, av.open(str(dst), "w", format=fmt) as out:
        ost = out.add_stream(codec, rate=rate)
        ost.layout = "mono"
        res = av.AudioResampler(format=ost.codec_context.format.name if ost.codec_context.format else "fltp",
                                layout="mono", rate=rate)
        for frame in inp.decode(audio=0):
            for f in res.resample(frame):
                for pkt in ost.encode(f):
                    out.mux(pkt)
        for f in res.resample(None):
            for pkt in ost.encode(f):
                out.mux(pkt)
        for pkt in ost.encode(None):
            out.mux(pkt)


if __name__ == "__main__":
    src = HERE / "scenario1.wav"
    transcode(src, HERE / "scenario1.webm", "webm", "libopus", 48000)
    transcode(src, HERE / "scenario1.m4a", "mp4", "aac", 44100)
    for p in sorted(HERE.glob("scenario1.*")):
        print(p.name, p.stat().st_size)
