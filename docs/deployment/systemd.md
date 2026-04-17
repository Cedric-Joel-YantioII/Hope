# systemd Service (Linux)

Hope includes a systemd unit file for running the API server as a managed background service on Linux. This provides automatic startup on boot, crash recovery, and integration with standard Linux service management tools.

## Prerequisites

Before installing the service, ensure that:

1. Hope is installed in a virtual environment at `/opt/hope/.venv` (or adjust paths accordingly).
2. A dedicated `hope` system user exists (recommended for security).
3. An inference engine (such as Ollama) is running and accessible.

Create the user and installation directory:

```bash
sudo useradd --system --create-home --home-dir /opt/hope hope
sudo -u hope python3 -m venv /opt/hope/.venv
sudo -u hope git clone https://github.com/open-hope/Hope.git /opt/hope/Hope
cd /opt/hope/Hope && sudo -u hope uv sync --extra server
```

## Installing the Service

Copy the unit file to the systemd directory, reload the daemon, and enable the service:

```bash
sudo cp deploy/systemd/hope.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hope
sudo systemctl start hope
```

Verify it is running:

```bash
sudo systemctl status hope
```

## Service File Reference

The provided unit file at `deploy/systemd/hope.service`:

```ini
[Unit]
Description=Hope API Server
After=network.target

[Service]
Type=simple
User=hope
WorkingDirectory=/opt/hope
ExecStart=/opt/hope/.venv/bin/hope serve --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
Environment=HOME=/opt/hope

[Install]
WantedBy=multi-user.target
```

### `[Unit]` Section

| Directive     | Value              | Description                                                                 |
|---------------|--------------------|-----------------------------------------------------------------------------|
| `Description` | `Hope API Server` | Human-readable name shown in `systemctl status` and logs.              |
| `After`       | `network.target`   | Delays startup until the network stack is available, since the server binds to a network socket and may need to reach a remote engine. |

### `[Service]` Section

| Directive          | Value                                                              | Description                                                                                     |
|--------------------|--------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| `Type`             | `simple`                                                           | The process started by `ExecStart` is the main service process. systemd considers the service started immediately. |
| `User`             | `hope`                                                       | Runs the server as the `hope` user rather than root, limiting the blast radius of any security issue. |
| `WorkingDirectory` | `/opt/hope`                                                  | Sets the working directory for the process. This is where Hope looks for local files and writes data. |
| `ExecStart`        | `/opt/hope/.venv/bin/hope serve --host 0.0.0.0 --port 8000` | The command to start the server. Uses the full path to the `hope` binary inside the virtual environment. |
| `Restart`          | `on-failure`                                                       | Automatically restarts the service if it exits with a non-zero exit code. Does not restart on clean shutdown (`systemctl stop`). |
| `RestartSec`       | `5`                                                                | Waits 5 seconds before attempting a restart, preventing rapid restart loops if the service crashes immediately on startup. |
| `Environment`      | `HOME=/opt/hope`                                             | Sets the `HOME` environment variable so Hope finds its configuration at `~/.hope/config.toml` (resolving to `/opt/hope/.hope/config.toml`). |

### `[Install]` Section

| Directive    | Value               | Description                                                                                 |
|--------------|---------------------|---------------------------------------------------------------------------------------------|
| `WantedBy`   | `multi-user.target` | The service starts when the system reaches multi-user mode (standard boot target for servers). `systemctl enable` creates a symlink under this target. |

## Configuration Options

### Changing the Bind Address and Port

Edit the `ExecStart` line to change the host or port:

```ini
ExecStart=/opt/hope/.venv/bin/hope serve --host 127.0.0.1 --port 9000
```

!!! tip
    Binding to `127.0.0.1` restricts access to localhost only. Use this when running behind a reverse proxy like Nginx or Caddy.

### Setting the Engine and Model

Pass additional flags to `hope serve`:

```ini
ExecStart=/opt/hope/.venv/bin/hope serve --host 0.0.0.0 --port 8000 --engine ollama --model qwen3:8b
```

### Adding Environment Variables

Add multiple `Environment` directives or use `EnvironmentFile` for complex configurations:

```ini
[Service]
Environment=HOME=/opt/hope
Environment=HOPE_ENGINE_DEFAULT=vllm
Environment=HOPE_OLLAMA_HOST=http://localhost:11434
```

Or load from a file:

```ini
[Service]
EnvironmentFile=/opt/hope/.env
```

### Changing the User

If you prefer a different service user, update both the `User` directive and the paths:

```ini
[Service]
User=myuser
WorkingDirectory=/home/myuser/hope
ExecStart=/home/myuser/hope/.venv/bin/hope serve --host 0.0.0.0 --port 8000
Environment=HOME=/home/myuser/hope
```

### Using a Configuration File

Ensure the configuration file exists at the path where `HOME` points:

```bash
sudo -u hope mkdir -p /opt/hope/.hope
sudo -u hope cp config.toml /opt/hope/.hope/config.toml
```

The server reads `~/.hope/config.toml` on startup, where `~` resolves from the `HOME` environment variable.

## Viewing Logs

Hope logs are captured by journald. View them with `journalctl`:

```bash
# View all logs for the service
sudo journalctl -u hope

# Follow logs in real time
sudo journalctl -u hope -f

# View logs since the last boot
sudo journalctl -u hope -b

# View logs from the last hour
sudo journalctl -u hope --since "1 hour ago"

# View only error-level messages
sudo journalctl -u hope -p err
```

## Managing the Service

### Start, Stop, and Restart

```bash
# Start the service
sudo systemctl start hope

# Stop the service
sudo systemctl stop hope

# Restart the service (stop + start)
sudo systemctl restart hope

# Reload configuration without full restart (sends SIGHUP)
sudo systemctl reload-or-restart hope
```

### Check Status

```bash
sudo systemctl status hope
```

Example output:

```
● hope.service - Hope API Server
     Loaded: loaded (/etc/systemd/system/hope.service; enabled; preset: enabled)
     Active: active (running) since Fri 2026-02-21 10:00:00 UTC; 2h ago
   Main PID: 12345 (hope)
      Tasks: 4 (limit: 4915)
     Memory: 256.0M
        CPU: 1min 23s
     CGroup: /system.slice/hope.service
             └─12345 /opt/hope/.venv/bin/python /opt/hope/.venv/bin/hope serve --host 0.0.0.0 --port 8000
```

### Enable and Disable on Boot

```bash
# Enable automatic start on boot
sudo systemctl enable hope

# Disable automatic start on boot
sudo systemctl disable hope
```

### Apply Changes After Editing the Unit File

After modifying `/etc/systemd/system/hope.service`, reload the systemd daemon and restart the service:

```bash
sudo systemctl daemon-reload
sudo systemctl restart hope
```

## Running Alongside Ollama

If Ollama is also managed via systemd, you can add an ordering dependency so the Hope service waits for Ollama to start:

```ini
[Unit]
Description=Hope API Server
After=network.target ollama.service
Requires=ollama.service
```

| Directive  | Description                                                              |
|------------|--------------------------------------------------------------------------|
| `After`    | Ensures Hope starts after Ollama.                                  |
| `Requires` | If Ollama fails to start, Hope will not start either.              |

!!! note
    Use `Wants` instead of `Requires` if you want Hope to start even when Ollama is unavailable (for example, if you plan to start Ollama manually later).
