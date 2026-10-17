'use client';
import {useEffect,useState} from 'react';
import {api,DetailedRisk} from '../lib/api';
import {useWorkspace} from '../lib/use-workspace';
import {DemoPath} from './demo-path';
import {Empty,PageHead,Section,Status,Table,label,plural} from './status';

type Workspace=ReturnType<typeof useWorkspace>;
const useSite=(r:DetailedRisk)=>[...r.evidence].reverse().find(e=>e.kind==='source'&&e.file)??r.evidence[0];
const stateOf=(r:DetailedRisk)=>r.evidence_state.current?(r.evidence_state.validated?'patch_validated':r.evidence_state.verified?'verified_regression':'predicted'):'stale';
type Proof={expected:string;actual:string};
// Assertion evidence from the private test log: harness reports ("expected $10.00, actual $0.10")
// or unittest failures ('$0.10' != '$10.00').
const shown=(v:unknown)=>typeof v==='boolean'?(v?'Flagged':'Not flagged'):String(v);
// Scenario case reports (SHOCKWAVE-PROOF lines: the relevant case per phase) win; otherwise
// assertion messages from the test log.
function proofFrom(log:string){
  const cases=log.split('\n').filter(l=>l.startsWith('SHOCKWAVE-PROOF ')).map(l=>{try{return JSON.parse(l.slice(16))}catch{return null}}).filter(Boolean);
  const find=(phase:string)=>{const c=cases.find((x:{phase:string})=>x.phase===phase);return c?{expected:shown(c.expected),actual:shown(c.actual)}:undefined};
  if(cases.length)return {baseline:find('baseline'),proposed:find('proposed')};
  const pairs:Proof[]=[...log.matchAll(/expected (\$[\d.,]+), actual (\$[\d.,]+)/g)].map(m=>({expected:m[1],actual:m[2]}));
  for(const m of log.matchAll(/'(\$[\d.,]+)' != '(\$[\d.,]+)'/g))pairs.push({expected:m[2],actual:m[1]});
  return {baseline:pairs.find(p=>p.actual===p.expected),proposed:pairs.find(p=>p.actual!==p.expected)};
}

