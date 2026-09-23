// Shared viewer implementation, extracted from build_comparison.py.
function decode(value) { return typeof value === 'string' ? JSON.parse(value) : value; }
function normalize(data) {
  if (!data || typeof data !== 'object') throw Error('Expected an object with a cases array, or an array of encounters.');
  const cases = Array.isArray(data) ? data : data.cases;
  if (!Array.isArray(cases) || !cases.length || cases.length > 1000) throw Error('Expected 1–1000 encounters in a cases array.');
  return cases.map((entry, index) => {
    if (!entry || typeof entry !== 'object') throw Error('Each encounter must be an object.');
    const source = entry.source || entry;
    const generated = decode(source.deid_soap_note);
    const edited = decode(source.deid_soap_note_edited);
    if (!generated || typeof generated !== 'object' || Array.isArray(generated) || !Array.isArray(edited)) throw Error('Each encounter needs deid_soap_note (object) and deid_soap_note_edited (array).');
    const baseline = new Map();
    const key = (section,id) => JSON.stringify([section,id]);
    for (const [section, summaries] of Object.entries(generated)) {
      if (!Array.isArray(summaries)) throw Error('Generated sections must contain summary arrays.');
      for (const item of summaries) {
        if (!item || typeof item.id !== 'string' || !item.id || typeof item.summary !== 'string' || baseline.has(key(section,item.id))) throw Error('Invalid or duplicate generated summary.');
        baseline.set(key(section,item.id), {section,text:item.summary});
      }
    }
    const sections = new Set(), observed = [], seen = new Set();
    for (const section of edited) {
      if (!section || typeof section.key !== 'string' || !section.key || sections.has(section.key) || !Array.isArray(section.summaries)) throw Error('Invalid or duplicate edited section.');
      sections.add(section.key);
      for (const item of section.summaries) {
        if (!item || typeof item.id !== 'string' || !item.id || typeof item.text !== 'string' || seen.has(key(section.key,item.id))) throw Error('Invalid or duplicate edited summary.');
        const k = key(section.key,item.id);
        seen.add(k);
        observed.push({section:section.key,before:baseline.has(k)?baseline.get(k).text:null,after:item.text});
      }
    }
    const missing = [...baseline].filter(([k])=>!seen.has(k)).map(([,v])=>({section:v.section,before:v.text,after:null}));
    return {label:typeof entry.case_id==='string'?entry.case_id:`Encounter ${index+1}`,id:typeof source.masked_encounter_id==='string'?source.masked_encounter_id:'Unspecified',patient:typeof source.masked_patient_id==='string'?source.masked_patient_id:'Unspecified',date:typeof source.appointment_time==='string'?source.appointment_time:source.sent_to_ehr_at,dateLabel:source.appointment_time?'Deidentified appointment':'Source EHR send date',split:source.split,observed,missing};
  });
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function wordDiff(before, after) {
  const tokenize=text=>[...text.matchAll(/[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu)].map(match=>({text:match[0],start:match.index,end:match.index+match[0].length}));
  const a=tokenize(before), b=tokenize(after), sameA=new Uint8Array(a.length), sameB=new Uint8Array(b.length);
  let start=0, endA=a.length, endB=b.length;
  while(start<endA && start<endB && a[start].text===b[start].text) {sameA[start]=sameB[start]=1;start++;}
  while(endA>start && endB>start && a[endA-1].text===b[endB-1].text) {sameA[--endA]=sameB[--endB]=1;}
  const n=endA-start, m=endB-start, width=m+1;
  // ponytail: bound quadratic LCS work; show an explicit notice for oversized rewrites instead of misleading highlights.
  if(n && m && (n+1)*(m+1)>4000000) return {left:[],right:[],unavailable:true};
  if(n && m) {
    const lengths=new Uint32Array((n+1)*width);
    for(let i=n-1;i>=0;i--) for(let j=m-1;j>=0;j--) {
      lengths[i*width+j]=a[start+i].text===b[start+j].text?1+lengths[(i+1)*width+j+1]:Math.max(lengths[(i+1)*width+j],lengths[i*width+j+1]);
    }
    let i=0,j=0;
    while(i<n && j<m) {
      if(a[start+i].text===b[start+j].text) {sameA[start+i++]=sameB[start+j++]=1;}
      else if(lengths[(i+1)*width+j]>=lengths[i*width+j+1]) i++;
      else j++;
    }
  }
  function ranges(tokens,same) {
    const result=[];
    for(let i=0;i<tokens.length;i++) if(!same[i]) {
      const range={start:tokens[i].start,end:tokens[i].end};
      while(i+1<tokens.length && !same[i+1]) range.end=tokens[++i].end;
      result.push(range);
    }
    return result;
  }
  const left=ranges(a,sameA),right=ranges(b,sameB);
  return {left,right,formattingOnly:!left.length&&!right.length&&before!==after};
}
function textCell(text, ranges=[], tag) {
  const node = el('div',undefined,'note');
  let cursor=0;
  for(const {start,end} of ranges) {
    node.append(document.createTextNode(text.slice(cursor,start)),el(tag,text.slice(start,end)));
    cursor=end;
  }
  node.append(document.createTextNode(text.slice(cursor)));
  return node;
}
function renderPair(pair) {
  const {section,before,after}=pair;
  const diff=before!==null && after!==null?wordDiff(before,after):{left:[],right:before===null?[{start:0,end:after.length}]:[]};
  const status = before===null?'Added summary':after===null?'Edit unobserved':before===after?'Unchanged':after===''?'Removed text':diff.unavailable?'Edited · too large for word highlighting':diff.formattingOnly?'Formatting only':'Edited';
  const row=el('section',undefined,'pair'), title=el('h3',section.split(':').pop().replaceAll('_',' '));
  title.append(el('span',status,'status'));row.append(title);
  const columns=el('div',undefined,'columns'), left=el('div'),right=el('div');
  left.append(el('h4','Generated'));right.append(el('h4','Edited · recorded text'));
  left.append(textCell(before===null?'No generated summary with this ID.':before,diff.left,'del'));
  right.append(after===null?el('div','No edited version recorded. This does not establish whether it was unchanged or deleted.','note'):after===''?el('div','Recorded edited text is empty.','note'):textCell(after,diff.right,'ins'));
  columns.append(left,right);row.append(columns);return row;
}
