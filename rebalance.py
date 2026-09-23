"""Replace abstentions with audited, type-matched new sets; never relabel to meet quota."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import tempfile
import uuid

import prepare
from prepare import ROOT, SPECIALTIES, TABLE, query, base, align, canonical_hash
from learn import draft
from server import db, validate_example, save_local


def category(example):
    return example['metadata']['specialty'],example['metadata']['cohort_type']


def read_state():
    with db() as con:
        rows=con.execute('SELECT id,state,position,data FROM examples ORDER BY position').fetchall()
    return [(row['state'],json.loads(row['data'])) for row in rows]


def plan(active, positives, originals):
    """Only unchanged abstention slots can be replaced; original positive cards are never targets."""
    slots=defaultdict(list)
    remaining=max(0,50-sum(e['metadata']['learning']['outcome']=='preference' for e in active))
    for e in active:
        original=originals.get(e['id'])
        if (original and e['revision']==original['revision'] and e['status']=='draft'
                and e['metadata']['learning']['outcome']=='abstain'):
            slots[category(e)].append(e)
    result=[]
    for candidate in positives:
        if remaining and slots[category(candidate)]:
            result.append((slots[category(candidate)].pop(0),candidate));remaining-=1
    return result,remaining


def candidates(excluded, categories, seed):
    patient_specs=sorted({s for s,k in categories if k=='patient'})
    random_specs=sorted({s for s,k in categories if k=='random'})
    params=prepare.parameters()+[
        'excluded:ARRAY<STRING>:'+json.dumps(sorted(excluded)),
        'patient_specs:ARRAY<STRING>:'+json.dumps(patient_specs),
        'random_specs:ARRAY<STRING>:'+json.dumps(random_specs)]
    sql=f"""WITH latest AS ({base()}), eligible AS (
      SELECT * FROM latest WHERE masked_patient_id NOT IN UNNEST(@excluded)),
      patients AS (SELECT specialty,masked_patient_id,COUNT(*) n FROM eligible
        WHERE specialty IN UNNEST(@patient_specs) GROUP BY 1,2 HAVING n>=5),
      picked AS (SELECT * FROM patients QUALIFY ROW_NUMBER() OVER(PARTITION BY specialty
        ORDER BY n>=7 DESC,FARM_FINGERPRINT(CONCAT(masked_patient_id,'{seed}')))<=8),
      patient_rows AS (SELECT e.*,'patient' cohort_type FROM eligible e JOIN picked p USING(specialty,masked_patient_id)
        QUALIFY ROW_NUMBER() OVER(PARTITION BY specialty,masked_patient_id
          ORDER BY FARM_FINGERPRINT(CONCAT(masked_encounter_id,'{seed}')))<=9),
      random_rows AS (SELECT e.*,'random' cohort_type FROM eligible e
        WHERE split='test' AND file_updated_at>=TIMESTAMP('2026-09-14')
          AND specialty IN UNNEST(@random_specs) AND masked_patient_id NOT IN (SELECT masked_patient_id FROM picked)
        QUALIFY ROW_NUMBER() OVER(PARTITION BY specialty ORDER BY FARM_FINGERPRINT(CONCAT(masked_encounter_id,'{seed}')))<=20)
      SELECT * FROM patient_rows UNION ALL SELECT * FROM random_rows LIMIT 1000"""
    rows=query(sql,params,budget=15_000_000_000)
    if not rows:return []
    keys=[{'id':r['masked_encounter_id'],'version':r['file_updated_at']} for r in rows]
    partitions=','.join("DATE('"+d+"')" for d in sorted({r['file_updated_at'][:10] for r in rows}))
    sql=f"""SELECT t.masked_encounter_id,t.file_updated_at,t.deid_soap_note,t.deid_soap_note_edited,
      JSON_VALUE(t.imputed_context,'$.encounter.appointment_time') appointment_time
      FROM `{TABLE}` t JOIN UNNEST(JSON_QUERY_ARRAY(@keys)) k
      ON t.masked_encounter_id=JSON_VALUE(k,'$.id') AND t.file_updated_at=TIMESTAMP(JSON_VALUE(k,'$.version'))
      WHERE DATE(t.file_updated_at) IN ({partitions}) LIMIT 1000"""
    params=['keys:JSON:'+json.dumps(keys)]
    estimate=query(sql,params,budget=800_000_000_000,dry=True)
    print(json.dumps({'candidate_encounters':len(rows),'bytes_to_scan':estimate.get('statistics',{}).get('totalBytesProcessed')}),flush=True)
    payload=query(sql,params,budget=800_000_000_000)
    metadata={r['masked_encounter_id']:r for r in rows}
    # Exclude every candidate patient on later rounds, including malformed/unselected rows.
    excluded.update(r['masked_patient_id'] for r in rows)
    grouped=defaultdict(list)
    for row in payload:
        source=dict(metadata[row['masked_encounter_id']]);kind=source.pop('cohort_type');source.update(row)
        try:
            for field in ('deid_soap_note','deid_soap_note_edited'):
                if isinstance(source[field],str):source[field]=json.loads(source[field])
            pairs,missing=align(source)
            changed=[p for p in pairs if p['before'] is not None and ' '.join(p['before'].split())!=' '.join(p['after'].split())]
            if not changed or not source.get('appointment_time'):continue
        except (ValueError,KeyError,TypeError):continue
        case={'source':source,'source_note_sha256':{k:canonical_hash(source[k]) for k in ('deid_soap_note','deid_soap_note_edited')},
              'observations':{'modified_summaries':len(changed),'unobserved_generated':missing}}
        grouped[source['specialty'],kind,source['masked_patient_id'] if kind=='patient' else ''].append(case)
    result=[];used=set();prepare.SEED=seed
    for (spec,kind,patient),cases in grouped.items():
        cases.sort(key=lambda c:hashlib.sha256((seed+c['source']['masked_encounter_id']).encode()).hexdigest())
        if kind=='patient':
            distinct={}
            for c in cases:distinct.setdefault(c['source']['appointment_time'],c)
            if len(distinct)>=5 and patient not in used:
                selected=sorted(list(distinct.values())[:5],key=lambda c:c['source']['appointment_time'])
                result.append(prepare.make_example(spec,kind,selected));used.add(patient)
        else:
            selected=[]
            for c in cases:
                p=c['source']['masked_patient_id']
                if p in used:continue
                used.add(p);selected.append(c)
                if len(selected)==5:
                    result.append(prepare.make_example(spec,kind,selected));selected=[]
    return result


def ready(active, positives, reviewed, originals):
    replacements,remaining=plan(active,positives,originals)
    consumed={c['id'] for _,c in replacements}
    reserve_categories={category(e) for state,e in read_state() if state=='reserve' and e['id'] in consumed}
    spare_categories={category(e) for e in reviewed if e['id'] not in consumed}
    return not remaining and reserve_categories<=spare_categories


def install(positives,reviewed,originals,run_id):
    with (ROOT/'data/preparation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            active=[json.loads(r['data']) for r in con.execute("SELECT data FROM examples WHERE state='active' ORDER BY position")]
            replacements,remaining=plan(active,positives,originals)
            if remaining:raise ValueError('Deck changed during review; not enough eligible replacements. Nothing installed.')
            consumed={candidate['id'] for _,candidate in replacements}
            reserves=[json.loads(r['data']) for r in con.execute("SELECT data FROM examples WHERE state='reserve'")]
            to_refill={category(e) for e in reserves if e['id'] in consumed}
            refills={}
            for e in reviewed:
                if category(e) in to_refill and e['id'] not in consumed:refills.setdefault(category(e),e)
            if set(refills)!=to_refill:raise ValueError('Missing replacement reserves; nothing installed.')
            for old,candidate in replacements:
                validate_example(candidate);save_local(con,old,'before_outcome_rebalance')
                replacement=deepcopy(candidate)
                replacement['metadata']['source_example_id']=candidate['id']
                replacement['metadata']['outcome_enrichment']={
                    'target':'50 preference / 50 abstention sets','run_id':run_id,
                    'selection':'Retained after an unchanged reference-learning prompt and second evidence audit produced a preference.',
                    'limitation':'Outcome-selected fixtures; the learning rate is not a natural-prevalence estimate.',
                    'replaced_revision':old['revision']}
                replacement.update(id=old['id'],revision=old['revision']+1,status='draft')
                if old.get('remote'):replacement['remote']=old['remote']
                save_local(con,replacement,'outcome_rebalanced')
                con.execute("UPDATE examples SET state='used' WHERE id=? AND state='reserve'",(candidate['id'],))
            for e in refills.values():
                validate_example(e)
                con.execute('INSERT INTO examples VALUES (?,?,?,?)',(e['id'],0,'reserve',json.dumps(e)))
            updated=[json.loads(r['data']) for r in con.execute("SELECT data FROM examples WHERE state='active' ORDER BY position")]
            assert Counter(e['metadata']['learning']['outcome'] for e in updated)=={'preference':50,'abstain':50}
            assert Counter(category(e) for e in updated)==Counter(category(e) for e in active)
            for e in updated:
                before=originals.get(e['id'])
                if before and before['metadata']['learning']['outcome']=='preference':assert e==before
        # A crash between DB commit and manifest update can be recovered from SQLite; the DB is authoritative.
        with db() as con:
            current={key:[json.loads(r['data']) for r in con.execute('SELECT data FROM examples WHERE state=? ORDER BY position',(state,))]
                     for key,state in [('examples','active'),('substitutes','reserve')]}
        path=ROOT/'data/examples.json';bundle=json.loads(path.read_text());bundle.update(current)
        bundle['outcome_enrichment']={'run_id':run_id,'target':'50/50','replaced_sets':len(replacements)}
        temp=path.with_suffix('.balance.tmp');temp.write_text(json.dumps(bundle,ensure_ascii=False));temp.replace(path)
        summary={'run_id':run_id,'replaced_sets':len(replacements),'outcomes':{'preference':50,'abstain':50},
                 'matched_specialty_and_grouping':True,'original_preferences_preserved':True,
                 'retained_substitutes':len(current['substitutes']),'no_automatic_langsmith_upload':True}
        (ROOT/'data/rebalance-summary.json').write_text(json.dumps(summary,indent=2))
        print(json.dumps(summary),flush=True)


def main():
    os.umask(0o077)
    run_id='balance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    state=read_state();active=[e for s,e in state if s=='active'];originals={e['id']:e for e in active}
    if sum(e['metadata']['learning']['outcome']=='preference' for e in active)>=50:
        print('The active deck already has at least 50 preference examples.');return
    excluded={c['source']['masked_patient_id'] for _,e in state for c in e['cases']}
    for path in (ROOT.parent/'datasets').glob('*/cohort.json'):
        excluded.update(c['source']['masked_patient_id'] for c in json.loads(path.read_text())['cases'])
    positives=[e for s,e in state if s=='reserve' and e['metadata']['learning']['outcome']=='preference']
    reviewed=[];failed=0;sampled=0
    with tempfile.TemporaryDirectory(prefix='preference-balance-') as directory:
        for round_number in range(1,7):
            replacements,_=plan(active,positives,originals)
            filled=Counter(category(c) for _,c in replacements)
            capacity=Counter(category(e) for e in active if e['metadata']['learning']['outcome']=='abstain')
            needed={k for k,n in capacity.items() if n>filled[k]}
            # Include consumed-reserve strata so their replacement pool can be replenished.
            needed.update(category(e) for e in positives if any(s=='reserve' and r['id']==e['id'] for s,r in state))
            batch=candidates(excluded,needed,run_id+'-round-'+str(round_number))
            # Interleave specialties so the search does not fill its target from only the first one.
            buckets=defaultdict(list)
            for e in batch:buckets[category(e)].append(e)
            batch=[]
            while any(buckets.values()):
                for key in sorted(buckets):
                    if buckets[key]:batch.append(buckets[key].pop())
            print(json.dumps({'round':round_number,'valid_candidate_sets':len(batch),'audited_positive_pool':len(positives)}),flush=True)
            iterator=iter(batch)
            with ThreadPoolExecutor(max_workers=6) as pool:
                tasks={}
                for _ in range(6):
                    candidate=next(iterator,None)
                    if candidate:tasks[pool.submit(draft,candidate)]=candidate
                while tasks:
                    done,_=wait(tasks,return_when=FIRST_COMPLETED)
                    for future in done:
                        candidate=tasks.pop(future);sampled+=1
                        try:
                            candidate['metadata']['learning']=future.result();validate_example(candidate)
                            reviewed.append(candidate)
                            if candidate['metadata']['learning']['outcome']=='preference':positives.append(candidate)
                            # Private checkpoint is deleted after this run; only installed sets are retained.
                            with open(directory+'/reviewed.json','w') as handle:json.dump(reviewed,handle)
                            _,remaining=plan(active,positives,originals)
                            print(json.dumps({'reviewed_sets':sampled,'audited_positive_pool':len(positives),'additional_positives_needed':remaining}),flush=True)
                        except Exception as error:
                            failed+=1;print(json.dumps({'failed_drafts':failed,'error_type':type(error).__name__}),flush=True)
                    if not ready(active,positives,reviewed,originals):
                        while len(tasks)<6:
                            candidate=next(iterator,None)
                            if candidate is None:break
                            tasks[pool.submit(draft,candidate)]=candidate
            if ready(active,positives,reviewed,originals):
                install(positives,reviewed,originals,run_id);return
        raise RuntimeError('Could not reach the balanced target with audited, type-matched replacements; active deck unchanged.')


if __name__=='__main__':main()
