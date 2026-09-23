"""Read-only, bounded source sampling. Never print note payloads."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import hashlib
import uuid
import fcntl
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent
SPECIALTIES = ['Family Medicine/Family Practice', 'Internal Medicine', 'Emergency Medicine',
               'Pediatrics', 'Obstetrics and Gynecology', 'Psychiatry', 'Orthopedic Surgery',
               'Neurology', 'Gastroenterology', 'Hematology and Oncology']
TABLE = 'abridge-client-prod.dbt_prod_deid_train.dm_train'
FLOOR = '2026-07-24'
SEED = 'preference-review-100-2026-09-23-v1'


def query(sql, params=(), budget=150_000_000_000, dry=False):
    command = ['bq', '--project_id=abridge-client-prod', '--quiet', 'query',
               '--use_legacy_sql=false', '--format=prettyjson', '--max_rows=1000',
               f'--maximum_bytes_billed={budget}']
    command.extend('--parameter=' + item for item in params)
    if dry:
        command.append('--dry_run')
    result = subprocess.run(command, input=sql, text=True, capture_output=True)
    if result.returncode:
        # Errors can contain source values; do not forward subprocess output.
        raise RuntimeError(f'BigQuery failed (exit {result.returncode}); inspect the job in BigQuery.')
    return json.loads(result.stdout)


def base():
    return f"""SELECT masked_encounter_id, masked_patient_id, file_updated_at, specialty,
      split, specialty_note_type, care_setting, hpi_style, hpi_format, pbap_style, pbap_format,
      sent_to_ehr, sent_to_ehr_at
      FROM `{TABLE}`
      WHERE file_updated_at >= TIMESTAMP('{FLOOR}')
        AND file_updated_at < TIMESTAMP('2026-09-24')
        AND specialty IN UNNEST(@specialties) AND sent_to_ehr = TRUE
        AND masked_encounter_id IS NOT NULL AND masked_patient_id IS NOT NULL
        AND split IN ('train','test','validation')
      QUALIFY ROW_NUMBER() OVER (PARTITION BY masked_encounter_id ORDER BY file_updated_at DESC)=1"""


def parameters():
    return ['specialties:ARRAY<STRING>:' + json.dumps(SPECIALTIES)]


def inventory():
    sql = f"""WITH latest AS ({base()}), patients AS (
      SELECT specialty, masked_patient_id, COUNT(*) n, COUNTIF(split='test') test_n
      FROM latest GROUP BY 1,2)
      SELECT specialty, SUM(n) encounters, SUM(test_n) test_encounters,
        COUNTIF(n>=5) patients_with_five, COUNTIF(n>=7) patients_with_seven
      FROM patients GROUP BY 1 ORDER BY 1 LIMIT 100"""
    result = query(sql, parameters(), budget=10_000_000_000)
    print(json.dumps(result, indent=2))


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def align(source):
    generated, edited = source['deid_soap_note'], source['deid_soap_note_edited']
    if not isinstance(generated, dict) or not isinstance(edited, list):
        raise ValueError('Invalid notes')
    baseline = {}
    for section, summaries in generated.items():
        if not isinstance(section,str) or not isinstance(summaries,list):raise ValueError('Invalid generated section')
        for summary in summaries:
            key = (section, summary['id'])
            if not isinstance(summary['id'],str) or not summary['id'] or key in baseline or not isinstance(summary['summary'], str):
                raise ValueError('Invalid generated summary')
            baseline[key] = summary['summary']
    pairs, seen, sections = [], set(), set()
    for section in edited:
        if not isinstance(section['key'],str) or not section['key'] or section['key'] in sections or not isinstance(section['summaries'],list):raise ValueError('Invalid edited section')
        sections.add(section['key'])
        for summary in section['summaries']:
            key = (section['key'], summary['id'])
            if not isinstance(summary['id'],str) or not summary['id'] or key in seen or not isinstance(summary['text'], str):
                raise ValueError('Invalid edited summary')
            seen.add(key)
            before, after = baseline.get(key), summary['text']
            status = 'added' if before is None else 'unchanged' if before == after else 'modified'
            pairs.append(dict(section=key[0], summary_id=key[1], before=before, after=after, status=status))
    return pairs, len(set(baseline)-seen)


def collect():
    global SEED
    os.umask(0o077)
    out = ROOT / 'data'
    out.mkdir(exist_ok=True, mode=0o700)
    (out / '.gitignore').write_text('*\n')
    excluded_patients = []
    existing = json.loads((out/'examples.json').read_text()) if (out/'examples.json').exists() else {'examples':[],'substitutes':[]}
    sampling_round=existing.get('sampling_round',3)+1
    if sampling_round>=4:SEED+=':replenish-'+str(sampling_round)
    previous = existing['examples']+existing['substitutes']
    for example in previous:
        excluded_patients += [c['source']['masked_patient_id'] for c in example['cases']]
    for path in (ROOT.parent / 'datasets').glob('*/cohort.json'):
        excluded_patients += [c['source']['masked_patient_id'] for c in json.loads(path.read_text())['cases']]
    patient_needed=[s for s in SPECIALTIES if sum(e['metadata']['specialty']==s and e['metadata']['cohort_type']=='patient' for e in previous)<8]
    random_needed=[s for s in SPECIALTIES if sum(e['metadata']['specialty']==s and e['metadata']['cohort_type']=='random' for e in previous)<4]
    if not patient_needed and not random_needed:
        print('All example and substitution quotas are already filled.')
        return
    params = parameters() + ['excluded:ARRAY<STRING>:' + json.dumps(sorted(set(excluded_patients))),
                            'patient_needed:ARRAY<STRING>:'+json.dumps(patient_needed),
                            'random_needed:ARRAY<STRING>:'+json.dumps(random_needed)]
    sql = f"""WITH latest AS ({base()}), eligible AS (
      SELECT * FROM latest WHERE masked_patient_id NOT IN UNNEST(@excluded)),
      patients AS (SELECT specialty, masked_patient_id, COUNT(*) n FROM eligible
        WHERE specialty IN UNNEST(@patient_needed) GROUP BY 1,2 HAVING n>=5),
      picked AS (SELECT * FROM patients QUALIFY ROW_NUMBER() OVER (
        PARTITION BY specialty ORDER BY n>=7 DESC, FARM_FINGERPRINT(CONCAT(masked_patient_id,'{SEED}')))<=10),
      patient_rows AS (SELECT e.*, 'patient' cohort_type FROM eligible e JOIN picked p USING(specialty,masked_patient_id)
        QUALIFY ROW_NUMBER() OVER(PARTITION BY specialty,masked_patient_id ORDER BY
          FARM_FINGERPRINT(CONCAT(masked_encounter_id,'{SEED}')))<=9),
      random_rows AS (SELECT e.*, 'random' cohort_type FROM eligible e
        WHERE split='test' AND file_updated_at>=TIMESTAMP('2026-09-14') AND specialty IN UNNEST(@random_needed)
          AND masked_patient_id NOT IN (SELECT masked_patient_id FROM picked)
        QUALIFY ROW_NUMBER() OVER(PARTITION BY specialty ORDER BY FARM_FINGERPRINT(CONCAT(masked_encounter_id,'{SEED}')))<=40)
      SELECT * FROM patient_rows UNION ALL SELECT * FROM random_rows LIMIT 1000"""
    candidates = query(sql, params, budget=15_000_000_000)
    print(json.dumps({'candidate_rows':len(candidates),'by_type':dict(Counter(c['cohort_type'] for c in candidates))}), flush=True)
    with tempfile.TemporaryDirectory(prefix='preference-review-') as temp:
        selected = [{'id':c['masked_encounter_id'],'version':c['file_updated_at']} for c in candidates]
        dates = sorted({c['file_updated_at'][:10] for c in candidates})
        partitions = ','.join("DATE('"+d+"')" for d in dates)
        payload_sql = f"""SELECT t.masked_encounter_id, t.file_updated_at,
          t.deid_soap_note, t.deid_soap_note_edited,
          JSON_VALUE(t.imputed_context, '$.encounter.appointment_time') appointment_time
          FROM `{TABLE}` t JOIN UNNEST(JSON_QUERY_ARRAY(@keys)) k
          ON t.masked_encounter_id=JSON_VALUE(k,'$.id') AND t.file_updated_at=TIMESTAMP(JSON_VALUE(k,'$.version'))
          WHERE DATE(t.file_updated_at) IN ({partitions}) LIMIT 1000"""
        payload_params = ['keys:JSON:' + json.dumps(selected)]
        estimate=query(payload_sql, payload_params, dry=True, budget=800_000_000_000)
        print(json.dumps({'payload_query_dry_run':estimate.get('statistics',{}).get('totalBytesProcessed','available')}),flush=True)
        rows = query(payload_sql, payload_params, budget=800_000_000_000)
        # Candidate payloads remain in this process; only selected examples persist.
        metadata = {c['masked_encounter_id']:c for c in candidates}
        groups = defaultdict(list)
        invalid = Counter()
        for row in rows:
            source = dict(metadata[row['masked_encounter_id']])
            kind = source.pop('cohort_type')
            source.update(row)
            for key in ('deid_soap_note','deid_soap_note_edited'):
                if isinstance(source[key],str): source[key] = json.loads(source[key])
            try:
                pairs, missing = align(source)
                modified = [p for p in pairs if p['before'] is not None and ' '.join(p['before'].split()) != ' '.join(p['after'].split())]
                if not modified or not source.get('appointment_time'):
                    invalid['no_observed_change_or_date'] += 1
                    continue
            except (ValueError,KeyError,TypeError):
                invalid['invalid_note_shape'] += 1
                continue
            case = dict(source=source,source_note_sha256={key:canonical_hash(source[key]) for key in ('deid_soap_note','deid_soap_note_edited')},
                        observations={'modified_summaries':len(modified),'unobserved_generated':missing})
            groupkey=(source['specialty'],kind,source['masked_patient_id'] if kind=='patient' else '')
            groups[groupkey].append(case)
        examples, spares, counts = [], [], {}
        used_patients = set(excluded_patients)
        for specialty in SPECIALTIES:
            eligible_groups=[]
            for (spec,kind,patient), cases in groups.items():
                if spec != specialty or kind != 'patient' or patient in used_patients: continue
                dates_seen=set()
                distinct=[]
                for case in cases:
                    date=case['source']['appointment_time']
                    if date not in dates_seen:
                        distinct.append(case); dates_seen.add(date)
                if len(distinct)>=5: eligible_groups.append((patient,distinct))
            eligible_groups.sort(key=lambda x: hashlib.sha256((SEED+x[0]).encode()).hexdigest())
            patient_sets=[e for e in previous if e['metadata']['specialty']==specialty and e['metadata']['cohort_type']=='patient']
            for patient,cases in eligible_groups:
                used_patients.add(patient)
                patient_sets.append(make_example(specialty,'patient',sorted(cases[:5],key=lambda c:c['source']['appointment_time'])))
            random_cases=groups.get((specialty,'random',''),[])
            random_cases.sort(key=lambda c: hashlib.sha256((SEED+c['source']['masked_encounter_id']).encode()).hexdigest())
            random_sets=[]
            for case in random_cases:
                patient=case['source']['masked_patient_id']
                if patient in used_patients: continue
                used_patients.add(patient)
                if not random_sets or len(random_sets[-1])==5: random_sets.append([])
                random_sets[-1].append(case)
            random_sets=[e for e in previous if e['metadata']['specialty']==specialty and e['metadata']['cohort_type']=='random']+[make_example(specialty,'random',s) for s in random_sets if len(s)==5]
            examples.extend(patient_sets[:7]+random_sets[:3])
            spares.extend(patient_sets[7:8]+random_sets[3:4])
            counts[specialty]={'patient':min(7,len(patient_sets)),'random':min(3,len(random_sets)), 'patient_available':len(patient_sets),'random_available':len(random_sets)}
        result={'schema_version':1,'dataset_name':'clinician-preference-review-100','sampling_round':sampling_round,'examples':examples,'substitutes':spares,'counts':counts}
        with (out/'preparation.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if (out/'examples.json').exists():
                latest=json.loads((out/'examples.json').read_text())
                learning_by_id={e['id']:e['metadata']['learning'] for e in latest['examples']+latest['substitutes']}
                for example in examples+spares:
                    if learning_by_id.get(example['id']):example['metadata']['learning']=learning_by_id[example['id']]
            tmp=out/'examples.collect.tmp'
            tmp.write_text(json.dumps(result,ensure_ascii=False));tmp.replace(out/'examples.json')
        print(json.dumps({'saved':len(examples),'substitutes':len(spares),'invalid':dict(invalid),'counts':counts},indent=2),flush=True)


def make_example(specialty, kind, cases):
    ids=[c['source']['masked_encounter_id'] for c in cases]
    identity=str(uuid.uuid5(uuid.NAMESPACE_URL,SEED+'|'.join(ids)))
    return {'id':identity,'revision':1,'status':'draft','cases':cases,'metadata':{
        'schema':'grouped_note_reflection_v1','specialty':specialty,'cohort_type':kind,
        'encounter_count':5,'patient_count':len({c['source']['masked_patient_id'] for c in cases}),
        'clinician_identity_verified':False,'sampling_seed':SEED,
        'sampling':'Seeded random sampling among source-sent records, then valid observed-edit and distinct appointment screening. Replenishment prioritizes patients with at least seven available records.',
        'source_table':TABLE,'source_version_floor':FLOOR,'source_splits':dict(Counter(c['source']['split'] for c in cases)),
        'authorship_limitation':'Same patient is a continuity proxy, not verified clinician authorship.' if kind=='patient' else 'Random encounters may have different authors; learning is an exploratory hypothesis.',
        'pair_key':['masked_encounter_id','file_updated_at'],'summary_alignment_key':['section','summary_id'],
        'missing_edits':'Generated summaries absent from edited data are unobserved, not deletions.',
        'learning':None}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inventory','collect'])
    args = parser.parse_args()
    inventory() if args.action=='inventory' else collect()
