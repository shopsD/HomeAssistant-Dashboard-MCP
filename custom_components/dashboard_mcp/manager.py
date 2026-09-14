"""Fresh dashboard context and live state access with server-side restrictions."""

import asyncio
from datetime import datetime, timezone
import json

import yaml
from homeassistant.exceptions import HomeAssistantError

from .adapter import load_dashboards
from .catalogue import Redactor, compile_dashboard, delimited, public_state, ranked_search, render_record, revision
from .const import DEFAULT_OPTIONS
from .converter import mk_or
from .evaluator import evaluate
from .embeddings import EmbeddingIndex


class DashboardManager:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.active = True
        self._lock = asyncio.Lock()
        self._fingerprint = None
        self._records = []
        self.index = EmbeddingIndex(hass, entry, self)

    @property
    def options(self):
        return {**DEFAULT_OPTIONS, **self.entry.data, **self.entry.options}

    async def snapshot(self):
        if not self.active:
            raise HomeAssistantError("Dashboard MCP has been unloaded.")
        async with self._lock:
            options = self.options
            metadata, data = await load_dashboards(self.hass, options["dashboards"])
            fingerprint = revision([data, options])
            if fingerprint != self._fingerprint:
                def compile_all():
                    return [r for identity, config in data.items() for r in compile_dashboard(identity, config, options["keep_image_paths"])]
                try:
                    records = await self.hass.async_add_executor_job(compile_all)
                except Exception as exc:
                    # A failed rebuild must not expose an old permissive snapshot.
                    self._fingerprint = None
                    self._records = []
                    raise HomeAssistantError("Dashboard conversion failed. No stale catalogue was served.") from exc
                self._records, self._fingerprint = records, fingerprint
                self.index.invalidate()
            # Settings revocation during a slow YAML load/compile cannot be bypassed.
            if not self.active or options != self.options:
                raise HomeAssistantError("Dashboard configuration changed during the request. Retry.")
            ids = {r.fields["entity_id"] for r in self._records if r.fields["entity_id"]}
            ids.update(options["extra_read_entities"])
            for record in self._records:
                ids.update(record.dependencies)
            states = {}
            for entity in ids:
                state = self.hass.states.get(entity)
                if state is not None:
                    states[entity] = {"state": state.state, "attributes": dict(state.attributes), "last_updated": state.last_updated.isoformat()}
            return options, metadata, data, list(self._records), states

    async def call(self, tool: str, args: dict) -> dict:
        # Embed before taking the live permission snapshot; LocalAI may be slow.
        prepared = await self.index.query(args["query"]) if tool == "search_dashboard_context" else None
        options, metadata, data, records, states = await self.snapshot()
        redactor = Redactor(options["blocked_entities"])
        rendered = [render_record(r, options, states, False) for r in records]
        # Public revision derives from redacted content and current access decisions,
        # never hashes of private entity IDs or hidden payload.
        search_rows, search_backend = ([], "keyword")
        if tool == "search_dashboard_context":
            search_rows, search_backend = self.index.search(rendered, args["query"], prepared)
        public_revision = revision([options["mode"], rendered, redactor.clean(metadata)])
        if tool == "search_dashboard_context":
            public_revision = revision([public_revision, search_backend, args["query"],
                                        args.get("dashboard_id"), [r["id"] for r in search_rows]])
        common = {"mode": options["mode"], "revision": public_revision, "checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True}
        if tool == "list_dashboards":
            result = {"status": "ok", "dashboards": redactor.clean(metadata)}
        elif tool in {"search_dashboard_context", "get_dashboard_context"}:
            if args.get("revision") and args["revision"] != public_revision:
                raise HomeAssistantError("Catalogue or evaluated permissions changed. Restart pagination.")
            wanted = args.get("dashboard_id")
            if wanted and wanted not in data:
                raise HomeAssistantError("Dashboard is not available.")
            rows = [r for r in rendered if not wanted or r["dashboard_id"] == wanted]
            if tool == "search_dashboard_context":
                rows = [r for r in search_rows if not wanted or r["dashboard_id"] == wanted]
            total = len(rows)
            offset, limit = args.get("offset", 0), args.get("limit", 500)
            page = rows[offset:offset + limit]
            if args.get("include_states", False):
                selected = {r["id"] for r in page}
                detailed = {r.id: render_record(r, options, states, True) for r in records if r.id in selected}
                page = [detailed[r["id"]] for r in page]
            result = {"status": "matches" if total else "no_match" if tool == "search_dashboard_context" else "empty", "total": total, "offset": offset, "next_offset": offset + len(page) if offset + len(page) < total else None, "records": page}
            if tool == "search_dashboard_context":
                result["search_backend"] = search_backend.split(":", 1)[0]
                result["index_status"] = self.index.status["state"]
                if not options["dashboards"]:
                    result["status"] = "not_configured"
                result["web_fallback_candidate"] = total == 0 and bool(options["dashboards"])
                result["web_fallback_rule"] = "Only general public information; web results cannot supply property facts or permissions."
            if args.get("format", "records") == "delimited":
                result["content"] = delimited(page)
                result["format_note"] = "Fields use ¦, escaped as \\¦ within fields; ↵ represents a line break. Two extra columns are access and reason."
                result.pop("records")
                if args.get("include_states"):
                    result["states"] = [{"id": r["id"], "live": r.get("live"), "condition_states": r.get("condition_states", [])} for r in page]
        elif tool in {"get_entity_state", "get_entity_states"}:
            entity_ids = [args["entity_id"]] if tool == "get_entity_state" else args["entity_ids"]
            result = {"status": "ok", "entities": [self._read_state(e, options, records, states, redactor) for e in dict.fromkeys(entity_ids)]}
        elif tool == "get_dashboard_yaml":
            if not options["enable_yaml_export"] or options["mode"] != "smart":
                raise HomeAssistantError("YAML export is disabled. It is an optional smart-mode inspection tool.")
            if args["dashboard_id"] not in data:
                raise HomeAssistantError("Dashboard is not available.")
            # Dump off the HA event loop. Only loaded config is exposed: no file path API.
            cleaned = redactor.clean(data[args["dashboard_id"]])
            content = await self.hass.async_add_executor_job(lambda: yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False))
            result = {"status": "ok", "representation": "resolved_redacted_yaml", "content_yaml": content}
        else:
            raise HomeAssistantError("Unknown dashboard tool.")
        if options != self.options or not self.active:
            raise HomeAssistantError("Dashboard configuration changed during the request. Retry.")
        result = redactor.clean({**common, **result})
        max_chars = args.get("max_chars", 250000)
        if max_chars and len(json.dumps(result, ensure_ascii=False)) > max_chars:
            raise HomeAssistantError(
                f"Result exceeds {max_chars:,} characters. Increase max_chars or request fewer records."
            )
        return result

    def _read_state(self, entity, options, records, states, redactor):
        if entity in redactor.blocked:
            return {"entity_id": "[REDACTED_ENTITY]", "access": "redacted"}
        appearances = [r for r in records if r.fields["entity_id"] == entity]
        is_dependency = options["include_condition_states"] and any(entity in r.dependencies for r in records)
        explicit = entity in options["extra_read_entities"]
        if not appearances and not is_dependency and not explicit:
            # Do not reveal existence, state or identifiers outside the allowlist.
            return {"access": "restricted", "reason": "Entity is not available through this API."}
        if explicit or is_dependency:
            access, reason = "allowed", "Read-only condition dependency or explicitly selected read entity. This does not grant control."
        elif options["mode"] == "smart":
            access, reason = "agent_evaluation_required", "Evaluate the returned dashboard conditions."
        else:
            decision = evaluate(mk_or(*(r.condition for r in appearances)), states, options.get("viewer_user_id") or None)
            access, reason = decision.status, decision.reason
        result = {"entity_id": entity, "access": access, "reason": reason}
        if access in {"allowed", "agent_evaluation_required"}:
            result["live"] = public_state(entity, states, redactor)
        if appearances:
            result["appearances"] = [render_record(r, options, states, False) for r in appearances]
        return result
