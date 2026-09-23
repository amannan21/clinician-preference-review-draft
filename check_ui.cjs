const assert=require('node:assert/strict');
const {chromium}=require('playwright');
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1400,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8766');
  await page.waitForFunction(()=>document.querySelectorAll('#encounters button').length===5);
  assert.equal(await page.locator('#send').isDisabled(),await page.evaluate(()=>!overviewData.connected||!current.metadata.learning||(isSent()&&!dirty)));
  const counts=await page.evaluate(async()=>{
    const manifest=await (await fetch('/api/overview')).json();
    let pairs=0,encounters=0,examples=0;
    const ids=new Set(),quota={};
    for(const item of manifest.examples){
      const example=await (await fetch('/api/example/'+item.id)).json();
      const normalized=normalize(example);
      if(normalized.length!==5)throw Error('Incorrect set size');
      for(const entry of normalized){
        if(ids.has(entry.id))throw Error('Duplicate encounter');ids.add(entry.id);
        for(const pair of entry.observed){
          const row=renderPair(pair),notes=row.querySelectorAll('.note');
          if(pair.before!==null&&notes[0].textContent!==pair.before)throw Error('Generated text altered');
          if(pair.after&&notes[1].textContent!==pair.after)throw Error('Edited text altered');
          pairs++;
        }
        encounters++;
      }
      const key=item.specialty+'|'+item.cohort_type;quota[key]=(quota[key]||0)+1;examples++;
    }
    for(const [key,n] of Object.entries(quota))if(n!==(key.endsWith('|patient')?7:3))throw Error('Quota mismatch');
    const before='Paula Smith is an 88 year old patient who presents with edema.';
    const after='Recent hospitalization.\nPaula Smith is an 88 year old patient who presents after hospitalization.';
    const diff=wordDiff(before,after);
    if(diff.right.some(r=>after.slice(r.start,r.end).includes('Paula Smith')))throw Error('Shared sentence incorrectly highlighted');
    const hostile=renderPair({section:'HPI',before:'',after:'<img src=x onerror=alert(1)>'});
    if(hostile.querySelector('img'))throw Error('Unsafe note rendering');
    return {examples,encounters,pairs};
  });
  assert.equal(counts.examples,100);assert.equal(counts.encounters,500);
  await page.locator('#specialty').selectOption('Neurology');
  await page.waitForFunction(()=>document.getElementById('title').textContent==='Neurology');
  assert.match(await page.locator('#counter').textContent(),/of 10/);
  await page.locator('#cohort-type').selectOption('random');
  await page.waitForFunction(()=>document.getElementById('counter').textContent.includes('of 3'));
  await page.locator('#encounters button').nth(4).click();
  assert.equal(await page.locator('#encounters button').nth(4).getAttribute('aria-pressed'),'true');
  await page.locator('#reveal').click();assert.equal(await page.locator('#learning-content').isVisible(),false);
  await page.locator('#reveal').click();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
  assert.deepEqual(errors,[]);
  await browser.close();console.log(JSON.stringify({pass:true,...counts,checks:['quota','all source text','alignment','word diff','XSS','filters','encounter navigation','learning reveal','mobile layout']}));
})().catch(e=>{console.error(e.message);process.exit(1)});
