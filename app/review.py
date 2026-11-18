"""逐张主单的复核证据、阻断原因与补证裁决（纯函数，无副作用）。

裁决原则（对应用户场景）：
- 每张主单（root）只对自己子树的证据负责，补证后只有内容指纹变化的主单需要重签；
- 当封存件无法逐张说明依据（缺少历史指纹/源快照）时，整批复核回到待复核，
  而不是让封存件引用一份说不清来源的证据。
"""
import hashlib
import json
from collections import defaultdict

PENDING = '待复核'
CONFIRMED = '证据已确认'
RESIGN = '需重签'

# 封存级别阻断原因码（不归属任何一张主单）
RUN_BLOCK_CODES = {'MISSING_PARENT', 'DEPENDENCY_CYCLE'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(bundle):
    """证据内容指纹：证据不变则指纹不变，任何核验依据变化都会换指纹。"""
    return hashlib.sha256(_canonical(bundle).encode('utf-8')).hexdigest()


def _subtree_of(records):
    """node -> 所属主单root（含自身）。无法归入主单的节点返回 None。"""
    parent_of = {r['id']: (r.get('parent') or '').strip() for r in records}
    cache = {}

    def root_of(node):
        if node in cache:
            return cache[node]
        seen = set()
        cur = node
        while cur:
            if cur in cache:
                owner = cache[cur]
                break
            if cur in seen:  # 环：无法归入任何主单
                owner = None
                break
            if cur not in parent_of:  # 非节点标识（如箱号）无法归属主单
                owner = None
                break
            seen.add(cur)
            nxt = parent_of.get(cur, '')
            if not nxt:
                owner = cur
                break
            cur = nxt
        else:
            owner = None
        for n in seen:
            cache[n] = owner
        return owner

    groups = defaultdict(list)
    for r in records:
        owner = root_of(r['id'])
        if owner is not None:
            groups[owner].append(r['id'])
    return {node: root_of(node) for node in parent_of}, dict(groups)


def build_root_evidence(state):
    """从一次完整核验结果中，逐张主单切分证据并计算指纹与阻断原因。

    返回:
        roots: 每张主单的复核视图（含 evidence_fingerprint / block_reasons / confirmable）
        run_block_reasons: 封存级阻断（缺父级、依赖环、对账差异、无法归属的隔离）
    """
    records = state['records']
    roots = list(state['graph']['roots'])
    owner_of, groups = _subtree_of(records)
    children = state['graph']['children']

    weight_by_root = defaultdict(list)
    for check in state.get('weight_checks', []):
        owner = owner_of.get(check['id'])
        if owner is not None:
            weight_by_root[owner].append(check)

    # 箱级问题（如同箱不相容）：按箱内节点归属到使用该箱的主单
    container_roots = defaultdict(set)
    for r in records:
        owner = owner_of.get(r['id'])
        if owner is not None and r.get('container'):
            container_roots[r['container']].add(owner)

    hazard_by_root = defaultdict(list)
    run_blocks = []
    for issue in state.get('hazard_issues', []):
        owner = owner_of.get(issue['id'])
        if owner is not None:
            hazard_by_root[owner].append(issue)
        elif issue.get('id') in container_roots:
            for holder in sorted(container_roots[issue['id']]):
                hazard_by_root[holder].append(issue)
        else:
            run_blocks.append({'code': issue.get('code', 'HAZARD'),
                               'detail': f"{issue.get('id','')} {issue.get('code','')}".strip()})

    graph_block_ids = set()
    for err in state.get('graph', {}).get('errors', []):
        if err.get('code') in RUN_BLOCK_CODES:
            run_blocks.append({'code': err['code'],
                               'detail': f"{err.get('id','')} {'/'.join(err.get('path', []))}".strip()})
            graph_block_ids.add(err.get('id'))

    quarantine_by_root = defaultdict(list)
    for q in state.get('quarantine', []):
        owner = owner_of.get(q.get('id'))
        if owner is not None:
            quarantine_by_root[owner].append(
                {'code': q.get('code'), 'detail': q.get('detail'), 'line': q.get('line')})
        else:
            run_blocks.append({'code': q.get('code', 'QUARANTINE'),
                               'detail': f"{q.get('id','')} {q.get('detail','')}".strip()})

    # 报关对账差异是全批级阻断：任一张主单都无法单独解除
    for field in state.get('balance', {}).get('declaration_differences', []):
        run_blocks.append({'code': 'DECLARATION_MISMATCH',
                           'detail': f'报关对账总量不一致：{field}'})
    if not state.get('balance', {}).get('row_balanced', True):
        run_blocks.append({'code': 'ROW_NOT_BALANCED', 'detail': '原始行守恒不成立'})

    node_index = {r['id']: r for r in records}
    result = []
    for root in roots:
        member_ids = sorted(set(groups.get(root, [root])))
        nodes = [node_index[i] for i in member_ids if i in node_index]
        weight = sorted(weight_by_root.get(root, []),
                        key=lambda x: (x['id'], x['field']))
        hazards = sorted(hazard_by_root.get(root, []),
                         key=lambda x: (x.get('id', ''), x.get('code', '')))
        quarantine = sorted(quarantine_by_root.get(root, []),
                            key=lambda x: (x.get('line') or 0, x.get('code') or ''))
        block_reasons = []
        for w in weight:
            if w['code'] != 'BALANCED':
                block_reasons.append({
                    'code': 'WEIGHT_MISMATCH',
                    'detail': f"{w['id']} {w['field']} 差异 {w['difference']}kg（申报 {w['declared']} / 子单合计 {w['children_sum']}）"})
        for h in hazards:
            block_reasons.append({
                'code': h.get('code', 'HAZARD'),
                'detail': f"{h.get('id','')} {h.get('code','')}" + (f" {h.get('classes','')}" if h.get('classes') else '')})
        block_reasons.extend(quarantine)

        bundle = {
            'root': root,
            'nodes': [{k: n.get(k) for k in
                       ('id', 'parent', 'pieces', 'net_kg', 'gross_kg',
                        'hazard', 'un_code', 'container', 'revision')}
                      for n in sorted(nodes, key=lambda n: n['id'])],
            'weight_checks': weight,
            'hazard_issues': hazards,
            'quarantine': quarantine,
            'children': {p: sorted(c) for p, c in children.items()
                         if owner_of.get(p) == root},
        }
        result.append({
            'root': root,
            'member_count': len(member_ids),
            'evidence_fingerprint': fingerprint(bundle),
            'block_reasons': block_reasons,
            'confirmable': not block_reasons,
            'evidence_bundle': bundle,
        })
    return {'roots': result, 'run_block_reasons': run_blocks}


def adjudicate(previous, current):
    """对同一 run 补证后的新旧两份 root 证据做裁决。

    previous/current 均为 build_root_evidence 的返回。
    返回:
        mode: 'selective'（只让证据变化的主单重签）或 'reset'（整批回到待复核）
        changed_roots: 指纹变化的主单
        reason: 裁决依据的人读说明
    """
    prev_map = {r['root']: r for r in previous['roots']}
    cur_map = {r['root']: r for r in current['roots']}

    # 封存件说不清依据的情形：历史指纹缺失，或主单集合本身发生变化（无法逐张对账）
    if not previous['roots'] or set(prev_map) != set(cur_map):
        changed = sorted(set(prev_map) ^ set(cur_map)) or sorted(cur_map)
        return {'mode': 'reset', 'changed_roots': changed,
                'reason': '历史主单证据不完整或主单集合变化，封存件无法逐张说明依据，整批回到待复核'}

    changed = sorted(root for root, cur in cur_map.items()
                     if prev_map[root]['evidence_fingerprint'] != cur['evidence_fingerprint'])
    if not changed:
        return {'mode': 'selective', 'changed_roots': [],
                'reason': '各主单证据指纹均未变化，沿用原复核结论'}
    return {'mode': 'selective', 'changed_roots': changed,
            'reason': '封存件可逐张说明依据，仅证据变化的主单需重签，其余主单保留原结论'}


def root_summary(evidence, review_row=None):
    """供工作台/交接清单展示的单张主单视图（不含大体积 bundle）。"""
    summary = {k: evidence[k] for k in
               ('root', 'member_count', 'evidence_fingerprint', 'block_reasons', 'confirmable')}
    if review_row is None:
        summary.update(status=PENDING, evidence_version=1, confirmed_at=None, reviewer=None)
    else:
        summary.update(status=review_row['status'], evidence_version=review_row['evidence_version'],
                       confirmed_at=review_row['confirmed_at'], reviewer=review_row['reviewer'],
                       resign_required=review_row['status'] == RESIGN or
                                       review_row['evidence_fingerprint'] != evidence['evidence_fingerprint'])
    return summary
