"""Persona assembly: static yaml core + evolvable drift table → system prompts.

Layered persona model:
  1. Static core (`data/personas/shared.yaml` when present; otherwise a
     per-group yaml or the private-chat fallback) — voice, style, identity.
     Hand-edited; agent does NOT modify.
  2. Drift (`persona_drift` table) — agent-writable supplement; replaces
     wholesale on each `update_persona_drift` call.
  3. Operational rules (`prompts.PHASE_A_OPS_RULES`) — what the agent
     CAN do (tool list, msg_id format, terseness rules). Identical across
     groups; not user-editable.

Phase A and Phase B compose these layers slightly differently:
  - Phase A: core + drift + ops_rules + tool inventory
  - Phase B: core + drift + reflection-specific rules (the model is
    deciding whether/what to write, not chatting)

If the yaml file is missing or malformed, we fall back to a generic
default — the agent stays usable on a fresh install before personas
are authored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from .. import prompts
from ..config import settings
from .memory import get_persona_drift


# --- yaml loading ----------------------------------------------------------


def load_persona_yaml(personas_dir: Path, group_id: str) -> dict[str, Any]:
    """Load `shared.yaml` when present, else one group's card or the private-chat card.

    Recognized keys (all optional):
      identity:         one-line "you are X in this group"
      voice:            multi-line voice / style description
      knows_about:      list[str] — topics the bot has standing context on
      avoid:            list[str] — topics / tones to avoid
      default_drift:    string seed used as fallback when persona_drift table
                        is empty for this group (still appears in prompts)

    Missing, empty or malformed cards produce an empty dict, so a fresh
    install still gets the built-in default identity.
    """
    # Opt-in group overrides: a group card with `override_shared: true`
    # wins over shared.yaml. Other groups stay on the shared core.
    group_card = personas_dir / f"{group_id}.yaml"
    override = False
    if group_card.exists():
        try:
            preview = yaml.safe_load(group_card.read_text(encoding="utf-8"))
            override = bool(preview and isinstance(preview, dict)
                            and preview.get("override_shared"))
        except yaml.YAMLError:
            override = False
    shared = personas_dir / "shared.yaml"
    if override:
        path = group_card
    elif shared.exists():
        path = shared
    else:
        path = group_card
        if not path.exists() and "@chatroom" not in group_id:
            path = personas_dir / "direct-message.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        logger.warning("persona yaml {} malformed; using defaults: {}", path, e)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("persona yaml {} root is not a mapping; using defaults", path)
        return {}
    return raw


# --- prompt assembly -------------------------------------------------------


def _identity_block(
    persona: dict[str, Any], bot_name: str, group_name: str | None
) -> str:
    """First paragraph: who the bot is here. `{group}` / `{bot_name}` in a
    shared card are filled from runtime context. Defaults are deliberately
    bare — users opt into a richer identity via `data/personas/*.yaml`."""
    identity = persona.get("identity")
    if isinstance(identity, str) and identity.strip():
        return (
            identity.strip()
            .replace("{group}", group_name or prompts.IDENTITY_GROUP_FALLBACK)
            .replace("{bot_name}", bot_name)
        )
    return prompts.IDENTITY_DEFAULT.format(
        group=group_name or prompts.IDENTITY_GROUP_FALLBACK,
        bot_name=bot_name,
    )


def _voice_block(persona: dict[str, Any]) -> str:
    voice = persona.get("voice")
    if isinstance(voice, str) and voice.strip():
        return voice.strip()
    return ""  # no default voice — let the model + drift settle naturally


def _list_block(persona: dict[str, Any], key: str, label: str) -> str:
    """Render `knows_about` / `avoid` style lists. Empty/missing → empty."""
    items = persona.get(key)
    if not isinstance(items, list) or not items:
        return ""
    bullets = "\n".join(f" - {it}" for it in items if isinstance(it, str) and it.strip())
    if not bullets:
        return ""
    return f"\n\n{label}：\n{bullets}"


def _drift_block(drift_text: str, persona: dict[str, Any]) -> str:
    """Drift goes after core voice. Falls back to yaml `default_drift` if
    the table is empty — lets users seed an initial drift in yaml without
    pre-populating the table."""
    if drift_text.strip():
        return f"\n\n{prompts.DRIFT_HEADER_LIVE}\n{drift_text.strip()}"
    seed = persona.get("default_drift")
    if isinstance(seed, str) and seed.strip():
        return f"\n\n{prompts.DRIFT_HEADER_SEED}\n{seed.strip()}"
    return ""


def _join_nonempty(*parts: str, sep: str = "\n\n") -> str:
    """Drop any empty / whitespace-only parts before joining. Keeps the
    output tight when persona is mostly default (no yaml + no drift)."""
    return sep.join(p.strip() for p in parts if p and p.strip())


def build_phase_a_system(
    *,
    persona: dict[str, Any],
    drift_text: str,
    bot_name: str,
    group_name: str | None,
    verified_father: bool = False,
) -> str:
    """Compose persona layers (identity → voice → knows/avoid → drift) and
    operational rules into one system prompt. Empty layers drop out clean —
    bare default persona renders to just identity + ops_rules."""
    persona_block = _join_nonempty(
        _identity_block(persona, bot_name, group_name),
        _voice_block(persona),
        _list_block(persona, "knows_about", prompts.LIST_LABEL_KNOWS_ABOUT),
        _list_block(persona, "avoid", prompts.LIST_LABEL_AVOID),
        _drift_block(drift_text, persona),
    )
    base = _join_nonempty(persona_block, "---", prompts.PHASE_A_OPS_RULES)
    return base + (prompts.VERIFIED_FATHER_RULE if verified_father else "")


def build_phase_b_system(
    *,
    persona: dict[str, Any],
    drift_text: str,
    bot_name: str,
    group_name: str | None,
    base_phase_b_prompt: str,
    verified_father: bool = False,
) -> str:
    """Phase B keeps the persona stack (so writes to drift/memory stay in
    voice) and swaps the chat ops rules for reflection rules."""
    persona_block = _join_nonempty(
        _identity_block(persona, bot_name, group_name),
        _voice_block(persona),
        _drift_block(drift_text, persona),
    )
    base = _join_nonempty(persona_block, "---", base_phase_b_prompt)
    return base + (prompts.VERIFIED_FATHER_RULE if verified_father else "")


def verified_father(
    *, conn: sqlite3.Connection, group_id: str, trigger_msg_id: int | None,
    trigger_kind: str,
) -> bool:
    """Check the direct trigger's stored account against WO_FATHER_WXID."""
    father = (settings.father_wxid or "").strip()
    if not father or trigger_kind not in {"mention", "reply"} or trigger_msg_id is None:
        return False
    row = conn.execute(
        "SELECT sender_wxid FROM messages WHERE msg_id=? AND group_id=?",
        (int(trigger_msg_id), group_id),
    ).fetchone()
    return bool(row and row["sender_wxid"] == father)


# --- top-level convenience -------------------------------------------------


def assemble_system_prompts(
    *,
    conn: sqlite3.Connection,
    group_id: str,
    group_name: str | None,
    bot_name: str,
    personas_dir: Path,
    base_phase_b_prompt: str,
    trigger_msg_id: int | None = None,
    trigger_kind: str = "",
) -> tuple[str, str]:
    """One-shot: load yaml + drift + render both phase prompts.

    Returns `(phase_a_system, phase_b_system)`. Caller passes phase_b
    through to `run_agent` whether or not reflection is enabled — the
    runtime ignores it when reflection is off.
    """
    persona = load_persona_yaml(personas_dir, group_id)
    drift = get_persona_drift(conn, group_id)
    is_father = verified_father(
        conn=conn, group_id=group_id, trigger_msg_id=trigger_msg_id,
        trigger_kind=trigger_kind,
    )
    return (
        build_phase_a_system(
            persona=persona, drift_text=drift,
            bot_name=bot_name, group_name=group_name,
            verified_father=is_father,
        ),
        build_phase_b_system(
            persona=persona, drift_text=drift,
            bot_name=bot_name, group_name=group_name,
            base_phase_b_prompt=base_phase_b_prompt,
            verified_father=is_father,
        ),
    )
