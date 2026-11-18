from app.engine import evaluate
from app.review import build_root_evidence, adjudicate, fingerprint, PENDING, CONFIRMED, RESIGN
from copy import deepcopy


def test_fingerprint_stable_under_same_evidence(clean):
    again=build_root_evidence(deepcopy(clean))
    first={r['root']:r['evidence_fingerprint'] for r in build_root_evidence(clean)['roots']}
    second={r['root']:r['evidence_fingerprint'] for r in again['roots']}
    assert first==second


def test_clean_all_roots_confirmable(clean):
    evidence=build_root_evidence(clean)
    assert evidence['run_block_reasons']==[]
    assert len(evidence['roots'])==6
    assert all(r['confirmable'] for r in evidence['roots'])


def test_dirty_weight_issue_attributed_to_owner_root(sample):
    evidence=build_root_evidence(sample)
    by={r['root']:r for r in evidence['roots']}
    m001=by['M001']
    assert not m001['confirmable']
    assert any(b['code']=='WEIGHT_MISMATCH' and 'M001' in b['detail'] for b in m001['block_reasons'])
    # 危险品同箱问题归属 M001 子树
    assert any(b['code']=='INCOMPATIBLE' for b in m001['block_reasons'])
    # 未受影响的主单仍然可确认，不会被别单证据牵连
    assert by['M002']['confirmable']


def test_run_level_blocks_not_attributed_to_roots(sample):
    evidence=build_root_evidence(sample)
    codes={b['code'] for b in evidence['run_block_reasons']}
    assert 'MISSING_PARENT' in codes       # ORPHAN
    assert 'DEPENDENCY_CYCLE' in codes     # LOOP-A/LOOP-B
    assert 'COLUMN_COUNT' in codes         # COLUMN 隔离行
    assert 'DUPLICATE_CONFLICT' in codes   # CONFLICT 隔离行
    assert 'DECLARATION_MISMATCH' in codes # 报关毛重差异
    # 这些阻断不出现在任何一张主单名下
    for root in evidence['roots']:
        assert not any(b['code'] in {'MISSING_PARENT','DEPENDENCY_CYCLE','DECLARATION_MISMATCH'} for b in root['block_reasons'])


def test_fingerprint_changes_when_weight_evidence_changes(clean):
    before={r['root']:r['evidence_fingerprint'] for r in build_root_evidence(clean)['roots']}
    changed=deepcopy(clean)
    target=next(r for r in changed['weight_checks'] if r['id']=='M002' and r['field']=='gross_kg')
    target['code']='WEIGHT_MISMATCH';target['difference']='5'
    after={r['root']:r['evidence_fingerprint'] for r in build_root_evidence(changed)['roots']}
    assert after['M002']!=before['M002']
    assert after['M001']==before['M001']


def test_adjudicate_selective_only_changed_root(clean):
    old=build_root_evidence(clean)
    new_state=deepcopy(clean)
    target=next(r for r in new_state['weight_checks'] if r['id']=='M003' and r['field']=='net_kg')
    target['code']='WEIGHT_MISMATCH';target['difference']='2'
    new=build_root_evidence(new_state)
    verdict=adjudicate(old,new)
    assert verdict['mode']=='selective'
    assert verdict['changed_roots']==['M003']


def test_adjudicate_reset_when_root_set_changes(clean):
    old=build_root_evidence(clean)
    new_state=deepcopy(clean)
    new_state['graph']['roots']=[r for r in new_state['graph']['roots'] if r!='M006']
    new=build_root_evidence(new_state)
    verdict=adjudicate(old,new)
    assert verdict['mode']=='reset'
    assert 'M006' in verdict['changed_roots']


def test_adjudicate_no_change_keeps_conclusions(clean):
    old=build_root_evidence(clean)
    verdict=adjudicate(old,build_root_evidence(deepcopy(clean)))
    assert verdict['mode']=='selective'
    assert verdict['changed_roots']==[]
