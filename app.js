const $=id=>document.getElementById(id);
let overviewData,visible=[],position=0,current=null,dirty=false,busy=false,selectionSequence=0;
const fields=['outcome','preference','explanation','limits','confidence','avoid_learning'];
function message(text,error=false){$('message').textContent=text;$('message').hidden=!text;$('message').classList.toggle('error',error);}
async function api(path,body){
  const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':document.querySelector('meta[name=review-token]').content},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'Request failed.');return value;
}
function setDirty(value){dirty=value;$('dirty').hidden=!value;renderSendStatus();buttons();}
function exampleLabel(example){return `Example ${overviewData.examples.findIndex(e=>e.id===example.id)+1}`;}
function isSent(){return current?.status==='sent'&&current.remote?.revision===current.revision;}
function renderSendStatus(){
  if(!current)return;
  const sent=isSent()&&!dirty,remote=current.remote;
  $('send-status').dataset.state=sent?'sent':remote?'changed':'unsent';
  $('send-status-title').textContent=sent?'Saved and verified in LangSmith De-ID.':remote?'Changes not sent to LangSmith.':'Not sent to LangSmith.';
  $('send-status-detail').textContent=`${exampleLabel(current)} · ${current.id.slice(0,8)} · `+(sent?`Revision ${current.revision} is saved in clinician-preference-review-100.`:remote?`LangSmith has revision ${remote.revision}. ${dirty?'Your unsaved changes':'Current revision '+current.revision} still need approval and sending.`:'This example is saved locally only.');
  $('action-status').textContent=`${exampleLabel(current)} · ${sent?'Sent · revision '+current.revision:remote?'Changes not sent':'Not sent'}`;
  $('send').textContent=sent?'Sent to LangSmith ✓':remote?'Approve & send changes':'Approve & send to LangSmith';
  $('remote-link').textContent=sent?'Open this saved example in LangSmith ↗':`Open previously sent revision ${remote?.revision} ↗`;
}
function canLeave(){return !dirty||window.confirm('You have unsaved learning changes. Discard them?');}
function buttons(){
  for(const id of ['previous','next','save','send','substitute'])$(id).disabled=busy||!current;
  $('previous').disabled=busy||position===0||!current;$('next').disabled=busy||position>=visible.length-1||!current;
  $('send').disabled=busy||!current?.metadata.learning||!overviewData?.connected||(isSent()&&!dirty);
  $('save').disabled=busy||!current?.metadata.learning;
  const reserves=current?(overviewData.reserves[current.metadata.specialty+'|'+current.metadata.cohort_type]||0):0;
  $('substitute').disabled=busy||!reserves||!current;
  $('reserve-count').textContent=current?`${reserves} unused substitute${reserves===1?'':'s'}`:'';
  for(const id of ['specialty','cohort-type','review-filter'])$(id).disabled=busy;
}
async function overview(){
  overviewData=await api('/api/overview');
  $('connection-badge').textContent=overviewData.connected?'LangSmith connected':'LangSmith disconnected';
  const ready=overviewData.examples.filter(x=>x.outcome!=='pending').length;
  const sent=overviewData.examples.filter(x=>x.status==='sent').length;
  $('progress').textContent=`All examples: ${sent} sent · ${overviewData.examples.length-sent} need sending · ${ready} learnings ready`;
  if($('specialty').options.length===1)for(const name of [...new Set(overviewData.examples.map(x=>x.specialty))]){const option=el('option',name);option.value=name;$('specialty').append(option);}
}
function filter(){
  visible=overviewData.examples.filter(x=>(!$('specialty').value||x.specialty===$('specialty').value)&&(!$('cohort-type').value||x.cohort_type===$('cohort-type').value)&&(!$('review-filter').value||($('review-filter').value==='sent'?x.status==='sent':x.status!=='sent')));
}
async function load(index){
  const sequence=++selectionSequence;position=index;current=null;message('');$('action-status').textContent='';buttons();
  $('empty').hidden=!!visible.length;$('card').hidden=!visible.length;
  if(!visible.length)return;
  const example=await api('/api/example/'+visible[index].id);if(sequence!==selectionSequence)return;
  current=example;setDirty(false);renderCard();buttons();
}
function renderCard(){
  const m=current.metadata,l=m.learning;
  $('counter').textContent=`${exampleLabel(current)} · ${position+1} of ${visible.length} in view · revision ${current.revision}`;$('title').textContent=m.specialty;
  const chips=[m.cohort_type==='patient'?'Same patient':'Random encounters','5 encounters',`${m.patient_count} patient${m.patient_count===1?'':'s'}`,l?.outcome==='preference'?'Preference':l?.outcome==='abstain'?'Abstention':'Learning pending',...Object.entries(m.source_splits).map(([k,v])=>`${v} ${k}`)];
  $('chips').replaceChildren(...chips.map(x=>el('span',x,'chip')));$('limitation').textContent=m.authorship_limitation;
  $('remote-link').hidden=true;
  if(current.remote?.url){try{const url=new URL(current.remote.url);if(url.protocol==='https:'&&url.hostname==='langsmith-deid.abridge.services'){url.hostname='langsmith-deid.abridge.teleport.sh';$('remote-link').href=url.href;$('remote-link').hidden=false;}}catch{}}
  renderSendStatus();
  const metadata={example_id:current.id,review_revision:current.revision,...m,reference_generation:l?.generation||null};delete metadata.learning;
  metadata.encounters=current.cases.map(c=>({encounter_id:c.source.masked_encounter_id,patient_id:c.source.masked_patient_id,appointment:c.source.appointment_time,source_version:c.source.file_updated_at,care_setting:c.source.care_setting,note_type:c.source.specialty_note_type,split:c.source.split}));
  $('metadata').textContent=JSON.stringify(metadata,null,2);
  $('preference-display').textContent=l?.outcome==='abstain'?'No durable preference supported':l?.preference||'Learning generation pending';
  $('explanation-display').textContent=l?.explanation||'';$('limits-display').textContent=l?.limits||'';
  $('avoid-display').textContent=l?'Avoid learning: '+l.avoid_learning:'';
  for(const field of fields)$(field).value=l?.[field]||'';
  $('evidence-json').value=JSON.stringify(l?.evidence||[],null,2);
  $('evidence-details').open=false;
  $('evidence').replaceChildren(...(l?.evidence||[]).map(e=>{
    const index=current.cases.findIndex(c=>c.source.masked_encounter_id===e.encounter_id),row=el('div',undefined,'evidence-item'),button=el('button',`Encounter ${index+1} ↗`,'evidence-button');
    button.onclick=()=>{renderEncounter(index);const target=[...$('notes').querySelectorAll('.pair')].find(p=>p.dataset.section===e.section&&p.dataset.summary===e.summary_id);target?.scrollIntoView({behavior:'smooth',block:'start'});};
    row.append(button,document.createTextNode(e.observation));return row;
  }));
  $('encounters').replaceChildren(...current.cases.map((c,i)=>{const button=el('button',`${i+1} · ${c.source.appointment_time?.slice(0,10)||'Date unavailable'}`);button.onclick=()=>renderEncounter(i);return button;}));
  $('edit-panel').open=false;renderEncounter(0);
}
function renderEncounter(index){
  const source=current.cases[index].source,entry=normalize({cases:[current.cases[index]]})[0];
  [...$('encounters').children].forEach((button,i)=>button.setAttribute('aria-pressed',String(i===index)));
  const nodes=[el('h3',`Encounter ${index+1}`),el('p',`Encounter ${entry.id} · Patient ${entry.patient}`,'source'),el('p',`${entry.date?.slice(0,10)||'Unknown date'} · ${entry.split} · ${source.care_setting||'Care setting unspecified'} · ${source.specialty_note_type||'Note type unspecified'}`,'source')];
  // Keep summary IDs on the rendered rows so evidence links jump to the exact diff.
  const editRefs=source.deid_soap_note_edited.flatMap(section=>section.summaries.map(s=>({section:section.key,summary:s.id})));
  entry.observed.forEach((pair,i)=>{const row=renderPair(pair);row.dataset.section=editRefs[i].section;row.dataset.summary=editRefs[i].summary;nodes.push(row);});
  if(entry.missing.length){const details=el('details');details.append(el('summary',`${entry.missing.length} generated summaries without an observed edit`));entry.missing.forEach(pair=>details.append(renderPair(pair)));nodes.push(details);}
  $('notes').replaceChildren(...nodes);
}
function editedLearning(){const l=Object.fromEntries(fields.map(field=>[field,$(field).value]));try{l.evidence=JSON.parse($('evidence-json').value);}catch{throw Error('Evidence must be valid JSON.');}return l;}
async function action(kind){
  if(!current||busy)return;
  if(kind==='substitute'&&!canLeave())return;
  const id=current.id,label=exampleLabel(current);busy=true;buttons();message(kind==='send'?`${label}: saving locally and sending to LangSmith…`:`${label}: saving…`);
  try{
    const body={id,revision:current.revision};if(kind!=='substitute')body.learning=editedLearning();
    current=await api('/api/'+kind,body);setDirty(false);await overview();filter();position=Math.max(0,visible.findIndex(e=>e.id===id));
    if(visible.length&&visible[position].id===id)renderCard();else await load(position);
    message(kind==='send'?(current?.id===id?'':`${label} was saved and verified in LangSmith De-ID. It is no longer in this filtered view.`):kind==='substitute'?`${label}: replaced with an unused set. This replacement has not been sent.`:`${label}: learning revision saved locally. This revision has not been sent.`);
  }catch(error){
    // A failed remote send can follow a successful local save; refresh its revision for a safe retry.
    if(kind==='send'){
      const saved=await api('/api/example/'+id);
      if(saved.revision>current.revision){current=saved;setDirty(false);renderCard();}
    }
    message(`${label}: ${error.message}`,true);
  }finally{busy=false;buttons();}
}
for(const field of [...fields,'evidence-json'])$(field).addEventListener('input',()=>setDirty(true));
$('outcome').addEventListener('change',()=>{if($('outcome').value==='abstain')$('preference').value='';setDirty(true);});
const selectedFilters={'specialty':'','cohort-type':'','review-filter':''};
for(const id of Object.keys(selectedFilters))$(id).addEventListener('change',async()=>{if(!canLeave()){$(id).value=selectedFilters[id];return;}selectedFilters[id]=$(id).value;filter();try{await load(0);}catch(e){message(e.message,true);}});
$('previous').onclick=()=>{if(canLeave())load(position-1).catch(e=>message(e.message,true));};$('next').onclick=()=>{if(canLeave())load(position+1).catch(e=>message(e.message,true));};
$('save').onclick=()=>action('save');$('send').onclick=()=>action('send');$('substitute').onclick=()=>action('substitute');
$('reveal').onclick=()=>{const hidden=!$('learning-content').hidden;$('learning-content').hidden=hidden;$('reveal').textContent=hidden?'Show learning':'Hide learning';$('reveal').setAttribute('aria-expanded',String(!hidden));};
$('connect-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await api('/api/connect',{api_key:$('api-key').value,workspace_id:$('workspace').value});await overview();buttons();message('Authenticated with LangSmith De-ID. No examples were sent.');$('connection').open=false;}catch(e){await overview();buttons();message(e.message,true);}finally{$('api-key').value='';button.disabled=false;}};
$('disconnect').onclick=async()=>{await api('/api/disconnect',{});await overview();buttons();message('Disconnected. Credentials cleared from server memory.');};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
overview().then(()=>{filter();return load(0);}).catch(e=>message(e.message,true));
const preparationPoll=setInterval(async()=>{
  if(!overviewData||busy||dirty)return;
  try{
    const wasPending=current&&!current.metadata.learning;
    await overview();
    if(wasPending&&overviewData.examples.find(e=>e.id===current.id)?.outcome!=='pending'){
      current=await api('/api/example/'+current.id);renderCard();
    }
    buttons();
    if(overviewData.examples.every(e=>e.outcome!=='pending')&&Object.keys(overviewData.reserves).length===20)clearInterval(preparationPoll);
  }catch{}
},10000);
