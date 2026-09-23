"""Single-user, loopback-only review desk. Credentials live in server memory."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import urlparse
import uuid

from langsmith import Client
from langsmith.utils import LangSmithNotFoundError, LangSmithConflictError
from learn import validate_learning
from prepare import ROOT, SPECIALTIES, canonical_hash, align

DATASET = 'clinician-preference-review-100'
ENDPOINT = 'https://langsmith-deid.abridge.services/api/v1'
SCHEMA = 'grouped_note_reflection_v1'
TOKEN = secrets.token_urlsafe(32)
# ponytail: one reviewer; serialize writes, including remote saves. Use per-example locks for multiuser hosting.
LOCK = threading.RLock()
CLIENT = None
DATASET_ID = None
DB = ROOT/'data/review.sqlite3'
ORIGIN = ''
IMPORTED_MTIME = 0
logging.getLogger('langsmith').setLevel(logging.CRITICAL)


def db():
    connection=sqlite3.connect(DB)
    connection.row_factory=sqlite3.Row
    return connection


def initialize():
    global IMPORTED_MTIME
    os.umask(0o077)
    with db() as con:
        con.execute('CREATE TABLE IF NOT EXISTS examples (id TEXT PRIMARY KEY, position INTEGER, state TEXT, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, example_id TEXT, action TEXT, at TEXT, data TEXT)')
        if not con.execute('SELECT 1 FROM examples LIMIT 1').fetchone():
            bundle=json.loads((ROOT/'data/examples.json').read_text())
            for state,key in [('active','examples'),('reserve','substitutes')]:
                for index,example in enumerate(bundle[key]):
                    con.execute('INSERT INTO examples VALUES (?,?,?,?)',(example['id'],index,state,json.dumps(example)))
        else:
            # Finish an initial background generation without overwriting any human revisions.
            bundle=json.loads((ROOT/'data/examples.json').read_text())
            for key,state in [('examples','active'),('substitutes','reserve')]:
                for index,incoming in enumerate(bundle[key]):
                    row=con.execute('SELECT data FROM examples WHERE id=?',(incoming['id'],)).fetchone()
                    if row:
                        old=json.loads(row['data'])
                        if old['revision']==1 and old['status']=='draft' and not old['metadata'].get('source_example_id'):
                            con.execute('UPDATE examples SET data=? WHERE id=?',(json.dumps(incoming),incoming['id']))
                    elif state=='reserve':con.execute('INSERT INTO examples VALUES (?,?,?,?)',(incoming['id'],index,state,json.dumps(incoming)))
    IMPORTED_MTIME=(ROOT/'data/examples.json').stat().st_mtime_ns


def get_example(con, identity):
    row=con.execute('SELECT data FROM examples WHERE id=? AND state=\'active\'',(identity,)).fetchone()
    if not row: raise ValueError('Example not found.')
    return json.loads(row['data'])


def validate_example(example):
    cases=example['cases']; meta=example['metadata']
    if len(cases)!=5 or meta['specialty'] not in SPECIALTIES or meta['cohort_type'] not in ('patient','random'):
        raise ValueError('Expected five encounters in a selected specialty.')
    if len({c['source']['masked_encounter_id'] for c in cases})!=5:
        raise ValueError('Duplicate encounters.')
    if len({c['source']['masked_patient_id'] for c in cases})!=(1 if meta['cohort_type']=='patient' else 5):
        raise ValueError('Patient grouping does not match the sampling type.')
    for case in cases:
        source=case['source'];align(source)
        if source['specialty']!=meta['specialty']:raise ValueError('Wrong specialty.')
        if meta['cohort_type']=='random' and source['split']!='test':raise ValueError('Random set must use test records.')
        for field in ('deid_soap_note','deid_soap_note_edited'):
            if canonical_hash(source[field])!=case['source_note_sha256'][field]:raise ValueError('Source integrity check failed.')
    validate_learning(example,{k:v for k,v in meta['learning'].items() if k!='generation'})


def save_local(con,example,action):
    con.execute('INSERT INTO history(example_id,action,at,data) VALUES (?,?,?,?)',
                (example['id'],action,datetime.now(timezone.utc).isoformat(),json.dumps(example)))
    con.execute('UPDATE examples SET data=? WHERE id=?',(json.dumps(example),example['id']))


def overview():
    with LOCK:
        if (ROOT/'data/examples.json').stat().st_mtime_ns!=IMPORTED_MTIME:initialize()
    with db() as con:
        rows=con.execute("SELECT data,state FROM examples WHERE state IN ('active','reserve') ORDER BY position").fetchall()
    examples=[]; reserves={}
    for row in rows:
        example=json.loads(row['data']);m=example['metadata']
        if row['state']=='reserve':
            if m.get('learning'):
                key=m['specialty']+'|'+m['cohort_type'];reserves[key]=reserves.get(key,0)+1
        else:
            examples.append({'id':example['id'],'revision':example['revision'],'status':example['status'],
                'specialty':m['specialty'],'cohort_type':m['cohort_type'],
                'outcome':(m.get('learning') or {}).get('outcome','pending')})
    return {'examples':examples,'reserves':reserves,'connected':CLIENT is not None,
            'dataset_name':DATASET,'dataset_id':DATASET_ID,'endpoint':ENDPOINT}


def connect(body):
    global CLIENT,DATASET_ID
    CLIENT=None;DATASET_ID=None
    key=body.get('api_key','').strip();workspace=body.get('workspace_id','').strip()
    if not key or len(key)>1000:raise ValueError('Enter a LangSmith De-ID API key.')
    if workspace:
        try:uuid.UUID(workspace)
        except ValueError:raise ValueError('Workspace ID must be a UUID.') from None
    client=Client(api_url=ENDPOINT,api_key=key,workspace_id=workspace or None,
                  timeout_ms=25000,auto_batch_tracing=False)
    # Authenticated dataset listing, not the public /info endpoint, verifies access.
    matches=list(client.list_datasets(dataset_name=DATASET,limit=2))
    dataset=matches[0] if matches else None
    if dataset:
        metadata=dataset.metadata or {}
        if metadata.get('schema')!=SCHEMA:
            raise ValueError('A dataset with this name exists but has a different or unspecified schema. It was not modified.')
    CLIENT=client;DATASET_ID=str(dataset.id) if dataset else None
    return overview()


def payload(example):
    validate_example(example)
    metadata=deepcopy(example['metadata'])
    metadata.update({'example_id':example['id'],'review_revision':example['revision'],
                     'review_status':'human_approved','reviewed_at':datetime.now(timezone.utc).isoformat(),
                     'source_encounters':[{k:v for k,v in c['source'].items() if k not in ('deid_soap_note','deid_soap_note_edited')} for c in example['cases']],
                     'source_note_sha256':[c['source_note_sha256'] for c in example['cases']]})
    return {'inputs':{'encounters':[{'encounter_id':c['source']['masked_encounter_id'],
             'patient_id':c['source']['masked_patient_id'],'generated_note':c['source']['deid_soap_note'],
             'edited_note':c['source']['deid_soap_note_edited']} for c in example['cases']]},
            'metadata':metadata}


def publish(example):
    global DATASET_ID
    if CLIENT is None:raise ValueError('Connect to LangSmith De-ID first.')
    value=payload(example)
    if DATASET_ID is None:
        try:
            dataset=CLIENT.create_dataset(DATASET,description='Human-reviewed exploratory note-preference examples: five generated/edited note pairs per example; authorship unverified.',
                metadata={'schema':SCHEMA},inputs_schema={'type':'object','required':['encounters'],'properties':{'encounters':{'type':'array','minItems':5,'maxItems':5,'items':{'type':'object'}}}})
        except LangSmithConflictError:
            dataset=CLIENT.read_dataset(dataset_name=DATASET)
            if (dataset.metadata or {}).get('schema')!=SCHEMA:raise ValueError('Dataset schema collision.') from None
        DATASET_ID=str(dataset.id)
    remote_id=str(uuid.uuid5(uuid.UUID(DATASET_ID),example['id']))
    try:
        previous=CLIENT.read_example(remote_id)
    except LangSmithNotFoundError:
        previous=None
    if previous:
        if str(previous.dataset_id)!=DATASET_ID:raise ValueError('Remote example belongs to another dataset.')
        CLIENT.update_example(remote_id,**value)
    else:
        try:CLIENT.create_example(dataset_id=DATASET_ID,example_id=remote_id,**value)
        except LangSmithConflictError:
            # Another retry may have completed the write; verify ownership before updating.
            previous=CLIENT.read_example(remote_id)
            if str(previous.dataset_id)!=DATASET_ID:raise ValueError('Remote identity collision.') from None
            CLIENT.update_example(remote_id,**value)
    confirmed=CLIENT.read_example(remote_id)
    if (confirmed.inputs!=value['inputs'] or confirmed.metadata.get('review_revision')!=example['revision']
            or confirmed.metadata.get('learning')!=value['metadata']['learning']):
        raise ValueError('Remote save could not be verified; retry uses the same example ID.')
    example['status']='sent'
    example['remote']={'dataset_id':DATASET_ID,'example_id':remote_id,
                       'url':str(confirmed.url) if confirmed.url else None,'revision':example['revision']}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def respond(self,value,status=200,kind='application/json'):
        data=json.dumps(value,ensure_ascii=False).encode() if kind=='application/json' else value
        self.send_response(status)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers();self.wfile.write(data)

    def host_valid(self):
        return self.headers.get('Host')==ORIGIN.removeprefix('http://')

    def do_GET(self):
        if not self.host_valid():return self.respond({'error':'Invalid host.'},403)
        path=urlparse(self.path).path
        try:
            if path=='/api/overview':return self.respond(overview())
            if path.startswith('/api/example/'):
                with db() as con:value=get_example(con,path.split('/')[-1])
                return self.respond(value)
            files={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/diff.js':('diff.js','text/javascript')}
            if path not in files:return self.respond({'error':'Not found.'},404)
            filename,kind=files[path];data=(ROOT/filename).read_bytes()
            if path=='/':data=data.replace(b'CSRF_TOKEN',TOKEN.encode())
            return self.respond(data,kind=kind)
        except (ValueError,KeyError):self.respond({'error':'Example not found.'},404)

    def do_POST(self):
        if not self.host_valid() or self.headers.get('Origin')!=ORIGIN or not secrets.compare_digest(self.headers.get('X-Review-Token',''),TOKEN):
            return self.respond({'error':'Invalid request origin or token.'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<=0 or size>100000:return self.respond({'error':'Invalid request size.'},413)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Expected JSON.')
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict):raise ValueError('Expected an object.')
            with LOCK:
                if self.path=='/api/connect':return self.respond(connect(body))
                if self.path=='/api/disconnect':
                    global CLIENT,DATASET_ID
                    CLIENT=None;DATASET_ID=None
                    return self.respond(overview())
                if self.path not in ('/api/save','/api/send','/api/substitute'):raise ValueError('Unknown action.')
                with db() as con:
                    example=get_example(con,body.get('id'))
                    if body.get('revision')!=example['revision']:
                        return self.respond({'error':'This card changed in another window. Reload before editing.'},409)
                    if self.path=='/api/substitute':
                        reserves=con.execute("SELECT id,data FROM examples WHERE state='reserve' ORDER BY position").fetchall()
                        reserve=next((r for r in reserves if all(json.loads(r['data'])['metadata'][k]==example['metadata'][k] for k in ('specialty','cohort_type'))),None)
                        if not reserve:raise ValueError('No unused substitute remains for this specialty and sampling type.')
                        save_local(con,example,'before_substitution')
                        replacement=json.loads(reserve['data']);validate_example(replacement)
                        replacement['metadata']['source_example_id']=replacement['id']
                        replacement.update(id=example['id'],revision=example['revision']+1,status='draft')
                        if example.get('remote'):replacement['remote']=example['remote']
                        example=replacement
                        con.execute("UPDATE examples SET state='used' WHERE id=?",(reserve['id'],))
                        save_local(con,example,'substituted')
                    else:
                        learning=validate_learning(example,body.get('learning'))
                        previous=example['metadata'].get('learning') or {}
                        if previous.get('generation'):learning['generation']=previous['generation']
                        save_local(con,example,'before_revision')
                        example['metadata']['learning']=learning
                        example['revision']+=1;example['status']='revised'
                        save_local(con,example,'saved_locally')
                # Commit the local draft before network I/O so failures never lose the user's edits.
                if self.path=='/api/send':
                    publish(example)
                    with db() as con:save_local(con,example,'sent_to_langsmith')
                return self.respond(example)
        except (ValueError,TypeError,KeyError) as exc:
            # Pydantic error strings can echo input; never send the full validation exception.
            message='Invalid learning or request. Check the fields and evidence references.' if type(exc).__name__=='ValidationError' else str(exc)
            self.respond({'error':message[:350]},400)
        except Exception:
            self.respond({'error':'LangSmith request failed. Local edits are saved; check credentials, workspace and network, then reload and retry. No note content was logged.'},502)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args();ORIGIN=f'http://127.0.0.1:{args.port}'
    initialize()
    print(f'Review desk: {ORIGIN} · dataset: {DATASET}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
