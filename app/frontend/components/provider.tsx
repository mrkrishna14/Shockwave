'use client';
import {Fragment,useState} from 'react';
import {api,APIProject,Endpoint} from '../lib/api';
import {useWorkspace} from '../lib/use-workspace';
import {DemoPath} from './demo-path';
import {Empty,PageHead,Section,Status,Table,label,plural} from './status';

type Workspace=ReturnType<typeof useWorkspace>;
const SOURCES:Record<string,string>={openapi:'OpenAPI',source:'Server source',docs:'Docs',declared_change:'Declared change',test:'Test'};
const sourceOf=(e:Endpoint)=>[...new Set(e.evidence.map(x=>SOURCES[x.kind]??x.kind))].join(' + ')||'Unknown';
const amountType=(e?:Endpoint)=>{const p=(e?.response_schema as {properties?:Record<string,{type?:string}>}|undefined)?.properties;return p?.amount?.type};
const Search=()=><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><circle cx="7" cy="7" r="5"/><path d="m11 11 3.5 3.5"/></svg>;

export function Provider({w,run}:{w:Workspace;run:(fn:()=>Promise<unknown>)=>void}){
  const [projectName,setProjectName]=useState('Stripe');
  const [email,setEmail]=useState('');
  const [search,setSearch]=useState('payment_intents');
  const [picked,setPicked]=useState('');
  const [oldSemantics,setOldSemantics]=useState('USD in cents (minor units). 1000 represents $10.00.');
  const [newSemantics,setNewSemantics]=useState('USD in dollars (major units). 10 represents $10.00.');
  const data=w.data,org=w.org!;
  const project=data.projects.find(p=>p.id===w.project);
  const tab=w.location.tab;
  const real=w.capabilities?.mode==='integrated';
  const stripe=project?.service_id==='stripe';
  const latest=data.changes.at(-1);
  const changed=data.endpoints.find(e=>e.id===latest?.endpoint_id);
  const endpointName=changed?`${changed.method} ${changed.path}`:stripe?'GET /v1/payment_intents/{intent}':'GET /v1/charges/{id}';
  const schemaType=amountType(changed)??(stripe?'integer':'number');
  const verified=data.impact.reduce((n,x)=>n+x.verified_regression_count,0);
  const projectSelect=data.projects.length>0&&<select className="select" aria-label="API project" value={w.project} onChange={e=>w.navigate({project:e.target.value})}>{data.projects.map(p=><option key={p.id} value={p.id}>{p.name} · {p.service_id}</option>)}</select>;
  const needProject=<Section title="No API project yet" sub="Create an API project from the overview first."><button className="link" onClick={()=>w.navigate({tab:'overview'})}>Go to overview</button></Section>;

  const impactTable=data.impact.length?<Table headers={['Consumer','Coverage','Predicted','Verified','Validated','Deployment']}>
    {data.impact.map(i=>{const none=i.coverage==='unscanned';return <tr key={i.enrollment_id}><td>{i.consumer_display_name}</td><td><Status value={i.coverage}/></td>
      <td className="num">{none?<span className="dash">—</span>:`${i.predicted_risk_count} predicted`}</td><td className="num">{none?<span className="dash">—</span>:`${i.verified_regression_count} verified`}</td>
      <td className="num">{none?<span className="dash">—</span>:`${i.validated_fix_count} validated`}</td><td><Status value={i.deployment}/></td></tr>})}
  </Table>:<Empty title="No impact summary yet">Run an assessment after an opted-in consumer scans a repository.</Empty>;

  if(tab==='endpoints'){
    const shown=data.endpoints.filter(e=>(e.method+' '+e.path).toLowerCase().includes(search.toLowerCase()));
    const detail=shown.find(e=>e.id===picked)??shown[0];
    return <>
      <PageHead title="Endpoints" subtitle={!project?undefined:stripe?'Stripe’s published OpenAPI description, pinned version 2026-09-30. Stripe’s server code is not part of this inventory.':'The API’s OpenAPI description, reconciled with its server code.'}/>
      {!project?needProject:<section className="section">
        <div className="toolbar">{projectSelect}
          <label className="search"><span className="sr-only">Search methods and paths</span><Search/><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search method or path"/></label>
          <button className="button primary" disabled={w.capabilities?.discovery!=='fixture'} onClick={()=>run(()=>api(`/api-projects/${project.id}/discoveries`,{method:'POST',org:org.id,body:{spec_reference:'demo-fixture'}}))}>Load contract</button>
        </div>
        <p className="hint">{data.endpoints.length?(search?`${shown.length} of ${plural(data.endpoints.length,'operation')}`:plural(data.endpoints.length,'operation')):'Contract not loaded'}</p>
        {data.endpoints.length?<div style={{marginTop:18}}><Table headers={['Method','Path','Source','Coverage']}>
          {shown.map(e=><tr key={e.id} className={'selectable'+(e.id===detail?.id?' selected':'')} onClick={()=>setPicked(e.id)}><td><code>{e.method}</code></td><td><code>{e.path}</code></td><td>{sourceOf(e)}</td><td><Status value={e.reconciliation}/></td></tr>)}
        </Table></div>:<Empty title="No endpoints loaded">Load the contract to inspect its documented operations.</Empty>}
      </section>}
      {detail&&<section className="section" aria-label="Endpoint detail">
        <h2 className="detail-title">{detail.method} {detail.path}</h2>
        {amountType(detail)&&<p className="field-line" style={{marginTop:10}}><code>response.amount</code>·<code>{amountType(detail)}</code></p>}
        <dl className="kv">
          {detail.operation_id&&<><dt>Operation</dt><dd>{detail.operation_id}</dd></>}
          {detail.evidence.slice(0,2).map(x=><Fragment key={x.id}><dt>Source</dt><dd>{x.source_uri}</dd><dt>Revision</dt><dd>{x.revision}</dd></Fragment>)}
          <dt>Confidence</dt><dd>{detail.confidence}</dd>
        </dl>
        <p className="hint" style={{marginTop:18}}>Not found in scan does not mean absent. An incomplete scan never means an endpoint is missing.</p>
      </section>}
    </>;
  }

  if(tab==='consumers')return <>
    <PageHead title="Consumers" subtitle={project?`Teams invited to ${project.name}`:undefined} aside={projectSelect}/>
    {!project?needProject:<><section className="section">
      <form className="inline-form" onSubmit={e=>{e.preventDefault();run(()=>api(`/api-projects/${project.id}/invitations`,{method:'POST',org:org.id,body:{recipient_email:email}}))}}>
        <label className="field">Recipient email<input type="email" value={email} onChange={e=>setEmail(e.target.value)} placeholder="name@company.com" required/></label><button className="button primary">Invite consumer</button>
      </form>
      <p className="hint">{w.capabilities?.email==='development_inbox'?'Delivery uses the development inbox in this workspace.':w.capabilities?.email==='smtp'?'Delivery sends an email notification over SMTP.':'Email delivery is unavailable.'} Invited teams sign in with that address to accept.</p>
      <div style={{marginTop:28}}>{data.consumers.length?<Table headers={['Recipient','Invitation','Delivery','Enrollment','Summary sharing','Action']}>
        {data.consumers.map(c=><tr key={c.invitation_id}><td><code>{c.recipient_email}</code></td><td><Status value={c.invitation_status}/></td>
          <td><Status value={c.delivery_status}/>{c.delivery_attention&&<div className="small-muted">Needs reconciliation; not retried automatically.</div>}</td>
          <td><Status value={c.enrollment_status}/></td><td>{c.enrollment_status==='active'?(c.share_summary?'On':'Off'):<span className="dash">—</span>}</td>
          <td>{c.invitation_status==='pending'?<button className="link" onClick={()=>run(()=>api(`/invitations/${c.invitation_id}`,{method:'DELETE',org:org.id}))}>Revoke</button>:c.enrollment_status==='active'&&<button className="link" onClick={()=>w.navigate({tab:'changes'})}>View summary</button>}</td></tr>)}
      </Table>:<Empty title="No consumers invited">Invite a team by email to start onboarding.</Empty>}</div>
    </section>
    <Section title="What providers can see"><p className="prose">Shared impact counts and readiness. Repository names, source, logs, patches, and PR URLs stay private to each consumer.</p></Section></>}
  </>;

  if(tab==='changes')return <>
    <PageHead title="Assess a change"/>
    {!project?needProject:<>
      <div className="facts"><div><span>Project</span>{project.name}</div><div><span>Endpoint</span><code>{endpointName}</code></div><div><span>Field</span><code>response.amount</code></div></div>
      <div className="columns">
        <div><h2 className="col-title">Current contract</h2><p className="section-sub">Describe the current meaning and format of this field.</p><div className="field col-field"><textarea aria-label="Current behavior" value={oldSemantics} onChange={e=>setOldSemantics(e.target.value)}/></div></div>
        <div><h2 className="col-title">Proposed contract</h2><p className="section-sub">Describe the new meaning and format of this field.</p><div className="field col-field"><textarea aria-label="Proposed behavior" value={newSemantics} onChange={e=>setNewSemantics(e.target.value)}/></div></div>
      </div>
      <div className="facts plain"><div><span>Schema:</span><code>{schemaType} → {schemaType}</code></div><div><span>Declared semantic change:</span><code>units_changed</code></div></div>
      <div className="button-row"><button className="button primary" onClick={()=>run(()=>api(`/api-projects/${project.id}/assessments`,{method:'POST',org:org.id,body:{}}))}>Assess change</button>
        {latest&&<Status value="succeeded" text={`Assessment recorded · ${label(latest.kind)}`}/>}</div>
      <p className="hint">Shockwave compares the current and proposed contracts, applies the declared change in meaning, and checks every opted-in consumer.</p>
      <div style={{height:28}}/>
      <Section title="Shared impact" sub="Predicted and verified impact for consumers of this field change.">{impactTable}</Section>
      {data.changes.length>0&&<Section title="Assessment history"><ul className="history">{[...data.changes].reverse().slice(0,8).map(c=><li key={c.id}><strong>{label(c.kind)}</strong><span><code>{c.field_path}</code> · {c.old_semantics} → {c.new_semantics}</span></li>)}</ul></Section>}
      <p className="caution-line">The schema stays {schemaType}, so a schema check alone sees no change. The meaning changes.</p>
    </>}
  </>;

  return <>
    <PageHead title={project?.name??org.name} subtitle={project?'Provider overview':'Provider workspace · create an API project to begin'} aside={projectSelect}
      meta={project?[plural(data.endpoints.length,'endpoint'),plural(data.consumers.length,'consumer'),plural(data.changes.length,'assessed change'),plural(verified,'verified regression')]:undefined}/>
    <DemoPath role="provider" data={data}/>
    {project&&<Section title="Latest assessment" action={latest&&<button className="button primary" onClick={()=>w.navigate({tab:'changes'})}>Review assessment</button>}>
      {latest?<>
        <p className="field-line">Field <code>{latest.field_path}</code></p>
        <p className="field-line">{label(latest.kind)} <Status value="proposed"/></p>
        <div className="compare"><div><div className="label">Current</div><div className="value">{latest.old_semantics}</div></div><div><div className="label">Proposed</div><div className="value">{latest.new_semantics}</div></div></div>
        <p className="hint">The schema remains {schemaType}. The meaning changes.</p>
      </>:<Empty title="No assessment yet"><button className="link" onClick={()=>w.navigate({tab:'changes'})}>Assess a change</button> after a consumer has scanned a repository.</Empty>}
    </Section>}
    {project&&<Section title="Consumer readiness">{impactTable}</Section>}
    <Section title="API project" sub={real?'Creates a project for Stripe’s published API contract.':'Creates a project for the Demo Payments contract.'}>
      <form className="inline-form" onSubmit={e=>{e.preventDefault();run(async()=>{const created=await api<APIProject>('/api-projects',{method:'POST',org:org.id,body:real?{name:projectName,service_id:'stripe',base_urls:['https://api.stripe.com']}:{name:projectName,service_id:'demo-payments',base_urls:['http://demo-payments.test']}});w.navigate({project:created.id,tab:'endpoints'})})}}>
        <label className="field">New project name<input value={projectName} onChange={e=>setProjectName(e.target.value)} required/></label>
        <button className={'button'+(project?'':' primary')}>{real?'Create Stripe API project':'Create Demo Payments API'}</button>
      </form>
    </Section>
  </>;
}
