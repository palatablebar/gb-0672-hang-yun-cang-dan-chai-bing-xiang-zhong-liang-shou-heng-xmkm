import io, json, zipfile
from fastapi.testclient import TestClient
from app import main
from app.config import ROOT


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'review.sqlite'))
    return TestClient(main.app)


def start(c, dataset='clean'):
    run = c.post('/api/runs', json={'dataset': dataset}).json()
    rid = run['manifest']['run_id']
    c.get('/api/runs/' + rid + '/worklist')  # 惰性建立复核线程
    return rid


def worklist(c, rid):
    return c.get(f'/api/runs/{rid}/worklist').json()


def csv_with_root_gross_break(root='M002', difference=5):
    lines = (ROOT / 'sample_data' / 'clean.csv').read_text(encoding='utf-8-sig').splitlines()
    out = []
    for line in lines:
        cells = line.split(',')
        if cells[0] == root and cells[1] == '':
            cells[5] = str(int(cells[5]) + difference)
        out.append(','.join(cells))
    return '\n'.join(out)


def csv_without_root(root='M006'):
    lines = (ROOT / 'sample_data' / 'clean.csv').read_text(encoding='utf-8-sig').splitlines()
    kept = [line for line in lines if not line.split(',')[0].startswith(root)]
    return '\n'.join(kept)


def test_worklist_lists_every_root_with_evidence_and_reasons(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c, 'dirty')
    wl = worklist(c, rid)
    assert {r['root'] for r in wl['roots']} == {'M001', 'M002', 'M003', 'M004', 'M005', 'M006'}
    assert wl['evidence_version'] == 1
    blocked = next(r for r in wl['roots'] if r['root'] == 'M001')
    assert blocked['status'] == '待复核'
    assert blocked['block_reasons']
    assert len(blocked['evidence_fingerprint']) == 64
    assert wl['run_block_reasons']
    assert wl['sealing']['eligible_count'] == 0


def test_blocked_root_cannot_be_confirmed(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c, 'dirty')
    resp = c.post(f'/api/runs/{rid}/roots/M001/confirm', json={'revision': 1})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'ROOT_BLOCKED'


