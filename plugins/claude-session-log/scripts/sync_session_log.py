#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CONTROL_OUTPUT = {"continue": True, "suppressOutput": True}
TEXT_INLINE_LIMIT = 6000
JSON_INLINE_LIMIT = 3000
TOOL_RESULT_INLINE_LIMIT = 4000
LARGE_TELEMETRY_INLINE_LIMIT = 2000
SUMMARY_TEXT_INLINE_LIMIT = 4000
SUMMARY_VALUE_INLINE_LIMIT = 1200
STATE_VERSION = 1

IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}

COMMON_ENTRY_KEYS = {
    "parentUuid",
    "isSidechain",
    "userType",
    "cwd",
    "sessionId",
    "version",
    "gitBranch",
    "type",
    "uuid",
    "timestamp",
    "agentId",
    "isMeta",
    "message",
    "toolUseResult",
    "error",
    "isApiErrorMessage",
    "todos",
}


@dataclass
class SessionPaths:
    session_id: str
    transcript_path: Path
    session_dir: Path
    subagent_paths: list[Path]


@dataclass
class TranscriptEvent:
    entry: dict[str, Any]
    line_no: int
    source_path: Path
    source_label: str
    sequence: int
    timestamp: datetime | None

    @property
    def entry_type(self) -> str:
        value = self.entry.get("type")
        return str(value) if value is not None else "unknown"


@dataclass
class RenderContext:
    session_id: str
    markdown_path: Path
    artifact_store: "ArtifactStore"

    def create_artifact_path(self, prefix: str, extension: str) -> Path:
        return self.artifact_store.create_artifact_path(prefix, extension)

    def relative_link(self, target: Path) -> str:
        return os.path.relpath(target, self.markdown_path.parent)


@dataclass
class ArtifactStore:
    render_artifacts_dir: Path
    artifact_counter: int = 0
    artifact_cache: dict[str, Path] = field(default_factory=dict)

    def create_artifact_path(self, prefix: str, extension: str) -> Path:
        self.artifact_counter += 1
        safe_prefix = slugify(prefix)[:48] or "artifact"
        safe_extension = extension.lstrip(".") or "txt"
        return self.render_artifacts_dir / (
            f"{self.artifact_counter:03d}-{safe_prefix}.{safe_extension}"
        )

    def write_text(self, prefix: str, content: str, extension: str = "txt") -> Path:
        cache_key = f"text:{extension}:{hashlib.sha1(content.encode('utf-8')).hexdigest()}"
        cached = self.artifact_cache.get(cache_key)
        if cached is not None:
            return cached
        artifact_path = self.create_artifact_path(prefix, extension)
        artifact_path.write_text(content, encoding="utf-8")
        self.artifact_cache[cache_key] = artifact_path
        return artifact_path

    def write_bytes(self, prefix: str, content: bytes, extension: str) -> Path:
        cache_key = f"bytes:{extension}:{hashlib.sha1(content).hexdigest()}"
        cached = self.artifact_cache.get(cache_key)
        if cached is not None:
            return cached
        artifact_path = self.create_artifact_path(prefix, extension)
        artifact_path.write_bytes(content)
        self.artifact_cache[cache_key] = artifact_path
        return artifact_path


