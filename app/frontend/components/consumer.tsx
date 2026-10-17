'use client';
import {useEffect,useState} from 'react';
import {api,DetailedRisk,RepoOption} from '../lib/api';
import {useWorkspace} from '../lib/use-workspace';
import {Empty,Status,Table} from './status';

type Workspace=ReturnType<typeof useWorkspace>;
const useSite=(r:DetailedRisk)=>[...r.evidence].reverse().find(e=>e.kind==='source'&&e.file)??r.evidence[0];
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
function ArtifactView({content}:{content:string}){
  const diff=content.startsWith('--- ')||content.includes('\n@@ ');
  return <pre className="code-block" aria-label="Private artifact">{diff?content.split('\n').map((line,i)=><div key={i} className={line.startsWith('+')&&!line.startsWith('+++')?'diff-add':line.startsWith('-')&&!line.startsWith('---')?'diff-del':line.startsWith('@@')?'diff-hunk':undefined}>{line||' '}</div>):content}</pre>;
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
  const real=w.capabilities?.mode==='integrated';
  return <>
    <div className="metric-grid">
      <div className="metric"><span>Selected repositories</span><strong>{selected.length}</strong><small>For this enrollment</small></div>
      <div className="metric"><span>Current risks</span><strong>{data.risks.filter(r=>r.evidence_state.current).length}</strong><small>Private to your organization</small></div>
      <div className="metric"><span>Current validated patches</span><strong>{data.risks.filter(r=>r.evidence_state.validated).length}</strong><small>Tested and passing</small></div>
    </div>
    {preview&&(tab==='overview'||tab==='invitations')&&<section className="panel invitation" aria-live="polite">
      <span className="eyebrow">INVITATION REVIEW</span>
      <h2>{preview.provider_name} invited you to {preview.api_name}</h2><p>{preview.processing}</p><p className="small-muted">Expires {new Date(preview.expires_at).toLocaleString()} · terms {preview.consent_version}</p>
        <label className="check"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>I consent to central processing of repositories I select.</label>
        <label className="check"><input type="checkbox" checked={share} onChange={e=>setShare(e.target.checked)}/>Share summary counts and readiness with this provider.</label>
        <button className="button primary" disabled={!consent} onClick={()=>run(async()=>{await api('/invitations/accept',{method:'POST',org:org.id,body:{invitation_id:preview.invitation_id,consumer_org_id:org.id,consent_version:preview.consent_version,share_summary:share}});setReviewing('');setConsent(false);w.navigate({tab:'repositories'})})}>Accept invitation</button>
      <button className="button subtle" onClick={()=>setReviewing('')}>Not now</button>
    </section>}
    {(tab==='overview'||tab==='invitations')&&<section className="panel"><div className="panel-header"><div><span className="eyebrow">ONBOARDING</span><h2>Invitations and enrollments</h2></div><Status value={data.incoming.length+' pending'}/></div>
      {data.incoming.length?<Table headers={['Provider','API','Expires','Action']}>{data.incoming.map(i=><tr key={i.invitation_id}><td>{i.provider_name}</td><td>{i.api_name}</td><td>{new Date(i.expires_at).toLocaleDateString()}</td><td><button className="button subtle" onClick={()=>review(i.invitation_id)}>Review invitation</button></td></tr>)}</Table>:<Empty title="No pending invitations">Providers invite you by email address. Invitations sent to {w.session?.user.email} appear here.</Empty>}
      {data.mail.length>0&&<details className="section-divider"><summary>Development inbox ({data.mail.length}) · emails that would be sent without SMTP</summary>{data.mail.map(m=><div key={m.id} className="section-divider"><strong>{m.subject}</strong><p className="mail-body">{m.body}</p></div>)}</details>}
      {data.enrollments.length>0&&<div className="section-divider"><label className="field">Active enrollment<select value={enrollment?.id||''} onChange={e=>w.navigate({enrollment:e.target.value,risk:''})}>{data.enrollments.map(e=><option key={e.id} value={e.id}>{e.api_project_id.slice(0,8)} · {e.status}</option>)}</select></label></div>}
    </section>}
    {(tab==='overview'||tab==='repositories')&&<section className="panel"><div className="panel-header"><div><span className="eyebrow">SOURCE CONSENT</span><h2>Repository selection</h2></div><Status value={w.capabilities?.repository_access==='fixture'?'ready':w.capabilities?.repository_access??'unavailable'}/></div>
      {!enrollment?<Empty title="Accept an invitation first">Repository selection is specific to one active enrollment.</Empty>:<>
        <p>You choose exactly which repositories Shockwave may analyze. Processing, AI model access and pull-request permissions are granted separately.</p>
        <button className="button" disabled={w.capabilities?.repository_access!=='fixture'} onClick={()=>run(async()=>{const connection=await api<{installation_url:string}>('/github/connect',{method:'POST',org:org.id,body:{return_route:'/'}});await api(connection.installation_url.replace('/api/v1',''),{org:org.id})})}>Connect repositories</button>
        {data.repoOptions.length>0&&<div className="section-divider"><h3>Available repositories</h3><div className="option-list">{data.repoOptions.map(option=><label className="check" key={option.github_repository_id}><input type="checkbox" checked={repoIds.includes(option.github_repository_id)} onChange={e=>setRepoIds(e.target.checked?[...repoIds,option.github_repository_id]:repoIds.filter(x=>x!==option.github_repository_id))}/>{option.full_name}</label>)}</div>
          <label className="check"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>Allow central processing of selected repositories.</label>
          <label className="check"><input type="checkbox" checked={model} onChange={e=>setModel(e.target.checked)}/>Allow optional AI model processing.</label>
          <label className="check"><input type="checkbox" checked={write} onChange={e=>setWrite(e.target.checked)}/>Allow pull-request publishing.</label>
          <button className="button primary" disabled={!consent||repoIds.length===0} onClick={()=>run(async()=>{for(const id of repoIds)await api('/repository-connections',{method:'POST',org:org.id,body:{github_repository_id:id,enrollment_id:enrollment.id,scan_consent:true,allow_model_processing:model,allow_pr_write:write}});setRepoIds([])})}>Select {repoIds.length||''} repositories</button>
        </div>}
        <div className="section-divider"><h3>Selected for this enrollment</h3>{data.selectedRepos.length?<Table headers={['Repository','Permissions','Status','Actions']}>
          {data.selectedRepos.map(r=><tr key={r.id}><td>{r.full_name}<div className="small-muted">{r.default_branch}</div></td><td>{r.enrollment_permissions.allow_model_processing?'Model allowed':'Model off'} · {r.enrollment_permissions.allow_pr_write?'PR write allowed':'PR write off'}</td><td><Status value={r.status}/></td><td><div className="button-row"><button className="button subtle" disabled={r.status!=='connected'} onClick={()=>run(()=>api(`/repository-connections/${r.id}/scans`,{method:'POST',org:org.id,body:{enrollment_id:enrollment.id}}))}>Scan / retry</button><button className="button danger" disabled={r.status!=='connected'} onClick={()=>run(()=>api(`/repository-connections/${r.id}`,{method:'DELETE',org:org.id}))}>Disconnect</button></div></td></tr>)}
        </Table>:<Empty title="No selected repositories">Connect repositories and choose one to analyze.</Empty>}</div>
        <div className="section-divider"><button className="button danger" disabled={enrollment.status!=='active'} onClick={()=>run(()=>api(`/enrollments/${enrollment.id}`,{method:'DELETE',org:org.id}))}>Revoke enrollment consent</button><p className="small-muted">Revocation stops new work and removes access to private artifacts.</p></div>
      </>}
    </section>}
    {(tab==='overview'||tab==='findings')&&<section className="panel"><div className="panel-header"><div><span className="eyebrow">PRIVATE EVIDENCE</span><h2>Impact findings</h2></div><span className="small-muted">Predictions and executed tests are separate.</span></div>
      {data.risks.length?<Table headers={['Source','Severity','Evidence','Action']}>{data.risks.map(r=><tr key={r.id}><td><code>{useSite(r)?.file??'Unknown file'}</code><div className="small-muted">line {useSite(r)?.line_start??'?'}</div></td><td><Status value={r.severity}/></td><td><Status value={r.evidence_state.current?(r.evidence_state.validated?'patch_validated':r.evidence_state.verified?'verified_regression':'predicted'):'stale'}/></td><td><button className="button subtle" onClick={()=>w.navigate({risk:r.id,tab:'findings'})}>Inspect</button></td></tr>)}</Table>:<Empty title="No findings">Scan a repository and ask the provider to assess a change.</Empty>}
    </section>}
    {risk&&(tab==='overview'||tab==='findings')&&<RiskDetail risk={risk} org={org.id} sha={snapshotSha} artifact={artifact} setArtifact={setArtifact} publishConsent={publishConsent} setPublishConsent={setPublishConsent} publication={w.capabilities?.publication??'unavailable'} publicationReason={w.capabilities?.reasons.publication??''} run={run} real={real}/>}
  </>;
}

