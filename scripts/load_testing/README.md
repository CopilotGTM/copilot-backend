# Load Testing Scripts

Load testing for CopilotGTM Backend using [Locust](https://locust.io/).

## Setup

Ensure you have dev dependencies installed:

```bash
uv sync --group dev
```

## Usage

### Web UI Mode (Recommended)

```bash
cd scripts/load_testing
locust
```

1. Open http://localhost:8089 in your browser
2. Enter the number of users and spawn rate
3. Enter the target host (e.g., `http://localhost:8000` or `https://devapi.copilotgtm.com`)
4. Click "Start Swarming"

The script will prompt for email and password before starting.

### Headless Mode

```bash
cd scripts/load_testing
locust --headless -u 10 -r 2 -t 1m --host=https://devapi.copilotgtm.com
```

Options:
- `-u 10` - Number of concurrent users
- `-r 2` - Spawn rate (users per second)
- `-t 1m` - Test duration (1 minute)
- `--host` - Target API host

### Environment Variables

You can set credentials via environment variables to avoid prompts:

**Windows (PowerShell):**
```powershell
$env:LOAD_TEST_EMAIL="your@email.com"
$env:LOAD_TEST_PASSWORD="your_password"
locust --headless -u 10 -r 2 -t 1m --host=https://devapi.copilotgtm.com
```

**Windows (Command Prompt):**
```cmd
set LOAD_TEST_EMAIL=your@email.com
set LOAD_TEST_PASSWORD=your_password
locust --headless -u 10 -r 2 -t 1m --host=https://devapi.copilotgtm.com
```

**Linux/macOS:**
```bash
export LOAD_TEST_EMAIL="your@email.com"
export LOAD_TEST_PASSWORD="your_password"
locust --headless -u 10 -r 2 -t 1m --host=https://devapi.copilotgtm.com
```

Or pass via command line:

```bash
locust --email your@email.com --password yourpass --headless -u 10 -r 2 -t 1m --host=http://localhost:8000
```

## User Types

The load test includes three user behavior types:

| User Type | Weight | Description |
|-----------|--------|-------------|
| CRMUser | 3 | Heavy on deals API operations |
| ChatUser | 1 | Chat session operations |
| MixedUser | 2 | Mix of deals and chat operations |

Weights determine the proportion of each user type spawned.

## Endpoints Tested

- `POST /users/auth/login` - Authentication
- `POST /users/auth/refresh-token` - Token refresh
- `GET /users/crm/deals` - List deals (with various filters)
- `GET /users/chats/sessions` - List chat sessions

## Configuration

Edit `config.py` to modify:
- API endpoints
- Wait times between requests

## Results

Locust provides real-time statistics in the web UI:
- Request/second
- Response times (median, 95th percentile)
- Failure rate
- Charts and downloadable reports
