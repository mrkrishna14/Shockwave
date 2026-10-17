'use client';
import {useState} from 'react';
import {api,APIProject} from '../lib/api';
import {useWorkspace} from '../lib/use-workspace';
import {Empty,Status,Table} from './status';

type Workspace=ReturnType<typeof useWorkspace>;
export function Provider({w,run}:{w:Workspace;run:(fn:()=>Promise<unknown>)=>void}){
  const [projectName,setProjectName]=useState('Demo Payments');
  const [email,setEmail]=useState('consumer@example.test');
  const [search,setSearch]=useState('');
  const [oldSemantics,setOldSemantics]=useState('USD cents; 1000 = $10');
  const [newSemantics,setNewSemantics]=useState('USD dollars; 10 = $10');
  const data=w.data,org=w.org!;
  const project=data.projects.find(p=>p.id===w.project);
  const tab=w.location.tab;
  const real=w.capabilities?.mode==='integrated';
  return <>
    <div className="metric-grid">
      <div className="metric"><span>Documented endpoints</span><strong>{data.endpoints.length}</strong><small>Current selected project</small></div>
      <div className="metric"><span>Consumers</span><strong>{data.consumers.length}</strong><small>Invited for this API</small></div>
      <div className="metric"><span>Verified regressions</span><strong>{data.impact.reduce((n,x)=>n+x.verified_regression_count,0)}</strong><small>Opted-in summaries only</small></div>
    </div>
    <section className="panel">
      <div className="panel-header"><div><span className="eyebrow">API PROJECT</span><h2>Choose an API</h2></div><Status value={w.capabilities?.discovery==='fixture'?'ready':w.capabilities?.discovery??'unavailable'}/></div>
      {data.projects.length>0&&<label className="field">Project<select value={w.project} onChange={e=>w.navigate({project:e.target.value})}>{data.projects.map(p=><option key={p.id} value={p.id}>{p.name} · {p.service_id}</option>)}</select></label>}
      <form className="inline-form" onSubmit={e=>{e.preventDefault();run(async()=>{const created=await api<APIProject>('/api-projects',{method:'POST',org:org.id,body:{name:projectName,service_id:'demo-payments',base_urls:['http://demo-payments.test']}});w.navigate({project:created.id,tab:'endpoints'})})}}>
        <label className="field">New project name<input value={projectName} onChange={e=>setProjectName(e.target.value)} required/></label>
        <button className="button primary">Create Demo Payments API</button>
      </form>
    </section>
    {project&&(tab==='overview'||tab==='endpoints')&&<section className="panel">
      <div className="panel-header"><div><span className="eyebrow">CONTRACT INVENTORY</span><h2>Endpoints and provenance</h2></div><button className="button primary" disabled={w.capabilities?.discovery!=='fixture'} onClick={()=>run(()=>api(`/api-projects/${project.id}/discoveries`,{method:'POST',org:org.id,body:{spec_reference:'demo-fixture'}}))}>Load contract</button></div>
      <p>Shockwave reads the API&apos;s OpenAPI description and its server code, then reconciles the two. An incomplete scan never means an endpoint is absent.</p>
      <label className="field search">Search methods and paths<input value={search} onChange={e=>setSearch(e.target.value)} placeholder="GET /v1/charges"/></label>
      {data.endpoints.length?<Table headers={['Method','Path','Provenance','Coverage']}>
        {data.endpoints.filter(e=>(e.method+' '+e.path).toLowerCase().includes(search.toLowerCase())).map(e=><tr key={e.id}><td><Status value={e.method}/></td><td><code>{e.path}</code></td><td>{e.evidence.map(x=>x.source_uri).join(', ')}<div className="small-muted">{e.confidence}</div></td><td><Status value={e.reconciliation}/></td></tr>)}
      </Table>:<Empty title="No endpoints loaded">Load the contract to inspect its documented operations.</Empty>}
    </section>}
    {project&&(tab==='overview'||tab==='consumers')&&<section className="panel">
      <div className="panel-header"><div><span className="eyebrow">PERMISSIONED ONBOARDING</span><h2>Consumers</h2></div><Status value={w.capabilities?.email==='development_inbox'?'in-app inbox':w.capabilities?.email??'unavailable'}/></div>
      <p>Invited teams receive the invitation in their Shockwave inbox. Their repository details stay private to them.</p>
      <form className="inline-form" onSubmit={e=>{e.preventDefault();run(()=>api(`/api-projects/${project.id}/invitations`,{method:'POST',org:org.id,body:{recipient_email:email}}))}}>
        <label className="field">Recipient email<input type="email" value={email} onChange={e=>setEmail(e.target.value)} required/></label><button className="button primary">Invite consumer</button>
      </form>
      {data.consumers.length?<Table headers={['Recipient','Invitation','Delivery','Enrollment','Action']}>
        {data.consumers.map(c=><tr key={c.invitation_id}><td>{c.recipient_email}</td><td><Status value={c.invitation_status}/></td><td><Status value={c.delivery_status}/>{c.delivery_attention&&<div className="small-muted">Delivery needs reconciliation; do not retry automatically.</div>}</td><td><Status value={c.enrollment_status}/>{c.share_summary&&<div className="small-muted">Summary sharing on</div>}</td><td>{c.invitation_status==='pending'&&<button className="button subtle" onClick={()=>run(()=>api(`/invitations/${c.invitation_id}`,{method:'DELETE',org:org.id}))}>Revoke</button>}</td></tr>)}
      </Table>:<Empty title="No consumers invited">Invite a verified recipient to start onboarding.</Empty>}
    </section>}
    {project&&(tab==='overview'||tab==='changes')&&<section className="panel">
      <div className="panel-header"><div><span className="eyebrow">PROPOSED CHANGE</span><h2>Amount units assessment</h2></div><Status value="proposed"/></div>
      <p>Proposed: <code>amount</code> switches from cents to dollars. The schema still says <code>number</code>, so a schema check alone sees no change.</p>
      <div className="comparison"><label className="field">Current behavior<textarea value={oldSemantics} onChange={e=>setOldSemantics(e.target.value)}/></label><label className="field">Proposed behavior<textarea value={newSemantics} onChange={e=>setNewSemantics(e.target.value)}/></label></div>
      <p className="small-muted">Shockwave compares the current and proposed contracts, applies the declared change in meaning, and checks every opted-in consumer.</p>
      <button className="button primary" onClick={()=>run(()=>api(`/api-projects/${project.id}/assessments`,{method:'POST',org:org.id,body:{}}))}>Assess change</button>
      <div className="section-divider"><h3>Permissioned impact</h3>{data.impact.length?<Table headers={['Consumer','Coverage','Predicted','Verified','Validated','Deployment']}>
        {data.impact.map(i=><tr key={i.enrollment_id}><td>{i.consumer_display_name}</td><td><Status value={i.coverage}/></td><td>{i.predicted_risk_count}</td><td>{i.verified_regression_count}</td><td>{i.validated_fix_count}</td><td><Status value={i.deployment}/></td></tr>)}
      </Table>:<Empty title="No impact summary yet">Run an assessment after an opted-in consumer scans a repository.</Empty>}</div>
      {data.changes.length>0&&<div className="section-divider"><h3>Assessment history</h3><ul className="simple-list">{[...data.changes].reverse().slice(0,8).map(c=><li key={c.id}><strong>{c.kind.replaceAll('_',' ')}</strong><span>{c.field_path} · {c.old_semantics} → {c.new_semantics}</span></li>)}</ul></div>}
    </section>}
  </>;
}
