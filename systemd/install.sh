#!/usr/bin/env bash
# Install the user services (no sudo); print the one sudo step for the plug-in rule.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.config/systemd/user
# Copies, not links: `systemctl --user disable` deletes a linked unit file. Re-run after editing the units.
cp "$here/epever-server.service" "$here/epever-worker.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now epever-server.service
systemctl --user enable epever-worker.service  # at login only with the cable in (ConditionPathExistsGlob)
echo "Plug-in start needs one root step:"
echo "  sudo cp '$here/99-epever.rules' /etc/udev/rules.d/ && sudo udevadm control --reload"
