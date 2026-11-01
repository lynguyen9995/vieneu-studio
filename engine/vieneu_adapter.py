"""Engine adapter: run VieNeu-TTS's OpenAI-compatible server with a selectable model.

Runs INSIDE the upstream environment (cwd = VieNeu-TTS, `uv run python <this file>`).
Upstream files are never modified: apps/openai_speech.py hardcodes `Vieneu(mode="v3turbo")`,
so we swap the module-level `Vieneu` factory before the engine is created.

Env (set by gateway/engine.py):
  STUDIO_MODE            v3turbo (default) | v3nano
  STUDIO_BACKBONE_REPO   optional: local folder / HF repo of a fine-tuned (LoRA-merged) v3 Turbo
  + the server's own VIENEU_BACKEND / VIENEU_DEVICE / VIENEU_PRECISION / VIENEU_MAX_STREAMS / HOST / PORT
"""
import os
import sys

sys.path.insert(0, os.getcwd())          # make `apps` importable from the upstream checkout

import numpy as np                        # noqa: E402
import apps.openai_speech as srv          # noqa: E402

MODE = os.environ.get('STUDIO_MODE', 'v3turbo')
BACKBONE = os.environ.get('STUDIO_BACKBONE_REPO') or None
_original = srv.Vieneu


class NanoCompat:
    """Make v3 Nano look like v3 Turbo to the server: 48 kHz output, alias table, name resolver."""
    sample_rate = srv.SAMPLE_RATE

    def __init__(self, inner):
        self._inner = inner
        self.backend = 'onnx'
        self._voice_aliases = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def resolve_voice_name(self, name):
        if not name:
            return None
        return name if name in self._inner._preset_voices else None

    def infer_stream(self, text, voice=None, apply_watermark=False, max_chars=140, **_ignored):
        import soxr
        rs = soxr.ResampleStream(self._inner.sample_rate, srv.SAMPLE_RATE, 1, dtype='float32', quality='HQ')
        for chunk in self._inner.infer_stream(text, voice=voice, apply_watermark=apply_watermark,
                                              max_chars=min(int(max_chars), 140)):
            if chunk is None or len(chunk) == 0:
                continue
            out = rs.resample_chunk(np.asarray(chunk, dtype=np.float32))
            if len(out):
                yield out
        tail = rs.resample_chunk(np.zeros(0, np.float32), last=True)
        if len(tail):
            yield tail


def factory(mode='v3turbo', **kw):
    if MODE == 'v3nano':
        extra = {'backbone_repo': BACKBONE} if BACKBONE else {}
        return NanoCompat(_original(mode='v3nano', **extra))
    if BACKBONE:
        kw['backbone_repo'] = BACKBONE
    return _original(mode='v3turbo', **kw)


srv.Vieneu = factory

if __name__ == '__main__':
    srv.log.info('studio adapter: mode=%s backbone=%s', MODE, BACKBONE or 'default')
    srv.main()