@dataclass
class SyncResult:
    session_id: str
    session_markdown_path: Path
    summary_path: Path
    usage_path: Path
    state_path: Path
    index_path: Path
    telemetry_artifact_path: Path
    log_root: Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Claude Code transcript and telemetry into project-local Markdown logs."
    )
    parser.add_argument(
        "--hook-input",
        help="Optional path to a JSON file containing hook stdin payload.",
    )
    parser.add_argument("--transcript", help="Override transcript path.")
    parser.add_argument("--project-dir", help="Override project directory.")
    parser.add_argument("--session-id", help="Override session ID.")
    parser.add_argument("--hook-event", help="Override hook event name.")
    parser.add_argument(
        "--telemetry-dir",
        help="Override telemetry directory. Defaults to ~/.claude/telemetry.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    hook_input: dict[str, Any] = {}
    try:
        hook_input = load_hook_input(args.hook_input)
        apply_arg_overrides(hook_input, args)
        sync_session_log(
            hook_input=hook_input,
            project_dir=Path(args.project_dir).expanduser()
            if args.project_dir
            else None,
            telemetry_dir=Path(args.telemetry_dir).expanduser()
            if args.telemetry_dir
            else None,
        )
    except Exception as exc:  # pragma: no cover - exercised indirectly
        log_root = determine_log_root(hook_input, args.project_dir)
        write_plugin_error(log_root, exc)
    finally:
        print(json.dumps(CONTROL_OUTPUT, ensure_ascii=False))
    return 0


def load_hook_input(hook_input_path: str | None) -> dict[str, Any]:
    if hook_input_path:
        with open(Path(hook_input_path).expanduser(), encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}

    raw = sys.stdin.read()
    if not raw.strip():
        return {}

    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def apply_arg_overrides(hook_input: dict[str, Any], args: argparse.Namespace) -> None:
    if args.transcript:
        hook_input["transcript_path"] = args.transcript
    if args.session_id:
        hook_input["session_id"] = args.session_id
    if args.hook_event:
        hook_input["hook_event_name"] = args.hook_event
    if args.project_dir:
        hook_input["cwd"] = args.project_dir


def sync_session_log(
    hook_input: dict[str, Any],
    project_dir: Path | None = None,
    telemetry_dir: Path | None = None,
) -> SyncResult:
    project_root = (
        project_dir
        if project_dir is not None
        else resolve_project_dir(hook_input, os.environ.get("CLAUDE_PROJECT_DIR"))
    )
    log_root = project_root / ".claude-log"
    meta_root = log_root / "meta"
    state_dir = meta_root / "state"
    artifacts_root = meta_root / "artifacts"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    session_paths = resolve_session_paths(hook_input)
    state_path = state_dir / f"{session_paths.session_id}.json"
    state = load_state(state_path)

    transcript_events = load_transcript_events(session_paths)

    session_artifacts_dir = artifacts_root / session_paths.session_id
    session_artifacts_dir.mkdir(parents=True, exist_ok=True)
    telemetry_artifact_path = session_artifacts_dir / "telemetry.jsonl"
    telemetry_records, telemetry_state = ingest_telemetry(
        session_id=session_paths.session_id,
        telemetry_dir=telemetry_dir
        if telemetry_dir is not None
        else Path("~/.claude/telemetry").expanduser(),
        state=state,
        telemetry_artifact_path=telemetry_artifact_path,
    )

    session_title = derive_session_title(transcript_events)
    session_summary = derive_session_summary(transcript_events)
    first_timestamp = first_known_timestamp(transcript_events) or utcnow()
    markdown_relpath = state.get("markdown_relpath") or default_markdown_relpath(
        session_paths.session_id, first_timestamp
    )
    summary_dir_relpath, summary_markdown_relpath, usage_relpath = (
        resolve_summary_relpaths(session_paths.session_id, state)
    )
    session_markdown_path = log_root / markdown_relpath
    session_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = log_root / summary_markdown_relpath
    usage_path = log_root / usage_relpath
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    for legacy_path in (log_root / "summary.md", log_root / "usage.json"):
        if legacy_path.exists():
            legacy_path.unlink()

    render_artifacts_dir = session_artifacts_dir / "rendered"
    if render_artifacts_dir.exists():
        shutil.rmtree(render_artifacts_dir)
    render_artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_store = ArtifactStore(render_artifacts_dir=render_artifacts_dir)
    render_ctx = RenderContext(
        session_id=session_paths.session_id,
        markdown_path=session_markdown_path,
        artifact_store=artifact_store,
    )
    summary_render_ctx = RenderContext(
        session_id=session_paths.session_id,
        markdown_path=summary_path,
        artifact_store=artifact_store,
    )

    session_markdown = build_session_markdown(
        session_id=session_paths.session_id,
        session_title=session_title,
        session_summary=session_summary,
        transcript_events=transcript_events,
        telemetry_records=telemetry_records,
        hook_input=hook_input,
        render_ctx=render_ctx,
        transcript_path=session_paths.transcript_path,
    )
    write_text(session_markdown_path, session_markdown)
    summary_markdown = build_summary_markdown(
        session_id=session_paths.session_id,
        session_title=session_title,
        session_summary=session_summary,
        transcript_events=transcript_events,
        telemetry_records=telemetry_records,
        hook_input=hook_input,
        render_ctx=summary_render_ctx,
        session_markdown_path=session_markdown_path,
        index_path=meta_root / "index.md",
        usage_path=usage_path,
    )
    write_text(summary_path, summary_markdown)
    write_json(
        usage_path,
        build_usage_payload(
            session_id=session_paths.session_id,
            session_title=session_title,
            session_summary=session_summary,
            transcript_events=transcript_events,
            telemetry_records=telemetry_records,
            hook_input=hook_input,
            transcript_path=session_paths.transcript_path,
            session_markdown_path=session_markdown_path,
            summary_path=summary_path,
            index_path=meta_root / "index.md",
            telemetry_artifact_path=telemetry_artifact_path,
        ),
    )

    state_payload = build_state_payload(
        old_state=state,
        session_id=session_paths.session_id,
        markdown_relpath=markdown_relpath,
        summary_dir_relpath=summary_dir_relpath,
        summary_markdown_relpath=summary_markdown_relpath,
        usage_relpath=usage_relpath,
        session_title=session_title,
        session_summary=session_summary,
        transcript_events=transcript_events,
        telemetry_state=telemetry_state,
        transcript_path=session_paths.transcript_path,
        project_root=project_root,
    )
    write_json(state_path, state_payload)

    index_path = write_index(log_root)
    return SyncResult(
        session_id=session_paths.session_id,
        session_markdown_path=session_markdown_path,
        summary_path=summary_path,
        usage_path=usage_path,
        state_path=state_path,
        index_path=index_path,
        telemetry_artifact_path=telemetry_artifact_path,
        log_root=log_root,
    )


def resolve_project_dir(hook_input: dict[str, Any], env_project_dir: str | None) -> Path:
    candidates = [
        env_project_dir,
        hook_input.get("cwd"),
        hook_input.get("project_dir"),
        os.getcwd(),
    ]
    for candidate in candidates:
        if candidate:
            return Path(str(candidate)).expanduser().resolve()
    return Path(os.getcwd()).resolve()


def determine_log_root(hook_input: dict[str, Any], project_dir: str | None) -> Path:
    return resolve_project_dir(hook_input, project_dir) / ".claude-log"


def resolve_session_paths(hook_input: dict[str, Any]) -> SessionPaths:
    transcript_value = hook_input.get("transcript_path") or hook_input.get("transcriptPath")
    session_id = hook_input.get("session_id") or hook_input.get("sessionId")
    if not transcript_value:
        raise ValueError("Hook payload is missing transcript_path.")

    transcript_path = Path(str(transcript_value)).expanduser()
    if transcript_path.name.startswith("agent-") and transcript_path.parent.name == "subagents":
        main_session_id = transcript_path.parent.parent.name
        main_transcript_path = transcript_path.parent.parent.parent / f"{main_session_id}.jsonl"
        session_dir = transcript_path.parent.parent
    else:
        main_session_id = str(session_id or transcript_path.stem)
        main_transcript_path = transcript_path
        session_dir = main_transcript_path.parent / main_session_id

    subagent_dir = session_dir / "subagents"
    subagent_paths = sorted(subagent_dir.glob("agent-*.jsonl")) if subagent_dir.exists() else []
    return SessionPaths(
        session_id=main_session_id,
        transcript_path=main_transcript_path,
        session_dir=session_dir,
        subagent_paths=subagent_paths,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_transcript_events(session_paths: SessionPaths) -> list[TranscriptEvent]:
    sequence = 0
    events: list[TranscriptEvent] = []
    events.extend(load_single_jsonl(session_paths.transcript_path, "main", sequence))
    sequence += len(events)
    for subagent_path in session_paths.subagent_paths:
        loaded = load_single_jsonl(subagent_path, subagent_path.stem, sequence)
        events.extend(loaded)
        sequence += len(loaded)
    return sorted(
        events,
        key=lambda event: (
            event.timestamp if event.timestamp is not None else datetime.max.replace(tzinfo=timezone.utc),
            event.sequence,
        ),
    )


def load_single_jsonl(path: Path, source_label: str, start_sequence: int) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    if not path.exists():
        return events

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                payload = {
                    "type": "invalid-json",
                    "error": str(exc),
                    "rawLine": line,
                }
            if not isinstance(payload, dict):
                payload = {
                    "type": "raw-value",
                    "value": payload,
                }
            events.append(
                TranscriptEvent(
                    entry=payload,
                    line_no=line_no,
                    source_path=path,
                    source_label=source_label,
                    sequence=start_sequence + len(events),
                    timestamp=extract_event_timestamp(payload),
                )
            )
    return events


def extract_event_timestamp(entry: dict[str, Any]) -> datetime | None:
    candidates = [
        entry.get("timestamp"),
        entry.get("snapshot", {}).get("timestamp")
        if isinstance(entry.get("snapshot"), dict)
        else None,
        entry.get("event_data", {}).get("client_timestamp")
        if isinstance(entry.get("event_data"), dict)
        else None,
    ]
    for candidate in candidates:
        parsed = parse_timestamp(candidate)
        if parsed is not None:
            return parsed
    return None


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def ingest_telemetry(
    session_id: str,
    telemetry_dir: Path,
    state: dict[str, Any],
    telemetry_artifact_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    telemetry_dir = telemetry_dir.expanduser()
    telemetry_artifact_path.parent.mkdir(parents=True, exist_ok=True)

    existing_records = load_jsonl_dicts(telemetry_artifact_path)
    seen_event_ids = set(
        event_id
        for event_id in state.get("telemetry_event_ids", [])
        if isinstance(event_id, str)
    )
    for record in existing_records:
        event_id = telemetry_event_id(record)
        if event_id:
            seen_event_ids.add(event_id)

    offsets = {
        str(path): int(offset)
        for path, offset in state.get("telemetry_offsets", {}).items()
        if isinstance(path, str) and isinstance(offset, int)
    }
    new_records: list[dict[str, Any]] = []
    updated_offsets: dict[str, int] = {}

    if telemetry_dir.exists():
        for telemetry_file in sorted(telemetry_dir.glob("*.json")):
            previous_offset = offsets.get(str(telemetry_file), 0)
            file_size = telemetry_file.stat().st_size
            if previous_offset > file_size:
                previous_offset = 0
            with telemetry_file.open(encoding="utf-8", errors="replace") as handle:
                handle.seek(previous_offset)
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if telemetry_session_id(payload) != session_id:
                        continue
                    event_id = telemetry_event_id(payload)
                    if event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)
                    new_records.append(payload)
                updated_offsets[str(telemetry_file)] = handle.tell()

    if new_records:
        with telemetry_artifact_path.open("a", encoding="utf-8") as handle:
            for payload in new_records:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    all_records = existing_records + new_records
    all_records = dedupe_telemetry_records(all_records)
    telemetry_state = {
        "telemetry_offsets": updated_offsets if updated_offsets else offsets,
        "telemetry_event_ids": sorted(seen_event_ids),
    }
    return all_records, telemetry_state


def telemetry_session_id(record: dict[str, Any]) -> str | None:
    event_data = record.get("event_data")
    if isinstance(event_data, dict):
        value = event_data.get("session_id")
        return str(value) if value is not None else None
    return None


def telemetry_event_id(record: dict[str, Any]) -> str:
    event_data = record.get("event_data")
    if isinstance(event_data, dict):
        value = event_data.get("event_id")
        if value:
            return str(value)
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def dedupe_telemetry_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in records:
        event_id = telemetry_event_id(record)
        if event_id in seen:
            continue
        seen.add(event_id)
        deduped.append(record)
    return deduped


def build_session_markdown(
    session_id: str,
    session_title: str,
    session_summary: str | None,
    transcript_events: list[TranscriptEvent],
    telemetry_records: list[dict[str, Any]],
    hook_input: dict[str, Any],
    render_ctx: RenderContext,
    transcript_path: Path,
) -> str:
    first_ts = first_known_timestamp(transcript_events)
    last_ts = last_known_timestamp(transcript_events)
    common_meta = derive_common_session_metadata(transcript_events)
    transcript_usage = summarize_transcript_usage(transcript_events)
    telemetry_summary = summarize_telemetry(telemetry_records)
    event_counts = Counter(event.entry_type for event in transcript_events)

    lines: list[str] = [f"# {session_title}", ""]
    lines.append("## Session Metadata")
    lines.extend(
        bullet_lines(
            [
                ("Session ID", session_id),
                ("Transcript path", str(transcript_path)),
                ("Working directory", common_meta.get("cwd")),
                ("Git branch", common_meta.get("gitBranch")),
                ("Started at", format_timestamp(first_ts)),
                ("Last event at", format_timestamp(last_ts)),
                ("Last synced at", format_timestamp(utcnow())),
                ("Last hook event", hook_input.get("hook_event_name")),
            ]
        )
    )
    lines.append("")

    if session_summary:
        lines.append("## Summary")
        lines.extend(render_text_body(session_summary))
        lines.append("")

    lines.append("## Counts")
    lines.extend(
        bullet_lines(
            [
                ("Transcript events", str(len(transcript_events))),
                ("Assistant events", str(event_counts.get("assistant", 0))),
                ("User events", str(event_counts.get("user", 0))),
                ("Subagent events", str(sum(1 for event in transcript_events if is_sidechain_event(event.entry)))),
                ("Telemetry events", str(len(telemetry_records))),
            ]
        )
    )
    lines.append("")

    lines.append("## Transcript Usage")
    lines.extend(
        bullet_lines(
            [
                ("Input tokens", format_int(transcript_usage.get("input_tokens", 0))),
                ("Output tokens", format_int(transcript_usage.get("output_tokens", 0))),
                (
                    "Cache read input tokens",
                    format_int(transcript_usage.get("cache_read_input_tokens", 0)),
                ),
                (
                    "Cache creation input tokens",
                    format_int(transcript_usage.get("cache_creation_input_tokens", 0)),
                ),
            ]
        )
    )
    lines.append("")

    lines.append("## Telemetry Summary")
    lines.extend(
        bullet_lines(
            [
                ("API success events", str(telemetry_summary["api_success_count"])),
                ("Cost USD", f"{telemetry_summary['cost_usd']:.6f}"),
                ("Input tokens", format_int(telemetry_summary["input_tokens"])),
                ("Output tokens", format_int(telemetry_summary["output_tokens"])),
                ("Cached input tokens", format_int(telemetry_summary["cached_input_tokens"])),
                ("Total duration ms", format_int(telemetry_summary["duration_ms_total"])),
                ("Average TTFT ms", format_int(telemetry_summary["average_ttft_ms"])),
                ("Models", ", ".join(telemetry_summary["models"]) or "-"),
            ]
        )
    )
    lines.append("")

    lines.append("## Timeline")
    lines.append("")
    for index, event in enumerate(transcript_events, 1):
        lines.extend(render_event(index, event, render_ctx))
    if not transcript_events:
        lines.append("_No transcript events were found for this session yet._")
        lines.append("")

    lines.append("## Telemetry Events")
    lines.append("")
    if telemetry_records:
        for index, record in enumerate(
            sorted(telemetry_records, key=telemetry_sort_key),
            1,
        ):
            lines.extend(render_telemetry_event(index, record, render_ctx))
    else:
        lines.append("_No telemetry records were captured for this session._")
        lines.append("")

    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"


def build_summary_markdown(
    session_id: str,
    session_title: str,
    session_summary: str | None,
    transcript_events: list[TranscriptEvent],
    telemetry_records: list[dict[str, Any]],
    hook_input: dict[str, Any],
    render_ctx: RenderContext,
    session_markdown_path: Path,
    index_path: Path,
    usage_path: Path,
) -> str:
    first_ts = first_known_timestamp(transcript_events)
    last_ts = last_known_timestamp(transcript_events)
    common_meta = derive_common_session_metadata(transcript_events)
    transcript_usage = summarize_transcript_usage(transcript_events)
    telemetry_summary = summarize_telemetry(telemetry_records)
    models = collect_session_models(transcript_events, telemetry_records)

    lines: list[str] = [f"# {session_title}", ""]
    lines.extend(
        bullet_lines(
            [
                ("Session ID", session_id),
                ("Started at", format_timestamp(first_ts)),
                ("Last event at", format_timestamp(last_ts)),
                ("Last synced at", format_timestamp(utcnow())),
                ("Working directory", common_meta.get("cwd")),
                ("Git branch", common_meta.get("gitBranch")),
                ("Last hook event", hook_input.get("hook_event_name")),
                ("Models", ", ".join(models) or "-"),
                ("Detailed log", f"[Open session detail]({render_ctx.relative_link(session_markdown_path)})"),
                ("Detailed index", f"[Open meta index]({render_ctx.relative_link(index_path)})"),
                ("Usage JSON", f"[Open usage JSON]({render_ctx.relative_link(usage_path)})"),
            ]
        )
    )
    lines.append("")

    if session_summary:
        lines.append("## Session Summary")
        lines.extend(render_text_body(session_summary))
        lines.append("")

    lines.append("## Usage")
    lines.extend(
        bullet_lines(
            [
                ("Transcript input tokens", format_int(transcript_usage.get("input_tokens", 0))),
                ("Transcript output tokens", format_int(transcript_usage.get("output_tokens", 0))),
                (
                    "Transcript cache read input tokens",
                    format_int(transcript_usage.get("cache_read_input_tokens", 0)),
                ),
                (
                    "Transcript cache creation input tokens",
                    format_int(transcript_usage.get("cache_creation_input_tokens", 0)),
                ),
                ("Telemetry API success events", str(telemetry_summary["api_success_count"])),
                ("Telemetry cost USD", f"{telemetry_summary['cost_usd']:.6f}"),
                ("Telemetry input tokens", format_int(telemetry_summary["input_tokens"])),
                ("Telemetry output tokens", format_int(telemetry_summary["output_tokens"])),
                (
                    "Telemetry cached input tokens",
                    format_int(telemetry_summary["cached_input_tokens"]),
                ),
                ("Telemetry total duration ms", format_int(telemetry_summary["duration_ms_total"])),
                ("Telemetry average TTFT ms", format_int(telemetry_summary["average_ttft_ms"])),
            ]
        )
    )
    lines.append("")

    lines.append("## Conversation")
    lines.append("")
    conversation_lines = render_summary_conversation(transcript_events, render_ctx)
    if conversation_lines:
        lines.extend(conversation_lines)
    else:
        lines.append("_No user or assistant content was captured for this session yet._")
        lines.append("")

    system_lines = render_notable_system_events(transcript_events, render_ctx)
    if system_lines:
        lines.append("## Notable System Events")
        lines.append("")
        lines.extend(system_lines)

    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"


def build_usage_payload(
    session_id: str,
    session_title: str,
    session_summary: str | None,
    transcript_events: list[TranscriptEvent],
    telemetry_records: list[dict[str, Any]],
    hook_input: dict[str, Any],
    transcript_path: Path,
    session_markdown_path: Path,
    summary_path: Path,
    index_path: Path,
    telemetry_artifact_path: Path,
) -> dict[str, Any]:
    first_ts = first_known_timestamp(transcript_events)
    last_ts = last_known_timestamp(transcript_events)
    common_meta = derive_common_session_metadata(transcript_events)
    transcript_usage = summarize_transcript_usage(transcript_events)
    telemetry_summary = summarize_telemetry(telemetry_records)
    event_counts = Counter(event.entry_type for event in transcript_events)

    return {
        "version": 1,
        "session": {
            "id": session_id,
            "title": session_title,
            "summary": session_summary,
            "started_at": iso_or_none(first_ts),
            "last_event_at": iso_or_none(last_ts),
            "last_synced_at": iso_or_none(utcnow()),
            "cwd": common_meta.get("cwd"),
            "git_branch": common_meta.get("gitBranch"),
            "last_hook_event": hook_input.get("hook_event_name"),
            "transcript_path": str(transcript_path),
        },
        "paths": {
            "summary_md": str(summary_path),
            "session_md": str(session_markdown_path),
            "meta_index_md": str(index_path),
            "telemetry_jsonl": str(telemetry_artifact_path),
        },
        "models": collect_session_models(transcript_events, telemetry_records),
        "counts": {
            "transcript_events": len(transcript_events),
            "assistant_events": int(event_counts.get("assistant", 0)),
            "user_events": int(event_counts.get("user", 0)),
            "sidechain_events": sum(1 for event in transcript_events if is_sidechain_event(event.entry)),
            "telemetry_events": len(telemetry_records),
        },
        "transcript_usage": {
            "input_tokens": int(transcript_usage.get("input_tokens", 0)),
            "output_tokens": int(transcript_usage.get("output_tokens", 0)),
            "cache_read_input_tokens": int(transcript_usage.get("cache_read_input_tokens", 0)),
            "cache_creation_input_tokens": int(
                transcript_usage.get("cache_creation_input_tokens", 0)
            ),
        },
        "telemetry": {
            "api_success_count": int(telemetry_summary["api_success_count"]),
            "cost_usd": float(telemetry_summary["cost_usd"]),
            "input_tokens": int(telemetry_summary["input_tokens"]),
            "output_tokens": int(telemetry_summary["output_tokens"]),
            "cached_input_tokens": int(telemetry_summary["cached_input_tokens"]),
            "duration_ms_total": int(telemetry_summary["duration_ms_total"]),
            "average_ttft_ms": int(telemetry_summary["average_ttft_ms"]),
            "models": list(telemetry_summary["models"]),
        },
    }


def render_summary_conversation(
    transcript_events: list[TranscriptEvent],
    render_ctx: RenderContext,
) -> list[str]:
    lines: list[str] = []
    visible_index = 0
    for event in transcript_events:
        if event.entry_type not in {"user", "assistant"}:
            continue
        event_lines = render_summary_message_event(event, render_ctx)
        if not event_lines:
            continue
        visible_index += 1
        lines.append(
            f"### {visible_index:03d}. {format_timestamp(event.timestamp)} {summary_actor_label(event)}"
        )
        lines.append("")
        lines.extend(event_lines)
        lines.append("")
    return lines


def render_summary_message_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    entry = event.entry
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    role = str(message.get("role") or entry.get("type") or "unknown")
    content_items = normalize_message_content(message.get("content"))
    lines: list[str] = []

    if role == "assistant":
        for item_index, item in enumerate(content_items, 1):
            content_type = str(item.get("type", "unknown"))
            if content_type == "thinking":
                lines.extend(
                    render_summary_text_item(
                        title="Thinking",
                        text=str(item.get("thinking", "")),
                        render_ctx=render_ctx,
                        artifact_prefix=f"{event.source_label}-summary-thinking-{event.line_no}-{item_index}",
                        inline_limit=SUMMARY_TEXT_INLINE_LIMIT,
                    )
                )
            elif content_type == "text":
                lines.extend(
                    render_summary_text_item(
                        title="Output",
                        text=str(item.get("text", "")),
                        render_ctx=render_ctx,
                        artifact_prefix=f"{event.source_label}-summary-output-{event.line_no}-{item_index}",
                        inline_limit=SUMMARY_TEXT_INLINE_LIMIT,
                    )
                )
            elif content_type == "tool_use":
                lines.extend(
                    render_summary_tool_use_item(
                        event=event,
                        item_index=item_index,
                        item=item,
                        render_ctx=render_ctx,
                    )
                )
        return lines

    for item_index, item in enumerate(content_items, 1):
        content_type = str(item.get("type", "unknown"))
        if content_type == "text":
            lines.extend(
                render_summary_text_item(
                    title="User",
                    text=str(item.get("text", "")),
                    render_ctx=render_ctx,
                    artifact_prefix=f"{event.source_label}-summary-user-{event.line_no}-{item_index}",
                    inline_limit=SUMMARY_TEXT_INLINE_LIMIT,
                )
            )
        elif content_type == "tool_result":
            lines.extend(
                render_summary_tool_result_item(
                    event=event,
                    item_index=item_index,
                    item=item,
                    render_ctx=render_ctx,
                )
            )
    if (
        "toolUseResult" in entry
        and not any(str(item.get("type")) == "tool_result" for item in content_items)
    ):
        lines.extend(
            render_summary_tool_result_value(
                title="Tool Result",
                value=entry.get("toolUseResult"),
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-summary-tool-use-result-{event.line_no}",
            )
        )
    return lines


def render_summary_text_item(
    title: str,
    text: str,
    render_ctx: RenderContext,
    artifact_prefix: str,
    inline_limit: int,
) -> list[str]:
    lines = [f"#### {title}"]
    if len(text) > inline_limit:
        artifact_path = write_text_artifact(render_ctx, artifact_prefix, text)
        lines.extend(render_text_body(excerpt(text, 400)))
        lines.append(f"[Open full artifact]({render_ctx.relative_link(artifact_path)})")
        lines.append("")
        return lines
    lines.extend(render_text_body(text))
    return lines


def render_summary_tool_use_item(
    event: TranscriptEvent,
    item_index: int,
    item: dict[str, Any],
    render_ctx: RenderContext,
) -> list[str]:
    lines = [f"#### Tool Call `{item.get('name') or 'unknown'}`"]
    input_value = item.get("input")
    input_summary = summarize_tool_input(input_value)
    lines.extend(
        bullet_lines(
            [
                ("Call ID", item.get("id")),
                ("Input summary", input_summary),
            ]
        )
    )
    serialized, extension = serialize_value(input_value)
    if len(serialized) > SUMMARY_VALUE_INLINE_LIMIT:
        artifact_path = write_text_artifact(
            render_ctx,
            f"{event.source_label}-summary-tool-input-{event.line_no}-{item_index}",
            serialized,
            extension=extension,
        )
        lines.append(f"[Open tool input artifact]({render_ctx.relative_link(artifact_path)})")
    elif serialized not in {"(empty)", ""}:
        lines.extend(code_block(serialized, "json" if extension == "json" else "text"))
        return lines
    lines.append("")
    return lines


def render_summary_tool_result_item(
    event: TranscriptEvent,
    item_index: int,
    item: dict[str, Any],
    render_ctx: RenderContext,
) -> list[str]:
    value = item.get("content")
    lines = [f"#### Tool Result `{item.get('tool_use_id') or 'unknown'}`"]
    lines.extend(
        bullet_lines(
            [
                (
                    "Is error",
                    str(item.get("is_error")) if item.get("is_error") is not None else None,
                ),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_summary_tool_result_value(
            title="Result Summary",
            value=value,
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-summary-tool-result-{event.line_no}-{item_index}",
        )
    )
    return lines


def render_summary_tool_result_value(
    title: str,
    value: Any,
    render_ctx: RenderContext,
    artifact_prefix: str,
) -> list[str]:
    lines = [f"#### {title}"]
    serialized, extension = serialize_value(value)
    summary_text = excerpt(serialized, 400) if serialized not in {"", "(empty)"} else "(empty)"
    lines.extend(render_text_body(summary_text))
    if len(serialized) > SUMMARY_VALUE_INLINE_LIMIT:
        artifact_path = write_text_artifact(
            render_ctx,
            artifact_prefix,
            serialized,
            extension=extension,
        )
        lines.append(f"[Open full artifact]({render_ctx.relative_link(artifact_path)})")
        lines.append("")
    return lines


def render_notable_system_events(
    transcript_events: list[TranscriptEvent],
    render_ctx: RenderContext,
) -> list[str]:
    lines: list[str] = []
    visible_index = 0
    for event in transcript_events:
        if not is_notable_system_event(event):
            continue
        visible_index += 1
        entry = event.entry
        lines.append(
            f"### S{visible_index:03d}. {format_timestamp(event.timestamp)} `{entry.get('subtype') or event.entry_type}`"
        )
        lines.append("")
        lines.extend(
            bullet_lines(
                [
                    ("Level", entry.get("level")),
                    ("Error", entry.get("error")),
                ]
            )
        )
        content = entry.get("content")
        if content not in (None, ""):
            lines.append("")
            lines.extend(
                render_summary_text_item(
                    title="Details",
                    text=str(content),
                    render_ctx=render_ctx,
                    artifact_prefix=f"{event.source_label}-summary-system-{event.line_no}",
                    inline_limit=SUMMARY_VALUE_INLINE_LIMIT,
                )
            )
        lines.append("")
    return lines


def render_event(index: int, event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    entry = event.entry
    event_type = event.entry_type
    source_suffix = ""
    if event.source_label != "main":
        source_suffix = f" [{event.source_label}]"
    header = f"### {index:03d}. {format_timestamp(event.timestamp)} `{event_type}`{source_suffix}"
    lines = [header, ""]
    if event_type in {"user", "assistant"}:
        lines.extend(render_message_event(event, render_ctx))
    elif event_type == "summary":
        lines.extend(render_summary_event(event))
    elif event_type == "system":
        lines.extend(render_system_event(event, render_ctx))
    elif event_type == "progress":
        lines.extend(render_progress_event(event, render_ctx))
    elif event_type == "file-history-snapshot":
        lines.extend(render_snapshot_event(event, render_ctx))
    elif event_type == "queue-operation":
        lines.extend(render_queue_event(event, render_ctx))
    elif event_type == "last-prompt":
        lines.extend(render_last_prompt_event(event, render_ctx))
    else:
        lines.extend(render_generic_event(event, render_ctx))
    lines.append("")
    return lines


def render_message_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    entry = event.entry
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    role = message.get("role") or entry.get("type") or "unknown"
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Role", str(role)),
                ("Model", message.get("model")),
                ("Message ID", message.get("id")),
                ("UUID", entry.get("uuid")),
                ("Parent UUID", entry.get("parentUuid")),
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
                ("Working directory", entry.get("cwd")),
                ("Git branch", entry.get("gitBranch")),
                ("Sidechain", str(entry.get("isSidechain")) if "isSidechain" in entry else None),
                ("Agent ID", entry.get("agentId")),
                ("Stop reason", message.get("stop_reason")),
                ("Stop sequence", message.get("stop_sequence")),
                ("Entry error", entry.get("error")),
                (
                    "API error message",
                    str(entry.get("isApiErrorMessage"))
                    if entry.get("isApiErrorMessage") is not None
                    else None,
                ),
            ]
        )
    )
    if usage:
        lines.append("#### Usage")
        lines.extend(
            bullet_lines(
                [
                    ("Input tokens", format_int(usage.get("input_tokens", 0))),
                    ("Output tokens", format_int(usage.get("output_tokens", 0))),
                    (
                        "Cache creation input tokens",
                        format_int(usage.get("cache_creation_input_tokens", 0)),
                    ),
                    (
                        "Cache read input tokens",
                        format_int(usage.get("cache_read_input_tokens", 0)),
                    ),
                    ("Service tier", usage.get("service_tier")),
                ]
            )
        )
        lines.append("")

    todos = entry.get("todos")
    if isinstance(todos, list) and todos:
        lines.append("#### Todos")
        for item in todos:
            if not isinstance(item, dict):
                lines.append(f"- {item}")
                continue
            status = item.get("status", "unknown")
            content = item.get("content") or item.get("activeForm") or "(empty)"
            lines.append(f"- [{status}] {content}")
        lines.append("")

    for item_index, item in enumerate(normalize_message_content(message.get("content")), 1):
        lines.extend(render_content_item(event, item_index, item, render_ctx))

    if "toolUseResult" in entry:
        lines.extend(
            render_value_section(
                title="toolUseResult",
                value=entry.get("toolUseResult"),
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-tool-use-result-{event.line_no}",
                inline_limit=TOOL_RESULT_INLINE_LIMIT,
            )
        )

    extra_fields = {
        key: value
        for key, value in entry.items()
        if key not in COMMON_ENTRY_KEYS
    }
    if extra_fields:
        lines.extend(
            render_value_section(
                title="Extra fields",
                value=extra_fields,
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-message-extra-{event.line_no}",
                inline_limit=JSON_INLINE_LIMIT,
            )
        )
    return lines


def render_content_item(
    event: TranscriptEvent,
    item_index: int,
    item: dict[str, Any],
    render_ctx: RenderContext,
) -> list[str]:
    content_type = str(item.get("type", "unknown"))
    title = f"Content {item_index} `{content_type}`"
    if content_type == "text":
        return render_text_section(
            title=title,
            text=str(item.get("text", "")),
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-text-{event.line_no}-{item_index}",
            inline_limit=TEXT_INLINE_LIMIT,
        )
    if content_type == "thinking":
        return render_text_section(
            title=title,
            text=str(item.get("thinking", "")),
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-thinking-{event.line_no}-{item_index}",
            inline_limit=TEXT_INLINE_LIMIT,
            code_fence=False,
        )
    if content_type == "tool_use":
        lines: list[str] = [f"#### {title}"]
        lines.extend(
            bullet_lines(
                [
                    ("Name", item.get("name")),
                    ("Call ID", item.get("id")),
                ]
            )
        )
        lines.append("")
        lines.extend(
            render_value_section(
                title="Tool input",
                value=item.get("input"),
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-tool-input-{event.line_no}-{item_index}",
                inline_limit=JSON_INLINE_LIMIT,
            )
        )
        return lines
    if content_type == "tool_result":
        lines: list[str] = [f"#### {title}"]
        lines.extend(
            bullet_lines(
                [
                    ("Tool use ID", item.get("tool_use_id")),
                    (
                        "Is error",
                        str(item.get("is_error")) if item.get("is_error") is not None else None,
                    ),
                    ("Agent ID", item.get("agentId")),
                ]
            )
        )
        lines.append("")
        lines.extend(
            render_value_section(
                title="Tool result",
                value=item.get("content"),
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-tool-result-{event.line_no}-{item_index}",
                inline_limit=TOOL_RESULT_INLINE_LIMIT,
            )
        )
        return lines
    if content_type == "image":
        return render_image_section(
            title=title,
            image_item=item,
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-image-{event.line_no}-{item_index}",
        )
    return render_value_section(
        title=title,
        value=item,
        render_ctx=render_ctx,
        artifact_prefix=f"{event.source_label}-unknown-content-{event.line_no}-{item_index}",
        inline_limit=JSON_INLINE_LIMIT,
    )


def render_summary_event(event: TranscriptEvent) -> list[str]:
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Leaf UUID", event.entry.get("leafUuid")),
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
            ]
        )
    )
    lines.append("")
    lines.append("#### Summary")
    lines.extend(render_text_body(str(event.entry.get("summary", ""))))
    return lines


