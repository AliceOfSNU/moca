#!/bin/sh
# wait until the emulator is up and Android has finished booting (bounded, so a dead emulator fails fast)
ADB=/opt/android/platform-tools/adb
i=0
while [ $i -lt 60 ]; do
  if [ "$($ADB shell getprop sys.boot_completed 2>/dev/null | tr -d "\r")" = "1" ]; then exit 0; fi
  i=$((i + 1)); sleep 5
done
exit 1
