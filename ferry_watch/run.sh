#!/command/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting Ferry Watch"
exec python3 -m ferrywatch
