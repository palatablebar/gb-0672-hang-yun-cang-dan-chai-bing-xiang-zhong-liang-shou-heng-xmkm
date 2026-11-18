import sqlite3, json, hashlib
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from app.errors import BusinessError
from app import review as review_domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_threads (
  run_id TEXT PRIMARY KEY,
  evidence_version INTEGER NOT NULL DEFAULT 1,
  current_sha256 TEXT NOT NULL,
  last_adjudication TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_versions (
  run_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, version)
);
CREATE TABLE IF NOT EXISTS root_reviews (
  run_id TEXT NOT NULL,
  root_id TEXT NOT NULL,
  status TEXT NOT NULL,
  evidence_fingerprint TEXT NOT NULL,
  evidence_version INTEGER NOT NULL,
  reviewer TEXT,
  confirmed_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, root_id)
);
CREATE TABLE IF NOT EXISTS review_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  request_token TEXT NOT NULL,
  action TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (run_id, request_token)
);
CREATE TABLE IF NOT EXISTS handoff_packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  package_seq INTEGER NOT NULL,
  sha256 TEXT NOT NULL UNIQUE,
  filename TEXT NOT NULL,
  content BLOB NOT NULL,
  manifest TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (run_id, package_seq)
);
"""

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.executescript(SCHEMA)
    return con

@contextmanager
def transaction(path):
    con=connect(path)
    con.isolation_level=None
    con.execute('BEGIN IMMEDIATE')
    try:
        yield con
        con.execute('COMMIT')
    except Exception:
        con.execute('ROLLBACK')
        raise
    finally:
        con.close()

def now():
    return datetime.now(timezone.utc).isoformat()

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

# ------------------------------------------------------------------
# 逐张主单复核 / 补证裁决 / 交接封存
# ------------------------------------------------------------------

def _load_run(con, run_id):
    row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    return row

def _check_revision(row, revision):
    if not isinstance(revision,int) or revision<1:
        raise BusinessError('BAD_REVISION','版本号须为正整数')
    if row['revision']!=revision:
        raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)

def _replayed(con, run_id, token):
    """同一复核重试：命中幂等键则原样返回首次响应，不再生成第二套数据。"""
    row=con.execute('SELECT response FROM review_events WHERE run_id=? AND request_token=?',(run_id,token)).fetchone()
    return json.loads(row['response']) if row else None

def _record_event(con, run_id, token, action, response):
    con.execute('INSERT INTO review_events(run_id,request_token,action,response,created_at) VALUES(?,?,?,?,?)',
                (run_id,token,action,json.dumps(response,ensure_ascii=False),now()))

def ensure_thread(path, run_id):
    """惰性建立复核线程与首版证据，重复调用不产生新版本、不改变结论。"""
    with transaction(path) as con:
        row=_load_run(con,run_id)
        existing=con.execute('SELECT 1 FROM review_threads WHERE run_id=?',(run_id,)).fetchone()
        if existing:
            return _worklist(con,row)
        state=json.loads(row['result'])
        evidence=review_domain.build_root_evidence(state)
        stamp=now()
        con.execute('INSERT INTO review_threads(run_id,evidence_version,current_sha256,created_at,updated_at) VALUES(?,?,?,?,?)',
                    (run_id,1,state['manifest']['sha256'],stamp,stamp))
        con.execute('INSERT INTO evidence_versions(run_id,version,sha256,source_digest,result,created_at) VALUES(?,?,?,?,?,?)',
                    (run_id,1,state['manifest']['sha256'],state['manifest']['sha256'],
                     json.dumps(state,ensure_ascii=False),stamp))
        for item in evidence['roots']:
            con.execute('INSERT INTO root_reviews(run_id,root_id,status,evidence_fingerprint,evidence_version,reviewer,confirmed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (run_id,item['root'],review_domain.PENDING,item['evidence_fingerprint'],1,None,None,stamp))
        return _worklist(con,row)

def _current_evidence(con, run_id, state=None):
    if state is None:
        row=con.execute('SELECT result FROM runs WHERE id=?',(run_id,)).fetchone()
        state=json.loads(row['result'])
    return review_domain.build_root_evidence(state), state

def _worklist(con, run_row):
    run_id=run_row['id']
    state=json.loads(run_row['result'])
    evidence,_=_current_evidence(con,run_id,state)
    thread=con.execute('SELECT * FROM review_threads WHERE run_id=?',(run_id,)).fetchone()
    review_rows={r['root_id']:dict(r) for r in con.execute('SELECT * FROM root_reviews WHERE run_id=?',(run_id,))}
    roots=[]
    for item in evidence['roots']:
        roots.append(review_domain.root_summary(item, review_rows.get(item['root'])))
    packages=[dict(p) for p in con.execute(
        'SELECT id,package_seq,sha256,filename,is_current,created_at FROM handoff_packages WHERE run_id=? ORDER BY package_seq',(run_id,))]
    for p in packages: p['is_current']=bool(p['is_current'])
    confirmed_current=[r['root'] for r in roots if r.get('status')==review_domain.CONFIRMED and not r.get('resign_required')]
    return {
        'run_id':run_id,
        'revision':run_row['revision'],
        'status':run_row['status'],
        'evidence_version':thread['evidence_version'] if thread else 1,
        'current_sha256':thread['current_sha256'] if thread else state['manifest']['sha256'],
        'adjudication':json.loads(thread['last_adjudication']) if thread and thread['last_adjudication'] else None,
        'run_block_reasons':evidence['run_block_reasons'],
        'roots':roots,
        'sealing':{
            'eligible_roots':confirmed_current,
            'eligible_count':len(confirmed_current),
            'total_roots':len(roots),
            'pending_or_resign':[r['root'] for r in roots if r['root'] not in confirmed_current],
        },
        'packages':packages,
    }

def worklist(path, run_id):
    con=connect(path)
    try:
        row=_load_run(con,run_id)
        exists=con.execute('SELECT 1 FROM review_threads WHERE run_id=?',(run_id,)).fetchone()
    finally:
        con.close()
    if not exists:
        return ensure_thread(path,run_id)
    con=connect(path)
    try:
        row=_load_run(con,run_id)
        return _worklist(con,row)
    finally:
        con.close()

def confirm_root(path, run_id, root_id, revision, reviewer, request_token=None):
    with transaction(path) as con:
        row=_load_run(con,run_id)
        state=json.loads(row['result'])
        evidence,_=_current_evidence(con,run_id,state)
        target=next((r for r in evidence['roots'] if r['root']==root_id),None)
        if target is None: raise BusinessError('ROOT_NOT_FOUND','主单不在本批复核范围',404)
        token=request_token or f"confirm-root:{root_id}:{target['evidence_fingerprint']}"
        # 同一复核重试先于版本校验命中幂等键，原样返回首次响应
        cached=_replayed(con,run_id,token)
        if cached is not None: return cached
        _check_revision(row,revision)
        if not target['confirmable']:
            raise BusinessError('ROOT_BLOCKED','主单仍有阻断项，不能确认封存')
        stamp=now()
        thread=con.execute('SELECT evidence_version FROM review_threads WHERE run_id=?',(run_id,)).fetchone()
        existing=con.execute('SELECT status,evidence_fingerprint FROM root_reviews WHERE run_id=? AND root_id=?',(run_id,root_id)).fetchone()
        already=existing and existing['status']==review_domain.CONFIRMED and existing['evidence_fingerprint']==target['evidence_fingerprint']
        if not already:
            con.execute('UPDATE root_reviews SET status=?,evidence_fingerprint=?,evidence_version=?,reviewer=?,confirmed_at=?,updated_at=? WHERE run_id=? AND root_id=?',
                        (review_domain.CONFIRMED,target['evidence_fingerprint'],thread['evidence_version'],reviewer,stamp,stamp,run_id,root_id))
            con.execute('UPDATE runs SET revision=revision+1 WHERE id=?',(run_id,))
        refreshed=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        response=_worklist(con,refreshed)
        _record_event(con,run_id,token,'confirm_root',response)
        return response

def confirm_batch(path, run_id, revision, reviewer, request_token=None):
    """整批确认：只确认当前无阻断的主单，已确认且证据未变的主单不重复重签。"""
    with transaction(path) as con:
        row=_load_run(con,run_id)
        evidence,state=_current_evidence(con,run_id)
        fp_map={r['root']:r['evidence_fingerprint'] for r in evidence['roots']}
        token=request_token or 'confirm-batch:'+hashlib.sha256(
            '|'.join(f"{r['root']}@{fp_map[r['root']]}" for r in evidence['roots'] if r['confirmable']).encode()).hexdigest()
        # 同一复核重试先于版本校验命中幂等键，原样返回首次响应
        cached=_replayed(con,run_id,token)
        if cached is not None: return cached
        _check_revision(row,revision)
        stamp=now()
        thread=con.execute('SELECT evidence_version FROM review_threads WHERE run_id=?',(run_id,)).fetchone()
        touched=[]
        for item in evidence['roots']:
            if not item['confirmable']: continue
            existing=con.execute('SELECT status,evidence_fingerprint FROM root_reviews WHERE run_id=? AND root_id=?',(run_id,item['root'])).fetchone()
            # 已按当前指纹确认过：幂等跳过，不重签
            if existing and existing['status']==review_domain.CONFIRMED and existing['evidence_fingerprint']==item['evidence_fingerprint']:
                continue
            con.execute('UPDATE root_reviews SET status=?,evidence_fingerprint=?,evidence_version=?,reviewer=?,confirmed_at=?,updated_at=? WHERE run_id=? AND root_id=?',
                        (review_domain.CONFIRMED,item['evidence_fingerprint'],thread['evidence_version'],reviewer,stamp,stamp,run_id,item['root']))
            touched.append(item['root'])
        if touched:
            con.execute('UPDATE runs SET revision=revision+1 WHERE id=?',(run_id,))
        refreshed=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        response=_worklist(con,refreshed);response['confirmed_now']=touched
        _record_event(con,run_id,token,'confirm_batch',response)
        return response

def supplement(path, run_id, new_state, revision, request_token=None):
    """同一批补证：按封存件能否逐张说明依据，裁决选择性重签或整批重置。"""
    with transaction(path) as con:
        row=_load_run(con,run_id)
        new_sha=new_state['manifest']['sha256']
        token=request_token or f"supplement:{new_sha}"
        # 同一复核重试先于版本校验命中幂等键，原样返回首次响应
        cached=_replayed(con,run_id,token)
        if cached is not None: return cached
        _check_revision(row,revision)
        old_state=json.loads(row['result'])
        old_evidence=review_domain.build_root_evidence(old_state)
        new_evidence=review_domain.build_root_evidence(new_state)
        verdict=review_domain.adjudicate(old_evidence,new_evidence)
        # 同一批补证保留原批号；新内容哈希作为证据版本依据留痕
        new_state['manifest']['run_id']=run_id
        thread=con.execute('SELECT * FROM review_threads WHERE run_id=?',(run_id,)).fetchone()
        new_version=thread['evidence_version']+1
        stamp=now()
        con.execute('INSERT INTO evidence_versions(run_id,version,sha256,source_digest,result,created_at) VALUES(?,?,?,?,?,?)',
                    (run_id,new_version,new_sha,new_sha,json.dumps(new_state,ensure_ascii=False),stamp))
        if verdict['mode']=='reset':
            # 封存件无法逐张说明依据：整批回到待复核
            con.execute('DELETE FROM root_reviews WHERE run_id=?',(run_id,))
            for item in new_evidence['roots']:
                con.execute('INSERT INTO root_reviews(run_id,root_id,status,evidence_fingerprint,evidence_version,reviewer,confirmed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                            (run_id,item['root'],review_domain.PENDING,item['evidence_fingerprint'],new_version,None,None,stamp))
        else:
            current={r['root']:r for r in new_evidence['roots']}
            for changed in verdict['changed_roots']:
                item=current[changed]
                old=con.execute('SELECT status FROM root_reviews WHERE run_id=? AND root_id=?',(run_id,changed)).fetchone()
                next_status=review_domain.RESIGN if old and old['status']==review_domain.CONFIRMED else review_domain.PENDING
                con.execute('UPDATE root_reviews SET status=?,evidence_fingerprint=?,evidence_version=?,updated_at=? WHERE run_id=? AND root_id=?',
                            (next_status,item['evidence_fingerprint'],new_version,stamp,run_id,changed))
        con.execute('UPDATE review_threads SET evidence_version=?,current_sha256=?,last_adjudication=?,updated_at=? WHERE run_id=?',
                    (new_version,new_sha,json.dumps(verdict,ensure_ascii=False),stamp,run_id))
        con.execute('UPDATE runs SET result=?,revision=revision+1 WHERE id=?',
                    (json.dumps(new_state,ensure_ascii=False),run_id))
        refreshed=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        response=_worklist(con,refreshed)
        _record_event(con,run_id,token,'supplement',response)
        return response

def _finalize_switch(con, run_id, new_package_id):
    """切换当前交接包：先挂新版再摘旧版，失败由外层事务回滚，旧版保留。"""
    con.execute('UPDATE handoff_packages SET is_current=0 WHERE run_id=?',(run_id,))
    con.execute('UPDATE handoff_packages SET is_current=1 WHERE id=?',(new_package_id,))

def seal_handoff(path, run_id, revision, builder, request_token=None):
    """生成交接封存件并原子切换；切换失败回滚，旧版交接包保持可下载。"""
    # 先在事务外准备内容，事务内只做校验与切换
    con=connect(path)
    try:
        row=_load_run(con,run_id)
        pre_evidence,pre_state=_current_evidence(con,run_id)
        pre_rows={r['root_id']:dict(r) for r in con.execute('SELECT * FROM root_reviews WHERE run_id=?',(run_id,))}
        pre_eligible=[i for i in pre_evidence['roots']
                      if (rr:=pre_rows.get(i['root'])) and rr['status']==review_domain.CONFIRMED
                      and rr['evidence_fingerprint']==i['evidence_fingerprint']]
        token=request_token or 'seal:'+hashlib.sha256(
            '|'.join(f"{i['root']}@{i['evidence_fingerprint']}" for i in pre_eligible).encode()).hexdigest()
        # 同一复核重试先于版本校验命中幂等键，原样返回首次响应
        cached=_replayed(con,run_id,token)
        if cached is not None: return cached
        _check_revision(row,revision)
        evidence,state=pre_evidence,pre_state
        review_rows=pre_rows
        thread=dict(con.execute('SELECT * FROM review_threads WHERE run_id=?',(run_id,)).fetchone())
        versions=[dict(v) for v in con.execute('SELECT version,sha256,source_digest,created_at FROM evidence_versions WHERE run_id=? ORDER BY version',(run_id,))]
    finally:
        con.close()
    eligible=[]
    for item in evidence['roots']:
        rr=review_rows.get(item['root'])
        if rr and rr['status']==review_domain.CONFIRMED and rr['evidence_fingerprint']==item['evidence_fingerprint']:
            eligible.append((item,rr))
    if not eligible:
        raise BusinessError('NO_CONFIRMED_ROOT','没有证据已确认的当前主单，封存件为空')
    package=builder(run_id,state,eligible,evidence['run_block_reasons'],thread,versions)
    sha=hashlib.sha256(package['content']).hexdigest()
    with transaction(path) as con:
        row=_load_run(con,run_id)
        # 同一复核重试先于版本校验命中幂等键，原样返回首次响应
        cached=_replayed(con,run_id,token)
        if cached is not None: return cached
        _check_revision(row,revision)
        duplicate=con.execute('SELECT id FROM handoff_packages WHERE sha256=?',(sha,)).fetchone()
        if duplicate:
            # 同一复核重试：不重复生成第二套数据
            refreshed=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            response=_worklist(con,refreshed)
            _record_event(con,run_id,token,'seal_handoff',response)
            return response
        next_seq=(con.execute('SELECT COALESCE(MAX(package_seq),0)+1 FROM handoff_packages WHERE run_id=?',(run_id,)).fetchone()[0])
        cur=con.execute('INSERT INTO handoff_packages(run_id,package_seq,sha256,filename,content,manifest,is_current,created_at) VALUES(?,?,?,?,?,?,0,?)',
                        (run_id,next_seq,sha,package['filename'],package['content'],json.dumps(package['manifest'],ensure_ascii=False),now()))
        new_id=cur.lastrowid
        _finalize_switch(con,run_id,new_id)  # 抛错则整体回滚，旧版仍为 current
        con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=?',('已封存',run_id))
        refreshed=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        response=_worklist(con,refreshed)
        _record_event(con,run_id,token,'seal_handoff',response)
        return response

def list_packages(path, run_id):
    con=connect(path)
    try:
        _load_run(con,run_id)
        rows=[dict(p) for p in con.execute('SELECT id,package_seq,sha256,filename,is_current,created_at,manifest FROM handoff_packages WHERE run_id=? ORDER BY package_seq',(run_id,))]
    finally:
        con.close()
    for p in rows: p['is_current']=bool(p['is_current'])
    return rows

def get_package(path, run_id, package_id=None):
    con=connect(path)
    try:
        _load_run(con,run_id)
        if package_id is None:
            row=con.execute('SELECT * FROM handoff_packages WHERE run_id=? AND is_current=1',(run_id,)).fetchone()
        else:
            row=con.execute('SELECT * FROM handoff_packages WHERE run_id=? AND id=?',(run_id,package_id)).fetchone()
    finally:
        con.close()
    if not row: raise BusinessError('PACKAGE_NOT_FOUND','交接包不存在',404)
    return dict(row)