type Row={n?:number;text:string;kind:'ctx'|'del'|'add'|'gap'};
type DiffFile={name:string;left:Row[];right:Row[]};
// Unified diff -> two aligned columns (original | updated); hunk line counts decide what is a header.
function splitDiff(content:string){
  const files:DiffFile[]=[];let file:DiffFile|undefined;let oldName='';let l=0,r=0,remL=0,remR=0;let dels:Row[]=[],adds:Row[]=[];
  const flush=()=>{if(!file)return;for(let i=0;i<Math.max(dels.length,adds.length);i++){file.left.push(dels[i]??{text:'',kind:'gap'});file.right.push(adds[i]??{text:'',kind:'gap'})}dels=[];adds=[]};
  for(const line of content.split('\n')){
    if(file&&(remL>0||remR>0)){
      if(line.startsWith('-')){dels.push({n:l++,text:line.slice(1),kind:'del'});remL--;continue}
      if(line.startsWith('+')){adds.push({n:r++,text:line.slice(1),kind:'add'});remR--;continue}
      if(line.startsWith('\\'))continue;
      flush();file.left.push({n:l++,text:line.slice(1),kind:'ctx'});file.right.push({n:r++,text:line.slice(1),kind:'ctx'});remL--;remR--;continue;
    }
    flush();
    if(line.startsWith('--- ')){oldName=line.slice(4).replace(/^a\//,'');continue}
    if(line.startsWith('+++ ')){const name=line.slice(4).replace(/^b\//,'');file={name:name==='/dev/null'?oldName:name,left:[],right:[]};files.push(file);continue}
    const h=line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
    if(h&&file){if(file.left.length){file.left.push({text:'⋯',kind:'gap'});file.right.push({text:'⋯',kind:'gap'})}l=+h[1];remL=h[2]===undefined?1:+h[2];r=+h[3];remR=h[4]===undefined?1:+h[4]}
  }
  flush();return files;
}
function Code({rows}:{rows:Row[]}){
  return <pre className="code-block">{rows.map((row,i)=><span key={i} className={'code-row '+row.kind}><span className="ln">{row.n??''}</span><span className="mk">{row.kind==='del'?'-':row.kind==='add'?'+':''}</span><span>{row.text||' '}</span></span>)}</pre>;
}
function ArtifactView({content}:{content:string}){
  const files=content.startsWith('--- ')||content.startsWith('diff ')||content.includes('\n@@ ')?splitDiff(content):[];
  if(!files.length)return <div className="sub-block"><h3>Private test log</h3><pre className="code-block log" aria-label="Private artifact">{content}</pre></div>;
  return <div className="sub-block" aria-label="Private artifact"><h3>Code comparison</h3>{files.map(f=><div className="diff-file" key={f.name}>
    <div className="diff-name">{f.name}</div>
    <div className="diff-split"><div><div className="diff-label">Original</div><Code rows={f.left}/></div><div><div className="diff-label">Updated</div><Code rows={f.right}/></div></div>
  </div>)}</div>;
}

export function Consumer({w,run}:{w:Workspace;run:(fn:()=>Promise<unknown>)=>void}){
  const data=w.data,org=w.org!,tab=w.location.tab;
  const [reviewing,setReviewing]=useState('');
  const [consent,setConsent]=useState(false);const [share,setShare]=useState(true);
  const [repoIds,setRepoIds]=useState<string[]>([]);const [model,setModel]=useState(false);const [write,setWrite]=useState(false);
  const [artifact,setArtifact]=useState('');const [publishConsent,setPublishConsent]=useState(false);
  const [snapshotSha,setSnapshotSha]=useState('');
  const enrollment=data.enrollments.find(e=>e.id===w.enrollment)||data.enrollments.find(e=>e.status==='active');
  const risk=data.risks.find(r=>r.id===w.location.risk);
  useEffect(()=>{setArtifact('');setSnapshotSha('');if(!risk)return;let active=true;
    api<{commit_sha:string}>(`/risks/${risk.id}/snapshot`,{org:org.id}).then(x=>{if(active)setSnapshotSha(x.commit_sha)}).catch(()=>{});
    return()=>{active=false}
  },[risk?.id,org.id]);
  const preview=data.incoming.find(i=>i.invitation_id===reviewing);
  function review(id:string){setReviewing(id);setConsent(false);w.navigate({tab:'invitations'})}
  const selected=data.selectedRepos.filter(r=>r.status==='connected');
  const current=data.risks.filter(r=>r.evidence_state.current);
  const validated=data.risks.filter(r=>r.evidence_state.validated);
  const real=w.capabilities?.mode==='integrated';

  const findings=data.risks.length?<Table headers={['Source','Finding','Severity','Evidence','Action']}>{data.risks.map(r=>{const site=useSite(r);return <tr key={r.id} className={r.id===risk?.id?'selected':undefined}>
    <td><code>{site?.file??'Unknown file'}:{site?.line_start??'?'}</code></td><td><div className="clip" title={r.explanation}>{r.explanation}</div></td><td>{label(r.severity)}</td>
    <td><Status value={stateOf(r)}/></td><td><button className="link" onClick={()=>w.navigate({risk:r.id,tab:'findings'})}>Inspect</button></td></tr>})}</Table>
    :<Empty title="No findings">Scan a repository and ask the provider to assess a change.</Empty>;
  const repoTable=(actions:boolean)=>data.selectedRepos.length?<Table headers={actions?['Repository','Branch','Permissions','Status','Actions']:['Repository','Branch','Permissions','Status']}>
    {data.selectedRepos.map(r=><tr key={r.id}><td><code>{r.full_name}</code></td><td><code>{r.default_branch}</code></td>
      <td>{r.enrollment_permissions.allow_model_processing?'Model allowed':'Model off'} · {r.enrollment_permissions.allow_pr_write?'PR write allowed':'PR write off'}</td><td><Status value={r.status}/></td>
      {actions&&enrollment&&<td><div className="button-row"><button className="link" disabled={r.status!=='connected'} onClick={()=>run(()=>api(`/repository-connections/${r.id}/scans`,{method:'POST',org:org.id,body:{enrollment_id:enrollment.id}}))}>Scan / retry</button><button className="link danger" disabled={r.status!=='connected'} onClick={()=>run(()=>api(`/repository-connections/${r.id}`,{method:'DELETE',org:org.id}))}>Disconnect</button></div></td>}</tr>)}
  </Table>:<Empty title="No selected repositories">Connect repositories and choose which ones to analyze.</Empty>;
  const incoming=data.incoming.length>0&&<Table headers={['Provider','API','Expires','Action']}>{data.incoming.map(i=><tr key={i.invitation_id}><td>{i.provider_name}</td><td>{i.api_name}</td><td className="date">{new Date(i.expires_at).toLocaleDateString()}</td><td><button className="link" onClick={()=>review(i.invitation_id)}>Review invitation</button></td></tr>)}</Table>;

  const prerequisite=w.capabilities?.reasons.verification&&<div role="status" className="notice">{w.capabilities.reasons.verification}</div>;

  if(tab==='invitations')return <>
    <PageHead title="Invitations" subtitle={`Providers invite you by email address. Invitations sent to ${w.session?.user.email} appear here.`}
      meta={[`${data.incoming.length} pending`,plural(data.enrollments.filter(e=>e.status==='active').length,'active enrollment')]}/>
    {preview&&<section className="section" aria-live="polite" aria-label="Invitation review">
      <h2 className="detail-title">{preview.provider_name} invited you to {preview.api_name}</h2>
      <p className="prose" style={{marginTop:12}}>{preview.processing}</p><p className="small-muted">Expires {new Date(preview.expires_at).toLocaleString()} · terms {preview.consent_version}</p>
      <div className="checks">
        <label className="check"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>I consent to central processing of repositories I select.</label>
        <label className="check"><input type="checkbox" checked={share} onChange={e=>setShare(e.target.checked)}/>Share summary counts and readiness with this provider.</label>
      </div>
      <div className="button-row"><button className="button primary" disabled={!consent} onClick={()=>run(async()=>{await api('/invitations/accept',{method:'POST',org:org.id,body:{invitation_id:preview.invitation_id,consumer_org_id:org.id,consent_version:preview.consent_version,share_summary:share}});setReviewing('');setConsent(false);w.navigate({tab:'repositories'})})}>Accept invitation</button>
        <button className="button" onClick={()=>setReviewing('')}>Not now</button></div>
    </section>}
    <Section title="Pending invitations">{incoming||<Empty title="No pending invitations">Providers invite you by email address.</Empty>}
      {data.mail.length>0&&<details className="inbox"><summary>Development inbox ({data.mail.length}) · emails that would be sent without SMTP</summary>{data.mail.map(m=><div key={m.id} className="mail"><strong>{m.subject}</strong><p className="mail-body">{m.body}</p></div>)}</details>}
    </Section>
    {data.enrollments.length>0&&<Section title="Enrollments" sub="Repository selection and findings are specific to one enrollment.">
      <select className="select" aria-label="Active enrollment" value={enrollment?.id||''} onChange={e=>w.navigate({enrollment:e.target.value,risk:''})}>{data.enrollments.map(e=><option key={e.id} value={e.id}>{e.api_project_id.slice(0,8)} · {e.status}</option>)}</select>
    </Section>}
  </>;

  if(tab==='repositories')return <>
    {prerequisite}
    <PageHead title="Repositories" subtitle="You choose exactly which repositories Shockwave may analyze. Processing, AI model access and pull-request permissions are granted separately."
      meta={enrollment?[plural(selected.length,'selected repository','selected repositories'),`Enrollment ${enrollment.status}`]:undefined}/>
    {!enrollment?<Section title="Accept an invitation first" sub="Repository selection is specific to one active enrollment."><button className="link" onClick={()=>w.navigate({tab:'invitations'})}>Go to invitations</button></Section>:<>
      <Section title="Repository access" sub="Grant Shockwave read access through the GitHub installation, then pick repositories."
        action={<button className="button" disabled={w.capabilities?.repository_access!=='fixture'} onClick={()=>run(async()=>{const connection=await api<{installation_url:string}>('/github/connect',{method:'POST',org:org.id,body:{return_route:'/'}});await api(connection.installation_url.replace('/api/v1',''),{org:org.id})})}>Connect repositories</button>}>
        {data.repoOptions.length>0?<div>
          <h3>Available repositories</h3>
          <div className="option-list">{data.repoOptions.map(option=><label className="check" key={option.github_repository_id}><input type="checkbox" checked={repoIds.includes(option.github_repository_id)} onChange={e=>setRepoIds(e.target.checked?[...repoIds,option.github_repository_id]:repoIds.filter(x=>x!==option.github_repository_id))}/>{option.full_name}</label>)}</div>
          <div className="checks">
            <label className="check"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>Allow central processing of selected repositories.</label>
            <label className="check"><input type="checkbox" checked={model} onChange={e=>setModel(e.target.checked)}/>Allow optional AI model processing.</label>
            <label className="check"><input type="checkbox" checked={write} onChange={e=>setWrite(e.target.checked)}/>Allow pull-request publishing.</label>
          </div>
          <button className="button primary" disabled={!consent||repoIds.length===0} onClick={()=>run(async()=>{for(const id of repoIds)await api('/repository-connections',{method:'POST',org:org.id,body:{github_repository_id:id,enrollment_id:enrollment.id,scan_consent:true,allow_model_processing:model,allow_pr_write:write}});setRepoIds([])})}>Select {repoIds.length||''} repositories</button>
        </div>:<p className="small-muted">No repositories listed yet. Connect the installation to see them.</p>}
      </Section>
      <Section title="Selected for this enrollment">{repoTable(true)}</Section>
      <Section title="Consent" sub="Revocation stops new work, removes access to private artifacts, and deletes this enrollment’s copied source snapshots.">
        <button className="button danger" disabled={enrollment.status!=='active'} onClick={()=>run(()=>api(`/enrollments/${enrollment.id}`,{method:'DELETE',org:org.id}))}>Revoke enrollment consent</button>
      </Section>
    </>}
  </>;

  if(tab==='findings')return <>
    {prerequisite}
    <PageHead title="Findings" subtitle={`Private to ${org.name}. Predictions and executed tests are kept separate.`}
      meta={[plural(current.length,'current finding'),plural(data.risks.filter(r=>r.evidence_state.verified).length,'verified regression'),plural(validated.length,'validated patch','validated patches')]}/>
    <Section title="Impact findings">{findings}</Section>
    {risk&&<RiskDetail risk={risk} org={org.id} sha={snapshotSha} artifact={artifact} setArtifact={setArtifact} publishConsent={publishConsent} setPublishConsent={setPublishConsent} publication={w.capabilities?.publication??'unavailable'} run={run} real={real}/>}
  </>;

  return <>
    {prerequisite}
    <PageHead title={org.name} subtitle="Consumer workspace"
      meta={[plural(selected.length,'selected repository','selected repositories'),plural(current.length,'current finding'),plural(validated.length,'validated patch','validated patches')]}/>
    <DemoPath role="consumer" data={data}/>
    {data.incoming.length>0&&<Section title="Pending invitations">{incoming}</Section>}
    <Section title="Needs review">{findings}</Section>
    <Section title="Repository access">{repoTable(false)}<p style={{marginTop:14}}><button className="link" onClick={()=>w.navigate({tab:'repositories'})}>Manage repository permissions</button></p></Section>
    {validated.length>0&&<p className="caution-line">Validated for the proposed contract only. Deployment unknown.</p>}
  </>;
}

function RiskDetail({risk,org,sha,artifact,setArtifact,publishConsent,setPublishConsent,publication,run,real}:{risk:DetailedRisk;org:string;sha:string;artifact:string;setArtifact:(v:string)=>void;publishConsent:boolean;setPublishConsent:(v:boolean)=>void;publication:string;run:(fn:()=>Promise<unknown>)=>void;real:boolean}){
  const verification=risk.verifications.at(-1);const migration=risk.migrations.at(-1);const site=useSite(risk);
  const [proof,setProof]=useState<{baseline?:Proof;proposed?:Proof}>({});
  useEffect(()=>{setProof({});const id=verification?.artifact_ids[0];if(!id)return;let active=true;
    api<{content:string}>(`/artifacts/${id}`,{org}).then(x=>{if(active)setProof(proofFrom(x.content))}).catch(()=>{});
    return()=>{active=false}},[verification?.id,org]);
  const expected=proof.proposed?.expected??proof.baseline?.expected;
  const basePass=verification?.baseline?.exit_code===0;
  const regression=verification?.status==='verified_regression';
  return <section className="section" aria-label="Finding detail">
    <div className="section-head"><div><h2 className="detail-title">{site?.file}{site?.line_start?`:${site.line_start}`:''}</h2>
      <p className="section-sub">{label(risk.severity)} risk · scanned revision <code>{sha?sha.slice(0,12):'loading'}</code></p></div><Status value={stateOf(risk)}/></div>
    <p className="prose">{risk.explanation}</p>
    <div className="notice">{risk.suggested_action}</div>
    {site?.excerpt&&<div className="excerpt"><Code rows={[{n:site.line_start??undefined,text:site.excerpt,kind:'ctx'}]}/></div>}

    <div className="sub-block"><div className="section-head"><div><h3>Current versus proposed contract</h3><p className="section-sub">Your own code and tests, run against both versions of the response.</p></div>
      <button className="button primary" disabled={!risk.evidence_state.current} onClick={()=>run(()=>api(`/risks/${risk.id}/verifications`,{method:'POST',org}))}>Run verification (current vs proposed)</button></div>
      {verification&&<>
        <div className="compare">
          <div><div className="label">Current contract{real?' · amount in cents':''}</div><div className="value big">{proof.baseline?.actual??expected??'—'}</div>
            <Status value={basePass?'validated':'failed'} text={(basePass?'Your test passes':'Your test fails')+(expected?` · expects ${expected}`:'')}/></div>
          <div><div className="label">Proposed contract{real?' · amount in dollars':''}</div><div className="value big">{proof.proposed?.actual??(verification.proposed?.exit_code===0?expected:'—')}</div>
            <Status value={regression?'verified_regression':verification.proposed?.exit_code===0?'compatible':verification.status} text={regression?`Regression proven${expected?` · expected ${expected}`:''}`:label(verification.status)}/></div>
        </div>
        <p className="prose" style={{marginTop:16}}>{verification.summary}</p>
        <Table headers={['Contract','Exit','Tests','Infrastructure']}><tr><td>Current</td><td className="num">{verification.baseline?.exit_code??'Unknown'}</td><td className="num">{verification.baseline?.tests_run??0}</td><td>{verification.baseline?.infrastructure_error??'None'}</td></tr><tr><td>Proposed</td><td className="num">{verification.proposed?.exit_code??'Unknown'}</td><td className="num">{verification.proposed?.tests_run??0}</td><td>{verification.proposed?.infrastructure_error??'None'}</td></tr></Table>
        {verification.artifact_ids[0]&&<p style={{marginTop:14}}><button className="button" onClick={()=>run(async()=>setArtifact((await api<{content:string}>(`/artifacts/${verification.artifact_ids[0]}`,{org})).content))}>View private test log</button></p>}
      </>}
    </div>

    <div className="sub-block"><div className="section-head"><div><h3>Migration and validation</h3><p className="section-sub">The patch targets the proposed contract only, so it ships with the provider’s change.</p></div>
      <button className="button primary" disabled={!risk.evidence_state.verified} onClick={()=>run(()=>api(`/risks/${risk.id}/migrations`,{method:'POST',org,body:{mode:'preview'}}))}>Generate migration patch</button></div>
      {migration&&<>
        <div className="result-line"><Status value={migration.status}/><span className="small-muted">{migration.validation?.tests_run??0} validation tests · exit {migration.validation?.exit_code??'unknown'}</span>
          {migration.patch_artifact_id&&<button className="button" onClick={()=>run(async()=>setArtifact((await api<{content:string}>(`/artifacts/${migration.patch_artifact_id}`,{org})).content))}>Review patch diff</button>}</div>
        <div className="notice">Publishing pull requests is turned off in this workspace. Review the patch here; deployment timing stays with your team.</div>
        <div className="checks"><label className="check"><input type="checkbox" checked={publishConsent} onChange={e=>setPublishConsent(e.target.checked)}/>I authorize a publication request for this patch.</label></div>
        <button className="button primary" disabled={publication!=='live'||!publishConsent||!risk.evidence_state.validated||!sha} onClick={()=>run(()=>api(`/migrations/${migration.id}/publish`,{method:'POST',org,body:{explicit_consent:true,expected_commit:sha}}))}>Publish migration PR</button>
      </>}
    </div>
    {artifact&&<ArtifactView content={artifact}/>}
  </section>;
}
