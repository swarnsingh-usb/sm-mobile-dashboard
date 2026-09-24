let csrf = '', selected = null, selectedJob = null, polling = false;
const el = id => document.getElementById(id);
const text = (tag, value, cls) => { const n = document.createElement(tag); n.textContent = value; if (cls) n.className = cls; return n; };
function message(value = '') { el('message').textContent = value; }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...(options.headers || {})}});
  const body = response.headers.get('content-type')?.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) { if(response.status === 401) showLogin(); throw new Error(body.error || 'Request failed'); }
  return body;
}
function showLogin() { el('login').hidden=false; el('workspace').hidden=true; el('logout').hidden=true; }
function status(value) { return text('span', value, 'status ' + value.replace(/[^a-z_]/g,'')); }
async function meta() {
  const m = await api('/api/meta'); csrf=m.csrf;
  el('login').hidden=true; el('workspace').hidden=false; el('logout').hidden=false;
  el('project').textContent=m.project; el('storage').textContent=m.storage;
  if(!el('workflow').options.length) {for(const [key,value] of Object.entries(m.workflows)) {const o=text('option',value); o.value=key; el('workflow').append(o);} el('workflow').value='both-dev';}
  el('window').textContent=m.ready ? 'Pilot window open until ' + new Date(m.window.expires_at*1000).toLocaleTimeString() + '. Keep Bitrise stopped until the new jobs finish.' : m.reason;
  el('window').className='notice'+(m.ready?' ready':''); el('run').disabled=!m.ready;
}
function artifacts(container, manifests) {
  container.replaceChildren();
  if(!manifests.length) {container.append(text('p','No completed artifacts yet.','muted'));return;}
  for(const m of manifests) {
    const row=text('div','', 'artifact');
    row.append(text('strong',`${m.platform.toUpperCase()} · Run #${m.pipeline_id} · Job #${m.job_id} `),text('span',` · ${m.upload_status === 'failed' ? 'Artifactory upload failed; local files retained' : m.storage}`));
    row.append(document.createElement('br'),text('code',`${m.sha.slice(0,12)} · ${m.environment} · ${m.native.signing || 'signed'}`),document.createElement('br'));
    for(const f of m.files) { const a=text('a',`${f.name} (${(f.bytes/1048576).toFixed(1)} MB)`); a.href=f.download; row.append(a); if(f.url){const remote=text('a','Artifactory ↗'); if(f.url.startsWith('https://'))remote.href=f.url;remote.target='_blank';remote.rel='noopener';row.append(remote);} }
    container.append(row);
  }
}
async function detail() {
  if(!selected)return;
  const d=await api(`/api/builds/${selected}`);
  el('detail-title').textContent=`Run #${selected} · ${d.pipeline.status}`;el('detail-empty').hidden=true;
  el('cancel').hidden=!['running','pending','created','preparing'].includes(d.pipeline.status);
  el('jobs').replaceChildren(); const ms=[];
  for(const j of d.jobs){const row=text('div','','job');const button=text('button',j.name);button.append(status(j.status));button.onclick=()=>{selectedJob=j.id;loadLog().catch(e=>message(e.message));};row.append(button);
    if(['failed','canceled'].includes(j.status)){const retry=text('button','Retry');retry.onclick=async()=>{try{retry.disabled=true;await api(`/api/jobs/${j.id}/retry`,{method:'POST',body:'{}'});await refresh();}catch(e){message(e.message);}finally{retry.disabled=false;}};row.append(retry);}
    el('jobs').append(row);ms.push(...j.artifacts_local);}
  artifacts(el('artifact-list'),ms);
  if(!selectedJob && d.jobs.length)selectedJob=d.jobs[0].id;
  await loadLog();
}
async function loadLog(){if(!selectedJob)return;el('log-section').hidden=false;try{el('log').textContent=await api(`/api/jobs/${selectedJob}/log`);}catch(e){el('log').textContent='Logs may not be available until the job starts. '+e.message;}}
async function refresh(){
  if(polling)return;polling=true;
  try{await meta(); const [builds,local]=await Promise.allSettled([api('/api/builds'),api('/api/local-artifacts')]);
    if(builds.status==='fulfilled'){el('builds').replaceChildren();if(!builds.value.length)el('builds').append(text('p','Your first run will appear here.','muted'));for(const b of builds.value){const r=text('button',`#${b.id}`, 'build-row');r.append(status(b.status),text('small',`${b.name.replace('Bento Mac / ','')} · ${b.sha.slice(0,8)}`),text('small',new Date(b.created_at).toLocaleString()));r.onclick=()=>{selected=b.id;selectedJob=null;detail().catch(e=>message(e.message));};el('builds').append(r);}}
    else message(builds.reason.message);
    if(local.status==='fulfilled')artifacts(el('local-artifacts'),local.value);else message(local.reason.message);
    await detail();
  }finally{polling=false;}
}
el('login-form').onsubmit=async e=>{e.preventDefault();try{const form=new FormData(e.target);await api('/api/login',{method:'POST',body:JSON.stringify(Object.fromEntries(form))});e.target.reset();message();await refresh();}catch(e){message(e.message);}};
el('launch-form').onsubmit=async e=>{e.preventDefault();el('run').disabled=true;try{const r=await api('/api/builds',{method:'POST',body:JSON.stringify({workflow:el('workflow').value,request_id:crypto.randomUUID()})});selected=r.id;selectedJob=null;message();await refresh();}catch(e){message(e.message);}finally{await meta().catch(()=>{});}};
el('cancel').onclick=async()=>{try{await api(`/api/builds/${selected}/cancel`,{method:'POST',body:'{}'});await refresh();}catch(e){message(e.message);}};
el('logout').onclick=async()=>{await api('/api/logout',{method:'POST',body:'{}'});selected=selectedJob=null;showLogin();};
el('refresh').onclick=()=>refresh().catch(e=>message(e.message));
setInterval(()=>{if(!el('workspace').hidden)refresh().catch(e=>message(e.message));},5000);
refresh().catch(()=>showLogin());
