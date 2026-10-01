#!/bin/sh
# Virtual 1280x800 desktop for computer use; the container then idles until JARVIS execs into it.
mkdir -p "$HOME"
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
fluxbox >/tmp/wm.log 2>&1 &
exec sleep infinity
