"""Listen to an RTSP stream on mediamtx and log, once per second:
measured FPS, bitrate, resolution and codec.

Waits until the path is published (e.g. from the Publish tab) and reconnects
when the publisher stops.
"""
import logging
import time

import av

URL = "rtsp://192.168.1.11:8554/camera-upload"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def read(container):
    stream = container.streams.video[0]
    codec = stream.codec_context.name
    logging.info("connected %s | codec=%s", URL, codec)

    frames = nbytes = 0
    width = height = 0
    started = time.monotonic()
    # demux -> packets (for bitrate), decode -> frames (for fps, resolution)
    for packet in container.demux(stream):
        nbytes += packet.size
        for frame in packet.decode():
            frames += 1
            width, height = frame.width, frame.height

        elapsed = time.monotonic() - started
        if elapsed >= 1:
            logging.info("fps=%.1f bitrate=%.2f Mbps resolution=%dx%d codec=%s",
                         frames / elapsed, nbytes * 8 / elapsed / 1e6, width, height, codec)
            frames = nbytes = 0
            started = time.monotonic()


while True:
    try:
        with av.open(URL, options={"rtsp_transport": "tcp"}) as container:
            read(container)
        logging.warning("stream ended")
    except av.FFmpegError as e:
        logging.warning("stream not available (%s)", e)
    time.sleep(2)