def test_confirm_root_then_batch_does_not_resign_unchanged(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    first = c.post(f'/api/runs/{rid}/roots/M001/confirm',
                   json={'revision': 1, 'reviewer': '张单证'}).json()
    m001 = next(r for r in first['roots'] if r['root'] == 'M001')
    assert m001['status'] == '证据已确认' and m001['reviewer'] == '张单证'
    fp = m001['evidence_fingerprint']

    # 整批确认：已确认且证据未变的 M001 不重签，只签其余主单
    batch = c.post(f'/api/runs/{rid}/confirm-batch',
                   json={'revision': first['revision'], 'request_token': 'batch-1'}).json()
    assert 'M001' not in batch['confirmed_now']
    assert set(batch['confirmed_now']) == {'M002', 'M003', 'M004', 'M005', 'M006'}
    again = c.post(f'/api/runs/{rid}/confirm-batch',
                   json={'revision': batch['revision'], 'request_token': 'batch-2'}).json()
    assert again['confirmed_now'] == []  # 无新证据，整批确认是空操作
    assert again['revision'] == batch['revision']
    m001_again = next(r for r in again['roots'] if r['root'] == 'M001')
    assert m001_again['evidence_fingerprint'] == fp


def test_supplement_selective_resign_only_changed_root(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    batch = c.post(f'/api/runs/{rid}/confirm-batch', json={'revision': 1}).json()
    assert batch['sealing']['eligible_count'] == 6

    # 对 M002 补充重量证据（仅其主单毛重变化）
    resp = c.post(f'/api/runs/{rid}/supplement',
                  json={'revision': batch['revision'], 'csv_text': csv_with_root_gross_break('M002')})
    assert resp.status_code == 200, resp.text
    wl = resp.json()
    assert wl['evidence_version'] == 2
    assert wl['adjudication']['mode'] == 'selective'
    assert wl['adjudication']['changed_roots'] == ['M002']
    statuses = {r['root']: r for r in wl['roots']}
    assert statuses['M002']['status'] == '需重签'
    assert statuses['M002']['resign_required'] is True
    # 别单保持已确认，不回退、不重签
    assert statuses['M001']['status'] == '证据已确认'
    assert statuses['M001']['resign_required'] is False
    assert wl['sealing']['eligible_roots'] == ['M001', 'M003', 'M004', 'M005', 'M006']
    # 同一批号延续，证据版本留痕两份
    versions = c.get(f'/api/runs/{rid}/packages').json()
    assert worklist(c, rid)['evidence_version'] == 2


def test_supplement_reset_when_archive_cannot_explain_basis(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    batch = c.post(f'/api/runs/{rid}/confirm-batch', json={'revision': 1}).json()
    # 主单集合变化：封存件无法逐张说明依据，整批回到待复核
    resp = c.post(f'/api/runs/{rid}/supplement',
                  json={'revision': batch['revision'], 'csv_text': csv_without_root('M006')})
    wl = resp.json()
    assert wl['adjudication']['mode'] == 'reset'
    assert all(r['status'] == '待复核' for r in wl['roots'])
    assert wl['sealing']['eligible_count'] == 0


def test_same_request_token_never_creates_second_dataset(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    payload = {'revision': 1, 'request_token': 'token-xyz'}
    a = c.post(f'/api/runs/{rid}/roots/M001/confirm', json=payload).json()
    # 同一复核重试：原样返回首次结果，不产生第二套数据、版本不再增加
    b = c.post(f'/api/runs/{rid}/roots/M001/confirm', json=payload).json()
    assert a['revision'] == b['revision']
    first = next(r for r in b['roots'] if r['root'] == 'M001')
    assert first['status'] == '证据已确认'
    # 重试即使带着陈旧 revision，也走幂等回放而非 409
    stale_replay = c.post(f'/api/runs/{rid}/roots/M001/confirm', json=payload)
    assert stale_replay.status_code == 200
    assert stale_replay.json()['revision'] == a['revision']
    # 没有生成任何交接包或证据新版本
    assert c.get(f'/api/runs/{rid}/packages').json() == []
    assert worklist(c, rid)['evidence_version'] == 1


def test_stale_revision_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    c.post(f'/api/runs/{rid}/roots/M001/confirm', json={'revision': 1})
    stale = c.post(f'/api/runs/{rid}/roots/M002/confirm', json={'revision': 1})
    assert stale.status_code == 409
    assert stale.json()['code'] == 'STALE_VERSION'


def test_handoff_contains_only_confirmed_current_roots_and_keeps_old_package(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    c.post(f'/api/runs/{rid}/roots/M001/confirm', json={'revision': 1})
    seal1 = c.post(f'/api/runs/{rid}/seal', json={'revision': 2}).json()
    assert len(seal1['packages']) == 1 and seal1['packages'][0]['is_current']
    pkg1_id = seal1['packages'][0]['id']

    z1 = c.get(f'/api/runs/{rid}/packages/{pkg1_id}').content
    with zipfile.ZipFile(io.BytesIO(z1)) as zf:
        handoff = json.loads(zf.read('handoff/manifest.json'))
        assert [r['root'] for r in handoff['confirmed_roots']] == ['M001']
        assert handoff['evidence_version'] == 1
        assert 'M002' in handoff['excluded_roots']
        basis = json.loads(zf.read('handoff/root_evidence.json'))
        assert 'M001' in basis and 'M002' not in basis
        snapshot = json.loads(zf.read('handoff/source_snapshot.json'))
        assert snapshot['sha256']

    # 再确认一张主单后生成第二版交接包：旧版保留且可下载，当前指针切换
    c.post(f'/api/runs/{rid}/roots/M002/confirm', json={'revision': 3})
    seal2 = c.post(f'/api/runs/{rid}/seal', json={'revision': 4}).json()
    packages = seal2['packages']
    assert len(packages) == 2
    old, new = packages
    assert old['id'] == pkg1_id and old['is_current'] is False
    assert new['is_current'] is True
    # 当前交接包唯一下载入口返回新版
    latest = c.get(f'/api/handoff/{rid}').content
    with zipfile.ZipFile(io.BytesIO(latest)) as zf:
        roots = {r['root'] for r in json.loads(zf.read('handoff/manifest.json'))['confirmed_roots']}
        assert roots == {'M001', 'M002'}
    # 旧版字节仍可原样下载
    assert c.get(f'/api/runs/{rid}/packages/{pkg1_id}').content == z1


def test_seal_requires_confirmed_root(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c, 'dirty')
    resp = c.post(f'/api/runs/{rid}/seal', json={'revision': 1})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'NO_CONFIRMED_ROOT'


def test_seal_retry_is_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    c.post(f'/api/runs/{rid}/roots/M001/confirm', json={'revision': 1})
    payload = {'revision': 2, 'request_token': 'seal-once'}
    first = c.post(f'/api/runs/{rid}/seal', json=payload).json()
    second = c.post(f'/api/runs/{rid}/seal', json=payload).json()
    assert len(first['packages']) == 1
    assert second['packages'] == first['packages']
    assert second['revision'] == first['revision']


def test_resigned_root_must_reconfirm_before_entering_archive(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    c.post(f'/api/runs/{rid}/confirm-batch', json={'revision': 1})
    sup = c.post(f'/api/runs/{rid}/supplement',
                 json={'revision': 2, 'csv_text': csv_with_root_gross_break('M002')}).json()
    # 补证后 M002 阻断且需重签；此时封存只含其余五张
    seal = c.post(f'/api/runs/{rid}/seal', json={'revision': sup['revision']}).json()
    with zipfile.ZipFile(io.BytesIO(c.get('/api/handoff/' + rid).content)) as zf:
        roots = {r['root'] for r in json.loads(zf.read('handoff/manifest.json'))['confirmed_roots']}
        assert 'M002' not in roots and len(roots) == 5
    # 阻断中的 M002 不能重签
    blocked = c.post(f'/api/runs/{rid}/roots/M002/confirm', json={'revision': seal['revision']})
    assert blocked.status_code == 422 and blocked.json()['code'] == 'ROOT_BLOCKED'


def test_archive_page_renders_worklist(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = start(c)
    html = c.get(f'/archive?run_id={rid}').text
    assert '可交接复核清单' in html
    assert 'evidence_fingerprint' in html or '核验依据指纹' in html
    assert 'M001' in html
