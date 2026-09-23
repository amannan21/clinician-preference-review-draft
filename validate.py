"""Validate final fixtures without printing clinical content."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from prepare import ROOT, SPECIALTIES, FLOOR
from server import validate_example


def main():
    data=json.loads((ROOT/'data/examples.json').read_text())
    assert len(data['examples'])==100 and len(data['substitutes'])==20
    all_ids=set();all_patients=set();outcomes=Counter();splits=Counter()
    for key,quotas in [('examples',{'patient':7,'random':3}),('substitutes',{'patient':1,'random':1})]:
        counts=Counter()
        for example in data[key]:
            validate_example(example)
            m=example['metadata'];counts[m['specialty'],m['cohort_type']]+=1
            assert m['clinician_identity_verified'] is False
            ids={c['source']['masked_encounter_id'] for c in example['cases']}
            patients={c['source']['masked_patient_id'] for c in example['cases']}
            assert not all_ids.intersection(ids) and not all_patients.intersection(patients)
            all_ids.update(ids);all_patients.update(patients)
            assert all(c['source']['file_updated_at'][:10]>=FLOOR for c in example['cases'])
            dates=[c['source']['appointment_time'] for c in example['cases']]
            if m['cohort_type']=='patient':assert len(set(dates))==5 and dates==sorted(dates)
            assert Counter(c['source']['split'] for c in example['cases'])==Counter(m['source_splits'])
            generation=m['learning']['generation']
            prompt=ROOT/'data/prompts'/f"{generation['prompt_sha256']}.txt"
            assert prompt.exists() and hashlib.sha256(prompt.read_bytes()).hexdigest()==generation['prompt_sha256']
            if m['learning']['outcome']=='preference':assert generation['preference_evidence_audit']=='passed'
            if key=='examples':
                outcomes[m['learning']['outcome']]+=1;splits.update(m['source_splits'])
        assert counts==Counter({(s,k):n for s in SPECIALTIES for k,n in quotas.items()})
    assert len(all_ids)==600
    if data.get('outcome_enrichment'):
        assert outcomes=={'preference':50,'abstain':50}
        enriched=[e for e in data['examples'] if e['metadata'].get('outcome_enrichment',{}).get('run_id')==data['outcome_enrichment']['run_id']]
        assert len(enriched)==data['outcome_enrichment']['replaced_sets']
        assert all(e['metadata']['learning']['outcome']=='preference' for e in enriched)
    result={'valid':True,'examples':100,'encounters':500,'specialties':10,
            'patient_examples':70,'random_examples':30,'substitutes':20,
            'learning_outcomes':dict(outcomes),'source_splits':dict(splits)}
    (ROOT/'data/validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