def render_system_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    entry = event.entry
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Subtype", entry.get("subtype")),
                ("Level", entry.get("level")),
                ("UUID", entry.get("uuid")),
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
                ("Slug", entry.get("slug")),
                ("Duration ms", format_int(entry.get("durationMs")) if entry.get("durationMs") is not None else None),
                ("Retry in ms", format_int(entry.get("retryInMs")) if entry.get("retryInMs") is not None else None),
                (
                    "Retry attempt",
                    format_int(entry.get("retryAttempt")) if entry.get("retryAttempt") is not None else None,
                ),
                (
                    "Max retries",
                    format_int(entry.get("maxRetries")) if entry.get("maxRetries") is not None else None,
                ),
                ("Error", entry.get("error")),
            ]
        )
    )
    if entry.get("content") is not None:
        lines.append("")
        lines.extend(
            render_value_section(
                title="System content",
                value=entry.get("content"),
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-system-content-{event.line_no}",
                inline_limit=TEXT_INLINE_LIMIT,
            )
        )
    extra_fields = {
        key: value
        for key, value in entry.items()
        if key not in COMMON_ENTRY_KEYS
        and key
        not in {
            "subtype",
            "level",
            "slug",
            "durationMs",
            "retryInMs",
            "retryAttempt",
            "maxRetries",
            "content",
        }
    }
    if extra_fields:
        lines.extend(
            render_value_section(
                title="Extra system fields",
                value=extra_fields,
                render_ctx=render_ctx,
                artifact_prefix=f"{event.source_label}-system-extra-{event.line_no}",
                inline_limit=JSON_INLINE_LIMIT,
            )
        )
    return lines


