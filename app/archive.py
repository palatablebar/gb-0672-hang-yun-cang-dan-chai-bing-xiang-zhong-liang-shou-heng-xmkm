import io, csv, json, zipfile, hashlib
from datetime import datetime, timezone
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
    return buffer.getvalue()

def build_handoff(run_id, state, eligible, run_block_reasons, thread, versions):
    """交接封存件：只装入证据已确认的当前主单，逐张写清依据指纹与源证据版本。

    eligible: [(root_evidence, root_review), ...]
    封存件自身可解释每张主单依据哪份源证据（sha256 + 证据版本 + 指纹）。
    """
    stamp=datetime.now(timezone.utc).isoformat()
    roots_manifest=[]
    root_evidence={}
    for item, rr in eligible:
        roots_manifest.append({
            'root':item['root'],
            'member_count':item['member_count'],
            'evidence_fingerprint':item['evidence_fingerprint'],
            'evidence_version':rr['evidence_version'],
            'reviewer':rr['reviewer'],
            'confirmed_at':rr['confirmed_at'],
        })
        root_evidence[item['root']]=item['evidence_bundle']
    manifest={
        'run_id':run_id,
        'package_created_at':stamp,
        'rule_version':state['manifest']['rule_version'],
        'current_sha256':thread['current_sha256'],
        'evidence_version':thread['evidence_version'],
        'source_versions':versions,
        'confirmed_roots':roots_manifest,
        'excluded_roots':[r for r in state['graph']['roots'] if r not in {i['root'] for i,_ in eligible}],
        'run_block_reasons':run_block_reasons,
    }
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('handoff/manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        archive.writestr('handoff/root_evidence.json',json.dumps(root_evidence,ensure_ascii=False,indent=2))
        archive.writestr('handoff/source_snapshot.json',json.dumps({
            'sha256':state['manifest']['sha256'],
            'context':state['context'],
            'source':state['source'],
        },ensure_ascii=False,indent=2))
    content=buffer.getvalue()
    return {'filename':f'{run_id}-handoff-v{thread["evidence_version"]}.zip',
            'content':content,'manifest':manifest}
