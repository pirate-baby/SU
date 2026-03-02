#!/bin/bash
# Redirect to the entrypoint's setup mode.
# Preferred usage:
#   docker compose stop proton-bridge
#   docker compose run --rm proton-bridge setup
#   docker compose up -d proton-bridge
exec /entrypoint.sh setup
