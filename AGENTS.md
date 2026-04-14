# Project and User Background

> Do not use plan mode unless user explicitly mentioned

## Purpose

This repository contains a PowerPoint generation system with two **independent** generation pipelines:

- **`deeppresenter/`** (HTML pipeline): CLI, multi-agent loop, MCP tool wiring, Design agent → HTML slides → Playwright → PPTX/PDF. This is the freeform generation path.
- **`pptagent/`** (template pipeline): deterministic, template-based generation via `PPTAgent.generate_pres()`. Parses a source PPTX template, induces layouts, then fills them with content from a markdown Document. Also ships a standalone MCP server (`pptagent-mcp`) for external consumers.

These are **separate pipelines, not layers of the same stack**. deeppresenter does NOT depend on pptagent's MCP server. The CLI `--template` flag calls pptagent's Python API directly (`AgentLoop._run_pptagent`), bypassing the MCP agent loop entirely.

When making changes, treat `deeppresenter` as the primary product surface unless the task is explicitly about the pptagent template engine or MCP server.

## Ground Truth

- Python package metadata and console entrypoints live in `pyproject.toml`.
- The main CLI command is `pptagent`, which points to `deeppresenter.cli:main`.
- The MCP server command is `pptagent-mcp`, which points to `pptagent.mcp_server:main`. This is a **standalone** tool server — it is NOT registered in `deeppresenter/mcp.json.example` and should not be.
- Default runtime workspaces are created under `~/.cache/deeppresenter` unless `DEEPPRESENTER_WORKSPACE_BASE` is set.
- Configuration templates live at `deeppresenter/config.yaml.example` and `deeppresenter/mcp.json.example`.
- `deeppresenter/mcp.json.example` lists MCP tools for the **deeppresenter agent loop only** (sandbox, search, research, etc.). Do not add pptagent here — the `--template` path calls pptagent's Python API directly.

Do not assume the root `README.md` is fully current. It still references paths like `webui.py` that are not present in this checkout. Prefer the code and `pyproject.toml` over prose docs when they conflict.

## Core Philosophy

### 1. Good Taste First

> "Sometimes you can look at a problem differently, restate it, and the special case disappears."

- Prefer restructuring the code so edge cases become ordinary cases.
- Good taste is mostly accumulated engineering judgment.
- Removing special-case branches is better than piling on conditionals.

### 2. Pragmatism Over Theory

> "I am a pragmatic bastard."

- Solve the real problem in this repository, not a hypothetical one.
- Reject theoretically elegant but operationally heavy designs when simpler approaches work.
- Code serves reality, not paper architecture.

### 3. Simplicity As A Constraint

> "If you need more than 3 levels of indentation, you're screwed and should fix your program."

- Keep functions short and focused.
- Prefer direct, obvious naming and structure.
- Treat unnecessary complexity as a defect.

## Code Style Rules

1. Avoid excessive exception handling. Do not hide normal control flow behind defensive wrappers unless there is a concrete failure mode to handle.
2. Add type hints to all functions and methods.
3. Write technical documentation and code comments in English.
4. Prefer modern tooling and current best practices:
   - use `uv`, `rg`, and current Python features where appropriate
   - follow current library APIs such as Pydantic `model_dump()` instead of legacy patterns
5. Prefer fewer dependencies and less code.
6. Keep `pyproject.toml` focused on the main `deeppresenter` product surface. Dependencies for isolated subdirectories should live in local `requirements.txt` files when that separation is real and maintainable.

## Communication Rules

- Think in English, reply to the user in Chinese.
- Be direct and concise. If code is bad, explain why in technical terms.
- Keep criticism focused on the implementation, design, or assumptions, never the person.
- Do not dilute technical judgment just to sound polite.

## High-Level Architecture

### `deeppresenter/` (HTML pipeline)

- `cli/`: Typer CLI for `onboard`, `generate`, `serve`, `config`, and `clean`.
  - `pptagent generate "prompt"` → deeppresenter Design pipeline (HTML → PPTX)
  - `pptagent generate "prompt" --template xunfei` → pptagent template pipeline (direct API call)
- `main.py`: orchestration entrypoint. `AgentLoop.run()` executes `Research` first, then branches:
  - `ConvertType.DEEPPRESENTER` → Design agent → HTML → Playwright → PPTX/PDF
  - `ConvertType.PPTAGENT` → `_run_pptagent()` → `pptagent.pptgen.PPTAgent.generate_pres()` (no MCP, no agent loop)
- `agents/`: agent wrappers around the shared `Agent` base class.
  - `research.py`: builds manuscript / research output from prompt and attachments.
  - `design.py`: generates slide HTML, then relies on browser conversion.
  - `pptagent.py`: legacy MCP-based agent wrapper — no longer used by the main pipeline.
- `tools/`: MCP-style tool servers for search, research, reflection, file conversion, and task management.
- `utils/`: config loading, constants, logging, MinerU integration, web conversion, MCP client support.
- `html2pptx/`: Node-based conversion helper used by the HTML slide pipeline.
- `test/`: integration-style tests for sandbox tools, browser/PDF conversion, image processing, and related utilities.

### `pptagent/` (template pipeline)

