# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

USRA_UP (v1.14) is a Chinese-language multi-agent remote sensing (遥感) analysis system. It orchestrates LLM-powered agents to understand user analysis requests, plan scientific approaches, generate Python code, execute it safely, and produce results — all from local raster data. A Supervisor agent monitors the entire pipeline for deadlocks, repetition, and execution quality.

## Running the Application

```bash
# Install dependencies (Python 3.9+)
pip install openai rasterio numpy matplotlib flask

# Start the web GUI (default: http://127.0.0.1:7860)
python main.py

# Start on a custom port
python main.py --port 8080

# CLI mode (interactive terminal)
python main.py --cli
```

- Configuration is in `config.json` (model, API key, base_url for OpenAI-compatible endpoint, timeouts, retry limits).
- Put raster files (.tif/.tiff) in `data/` before starting. The system scans `data/` at startup and displays metadata.

## Architecture

### Agent Pipeline (sequential flow)

```
User → Manager → Scientist → Engineer → Executor → (report back to User)
                     ↑              ↑           ↑
                     └── Supervisor (observer) ──┘
```

- **UserAgent** (`agents/user_agent.py`): Entry point for user input, forwards to Manager.
- **ManagerAgent** (`agents/manager_agent.py`): Clarifies requirements, confirms with user, dispatches to Scientist, summarizes results. Never writes code.
- **ScientistAgent** (`agents/scientist_agent.py`): Converts user requirements into a structured analysis plan (problem statement, dataset, methodology, expected outputs). Uses semantic band names, not hardcoded indices.
- **EngineerAgent** (`agents/engineer_agent.py`): Generates executable Python code from the Scientist's plan. Features persistent error memory (`data/.engineer_memory.json`), pattern-based error classification, and auto-injection of Chinese font config for matplotlib.
- **ExecutorAgent** (`agents/executor_agent.py`): Runs generated code in a subprocess with safety checks (dangerous patterns, external data references, syntax pre-check, memory safety pre-check). Code files are written to `temp/` and not auto-deleted for debugging.
- **SupervisorAgent** (`agents/supervisor_agent.py`): v1.14 redesign — passive observer, not interceptor. Post-execution validation (checks `results/` for actual file output), deadlock detection, repetition detection, agent issue report handling, auto-pilot mode.
- **ProxyAgent** (`agents/proxy_agent.py`): Sits in the MessageBus interceptor chain. Routes every message through SupervisorAgent for observation; warning messages are forwarded to targets but do not block flow.

All agents extend `BaseAgent` (`agents/base_agent.py`) which wraps OpenAI-compatible LLM calls and provides message send/receive, data source scanning, and `report_issue()` for escalating fatal problems to Supervisor.

### Core Infrastructure

- **`core/message.py`**: Standardized `Message` dataclass with `MessageType` enum (TEXT, CODE, RESULT, ERROR, CONTROL, REVIEW, SYSTEM, DATA_REF). Every inter-agent communication uses this format.
- **`core/message_bus.py`**: Pub/sub message routing with an interceptor chain. Interceptors can modify or drop messages. The ProxyAgent is the primary interceptor for supervisor oversight.
- **`core/state_machine.py`**: Finite state machine (`StateMachine`) tracking `ConversationPhase` transitions. Each agent also has an `AgentState` (IDLE/THINKING/WAITING/EXECUTING/BLOCKED/ERROR). Built-in deadlock detection (transition count, blocked agents, waiting agents) and repetition detection.
- **`core/session.py`**: Lifecycle manager tying MessageBus, StateMachine, and TraceCollector together. Created per analysis session in CLI mode, or per-chat in GUI mode.
- **`core/trace_collector.py`**: Thread-safe event collector for LLM calls, code executions, and tool invocations. Feeds the execution trace panel in the GUI.

### Tools (`tools/`)

All tools return structured dicts: `{"success": bool, "message": str, "data": ...}`.

- **`io_kit.py`**: Raster I/O — scan `data/`, read metadata/bands (via rasterio with pure-TIFF fallback), save rasters. Uses 1-based band indexing.
- **`index_kit.py`**: Spectral indices — NDVI, EVI, NDWI, NBR, LST (single-channel algorithm), MSAVI. All output float32 GeoTIFFs to `results/`.
- **`analysis_kit.py`**: Thematic analysis — threshold segmentation, area calculation (km²/ha), mask application, zonal statistics.
- **`viz_kit.py`**: Visualization — index maps (geo-referenced), thematic classification maps, false-color composites. Uses matplotlib Agg backend for headless operation. Imports matplotlib before rasterio to avoid Windows DLL load-order crashes.
- **`registry.py`**: Tool catalog for function_map registration (AutoGen-compatible). Also builds OpenAI function-call schemas and can wrap tools with tracing.

### GUI (`gui/`)

- **`gui/app.py`**: Flask web app with a `URSAGUI` controller class. Three-column dark-themed UI. Polling-based updates via `/api/updates`. Supports chat mode, auto-pilot toggle, data summary, execution trace table, and conversation export.
- **`gui/templates/index.html`**: Jinja2 template for the web UI.
- **`gui/components.py`**: Gradio component definitions (possibly legacy/alternative UI path).

### Prompts (`prompts/`)

Each agent has a `.txt` system prompt loaded at startup: `manager_prompt.txt`, `scientist_prompt.txt`, `engineer_prompt.txt`, `executor_prompt.txt`, `supervisor_prompt.txt`. These are detailed Chinese-language instructions governing agent behavior.

### Runtime Directories

| Directory | Purpose |
|-----------|---------|
| `data/` | Input raster files (.tif/.tiff) |
| `results/` | Generated GeoTIFFs, JPGs, and text outputs |
| `temp/` | Executed code scripts (not auto-cleaned for debugging) |
| `exports/` | Saved conversation transcripts |
| `prompts/` | Agent system prompts |

## Key Configuration (`config.json`)

- `max_rounds`: Max conversation rounds (50)
- `code_timeout`: Subprocess timeout in seconds (300)
- `engineer_max_tokens`: LLM max tokens for engineer (32768)
- `executor_max_retries`: Max code fix retry attempts (5)
- `supervisor.max_warnings`: Warnings before forced termination (5)
- `supervisor.repetition_threshold`: Consecutive identical messages to flag (4)
- `supervisor.enable_auto_pilot`: Default auto-pilot state (false)

## Platform Notes

- **Windows only**: Encoding fixes (`PYTHONIOENCODING=utf-8`) are applied at startup. Matplotlib must be imported with `matplotlib.use("Agg")` before rasterio to avoid GDAL DLL conflicts. Conda environment DLL search paths are injected into subprocess environments.
- The Engineer agent's `_inject_chinese_font()` method patches generated code with Chinese font configuration before execution.
- The `ERROR_CLASSIFICATION` table in `agents/engineer_agent.py` maps error patterns to fix suggestions and feeds the persistent memory system.
