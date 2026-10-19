# Agent Service

A FastAPI-based, multi-user agent platform built with AgentScope 2.0: chat agents with tools, a drag-and-drop workflow builder, multi-agent discussions, and notification channels — all configured through the companion Web UI.

## Features

**Agents**
- Chat with agents that use tools (MCP servers, task planning, schedules, workflows)
- Per-agent configuration: system prompt, model, allowed tools, long-term memory
- Built-in tool approval (human-in-the-loop) and permission modes

**Models**
- Bring any provider: OpenAI, DashScope, Anthropic, DeepSeek, Gemini, Ollama, ...
- Custom Provider for any OpenAI-compatible endpoint (vLLM, LiteLLM, ...) — enter base URL, API key, and model names; test the connection before use
- Per-user credential storage; secrets never leave the server

**MCP Servers**
- One-click integrations (GitLab, Redmine) and hub installs
- Add any custom MCP server by hand (stdio command/args/env or HTTP URL/headers)
- Test button verifies each server connection; the agent form's tool picker lists the available tools per server

**Workflows**
- Build DAGs on a drag-and-drop canvas: agent nodes, condition branches, loops, and approval gates
- Validate graphs client-side, run them manually, on a schedule, via REST API, or as a chat tool
- Watch runs live (per-node status, outputs) and approve or cancel from the UI

**Discussions**
- Multi-agent debates: any number of debaters, each with its own stance and model, plus a judge that scores arguments and detects consensus
- Configurable number of rounds; live view shows each argument as it is made
- Domain-agnostic — the same mechanism works for project management, architecture, code review, or security debates

**Factory**
- Turn a problem statement into shipped work: a ticket flows through Requirements, Architecture, and Development phases
- The first two phases each run a multi-agent discussion with a dedicated role group (product, architecture) and a judge; disagreements stay visible — no forced consensus
- Humans approve at every gate with a note, or reject to re-run a phase; in Delegated mode gates auto-approve until the final review
- The Development phase breaks work into tasks (Plan Lead), executes them with agent sessions in a per-ticket workspace (Developer), and ends with a code review pass

**Channels & Notifications**
- Telegram bot (two-way chat)
- MS Teams push notifications (via Incoming Webhook) and a `send_teams_notification` agent tool
- DingTalk, Discord, and Feishu channels
- Cron schedules for any agent or workflow (e.g. a daily digest posted to Teams)

## Quickstart

Requirements: Python ≥ 3.11, Redis, Node.js ≥ 20 (`npx` for MCP servers, optional `uvx` for Redmine).

Start Redis (Docker is easiest):

```bash
docker run -d -p 6379:6379 redis:7
```

Install and start the service:

```bash
uv pip install agentscope[full]

# Linux/macOS
cd examples/agent_service
python main.py

# Windows (from the repo's venv)
cd examples\agent_service
..\.venv\Scripts\python.exe main.py
```

Start the Web UI in a second terminal:

```bash
cd examples/web_ui
pnpm install
pnpm dev
```

Open the UI (usually `http://localhost:5173`), point it at `http://localhost:8000`, then:

1. **Credentials** — add an API key (or a Custom Provider with your endpoint)
2. **MCP** — install the GitLab/Redmine integrations or add your own server
3. **Agents** — create an agent with a model and tools
4. Chat, schedule, or build a workflow

## Scheduling & Automation

- Give any agent a cron schedule (`Schedule` page) — the agent runs on time and its reply can be pushed to a channel
- Workflows run on schedules too; agents can call `WorkflowRun` as a tool
- Everything is also available over REST — see the interactive docs at `http://localhost:8000/docs`

## Customization

Edit `main.py` to register default MCP servers, custom subagent templates, extra middlewares, or additional channels. See the [tutorial](https://docs.agentscope.io/latest/en/deploy/agent-service) for details.
