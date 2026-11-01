"""Speech-to-text service (stt/): fake Whisper model and fake downloader, no GPU, no network."""
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from stt.core import Preparer, Recognizer, SttError, transcribe_bytes
from stt.server import create_app

VID = 'abcDEF12345'


class FakeModel:
    def __init__(self, segments=None, language='en', fail=False):
        self.calls = []
        self.segments = segments or [(0.0, 2.5, ' Hello everyone.'), (2.5, 3.0, '  '), (3.0, 6.0, ' Today we cook.')]
        self.language, self.fail = language, fail

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        if self.fail:
            raise RuntimeError('CUDA out of memory')
        segs = (NS(start=a, end=b, text=t) for a, b, t in self.segments)
        return segs, NS(language=self.language, duration=6.0)


def recognizer(model=None, clock=None, idle=600):
    model = model or FakeModel()
    r = Recognizer(loader=lambda: (model, 'cuda'), idle=idle, **({'clock': clock} if clock else {}))
    r.fake = model
    return r


def downloader_into(log):
    def download(video_id, folder, max_seconds):
        path = folder / f'{video_id}.webm'
        path.write_bytes(b'fake opus audio')
        log.append(path)
        return path, {'duration': 6.0}
    return download


def preparer(tmp_path, rec=None, **kw):
    log = []
    kw.setdefault('downloader', downloader_into(log))
    p = Preparer(rec or recognizer(), tmp_path / 'cache', tmp_path / 'tmp', autostart=False,
                 disk_usage=kw.pop('disk_usage', lambda path: NS(free=10 * 1024 ** 3)), **kw)
    p.downloaded = log
    return p


def test_prepare_transcribes_publishes_segments_and_deletes_audio(tmp_path):
    p = preparer(tmp_path)
    assert p.prepare(VID)['state'] == 'queued'
    p.run_pending()
    view = p.get(VID)
    assert view['state'] == 'done' and view['language'] == 'en' and view['next'] == 2
    assert [s['text'] for s in view['segments']] == ['Hello everyone.', 'Today we cook.']   # blank segment dropped
    assert not p.downloaded[0].exists() and not any((tmp_path / 'tmp').iterdir())            # audio gone
    assert (tmp_path / 'cache' / f'{VID}.json').exists()                                     # text kept
    kw = p.recognizer.fake.calls[0][1]
    assert kw['vad_filter'] is True and kw['condition_on_previous_text'] is False


def test_since_returns_only_new_segments(tmp_path):
    p = preparer(tmp_path)
    p.prepare(VID)
    p.run_pending()
    later = p.get(VID, since=1)
    assert [s['text'] for s in later['segments']] == ['Today we cook.'] and later['next'] == 2


def test_cached_transcript_is_reused_without_download(tmp_path):
    p = preparer(tmp_path)
    p.prepare(VID)
    p.run_pending()
    again = preparer(tmp_path)                       # e.g. after a restart
    view = again.prepare(VID)
    assert view['state'] == 'done' and view['cached'] and view['next'] == 2 and not again.downloaded


def test_cache_from_another_model_is_ignored(tmp_path):
    p = preparer(tmp_path)
    p.prepare(VID)
    p.run_pending()
    other = Preparer(Recognizer(model_name='small', loader=lambda: (FakeModel(), 'cpu')), tmp_path / 'cache',
                     tmp_path / 'tmp', downloader=downloader_into([]), autostart=False,
                     disk_usage=lambda path: NS(free=10 * 1024 ** 3))
    assert other.prepare(VID)['state'] == 'queued'


def test_bad_id_is_refused_before_anything_runs(tmp_path):
    p = preparer(tmp_path)
    for bad in ['', 'short', 'https://evil.example/x', '../../etc/pas', None]:
        with pytest.raises(SttError):
            p.prepare(bad)
    assert p.queue.empty()


def test_low_disk_and_too_long_become_readable_errors(tmp_path):
    p = preparer(tmp_path, disk_usage=lambda path: NS(free=1024 ** 3))
    p.prepare(VID)
    p.run_pending()
    view = p.get(VID)
    assert view['state'] == 'error' and view['code'] == 'low_disk' and not p.downloaded

    def too_long(video_id, folder, max_seconds):
        raise SttError('too_long', 'Video dài 400 phút')
    q = preparer(tmp_path / 'q', downloader=too_long)
    q.prepare(VID)
    q.run_pending()
    assert q.get(VID)['code'] == 'too_long'