def render_progress_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    entry = event.entry
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Tool use ID", entry.get("toolUseID")),
                ("Parent tool use ID", entry.get("parentToolUseID")),
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_value_section(
            title="Progress payload",
            value=entry.get("data"),
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-progress-{event.line_no}",
            inline_limit=JSON_INLINE_LIMIT,
        )
    )
    return lines


def render_snapshot_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    snapshot = event.entry.get("snapshot") if isinstance(event.entry.get("snapshot"), dict) else {}
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Message ID", event.entry.get("messageId")),
                ("Snapshot message ID", snapshot.get("messageId")),
                ("Is snapshot update", str(event.entry.get("isSnapshotUpdate"))),
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_value_section(
            title="Tracked file backups",
            value=snapshot.get("trackedFileBackups", {}),
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-snapshot-{event.line_no}",
            inline_limit=JSON_INLINE_LIMIT,
        )
    )
    return lines


def render_queue_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    entry = event.entry
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Operation", entry.get("operation")),
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_value_section(
            title="Queue content",
            value=entry.get("content"),
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-queue-{event.line_no}",
            inline_limit=TEXT_INLINE_LIMIT,
        )
    )
    return lines


def render_last_prompt_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_text_section(
            title="Last prompt",
            text=str(event.entry.get("lastPrompt", "")),
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-last-prompt-{event.line_no}",
            inline_limit=TEXT_INLINE_LIMIT,
            code_fence=False,
        )
    )
    return lines


