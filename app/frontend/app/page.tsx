'use client';
import {useEffect,useRef,useState} from 'react';
import {useWorkspace} from '../lib/use-workspace';
import {Provider} from '../components/provider';
import {Consumer} from '../components/consumer';
import {JobCenter} from '../components/job-center';
import {Brand} from '../components/brand';
import {AuthScreen} from '../components/auth';
import {DemoPath} from '../components/demo-path';

const PROVIDER_TITLES:Record<string,string>={overview:'Release confidence, with evidence.',endpoints:'Endpoint inventory',consumers:'Consumer onboarding',changes:'Change assessment'};
const CONSUMER_TITLES:Record<string,string>={overview:'Your migration workspace.',invitations:'Invitations & consent',repositories:'Repository access',findings:'Private impact findings'};

function modeLabel(mode?:string){
  if(mode==='integrated')return 'Live engine';
  if(mode==='fixture')return 'Demo mode';
  return mode||'checking';
}

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
  if(!w.ready)return <main className="login-screen" aria-busy="true"><div className="login-card"><Brand size={40} tagline/><h1 style={{marginTop:40}}>Loading your workspace</h1><p>Checking your session and permissions…</p></div></main>;
  if(!w.session)return <AuthScreen w={w} busy={busy} run={run}/>;
  const provider=w.org?.kind==='provider';
  const tabs=provider?['overview','endpoints','consumers','changes']:['overview','invitations','repositories','findings'];
  const titles=provider?PROVIDER_TITLES:CONSUMER_TITLES;
  const integrated=w.capabilities?.mode==='integrated';
  return <div className="app-shell"><aside className="sidebar"><Brand size={28}/><p className="sidebar-subtitle">Know your API&apos;s downstream impact.</p>
    <div className="workspace-picker"><span className="eyebrow">Workspace</span><label className="sr-only" htmlFor="workspace">Organization</label><select id="workspace" value={w.org?.id||''} onChange={e=>w.chooseOrg(e.target.value)}>{w.session.organizations.map(o=><option key={o.id} value={o.id}>{o.name}</option>)}</select><small>{w.session.user.email}</small></div>
    <nav className="nav" aria-label="Main navigation">{tabs.map((tab,i)=><button key={tab} className={w.location.tab===tab?'active':''} aria-current={w.location.tab===tab?'page':undefined} onClick={()=>w.navigate({tab,risk:''})}><span className="nav-index" aria-hidden="true">{i+1}</span>{tab[0].toUpperCase()+tab.slice(1)}</button>)}</nav>
    <div className="sidebar-bottom"><p>Private evidence belongs to consumers.</p><span>shockwave.v1 · RFS: Self-Maintaining APIs</span></div>
  </aside><div className="main-column"><header className="topbar"><div><span className="breadcrumb">{provider?'Provider':'Consumer'} / <strong>{w.location.tab}</strong></span><span className={'mode-tag'+(integrated?'':' neutral')} title={integrated?'Discovery, scanning, analysis, verification and fixes run on the Shockwave engine.':undefined}>{modeLabel(w.capabilities?.mode)}</span></div><div className="button-row"><button className="button subtle" onClick={()=>void w.refresh()}>Refresh</button><button className="button subtle" onClick={()=>void w.signOut().catch(e=>w.setError(e instanceof Error?e.message:'Sign out failed'))}>Sign out</button></div></header>
    <main className="content" aria-busy={busy||w.loading}><div className="page-heading"><div><span className="eyebrow">{provider?'Before the rollout':'Your code, your control'}</span><h1 ref={heading} tabIndex={-1}>{titles[w.location.tab]??titles.overview}</h1><p>{provider?'Trace a proposed change to every affected consumer without seeing their source.':'Review predictions, reproduce regressions with your own tests, and control publication.'}</p></div><div className="page-mode">{(busy||w.loading)&&<span className="small-muted">Working…</span>}</div></div>
      {w.error&&<div className="notice danger" role="alert">{w.error}<button className="button subtle" onClick={()=>w.setError('')}>Dismiss</button></div>}
      {w.location.tab==='overview'&&<DemoPath role={provider?'provider':'consumer'} data={w.data}/>}
      {provider?<Provider w={w} run={run}/>:<Consumer w={w} run={run}/>}
      <JobCenter jobs={w.data.jobs} org={w.org?.id||''} onRefresh={w.refresh} onError={w.setError}/>
      <p className="footer-note">A passing test covers its tested scenario. Unscanned impact and deployment remain unknown.</p>
    </main></div></div>;
}
