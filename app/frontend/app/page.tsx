'use client';
import {useEffect,useRef,useState} from 'react';
import {useWorkspace} from '../lib/use-workspace';
import {Provider} from '../components/provider';
import {Consumer} from '../components/consumer';
import {JobCenter} from '../components/job-center';

export default function Dashboard(){
  const w=useWorkspace();const [busy,setBusy]=useState(false);
  const heading=useRef<HTMLHeadingElement>(null);
  useEffect(()=>{if(w.ready&&w.session)heading.current?.focus()},[w.location.tab,w.ready,w.session?.user.email]);
  async function action(fn:()=>Promise<unknown>){
    setBusy(true);w.setError('');
    try{await fn();await w.refresh()}catch(e){w.setError(e instanceof Error?e.message:'Action failed')}
    finally{setBusy(false)}
  }
  const run=(fn:()=>Promise<unknown>)=>{void action(fn)};
  if(!w.ready)return <main className="login-screen" aria-busy="true"><div className="login-card"><div className="brand dark">↯ shockwave</div><h1>Loading your workspace</h1><p>Checking your session and permissions…</p></div></main>;
  if(!w.session)return <main className="login-screen"><div className="login-card"><div className="eyebrow">SHOCKWAVE / API CHANGE INTELLIGENCE</div><div className="brand dark">↯ shockwave</div><h1>Know what breaks<br/>before you ship.</h1><p>Connect a proposed API change to consumer impact, test the behavior, and review the fix.</p><div className="login-actions"><span className="mode-tag">LOCAL FIXTURE DEMO · SIMULATED ACCOUNTS</span><h2>Choose a workspace</h2><p>These personas are for local development. Live authentication and integrations are not connected.</p><div className="button-row"><button className="button primary" disabled={busy} onClick={()=>run(()=>w.signIn('provider'))}>Provider workspace →</button><button className="button" disabled={busy} onClick={()=>run(()=>w.signIn('consumer'))}>Consumer workspace →</button></div></div>{w.error&&<div role="alert" className="notice danger">{w.error}</div>}</div></main>;
  const provider=w.org?.kind==='provider';
  const tabs=provider?['overview','endpoints','consumers','changes']:['overview','invitations','repositories','findings'];
  return <div className="app-shell"><aside className="sidebar"><div className="brand">↯ shockwave</div><p className="sidebar-subtitle">Know what breaks before you ship.</p>
    <div className="workspace-picker"><span className="eyebrow">WORKSPACE</span><label className="sr-only" htmlFor="workspace">Organization</label><select id="workspace" value={w.org?.id||''} onChange={e=>w.chooseOrg(e.target.value)}>{w.session.organizations.map(o=><option key={o.id} value={o.id}>{o.name} · {o.kind}</option>)}</select><small>{w.session.user.email}</small></div>
    <nav className="nav" aria-label="Main navigation">{tabs.map(tab=><button key={tab} className={w.location.tab===tab?'active':''} aria-current={w.location.tab===tab?'page':undefined} onClick={()=>w.navigate({tab,risk:''})}>{tab[0].toUpperCase()+tab.slice(1)}</button>)}</nav>
    <div className="sidebar-bottom"><p>Private evidence belongs to consumers.</p><span>shockwave.v1 · platform preview</span></div>
  </aside><div className="main-column"><header className="topbar"><div><span className="breadcrumb">{provider?'Provider':'Consumer'} / {w.location.tab}</span><span className="mode-tag">{w.capabilities?.mode||'checking'} integrations</span></div><div className="button-row"><button className="button subtle" onClick={()=>void w.refresh()}>Refresh</button><button className="button subtle" onClick={()=>void w.signOut().catch(e=>w.setError(e instanceof Error?e.message:'Sign out failed'))}>Sign out</button></div></header>
    <main className="content" aria-busy={busy||w.loading}><div className="page-heading"><div><span className="eyebrow">{provider?'BEFORE THE ROLLOUT':'YOUR CODE, YOUR CONTROL'}</span><h1 ref={heading} tabIndex={-1}>{provider?(w.location.tab==='endpoints'?'Endpoint inventory':w.location.tab==='consumers'?'Consumer onboarding':w.location.tab==='changes'?'Change assessment':'Release confidence, with evidence.'):(w.location.tab==='invitations'?'Invitations & consent':w.location.tab==='repositories'?'Repository access':w.location.tab==='findings'?'Private impact findings':'Your migration workspace.')}</h1><p>{provider?'Trace a proposed change without exposing consumer source.':'Review predictions, reproduce regressions, and control publication.'}</p></div><div className="page-mode"><span className="mode-tag">{w.capabilities?.mode||'loading'} mode</span>{w.loading&&<span className="small-muted">Refreshing…</span>}</div></div>
      {w.error&&<div className="notice danger" role="alert">{w.error}<button className="button subtle" onClick={()=>w.setError('')}>Dismiss</button></div>}
      {provider?<Provider w={w} run={run}/>:<Consumer w={w} run={run}/>}
      <JobCenter jobs={w.data.jobs} org={w.org?.id||''} onRefresh={w.refresh} onError={w.setError}/>
      <p className="footer-note">A passing test covers its tested scenario. Unscanned impact and deployment remain unknown.</p>
    </main></div></div>;
}
