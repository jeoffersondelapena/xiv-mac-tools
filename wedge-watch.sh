#!/bin/bash
# Detects a black-screen boot wedge (Dalamud finished its Anytime phase but the game never
# delivered a framework tick) and samples the hung game process once per wedge.
L="$HOME/Library/Application Support/XIV on Mac/logs/dalamud.log"
S="$HOME/Library/Application Support/XIV on Mac/wedge-watch"
last_sampled=""
while true; do
  boot=$(grep -n "Initializing VFS database" "$L" 2>/dev/null | tail -1 | cut -d: -f1)
  if [ -n "$boot" ]; then
    bt=$(sed -n "${boot}p" "$L" | cut -c12-19)
    after=$(tail -n +"$boot" "$L")
    if echo "$after" | grep -q "AnytimeAsync) END" && ! echo "$after" | grep -q "FrameworkTickAsync) START"; then
      pid=$(pgrep -f 'ffxiv_dx11.exe' | head -1)
      et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
      if [ -n "$pid" ] && [ "${et:-0}" -ge 75 ] && [ "$last_sampled" != "$bt" ]; then
        last_sampled="$bt"
        out="$S/wedge-sample-$(date +%H%M%S).txt"
        sample "$pid" 8 -file "$out" >/dev/null 2>&1
        echo "WEDGE: boot $bt, game pid $pid alive ${et}s with no framework tick; thread sample saved to $out ($(wc -l < "$out" 2>/dev/null) lines)"
      fi
    fi
  fi
  sleep 15
done
