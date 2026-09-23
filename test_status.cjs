// No network or real uploads: exercise the actual UI state and action handlers.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const nodes=new Map();
const context=vm.createContext({assert,URL,document:{getElementById(id){
  if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false,disabled:false,value:'',dataset:{},classList:{toggle(){}}});
  return nodes.get(id);
}}});
vm.runInContext(fs.readFileSync(__dirname+'/app.js','utf8').split("for(const field of [...fields,'evidence-json'])")[0],context);
vm.runInContext(`(async()=>{
  const first={id:'first-id',revision:2,status:'draft',metadata:{specialty:'Test',cohort_type:'patient',learning:{}}};
  const second={...first,id:'second-id',revision:1};
  overviewData={connected:true,examples:[first,second],reserves:{}};
  visible=overviewData.examples;current=first;
  renderCard=()=>renderSendStatus();editedLearning=()=>({});overview=async()=>{};
  let filtered=false,fail=false;
  filter=()=>{visible=overviewData.examples.filter(e=>!filtered||e.status!=='sent');};
  api=async(path,body)=>{
    if(!body)return structuredClone(overviewData.examples.find(e=>path.endsWith(e.id)));
    if(fail)throw Error('Network unavailable');
    const e=overviewData.examples.find(e=>e.id===body.id);e.revision++;
    e.status=path.endsWith('send')?'sent':'revised';
    if(e.status==='sent')e.remote={revision:e.revision,url:'https://langsmith-deid.abridge.services/example'};
    return structuredClone(e);
  };
  renderCard();buttons();
  assert.equal($('send-status-title').textContent,'Not sent to LangSmith.');
  await action('send');
  assert.equal($('send-status-title').textContent,'Saved and verified in LangSmith De-ID.');
  assert.match($('send-status-detail').textContent,/Example 1.*Revision 3/);
  assert.equal($('send').disabled,true);
  assert.equal($('message').hidden,true);
  message('Old success');await load(1);
  assert.equal($('message').hidden,true);
  assert.equal($('send-status-title').textContent,'Not sent to LangSmith.');
  assert.equal($('action-status').textContent,'Example 2 · Not sent');
  await load(0);setDirty(true);
  assert.equal($('send-status-title').textContent,'Changes not sent to LangSmith.');
  assert.equal($('send').disabled,false);
  setDirty(false);assert.equal($('send').disabled,true);
  await action('save');
  assert.match($('send-status-detail').textContent,/LangSmith has revision 3.*Current revision 4/);
  assert.equal($('send').disabled,false);
  // A failed send cannot claim success; a sent-filter transition names the completed card.
  fail=true;await action('send');assert.match($('message').textContent,/Example 1: Network unavailable/);
  fail=false;filtered=true;filter();await load(0);await action('send');
  assert.equal(current.id,'second-id');
  assert.equal($('send-status-title').textContent,'Not sent to LangSmith.');
  assert.match($('message').textContent,/Example 1 was saved and verified.*no longer in this filtered view/);
  // Substitution retains an older remote revision, which must not look current.
  current={...first,revision:9,status:'draft',remote:{revision:3}};renderCard();buttons();
  assert.equal($('send-status').dataset.state,'changed');
  assert.equal($('send').disabled,false);
})()`,Object.assign(context,{structuredClone})).then(()=>console.log('PASS: per-example send, navigation, edits, errors, filters and replacement status.')).catch(e=>{console.error(e);process.exitCode=1;});
