import type {PlatformData} from '../lib/api';

type Step={title:string;hint:string;done:boolean};

function providerSteps(d:PlatformData):Step[]{
  return [
    {title:'Create the API project',hint:'Overview → Create API project',done:d.projects.length>0},
    {title:'Load its contract',hint:'Endpoints → Load contract',done:d.endpoints.length>0},
    {title:'Invite a consumer',hint:'Consumers → Invite their email',done:d.consumers.length>0},
    {title:'Consumer joins and scans',hint:'Consumer signs in and accepts',done:d.consumers.some(c=>c.enrollment_status==='active')},
    {title:'Assess the change',hint:'Changes → Assess change',done:d.changes.length>0},
    {title:'See verified impact',hint:'Consumer runs verification, then refresh',done:d.impact.some(i=>i.verified_regression_count>0)},
  ];
}

function consumerSteps(d:PlatformData):Step[]{
  return [
    {title:'Accept the invitation',hint:'Invitations → Review → Accept',done:d.enrollments.some(e=>e.status==='active')},
    {title:'Connect GitHub',hint:'Repositories → Connect repositories',done:d.repoOptions.length>0||d.selectedRepos.length>0},
    {title:'Select a repository',hint:'Tick it, consent, select',done:d.selectedRepos.some(r=>r.status==='connected')},
    {title:'Scan it',hint:'Repositories → Scan / retry',done:d.jobs.some(j=>j.kind==='scan'&&j.status==='succeeded')},
    {title:'Review the finding',hint:'After the provider assesses → Findings',done:d.risks.length>0},
    {title:'Prove the regression',hint:'Inspect → Run verification',done:d.risks.some(r=>r.evidence_state.verified)},
    {title:'Validate a fix',hint:'Generate migration patch',done:d.risks.some(r=>r.evidence_state.validated)},
  ];
}

export function DemoPath({role,data}:{role:'provider'|'consumer';data:PlatformData}){
  const steps=role==='provider'?providerSteps(data):consumerSteps(data);
  const next=steps.findIndex(s=>!s.done);
  return <section className="section" aria-label="Demo path">
    <div className="section-head"><div><h2>{next===-1?'All steps complete':`Next: ${steps[next].title}`}</h2><p className="section-sub">Demo path · {steps.filter(s=>s.done).length} of {steps.length} done</p></div></div>
    <ol className="steps">{steps.map((s,i)=><li key={s.title} className={'step'+(s.done?' done':i===next?' next':'')}>
      <span className="n">Step {i+1}</span><strong>{s.title}</strong><span>{s.done?'Done':s.hint}</span>
    </li>)}</ol>
  </section>;
}