def render_generic_event(event: TranscriptEvent, render_ctx: RenderContext) -> list[str]:
    lines: list[str] = []
    lines.extend(
        bullet_lines(
            [
                ("Source line", str(event.line_no)),
                ("Source file", event.source_path.name),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_value_section(
            title="Raw event",
            value=event.entry,
            render_ctx=render_ctx,
            artifact_prefix=f"{event.source_label}-raw-event-{event.line_no}",
            inline_limit=JSON_INLINE_LIMIT,
        )
    )
    return lines


def render_telemetry_event(
    index: int,
    record: dict[str, Any],
    render_ctx: RenderContext,
) -> list[str]:
    event_data = record.get("event_data") if isinstance(record.get("event_data"), dict) else {}
    event_name = event_data.get("event_name") or "unknown"
    timestamp = parse_timestamp(event_data.get("client_timestamp"))
    additional_metadata = parse_embedded_json(event_data.get("additional_metadata"))
    process_payload = parse_embedded_json(event_data.get("process"))

    lines: list[str] = [f"### T{index:03d}. {format_timestamp(timestamp)} `{event_name}`", ""]
    lines.extend(
        bullet_lines(
            [
                ("Event ID", event_data.get("event_id")),
                ("Model", event_data.get("model")),
                ("Entry point", event_data.get("entrypoint")),
                ("Client type", event_data.get("client_type")),
                ("User type", event_data.get("user_type")),
                ("Session ID", event_data.get("session_id")),
            ]
        )
    )
    lines.append("")
    lines.extend(
        render_value_section(
            title="Additional metadata",
            value=additional_metadata,
            render_ctx=render_ctx,
            artifact_prefix=f"telemetry-meta-{index}",
            inline_limit=LARGE_TELEMETRY_INLINE_LIMIT,
        )
    )
    if process_payload not in ({}, None, ""):
        lines.extend(
            render_value_section(
                title="Process snapshot",
                value=process_payload,
                render_ctx=render_ctx,
                artifact_prefix=f"telemetry-process-{index}",
                inline_limit=LARGE_TELEMETRY_INLINE_LIMIT,
            )
        )
    return lines


def render_text_section(
    title: str,
    text: str,
    render_ctx: RenderContext,
    artifact_prefix: str,
    inline_limit: int,
    code_fence: bool = False,
) -> list[str]:
    lines = [f"#### {title}"]
    if len(text) > inline_limit:
        artifact_path = write_text_artifact(render_ctx, artifact_prefix, text)
        lines.append(f"[Open artifact]({render_ctx.relative_link(artifact_path)}) ({len(text)} chars)")
        lines.append("")
        return lines
    if code_fence:
        lines.extend(code_block(text or "(empty)"))
    else:
        lines.extend(render_text_body(text))
    return lines


def render_value_section(
    title: str,
    value: Any,
    render_ctx: RenderContext,
    artifact_prefix: str,
    inline_limit: int,
) -> list[str]:
    lines = [f"#### {title}"]
    serialized, extension = serialize_value(value)
    if len(serialized) > inline_limit:
        artifact_path = write_text_artifact(render_ctx, artifact_prefix, serialized, extension=extension)
        lines.append(f"[Open artifact]({render_ctx.relative_link(artifact_path)}) ({len(serialized)} chars)")
        lines.append("")
        return lines
    if extension == "json":
        lines.extend(code_block(serialized, "json"))
    else:
        lines.extend(code_block(serialized, "text"))
    return lines


def render_image_section(
    title: str,
    image_item: dict[str, Any],
    render_ctx: RenderContext,
    artifact_prefix: str,
) -> list[str]:
    lines = [f"#### {title}"]
    source = image_item.get("source") if isinstance(image_item.get("source"), dict) else {}
    media_type = str(source.get("media_type", "application/octet-stream"))
    data = source.get("data")
    extension = IMAGE_EXTENSIONS.get(media_type, "bin")

    if isinstance(data, str):
        try:
            decoded = base64.b64decode(data, validate=False)
            artifact_path = render_ctx.artifact_store.write_bytes(
                artifact_prefix,
                decoded,
                extension,
            )
        except (ValueError, binascii.Error):
            artifact_path = write_text_artifact(
                render_ctx,
                artifact_prefix,
                data,
                extension="txt",
            )
    else:
        artifact_path = write_text_artifact(
            render_ctx,
            artifact_prefix,
            json.dumps(image_item, ensure_ascii=False, indent=2),
            extension="json",
        )

    lines.extend(
        bullet_lines(
            [
                ("Media type", media_type),
                ("Artifact", f"[{artifact_path.name}]({render_ctx.relative_link(artifact_path)})"),
            ]
        )
    )
    lines.append("")
    return lines


def render_text_body(text: str) -> list[str]:
    if not text:
        return ["_Empty_", ""]
    return [f"> {line}" if line else ">" for line in text.splitlines()] + [""]


def normalize_message_content(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        normalized: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                normalized.append(item)
            else:
                normalized.append({"type": "text", "text": str(item)})
        return normalized
    if isinstance(content, dict):
        return [content]
    return [{"type": "text", "text": str(content)}]


def serialize_value(value: Any) -> tuple[str, str]:
    if value is None:
        return "(empty)", "txt"
    if isinstance(value, str):
        return value, "txt"
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), "json"
    return str(value), "txt"


def summarize_transcript_usage(events: Iterable[TranscriptEvent]) -> dict[str, int]:
    totals = Counter()
    for event in events:
        if event.entry_type != "assistant":
            continue
        message = event.entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        totals["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
        totals["cache_creation_input_tokens"] += int(
            usage.get("cache_creation_input_tokens") or 0
        )
    return dict(totals)


def summarize_telemetry(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "api_success_count": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "duration_ms_total": 0,
        "average_ttft_ms": 0,
        "models": [],
    }
    models: Counter[str] = Counter()
    ttft_values: list[int] = []

    for record in records:
        event_data = record.get("event_data")
        if not isinstance(event_data, dict):
            continue
        event_name = event_data.get("event_name")
        model = event_data.get("model")
        if model:
            models[str(model)] += 1
        if event_name != "tengu_api_success":
            continue
        totals["api_success_count"] += 1
        additional_metadata = parse_embedded_json(event_data.get("additional_metadata"))
        if not isinstance(additional_metadata, dict):
            continue
        totals["cost_usd"] += float(additional_metadata.get("costUSD") or 0.0)
        totals["input_tokens"] += int(additional_metadata.get("inputTokens") or 0)
        totals["output_tokens"] += int(additional_metadata.get("outputTokens") or 0)
        totals["cached_input_tokens"] += int(
            additional_metadata.get("cachedInputTokens") or 0
        )
        totals["duration_ms_total"] += int(additional_metadata.get("durationMs") or 0)
        ttft = additional_metadata.get("ttftMs")
        if ttft is not None:
            ttft_values.append(int(ttft))

    totals["models"] = sorted(models)
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    if ttft_values:
        totals["average_ttft_ms"] = round(sum(ttft_values) / len(ttft_values))
    return totals


def derive_common_session_metadata(events: Iterable[TranscriptEvent]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for event in events:
        entry = event.entry
        for key in ("cwd", "gitBranch"):
            if metadata.get(key) is None and entry.get(key):
                metadata[key] = entry.get(key)
    return metadata


def collect_session_models(
    transcript_events: Iterable[TranscriptEvent],
    telemetry_records: Iterable[dict[str, Any]],
) -> list[str]:
    models: set[str] = set()
    for event in transcript_events:
        message = event.entry.get("message") if isinstance(event.entry.get("message"), dict) else {}
        model = message.get("model")
        if model:
            models.add(str(model))
    for record in telemetry_records:
        event_data = record.get("event_data") if isinstance(record.get("event_data"), dict) else {}
        model = event_data.get("model")
        if model:
            models.add(str(model))
    return sorted(models)


def is_meaningful_title_candidate(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return False
    if compact in {".", "..", "...", "。", "-", "--", "_", "/", "\\"}:
        return False
    if len(compact) <= 2 and not any(char.isalnum() for char in compact):
        return False
    return True


def derive_session_title(events: Iterable[TranscriptEvent]) -> str:
    for event in events:
        if event.entry_type == "summary" and event.entry.get("summary"):
            return f"Claude Session: {excerpt(str(event.entry['summary']), 80)}"
    preferred_events = [event for event in events if not is_sidechain_event(event.entry)]
    for candidate_events in (preferred_events, list(events)):
        for event in candidate_events:
            if event.entry_type != "user":
                continue
            message = event.entry.get("message")
            if not isinstance(message, dict):
                continue
            for item in normalize_message_content(message.get("content")):
                if item.get("type") != "text":
                    continue
                text = str(item.get("text", ""))
                if not is_meaningful_title_candidate(text):
                    continue
                return f"Claude Session: {excerpt(text, 80)}"
    return (
        f"Claude Session: "
        f"{next(iter([event.entry.get('sessionId') for event in events if event.entry.get('sessionId')]), 'Untitled Session')}"
    )


def derive_session_summary(events: Iterable[TranscriptEvent]) -> str | None:
    for event in events:
        if event.entry_type == "summary" and event.entry.get("summary"):
            return str(event.entry["summary"])
    return None


def first_known_timestamp(events: Iterable[TranscriptEvent]) -> datetime | None:
    for event in events:
        if event.timestamp is not None:
            return event.timestamp
    return None


def last_known_timestamp(events: Iterable[TranscriptEvent]) -> datetime | None:
    latest: datetime | None = None
    for event in events:
        if event.timestamp is None:
            continue
        if latest is None or event.timestamp > latest:
            latest = event.timestamp
    return latest


def default_markdown_relpath(session_id: str, timestamp: datetime) -> str:
    return f"meta/sessions/{timestamp.year:04d}/{timestamp.month:02d}/{session_id}.md"


def default_summary_relpaths(session_id: str) -> tuple[str, str, str]:
    summary_dir_relpath = f"summary/{session_id}"
    return (
        summary_dir_relpath,
        f"{summary_dir_relpath}/summary.md",
        f"{summary_dir_relpath}/usage.json",
    )


def resolve_summary_relpaths(
    session_id: str,
    state: dict[str, Any],
) -> tuple[str, str, str]:
    summary_dir_relpath = state.get("summary_dir_relpath")
    summary_markdown_relpath = state.get("summary_markdown_relpath")
    usage_relpath = state.get("usage_relpath")
    if (
        isinstance(summary_dir_relpath, str)
        and summary_dir_relpath
        and isinstance(summary_markdown_relpath, str)
        and summary_markdown_relpath
        and isinstance(usage_relpath, str)
        and usage_relpath
    ):
        return summary_dir_relpath, summary_markdown_relpath, usage_relpath
    return default_summary_relpaths(session_id)


def build_state_payload(
    old_state: dict[str, Any],
    session_id: str,
    markdown_relpath: str,
    summary_dir_relpath: str,
    summary_markdown_relpath: str,
    usage_relpath: str,
    session_title: str,
    session_summary: str | None,
    transcript_events: list[TranscriptEvent],
    telemetry_state: dict[str, Any],
    transcript_path: Path,
    project_root: Path,
) -> dict[str, Any]:
    first_ts = first_known_timestamp(transcript_events)
    last_ts = last_known_timestamp(transcript_events)
    event_counts = Counter(event.entry_type for event in transcript_events)
    common_meta = derive_common_session_metadata(transcript_events)
    payload = {
        "version": STATE_VERSION,
        "session_id": session_id,
        "title": session_title,
        "summary": session_summary,
        "markdown_relpath": markdown_relpath,
        "summary_dir_relpath": summary_dir_relpath,
        "summary_markdown_relpath": summary_markdown_relpath,
        "usage_relpath": usage_relpath,
        "project_root": str(project_root),
        "cwd": common_meta.get("cwd"),
        "gitBranch": common_meta.get("gitBranch"),
        "session_started_at": format_timestamp(first_ts),
        "last_event_at": format_timestamp(last_ts),
        "last_synced_at": format_timestamp(utcnow()),
        "event_counts": dict(event_counts),
        "transcript_cursor": {
            "path": str(transcript_path),
            "size": transcript_path.stat().st_size if transcript_path.exists() else 0,
            "mtime_ns": transcript_path.stat().st_mtime_ns if transcript_path.exists() else 0,
        },
        "telemetry_offsets": telemetry_state.get("telemetry_offsets", {}),
        "telemetry_event_ids": telemetry_state.get("telemetry_event_ids", []),
    }
    return payload


def write_index(log_root: Path) -> Path:
    meta_root = log_root / "meta"
    state_dir = meta_root / "state"
    index_path = meta_root / "index.md"
    states: list[dict[str, Any]] = []
    if state_dir.exists():
        for state_file in state_dir.glob("*.json"):
            state = load_state(state_file)
            if state:
                states.append(state)

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (item.get("last_synced_at") or "", item.get("session_id") or "")

    lines = ["# Claude Session Meta Index", "", "_Detailed and machine-oriented session artifacts._", ""]
    lines.extend(
        bullet_lines(
            [
                ("Updated at", format_timestamp(utcnow())),
                ("Sessions", str(len(states))),
            ]
        )
    )
    lines.append("")

    for state in sorted(states, key=sort_key, reverse=True):
        relpath = state.get("markdown_relpath")
        if not relpath:
            continue
        detail_path = log_root / str(relpath)
        relative_detail_path = os.path.relpath(detail_path, index_path.parent)
        title = state.get("title") or state.get("session_id") or "Untitled Session"
        lines.append(f"## [{title}]({relative_detail_path})")
        lines.append("")
        lines.extend(
            bullet_lines(
                [
                    ("Session ID", state.get("session_id")),
                    ("Last synced", state.get("last_synced_at")),
                    ("Started at", state.get("session_started_at")),
                    ("Working directory", state.get("cwd")),
                    ("Git branch", state.get("gitBranch")),
                ]
            )
        )
        if state.get("summary"):
            lines.append("> " + str(state["summary"]).replace("\n", "\n> "))
            lines.append("")
    write_text(index_path, "\n".join(lines).rstrip() + "\n")
    return index_path


def load_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def parse_embedded_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def telemetry_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    event_data = record.get("event_data") if isinstance(record.get("event_data"), dict) else {}
    timestamp = parse_timestamp(event_data.get("client_timestamp")) or datetime.max.replace(
        tzinfo=timezone.utc
    )
    event_id = telemetry_event_id(record)
    return (timestamp, event_id)


def is_sidechain_event(entry: dict[str, Any]) -> bool:
    return bool(entry.get("isSidechain"))


def summary_actor_label(event: TranscriptEvent) -> str:
    message = event.entry.get("message") if isinstance(event.entry.get("message"), dict) else {}
    role = str(message.get("role") or event.entry_type or "unknown").title()
    if is_sidechain_event(event.entry):
        agent_id = event.entry.get("agentId") or event.source_label
        return f"{role} [{agent_id}]"
    return role


def summarize_tool_input(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("command", "description", "prompt", "query", "path", "file_path"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                return excerpt(str(candidate), 200)
    serialized, _ = serialize_value(value)
    return excerpt(serialized, 200)


def is_notable_system_event(event: TranscriptEvent) -> bool:
    if event.entry_type != "system":
        return False
    entry = event.entry
    return bool(
        entry.get("error")
        or entry.get("level") in {"warning", "error", "critical"}
        or entry.get("subtype") in {"api_error", "local_command"}
    )


def write_text_artifact(
    render_ctx: RenderContext,
    artifact_prefix: str,
    content: str,
    extension: str = "txt",
) -> Path:
    return render_ctx.artifact_store.write_text(artifact_prefix, content, extension=extension)


def code_block(text: str, language: str = "") -> list[str]:
    fence = f"```{language}".rstrip()
    return [fence, text, "```", ""]


def bullet_lines(items: Iterable[tuple[str, Any]]) -> list[str]:
    lines: list[str] = []
    for label, value in items:
        if value in (None, ""):
            continue
        lines.append(f"- {label}: {value}")
    return lines


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def write_plugin_error(log_root: Path, exc: Exception) -> None:
    log_root.mkdir(parents=True, exist_ok=True)
    error_log = log_root / "plugin-errors.log"
    timestamp = format_timestamp(utcnow())
    with error_log.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {type(exc).__name__}: {exc}\n")


def excerpt(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    return slug.lower()


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    converted = value.astimezone(timezone.utc)
    return converted.isoformat().replace("+00:00", "Z")


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return format_timestamp(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


if __name__ == "__main__":
    raise SystemExit(main())
