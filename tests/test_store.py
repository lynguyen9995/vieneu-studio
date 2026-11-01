import io
import shutil
import wave
import subprocess
import pytest
from gateway.store import convert_wav, VoiceStore


def signal():
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        w.writeframes(b'\x01\x00' * 48000)
    return out.getvalue()


def test_m4a_conversion_with_existing_ffmpeg(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg unavailable; no global installation attempted')
    src = tmp_path / 'synthetic.wav'
    encoded = tmp_path / 'synthetic.m4a'
    src.write_bytes(signal())
    subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-i', str(src), str(encoded)], check=True, capture_output=True)
    converted = convert_wav(encoded.read_bytes(), 'synthetic.m4a', ffmpeg)
    with wave.open(io.BytesIO(converted), 'rb') as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert len(w.readframes(w.getnframes())) / 2 / 48000 == pytest.approx(3, abs=.1)


def test_state_write_never_follows_old_predictable_temp_link(tmp_path):
    store = VoiceStore(tmp_path)
    external = tmp_path / 'external.txt'
    external.write_text('untouched', encoding='utf-8')
    # A hardlink requires no Windows symlink privilege and has the same overwrite hazard.
    (store.directory / '.deleted.tmp').hardlink_to(external)
    store.deleted.add('Synthetic')
    store.save_deleted()
    assert external.read_text(encoding='utf-8') == 'untouched'


@pytest.mark.parametrize('state', ['.presets', '.deleted'])
def test_store_refuses_linked_internal_state(tmp_path, state):
    store = VoiceStore(tmp_path)
    outside = tmp_path / 'outside.json'
    outside.write_text('[]', encoding='utf-8')
    (store.directory / state).hardlink_to(outside)
    with pytest.raises(ValueError, match='link|reparse'):
        if state == '.presets':
            store.preset_catalog([])
        else:
            VoiceStore(tmp_path)
    assert outside.read_text(encoding='utf-8') == '[]'


def test_store_refuses_windows_junction(tmp_path):
    import os
    if os.name != 'nt':
        pytest.skip('Windows-only junction regression')
    target = tmp_path / 'outside'
    target.mkdir()
    junction = tmp_path / 'voices'
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(target)], check=True, capture_output=True)
    with pytest.raises(ValueError, match='link|reparse'):
        VoiceStore(tmp_path)


def test_metadata_write_failure_leaves_no_committed_record(tmp_path, monkeypatch):
    import gateway.store as module
    store = VoiceStore(tmp_path)
    real_dump = module.json.dump
    def broken_dump(obj, stream, **kwargs):
        if isinstance(obj, dict) and obj.get('name') == 'Synthetic':
            stream.write('{partial')
            raise OSError('disk full')
        return real_dump(obj, stream, **kwargs)
    monkeypatch.setattr(module.json, 'dump', broken_dump)
    with pytest.raises(OSError, match='disk full'):
        store.save('Synthetic', signal(), '', 'Generated signal, no speaker', False)
    assert store.records() == {}
    assert not store.audio_path('Synthetic').exists()
    # Either transaction committed or no JSON; never partially committed JSON.
    assert not any(p.read_text(encoding='utf-8') == '{partial' for p in store.directory.glob('*.json'))


def test_tombstoned_orphan_is_recoverable(tmp_path):
    store = VoiceStore(tmp_path)
    store.save('Synthetic', signal(), '', 'Generated signal', False)
    store.deleted.add('Synthetic')
    store.save_deleted()
    store.audio_path('Synthetic').with_suffix('.json').unlink()
    recovered = VoiceStore(tmp_path)
    assert not recovered.audio_path('Synthetic').exists()
    recovered.save('Synthetic', signal(), '', 'Generated signal', False)
    assert 'Synthetic' in recovered.records()


def test_conversion_refuses_missing_decoder_and_invalid_paths(tmp_path):
    with pytest.raises(ValueError, match='ffmpeg missing'):
        convert_wav(b'not real audio', 'input.m4a', None)
    with pytest.raises(ValueError):
        convert_wav(b'not real audio', 'input.wav', None)
    with pytest.raises(ValueError):
        convert_wav(b'not real audio', 'input.m3u8', shutil.which('ffmpeg'))
    store = VoiceStore(tmp_path)
    assert '..' not in store.slug('../../dangerous')
    assert '/' not in store.slug('../../dangerous')
