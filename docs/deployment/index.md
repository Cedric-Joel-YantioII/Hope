---
title: Deployment
description: Deploy Hope in production environments
---

# Deployment

Hope supports multiple deployment strategies for different environments
and scales.

## Docker

The recommended way to deploy Hope in production. Multi-stage builds
with CPU and GPU (NVIDIA CUDA, AMD ROCm) variants.

[:octicons-arrow-right-24: Docker deployment](docker.md)

## systemd (Linux)

Run Hope as a managed system service on Linux servers.

[:octicons-arrow-right-24: systemd setup](systemd.md)

## launchd (macOS)

Register Hope as a launch agent on macOS.

[:octicons-arrow-right-24: launchd setup](launchd.md)

## API Server

Run Hope as an OpenAI-compatible HTTP server via `hope serve`.

[:octicons-arrow-right-24: API server guide](api-server.md)