function RiskDetail({risk,org,sha,artifact,setArtifact,publishConsent,setPublishConsent,publication,publicationReason,run,real}:{risk:DetailedRisk;org:string;sha:string;artifact:string;setArtifact:(v:string)=>void;publishConsent:boolean;setPublishConsent:(v:boolean)=>void;publication:string;publicationReason:string;run:(fn:()=>Promise<unknown>)=>void;real:boolean}){
  const verification=risk.verifications.at(-1);const migration=risk.migrations.at(-1);const site=useSite(risk);
  const [proof,setProof]=useState<{baseline?:Proof;proposed?:Proof}>({});
  useEffect(()=>{setProof({});const id=verification?.artifact_ids[0];if(!id)return;let active=true;
    api<{content:string}>(`/artifacts/${id}`,{org}).then(x=>{if(active)setProof(proofFrom(x.content))}).catch(()=>{});
    return()=>{active=false}},[verification?.id,org]);
  const expected=proof.proposed?.expected??proof.baseline?.expected;
  return <section className="panel detail-panel"><div className="panel-header"><div><span className="eyebrow">FINDING DETAIL</span><h2>{site?.file} · {risk.severity} risk</h2></div><Status value={risk.evidence_state.current?'current':'stale'}/></div>
    <p>{risk.explanation}</p><div className="notice">{risk.suggested_action}</div>
    <div className="small-muted">Use site line {site?.line_start} · scanned revision {sha?sha.slice(0,12):'loading'}</div>
    {site?.excerpt&&<pre className="code-block"><span className="code-line"><span className="line-no">{site.line_start}</span><span>{site.excerpt}</span></span></pre>}
    <div className="section-divider"><h3>Old versus proposed behavior</h3><button className="button" disabled={!risk.evidence_state.current} onClick={()=>run(()=>api(`/risks/${risk.id}/verifications`,{method:'POST',org}))}>Run verification (current vs proposed)</button>
      {verification&&<><div className="proof-grid">
        <div className={'proof '+(verification.baseline?.exit_code===0?'pass':'fail')}><div className="label">Current contract · amount in cents</div><div className="value">{proof.baseline?.actual??expected??'—'} {verification.baseline?.exit_code===0?'✓':'✗'}</div><div className="detail">{verification.baseline?.exit_code===0?'Your test passes':'Your test fails'}{expected?` · expects ${expected}`:''}</div></div>
        <div className={'proof '+(verification.status==='verified_regression'?'fail':verification.proposed?.exit_code===0?'pass':'')}><div className="label">Proposed contract · amount in dollars</div><div className="value">{proof.proposed?.actual??(verification.proposed?.exit_code===0?expected:'—')} {verification.status==='verified_regression'?'✗':verification.proposed?.exit_code===0?'✓':''}</div><div className="detail">{verification.status==='verified_regression'?`Regression proven${expected?` · expected ${expected}`:''}`:verification.status.replaceAll('_',' ')}</div></div>
      </div><p>{verification.summary}</p><Table headers={['Contract','Exit','Tests','Infrastructure']}><tr><td>Current</td><td>{verification.baseline?.exit_code??'Unknown'}</td><td>{verification.baseline?.tests_run??0}</td><td>{verification.baseline?.infrastructure_error??'None'}</td></tr><tr><td>Proposed</td><td>{verification.proposed?.exit_code??'Unknown'}</td><td>{verification.proposed?.tests_run??0}</td><td>{verification.proposed?.infrastructure_error??'None'}</td></tr></Table>{verification.artifact_ids[0]&&<button className="button subtle" onClick={()=>run(async()=>setArtifact((await api<{content:string}>(`/artifacts/${verification.artifact_ids[0]}`,{org})).content))}>View private test log</button>}</>}
    </div>
    <div className="section-divider"><h3>Migration and validation</h3><p>The patch targets the proposed contract only, so its deployment must be coordinated with the provider&apos;s change.</p><button className="button" disabled={!risk.evidence_state.verified} onClick={()=>run(()=>api(`/risks/${risk.id}/migrations`,{method:'POST',org,body:{mode:'preview'}}))}>Generate migration patch</button>
      {migration&&<><div className="result-line"><Status value={migration.status}/><span>{migration.validation?.tests_run??0} validation tests · exit {migration.validation?.exit_code??'unknown'}</span></div>{migration.patch_artifact_id&&<button className="button subtle" onClick={()=>run(async()=>setArtifact((await api<{content:string}>(`/artifacts/${migration.patch_artifact_id}`,{org})).content))}>Review patch diff</button>}
        <div className="notice">Publishing pull requests is turned off in this workspace. Review the patch here; deployment timing stays with your team.</div><label className="check"><input type="checkbox" checked={publishConsent} onChange={e=>setPublishConsent(e.target.checked)}/>I authorize a publication request for this patch.</label><button className="button primary" disabled={publication!=='live'||!publishConsent||!risk.evidence_state.validated||!sha} onClick={()=>run(()=>api(`/migrations/${migration.id}/publish`,{method:'POST',org,body:{explicit_consent:true,expected_commit:sha}}))}>Publish migration PR</button>
      </>}
    </div>
    {artifact&&<ArtifactView content={artifact}/>}
  </section>;
}
