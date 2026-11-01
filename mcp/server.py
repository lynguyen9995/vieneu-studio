"""Official Python MCP SDK stdio adapter for the HTTP-only gateway client.

Launch this file, not `python -m mcp.server`; do not add mcp/__init__.py.
"""
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# A script launch puts ROOT/mcp first, which can shadow the SDK's server module.
sys.path[:] = [str(ROOT)] + [entry for entry in sys.path
                              if Path(entry or '.').resolve() not in (ROOT, ROOT / 'mcp')]

from mcp.server.fastmcp import FastMCP
from cli.speak import ClientError, GatewayClient

mcp = FastMCP('VieNeu Gateway')


def _call(method, *args):
    try:
        with GatewayClient() as client:
            return getattr(client, method)(*args)
    except ClientError as error:
        return {'ok': False, 'error': str(error)}


@mcp.tool()
def speak(text: str, voice: str = 'Mai Anh', out_path: str | None = None) -> dict[str, Any]:
    """Use to read Vietnamese text aloud via the local gateway, not to clone a voice.

    text: nonblank script up to 20000 characters; upstream splits sentences.
    voice: a preset or consented clone name from list_voices, default Mai Anh.
    out_path: optional new absolute .wav path; omit for timestamped outputs/ file.
    Returns absolute path, duration_seconds, sample_rate and voice, never audio bytes.
    If unavailable, start start-all.bat before retrying.
    """
    return _call('speak', text, voice, out_path)


@mcp.tool()
def list_voices() -> dict[str, Any]:
    """List gateway voices, including their preset/cloned kind."""
    return _call('list_voices')


@mcp.tool()
def clone_voice(name: str, audio_path: str, consent_confirmed: bool, consent_note: str) -> dict[str, Any]:
    """Use only after the voice owner has explicitly agreed to being cloned.

    name: new unique display name, never a preset name.
    audio_path: absolute path to an owner-provided local reference clip, preferably 3–8 seconds.
    consent_confirmed: must be true, reflecting actual owner agreement, not an inferred default.
    consent_note: nonblank record of that agreement, persisted with the voice metadata JSON.
    Gateway converts audio to WAV, stores it and replays enrollment on gateway startup.
    Returns enrollment metadata, not audio. Never use another person's voice without permission.
    """
    if consent_confirmed is not True or not consent_note.strip():
        return {'ok': False, 'error': 'Voice-owner consent must be confirmed with a nonblank consent note.'}
    return _call('clone_voice', name, audio_path, consent_confirmed, consent_note)


@mcp.tool()
def delete_voice(name: str) -> dict[str, Any]:
    """Use when the user requests removal of a consented cloned voice.

    name: exact cloned name from list_voices; presets/aliases are protected.
    Deletes managed reference/metadata and blocks it through gateway. Upstream has no
    delete API, so its residual RAM clears only on upstream restart. Returns deletion status.
    """
    return _call('delete_voice', name)


if __name__ == '__main__':
    mcp.run(transport='stdio')