- Core template-based generation: `pptgen.py` (`PPTAgent.generate_pres()`) orchestrates outline → layout selection → content generation → slide editing in a deterministic loop.
- `document/`: markdown parsing into structured `Document` objects.
- `presentation/`: PPTX template parsing, layout induction, element structures.
- `templates/*/`: bundled templates (source.pptx, slide_induction.json, image_stats.json).
- `mcp_server.py`: standalone MCP server exposing template tools for external consumers (not used by deeppresenter).
- `test/` contains both unit tests and tests marked `llm` / `parse`.

## Working Rules For Agents

- Inspect the actual entrypoint before editing. The same concept may exist in both `deeppresenter/` and `pptagent/`.
- Keep changes scoped. Do not refactor both stacks unless the task clearly spans both.
- If touching CLI behavior, inspect `deeppresenter/cli/commands.py`, `deeppresenter/cli/common.py`, and any config-loading path together.
- If touching orchestration, inspect `deeppresenter/main.py` and the relevant agent class under `deeppresenter/agents/`.
- If touching MCP behavior, confirm whether the change belongs in `deeppresenter/tools/*.py` or `pptagent/mcp_server.py`.
- If touching export/conversion, check both Python and Node sides:
  - `deeppresenter/utils/webview.py`
  - `deeppresenter/html2pptx/`
- Prefer preserving existing config names and environment variables; these are wired into onboarding and example configs.

## Known Sharp Edges

- The repository contains stale documentation from older layouts. Verify files exist before referencing or editing them.
- `deeppresenter` and `pptagent` are **independent pipelines**. Changing one does not automatically update the other. Do not assume one wraps or depends on the other.
- `deeppresenter/agents/pptagent.py` and `deeppresenter/roles/PPTAgent.yaml` are a legacy MCP-based agent wrapper. The `--template` CLI path now bypasses them entirely via `AgentLoop._run_pptagent()`.
- Browser and Docker dependencies are part of the deeppresenter HTML pipeline, not needed for the pptagent template pipeline.
- Some tests are integration-heavy and may fail if Playwright, Docker images, or model credentials are absent.
- pptagent's `_edit_slide` uses `eval()` to execute LLM-generated Python code. Content with unescaped quotes (especially Chinese quotation marks) can cause SyntaxError.

## File Map

- `pyproject.toml`: package metadata, dependencies, pytest markers, console scripts.
- `README.md`: user-facing overview, partially current.
- `pptagent/README.md`: older project framing focused on the original PPTAgent paper/system.
- `pptagent/DOC.md`: legacy documentation with useful conceptual background, but not always current for paths and startup flow.
- `deeppresenter/config.yaml.example`: model/runtime configuration schema.
- `deeppresenter/mcp.json.example`: MCP tool definitions and expected environment variables.

## Preferred Change Strategy

1. Confirm which stack owns the behavior.
2. Edit the smallest relevant surface.
3. Run the narrowest meaningful test subset.
4. Call out any dependency you could not validate locally.

## Anionex Fork Changes (merged 2026-04-14)

### `--template` CLI flag + direct Python API

`pptagent generate` now accepts `--template/-t <name>`. When provided, it sets `ConvertType.PPTAGENT` and calls `AgentLoop._run_pptagent()`, which **bypasses the MCP agent loop entirely** and calls `PPTAgentCore.generate_pres()` directly via Python API. Without `--template`, the default deeppresenter free-form pipeline runs unchanged.

```bash
uv run pptagent generate "..." --template xunfei -o out.pptx
```

The template name is resolved against `pptagent/templates/<name>/`. Available: beamer, cip, default, hit, thu, ucas, xunfei.

### PPTX parsing fixes

- **Multi-master layouts** (`presentation.py`): `layout_mapping` and `from_file` now iterate all slide masters, not just the first. Required for any PPTX with more than one master.
- **Empty layout names** (`presentation.py`): Layouts with blank names are auto-named `layout_N`. `__setstate__` now delegates to `__post_init__` instead of duplicating logic.
- **Nested group shapes** (`shapes.py`): Removed the arbitrary `shape_idx > 100` guard that blocked nested groups. Complex templates with deep shape trees now parse correctly.

### ViT model is now fully optional

- `ModelManager.image_model` returns `[]` (not a crash) when `torch` is not installed.
- Template induction (`induct.py`) catches ViT failure and falls back to one-slide-per-layout clustering, so `template_induct.py` runs without GPU/transformers.

### Other bug fixes

- `apis.py / replace_image`: validates image file exists and is non-empty before `PIL.open`, raises `SlideEditError` with clear message instead of crashing.
- `layout.py / remove_item`: silent no-op when item is absent (idempotent delete, no `ValueError`).
- `mcp_server.py / mcp_slide_validate`: skips already-missing elements to avoid secondary KeyError; `list_templates` return type annotation corrected to `dict`.
- `agents/env.py`: Docker unavailability is now a `warning` instead of `sys.exit(1)` — non-Docker environments can still start, sandbox tools just become unavailable.

### New assets

- `pptagent/templates/xunfei/`: new competition template with 21 fully annotated image slots.
- `start.sh`: convenience startup script for WSL deployment.
