"""Run with Bilrost's installed Python: python test_review.py."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import urllib.request
import urllib.error
import uuid

import server
from prepare import align, canonical_hash
from learn import validate_learning


def fixture():
    cases=[]
    for i in range(5):
        source={'masked_encounter_id':f'encounter-{i}','masked_patient_id':'patient-a',
                'specialty':server.SPECIALTIES[0],'split':'test',
                'deid_soap_note':{'HPI':[{'id':'hpi','summary':'First item. Second item.'}],
                                  'Exam':[{'id':'exam','summary':'Unobserved source.'}]},
                'deid_soap_note_edited':[{'key':'HPI','summaries':[{'id':'hpi','text':'- First item.\n- Second item.'}]}]}
        cases.append({'source':source,'source_note_sha256':{k:canonical_hash(source[k]) for k in ('deid_soap_note','deid_soap_note_edited')}})
    learning={'outcome':'preference','preference':'Use separate bullets for HPI items.',
              'explanation':'The same presentation change occurs across several encounters.',
              'evidence':[{'encounter_id':f'encounter-{i}','section':'HPI','summary_id':'hpi','observation':'The prose items were placed on separate bullet lines.'} for i in (0,1)],
              'limits':'Common authorship is not verified.','confidence':'medium','avoid_learning':'Do not add clinical facts absent from the current encounter.'}
    return {'id':str(uuid.uuid4()),'revision':1,'status':'draft','cases':cases,
            'metadata':{'specialty':server.SPECIALTIES[0],'cohort_type':'patient','learning':learning}}


class FakeClient:
    def __init__(self):self.examples={};self.creates=0
    def read_example(self,identity):
        if identity not in self.examples:raise server.LangSmithNotFoundError('Missing')
        return self.examples[identity]
    def create_example(self,*,example_id,dataset_id,inputs,metadata):
        self.creates+=1
        self.examples[example_id]=SimpleNamespace(dataset_id=dataset_id,inputs=inputs,metadata=metadata,url=None)
    def update_example(self,identity,*,inputs,metadata):
        self.examples[identity].inputs=inputs;self.examples[identity].metadata=metadata


def run():
    from rebalance import plan
    original_cards=[]
    for i in range(100):
        card=fixture();card['id']=f'card-{i}'
        if i>=27:
            card['metadata']['learning']['outcome']='abstain';card['metadata']['learning']['preference']=''
        original_cards.append(card)
    original_map={e['id']:deepcopy(e) for e in original_cards}
    replacements=[fixture() for _ in range(30)]
    swaps,remaining=plan(original_cards,replacements,original_map)
    assert len(swaps)==23 and remaining==0
    assert all(old['metadata']['learning']['outcome']=='abstain' for old,new in swaps)
    wrong_type=fixture();wrong_type['metadata']['cohort_type']='random'
    assert plan(original_cards,[wrong_type],original_map)[1]==23
    original_cards[27]['revision']+=1
    swaps,_=plan(original_cards,replacements,original_map)
    assert all(old['id']!='card-27' for old,new in swaps)
    original_client=server.Client
    dataset=SimpleNamespace(id=uuid.uuid4(),metadata={'schema':server.SCHEMA})
    class ExistingDatasetClient:
        def __init__(self,**kwargs):pass
        def list_datasets(self,**kwargs):return iter([dataset])
    server.Client=ExistingDatasetClient
    original_overview=server.overview;server.overview=lambda:{'connected':server.CLIENT is not None}
    assert server.connect({'api_key':'test-placeholder'})['connected'] is True
    assert server.DATASET_ID==str(dataset.id)
    dataset.metadata={'schema':'different'}
    try:server.connect({'api_key':'test-placeholder'})
    except ValueError:pass
    else:raise AssertionError('Existing incompatible dataset accepted')
    assert server.CLIENT is None
    server.Client=original_client;server.overview=original_overview
    example=fixture()
    rows,missing=align(example['cases'][0]['source'])
    assert missing==1 and len(rows)==1 and rows[0]['status']=='modified'
    server.validate_example(example)
    bad=deepcopy(example['metadata']['learning']);bad['evidence'][0]['summary_id']='unknown'
    try:validate_learning(example,bad)
    except ValueError:pass
    else:raise AssertionError('Unknown evidence accepted')
    body=server.payload(example)
    assert 'learning' not in body['inputs'] and body['metadata']['learning']['outcome']=='preference'
    assert 'deid_soap_note' not in body['metadata']['source_encounters'][0]
    client=FakeClient();server.CLIENT=client;server.DATASET_ID=str(uuid.uuid4())
    server.publish(example);server.publish(example)
    assert client.creates==1 and len(client.examples)==1 and example['status']=='sent'
    example['revision']+=1;server.publish(example)
    assert client.creates==1 and next(iter(client.examples.values())).metadata['review_revision']==2
    server.CLIENT=None;server.DATASET_ID=None
    with tempfile.TemporaryDirectory() as directory:
        old_db=server.DB;server.DB=Path(directory)/'review.sqlite3'
        with server.db() as con:
            con.execute('CREATE TABLE examples (id TEXT PRIMARY KEY, position INTEGER, state TEXT, data TEXT NOT NULL)')
            con.execute('CREATE TABLE history (id INTEGER PRIMARY KEY, example_id TEXT, action TEXT, at TEXT, data TEXT)')
            con.execute('INSERT INTO examples VALUES (?,?,?,?)',(example['id'],0,'active',json.dumps(example)))
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        server.ORIGIN=f'http://127.0.0.1:{http.server_address[1]}'
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        def request(path,data,headers):
            return urllib.request.urlopen(urllib.request.Request(server.ORIGIN+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json',**headers}),timeout=5)
        data={'id':example['id'],'revision':example['revision'],'learning':example['metadata']['learning']}
        for headers in ({},{'Origin':'https://untrusted.example','X-Review-Token':server.TOKEN}):
            try:request('/api/save',data,headers)
            except urllib.error.HTTPError as error:assert error.code==403
            else:raise AssertionError('Cross-origin write accepted')
        headers={'Origin':server.ORIGIN,'X-Review-Token':server.TOKEN}
        with request('/api/save',data,headers) as response:
            saved=json.load(response);assert saved['revision']==data['revision']+1
        try:request('/api/save',data,headers)
        except urllib.error.HTTPError as error:assert error.code==409
        else:raise AssertionError('Stale revision accepted')
        data['revision']=saved['revision']
        try:request('/api/send',data,headers)
        except urllib.error.HTTPError as error:assert error.code==400
        else:raise AssertionError('Unauthenticated send accepted')
        with server.db() as con:
            assert server.get_example(con,example['id'])['revision']==data['revision']+1
            replacement=fixture();replacement['id']=str(uuid.uuid4())
            for c in replacement['cases']:
                c['source']['masked_patient_id']='patient-b'
                c['source']['masked_encounter_id']+='-replacement'
            for evidence in replacement['metadata']['learning']['evidence']:evidence['encounter_id']+='-replacement'
            con.execute('INSERT INTO examples VALUES (?,?,?,?)',(replacement['id'],0,'reserve',json.dumps(replacement)))
        data['revision']+=1
        with request('/api/substitute',data,headers) as response:
            substituted=json.load(response)
        assert substituted['id']==example['id'] and substituted['revision']==data['revision']+1
        assert substituted['metadata']['specialty']==example['metadata']['specialty']
        assert substituted['cases'][0]['source']['masked_patient_id']=='patient-b'
        with server.db() as con:
            assert con.execute("SELECT COUNT(*) FROM examples WHERE state='reserve'").fetchone()[0]==0
            assert con.execute('SELECT COUNT(*) FROM history').fetchone()[0]>=5
        http.shutdown();http.server_close();server.DB=old_db
    print('PASS: alignment, evidence, export shape, idempotent writes, CSRF, revision conflicts, persistence on send failure, and quota-preserving substitution with history.')


if __name__=='__main__':run()