def test_failure_is_contained_audio_deleted_and_retry_possible(tmp_path):
    rec = recognizer(FakeModel(fail=True))
    p = preparer(tmp_path, rec=rec)
    p.prepare(VID)
    p.run_pending()
    view = p.get(VID)
    assert view['state'] == 'error' and view['code'] == 'failed' and 'out of memory' in view['error']
    assert not any((tmp_path / 'tmp').iterdir())
    rec.fake.fail = False
    assert p.prepare(VID)['state'] == 'queued'      # an error can be retried
    p.run_pending()
    assert p.get(VID)['state'] == 'done'


def test_worker_thread_processes_jobs(tmp_path):
    p = Preparer(recognizer(), tmp_path / 'cache', tmp_path / 'tmp', downloader=downloader_into([]),
                 disk_usage=lambda path: NS(free=10 * 1024 ** 3))
    p.prepare(VID)
    for _ in range(100):
        if p.get(VID)['state'] == 'done':
            break
        time.sleep(0.02)
    assert p.get(VID)['state'] == 'done'


def test_leftover_audio_from_a_killed_run_is_removed(tmp_path):
    (tmp_path / 'tmp').mkdir(parents=True)
    (tmp_path / 'tmp' / f'{VID}.webm').write_bytes(b'x' * 100)
    preparer(tmp_path)
    assert not any((tmp_path / 'tmp').iterdir())


def test_cache_is_capped_oldest_first(tmp_path):
    p = preparer(tmp_path, cache_limit=400)
    ids = ['aaaaaaaaaa1', 'aaaaaaaaaa2', 'aaaaaaaaaa3']
    for i, vid in enumerate(ids):
        p.prepare(vid)
        p.run_pending()
        f = tmp_path / 'cache' / f'{vid}.json'
        import os
        os.utime(f, (1000 + i, 1000 + i))
    left = sorted(x.stem for x in (tmp_path / 'cache').glob('*.json'))
    assert ids[-1] in left and ids[0] not in left


def test_model_unloads_after_idle_but_never_while_in_use():
    now = [0.0]
    r = recognizer(clock=lambda: now[0], idle=600)
    with r.use():
        now[0] = 10_000
        assert not r.unload_if_idle()               # in use
    assert r.loaded
    now[0] += 300
    assert not r.unload_if_idle()                   # not idle long enough
    now[0] += 400
    assert r.unload_if_idle() and not r.loaded
    with r.use():                                   # loads again on demand
        assert r.loaded


def test_transcribe_bytes_greedy_and_guards():
    r = recognizer()
    out = transcribe_bytes(r, b'RIFF....', 'en', decode=lambda b: [0.0] * 16000)
    assert out['text'] == 'Hello everyone. Today we cook.' and out['language'] == 'en'
    assert r.fake.calls[0][1]['beam_size'] == 1 and r.fake.calls[0][1]['language'] == 'en'
    with pytest.raises(SttError):
        transcribe_bytes(r, b'', decode=lambda b: [])
    with pytest.raises(SttError) as e:
        transcribe_bytes(r, b'xx', decode=lambda b: (_ for _ in ()).throw(ValueError('junk')))
    assert e.value.code == 'bad_audio'


def test_server_health_prepare_jobs_and_transcribe(tmp_path):
    p = preparer(tmp_path)
    app = create_app(p.recognizer, p, decode=lambda b: [0.0] * 16000)
    with TestClient(app) as c:
        h = c.get('/health').json()
        assert h['status'] == 'ok' and h['service'] == 'vieneu-stt' and h['loaded'] is False
        assert c.get(f'/jobs/{VID}').status_code == 404
        assert c.post('/prepare', json={'video_id': VID}).json()['state'] == 'queued'
        p.run_pending()
        j = c.get(f'/jobs/{VID}', params={'since': 1}).json()
        assert j['state'] == 'done' and len(j['segments']) == 1
        assert c.post('/prepare', json={'video_id': 'bad'}).status_code == 400
        t = c.post('/transcribe?language=en', content=b'RIFFfake').json()
        assert t['segments'][0]['text'] == 'Hello everyone.'
        assert c.post('/transcribe', content=b'').status_code == 400
        assert c.get('/health').json()['loaded'] is True


def test_missing_faster_whisper_gives_install_hint(tmp_path):
    def loader():
        raise SttError('not_installed', 'Chưa cài faster-whisper. Trong thư mục vieneu-studio chạy: uv sync')
    p = preparer(tmp_path, rec=Recognizer(loader=loader))
    with TestClient(create_app(p.recognizer, p, decode=lambda b: [0.0])) as c:
        r = c.post('/transcribe', content=b'RIFF')
        assert r.status_code == 503 and 'uv sync' in r.json()['error']
        assert 'uv sync' in c.get('/health').json()['error']
