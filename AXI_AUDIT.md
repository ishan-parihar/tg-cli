# AXI Compliance Audit — tg-cli

**Audit Date**: 2025-01-15
**CLI Version**: 0.6.0
**Auditor**: AXI Skill Evaluation

---

## Executive Summary

**Overall Compliance: 35/100** — Significant gaps across all 10 AXI principles. The CLI has structured JSON/YAML output and a decent error schema, but lacks TOON format, content-first design, minimal schemas, truncation, aggregates, and contextual discovery.

---

## Principle-by-Principle Assessment

### §1 Token-Efficient Output (TOON) — ❌ FAIL
- **Current**: JSON and YAML only
- **Required**: TOON as primary machine format
- **Gap**: No TOON serializer; agents pay ~40% token premium

### §2 Minimal Default Schemas — ❌ FAIL
- **Current**: Full object dumps (e.g., `chats` returns id, name, type, unread, username, etc.)
- **Required**: 3-4 fields default (id, title, status)
- **Gap**: No `--fields` flag; all fields always returned

### §3 Content Truncation — ❌ FAIL
- **Current**: Full content in detail views (`info`, `history`, `search` results)
- **Required**: Truncate at 500-1500 chars with escape hatch (`--full`)
- **Gap**: No truncation logic; large message bodies flood output

### §4 Pre-Computed Aggregates — ❌ FAIL
- **Current**: Lists return page only (e.g., `stats` returns `total` but `chats` doesn't include total count)
- **Required**: `count: N of M total` in headers; derived status fields inline
- **Gap**: Agents must paginate/count manually

### §5 Definitive Empty States — ⚠️ PARTIAL
- **Current**: Some commands return `[]` in structured mode; rich mode prints "No messages found"
- **Required**: Explicit "0 X found in Y" with context
- **Gap**: Inconsistent; structured empty arrays are ambiguous

### §6 Structured Errors & Exit Codes — ⚠️ PARTIAL
- **Current**: Structured error payload on stdout for `--json/--yaml`; rich errors to console
- **Required**: All errors on stdout in structured format; idempotent mutations exit 0
- **Gap**: 
  - Progress messages leak to stdout (spinners via Rich)
  - No idempotent mutation checks (e.g., `delete` on non-existent msg)
  - Exit codes: 0/1 only (no 2 for usage errors)

### §7 Ambient Context / Session Integration — ❌ FAIL
- **Current**: No session hooks, no setup command
- **Required**: Installable hook for Claude Code/Codex/OpenCode; skill package

### §8 Content First (No-Args Home View) — ❌ FAIL
- **Current**: `tg` → shows usage/help (exit 2)
- **Required**: `tg` → shows live dashboard (recent chats, unread counts, quick actions)

### §9 Contextual Disclosure — ❌ FAIL
- **Current**: No next-step hints after any command
- **Required**: 2-3 relevant, actionable suggestions per output

### §10 Consistent Help — ⚠️ PARTIAL
- **Current**: `--help` works but no examples; no bin path/description in home view
- **Required**: Bin path, 1-line description, 2-3 usage examples per subcommand

---

## Command-Level Findings

| Command | TOON | Minimal Schema | Truncation | Aggregates | Empty State | Contextual Hints |
|---------|------|----------------|------------|------------|-------------|------------------|
| `chats` | ❌ | ❌ | N/A | ❌ | ❌ | ❌ |
| `history` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `sync` | ❌ | N/A | N/A | ❌ | N/A | ❌ |
| `sync-all` | ❌ | N/A | N/A | ❌ | N/A | ❌ |
| `refresh` | ❌ | N/A | N/A | ❌ | N/A | ❌ |
| `info` | ❌ | ❌ | ❌ | N/A | N/A | ❌ |
| `whoami` | ❌ | ❌ | N/A | N/A | N/A | ❌ |
| `status` | ❌ | ❌ | N/A | N/A | N/A | ❌ |
| `send` | ❌ | N/A | N/A | N/A | N/A | ❌ |
| `edit` | ❌ | N/A | N/A | N/A | N/A | ❌ |
| `delete` | ❌ | N/A | N/A | N/A | N/A | ❌ |
| `search` | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| `recent` | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| `stats` | ❌ | ❌ | N/A | ⚠️ | ⚠️ | ❌ |
| `top` | ❌ | ❌ | N/A | ❌ | ⚠️ | ❌ |
| `timeline` | ❌ | ❌ | N/A | ❌ | ⚠️ | ❌ |
| `today` | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| `filter` | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| `export` | ❌ | N/A | N/A | N/A | ⚠️ | ❌ |
| `purge` | ❌ | N/A | N/A | N/A | N/A | ❌ |

---

## Priority Fixes

### P0 (Blocking agent usability)
1. **Add TOON output format** — replace JSON as default machine format
2. **Implement no-args home view** — show live dashboard instead of help
3. **Add minimal schemas + `--fields` flag** — default 3-4 fields per list

### P1 (Major token savings)
4. **Content truncation** — 500-char preview + `--full` escape hatch
5. **Pre-computed aggregates** — total counts in list headers
6. **Definitive empty states** — "0 chats found" not `[]`

### P2 (Discovery & polish)
7. **Contextual disclosure** — next-step hints after each command
7. **Bin path + description in home view**
8. **Per-command `--help` with examples**
9. **Idempotent mutations** — exit 0 on no-op

---

## Implementation Notes

- **TOON Spec**: https://toonformat.dev/reference/spec.html
- **Integration**: Keep internal logic on JSON; convert at output boundary
- **Backward Compat**: `--json`/`--yaml` remain for transition; `--toon` added
- **Default**: `--toon` when not TTY and no explicit format flag