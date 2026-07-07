#!/usr/bin/env bash
pkill -f "llama-server --models-preset" && echo "stopped" || echo "was not running"