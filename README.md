# Home Assistant Dashboard MCP Server

This is a basic MCP server that serves the dashboard as an MCP server.
It supports embeddings for searching items on the dashboard. 
It was created for dashboards that have a `Welcome Pack` and allows any AI Assistant to read the `Welcome Pack` or UI rules to determine what to display. This was designed particularly for holiday lets where dashboards are present

## Integration configuration

Configure the integration in Home Assistant (Settings > Devices & services). Options are applied without changing the underlying dashboards.

| Option | Default | Meaning |
| --- | --- | --- |
| Dashboards (`dashboards`) | None | Select which Lovelace dashboards can be read. |
| Mode (`mode`) | `dumb` | Default for all tools: `dumb` evaluates visibility conditions server-side; `smart` supplies conditions for caller-side evaluation. |
| Allow tool override (`allow_tool_override`) | Disabled | When enabled, expose the optional `mode` argument on tools. When disabled, the argument is hidden and rejected if supplied; the integration mode is enforced. |
| Blocked entities (`blocked_entities`) | None | Redact these entity IDs from tool output in either mode. |
| Include condition states (`include_condition_states`) | Enabled | If a request sets `include_states: true`, include available state information for entities referenced by conditions, subject to redaction. |
| Extra read entities (`extra_read_entities`) | None | Allow the read-only entity-state tools to retrieve specified entities outside the selected dashboards. |
| Viewer user ID (`viewer_user_id`) | Empty | Configured identity for evaluating user-based dashboard conditions; does not automatically track the MCP caller. |
| Keep image paths (`keep_image_paths`) | Disabled | Preserve local image paths in extracted information. |
| Enable YAML inspection (`enable_yaml_export`) | Disabled | Expose `get_dashboard_yaml` for inspecting resolved, redacted dashboard YAML. Calls require effective `smart` mode, and the YAML is not filtered by visibility conditions. |
| Semantic search (`semantic_search`) | Disabled | Enable embedding-assisted retrieval; otherwise use keyword search. |
| Embedding base URL (`embedding_base_url`) | Empty | Embedding provider endpoint. |
| Embedding API key (`embedding_api_key`) | Empty | Optional provider credential. |
| Embedding model (`embedding_model`) | Empty | Provider model name. |
| Embedding batch size (`embedding_batch_size`) | 8 | Embedding batch size, from 1 to 64. |
| Minimum embedding similarity (`embedding_min_similarity`) | 0.45 | Semantic similarity threshold, from -1 to 1. |

### Per-tool mode overrides

When **Allow tool override** is enabled, every tool accepts an optional `mode` argument:

- Omitted: use the integration's configured mode.
- `"mode": "dumb"`: evaluate record conditions on the server for this request.
- `"mode": "smart"`: return conditions for the caller to evaluate for this request.

When the option is disabled (the default, including existing installations), the `mode` field is absent from tool schemas and manually supplied values are rejected. This prevents an agent switching a Dumb-mode installation to Smart mode. The override, when enabled, is per-call; it does not update the integration configuration or subsequent calls. The result's `mode` field shows the effective mode.

Example:

```json
{
  "query": "cooker",
  "mode": "dumb",
  "dashboard_id": "storage:dashboard_guest",
  "limit": 500,
  "offset": 0,
  "format": "records"
}
```

In Dumb mode, records marked `restricted` or `undetermined` remain in results, but nonempty `text`, `service`, and `target` values are replaced with `[RESTRICTED_CONTENT]`. Only display records whose `access` equals `allowed` to a user who requires permission filtering. Smart mode marks records `agent_evaluation_required` and leaves evaluation to the caller.

**Security:** A mode override is not authentication or authorization. A caller with tool access can request Smart mode and receive condition-bearing records, so do not expose these tools directly to untrusted guests. YAML inspection exposes dashboard structure even when visibility conditions would hide it.

### Available tools

| Tool | Description |
| --- | --- |
| `list_dashboards` | Lists selected dashboards. |
| `search_dashboard_context` | Searches indexed records. |
| `get_dashboard_context` | Reads catalogue records with pagination. |
| `get_entity_state` | Reads one permitted live entity state. |
| `get_entity_states` | Reads multiple permitted entity states. |
| `get_dashboard_yaml` | Reads resolved, redacted dashboard YAML when enabled and effective mode is Smart. In configured Dumb mode it is only advertised if overrides are allowed, and requests still require `mode: smart`. |

The search and context tools support `dashboard_id`, `limit` (1-5000, default 500), `offset`, `revision`, `include_states`, `format` (`records` or `delimited`), and `max_chars` (default 250000; 0 disables the character limit). Exceeding a nonzero `max_chars` raises an error instead of truncating data. For pagination, pass the returned `revision` to subsequent requests so changes in the catalogue or permission decisions cause a restart instead of mixed results.

`include_states` requests state details; it does not itself evaluate conditions or grant access.


