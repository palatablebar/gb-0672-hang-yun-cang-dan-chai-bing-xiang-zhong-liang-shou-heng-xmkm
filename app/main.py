from contextlib import asynccontextmanager
from importlib import import_module
import base64
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store
from app.archive import serialize, build_handoff

@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)

def _token(payload):
    token=payload.get('request_token')
    if token is not None and not isinstance(token,str):
        raise BusinessError('BAD_TOKEN','request_token 须为字符串')
    return token

@app.get('/api/runs/{run_id}/worklist')
def worklist(run_id:str):
    """可交接复核清单：逐张主单的核验依据指纹、阻断原因与复核状态。"""
    return store.worklist(DB_PATH,run_id)

@app.post('/api/runs/{run_id}/roots/{root_id}/confirm')
def confirm_root(run_id:str,root_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    reviewer=str(payload.get('reviewer') or '单证员')[:64]
    return store.confirm_root(DB_PATH,run_id,root_id,revision,reviewer,_token(payload))

@app.post('/api/runs/{run_id}/confirm-batch')
def confirm_batch(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    reviewer=str(payload.get('reviewer') or '单证员')[:64]
    return store.confirm_batch(DB_PATH,run_id,revision,reviewer,_token(payload))

@app.post('/api/runs/{run_id}/supplement')
def supplement(run_id:str,payload:dict=Body(...)):
    """对同一批补充源证据，按封存件能否逐张说明依据裁决重签范围。"""
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，无法追溯补证依据')
    overrides=payload.get('overrides',{})
    if not isinstance(overrides,dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    if 'csv_text' in payload:
        snapshot={'bytes_b64':base64.b64encode(str(payload['csv_text']).encode()).decode(),
                  'context':previous['input_snapshot']['context']}
    else:
        snapshot=previous['input_snapshot']
    new_state=evaluate({'dataset':previous['source'],'overrides':overrides},snapshot=snapshot)
    return store.supplement(DB_PATH,run_id,new_state,revision,_token(payload))

@app.post('/api/runs/{run_id}/seal')
def seal(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.seal_handoff(DB_PATH,run_id,revision,build_handoff,_token(payload))

@app.get('/api/runs/{run_id}/packages')
def packages(run_id:str):
    return store.list_packages(DB_PATH,run_id)

@app.get('/api/runs/{run_id}/packages/{package_id}')
def package_download(run_id:str,package_id:int):
    pkg=store.get_package(DB_PATH,run_id,package_id)
    return Response(pkg['content'],media_type='application/zip',
                    headers={'Content-Disposition':f'attachment; filename="{pkg["filename"]}"'})

@app.get('/api/handoff/{run_id}')
def handoff_latest(run_id:str):
    """当前交接包唯一下载入口；未封存时回退到整批证据包。"""
    try:
        pkg=store.get_package(DB_PATH,run_id)
        return Response(pkg['content'],media_type='application/zip',
                        headers={'Content-Disposition':f'attachment; filename="{pkg["filename"]}"'})
    except BusinessError as error:
        if error.code!='PACKAGE_NOT_FOUND': raise
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
