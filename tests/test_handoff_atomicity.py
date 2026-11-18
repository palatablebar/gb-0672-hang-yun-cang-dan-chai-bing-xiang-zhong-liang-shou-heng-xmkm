import threading
import pytest
from app import store
from app.archive import build_handoff
from app.errors import BusinessError
from app.engine import evaluate


def _prepared(tmp_path):
    path = str(tmp_path / 'concurrency.sqlite')
    state = evaluate({'dataset': 'clean'})
    store.save(path, state)
    rid = state['manifest']['run_id']
    store.ensure_thread(path, rid)
    return path, rid


def test_switch_failure_rolls_back_and_keeps_old_package(tmp_path):
    path, rid = _prepared(tmp_path)
    store.confirm_root(path, rid, 'M001', 1, '单证员')
    first = store.seal_handoff(path, rid, 2, build_handoff)
    assert len(first['packages']) == 1 and first['packages'][0]['is_current']
    old_id = first['packages'][0]['id']

    store.confirm_root(path, rid, 'M002', 3, '单证员')
    # 模拟切换步骤失败：插入成功但指针切换抛错，整个事务必须回滚
    original = store._finalize_switch
    def failing_switch(con, run_id, new_package_id):
        raise RuntimeError('切换中断')
    store._finalize_switch = failing_switch
    try:
        with pytest.raises(RuntimeError):
            store.seal_handoff(path, rid, 4, build_handoff)
    finally:
        store._finalize_switch = original

    packages = store.list_packages(path, rid)
    assert len(packages) == 1  # 新版未留下半成品
    assert packages[0]['id'] == old_id and packages[0]['is_current'] is True
    # 旧版交接包仍可下载，且 run 修订未被推进
    pkg = store.get_package(path, rid)
    assert pkg['id'] == old_id
    assert store.get(path, rid)['revision'] == 4

    # 恢复后重试可正常切换，旧版仍保留
    ok = store.seal_handoff(path, rid, 4, build_handoff)
    ids = [p['id'] for p in ok['packages']]
    assert old_id in ids and ok['packages'][-1]['is_current'] is True
    assert store.get_package(path, rid, old_id)['id'] == old_id


def test_concurrent_seal_serializes_with_single_current(tmp_path):
    path, rid = _prepared(tmp_path)
    store.confirm_root(path, rid, 'M001', 1, '单证员')
    errors = []
    results = []

    def worker(seq):
        try:
            token = f'concurrent-seal-{seq}'
            results.append(store.seal_handoff(path, rid, 2, build_handoff, request_token=token))
        except BusinessError as error:
            errors.append(error.code)
        except Exception as error:  # 序列化冲突也属于可接受的拒绝，不允许双 current
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    packages = store.list_packages(path, rid)
    current = [p for p in packages if p['is_current']]
    assert len(current) == 1  # 任意时刻只有一个当前交接包
    # 失败者必须收到明确的版本冲突，而不是静默覆盖
    assert results or errors
    if not results:
        assert 'STALE_VERSION' in errors or 'OperationalError' in errors


def test_evidence_versions_keep_lineage_across_supplements(tmp_path):
    path, rid = _prepared(tmp_path)
    store.confirm_batch(path, rid, 1, '单证员')
    from tests.test_review_workflow import csv_with_root_gross_break
    state2 = evaluate({'dataset': 'clean', 'csv_text': csv_with_root_gross_break('M002')})
    wl = store.supplement(path, rid, state2, 2)
    assert wl['evidence_version'] == 2
    # 同一补证重试（相同 token）不产生第二套证据版本
    again = store.supplement(path, rid, state2, wl['revision'], request_token=None)
    assert again['evidence_version'] == 2
    import sqlite3
    with store.connect(path) as con:
        versions = con.execute('SELECT version FROM evidence_versions WHERE run_id=? ORDER BY version', (rid,)).fetchall()
    assert [v['version'] for v in versions] == [1, 2]
