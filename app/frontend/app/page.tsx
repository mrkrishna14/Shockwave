'use client';
import {useEffect,useState} from 'react';
import {useWorkspace} from '../lib/use-workspace';
import {Provider} from '../components/provider';
import {Consumer} from '../components/consumer';
import {JobCenter} from '../components/job-center';
import {Brand} from '../components/brand';
import {AuthScreen} from '../components/auth';

function modeLabel(mode?:string){
  if(mode==='integrated')return 'Live engine';
  if(mode==='fixture')return 'Demo mode';
  return mode||'Checking engine';
}

export default function Dashboard(){
  const w=useWorkspace();const [busy,setBusy]=useState(false);
  useEffect(()=>{if(w.ready&&w.session)document.querySelector<HTMLElement>('main h1')?.focus()},[w.location.tab,w.ready,w.session?.user.email]);
  async function action(fn:()=>Promise<unknown>){
    setBusy(true);w.setError('');
    try{await fn();await w.refresh()}catch(e){w.setError(e instanceof Error?e.message:'Action failed')}
    finally{setBusy(false)}
  }
  const run=(fn:()=>Promise<unknown>)=>{void action(fn)};
  if(!w.ready)return <div className="auth"><header className="topbar"><div className="topbar-inner"><Brand/></div></header>
    <main className="auth-main" aria-busy="true"><h1 className="page-title">Loading your workspace</h1><p className="subtitle">Checking your session and permissions…</p></main></div>;
  if(!w.session)return <AuthScreen w={w} busy={busy} run={run}/>;
  const provider=w.org?.kind==='provider';
  const tabs=provider?['overview','endpoints','consumers','changes']:['overview','invitations','repositories','findings'];
  return <div>
    <header className="topbar"><div className="topbar-inner">
      <Brand/>
      <nav className="tabs" aria-label="Main navigation">{tabs.map(tab=><button key={tab} className={w.location.tab===tab?'active':''} aria-current={w.location.tab===tab?'page':undefined} onClick={()=>w.navigate({tab,risk:''})}>{tab[0].toUpperCase()+tab.slice(1)}</button>)}</nav>
      <div className="topbar-right">
        {(busy||w.loading)&&<span className="working" role="status">Working…</span>}
        <button className="link quiet refresh" onClick={()=>void w.refresh()}>Refresh</button>
        <span className="divider" aria-hidden="true"/>
        <label className="org-select" title={w.session.user.email}><span className="sr-only">Organization</span><select value={w.org?.id||''} onChange={e=>w.chooseOrg(e.target.value)}>{w.session.organizations.map(o=><option key={o.id} value={o.id}>{o.name}</option>)}</select></label>
        <span className="divider" aria-hidden="true"/>
        <button className="link quiet" onClick={()=>void w.signOut().catch(e=>w.setError(e instanceof Error?e.message:'Sign out failed'))}>Sign out</button>
      </div>
    </div></header>
    <main className="content" aria-busy={busy||w.loading}>
      {w.error&&<div className="notice danger" role="alert"><span>{w.error}</span><button className="link" onClick={()=>w.setError('')}>Dismiss</button></div>}
      {provider?<Provider w={w} run={run}/>:<Consumer w={w} run={run}/>}
      <JobCenter jobs={w.data.jobs} org={w.org?.id||''} onRefresh={w.refresh} onError={w.setError}/>
      <footer className="page-foot"><span>A passing test covers its tested scenario only. Unscanned impact and deployment remain unknown.</span>
        <span title={w.capabilities?.mode==='integrated'?'Discovery, scanning, analysis, verification and fixes run on the Shockwave engine.':undefined}>{modeLabel(w.capabilities?.mode)} · {provider?'Counts cover opted-in consumers only':`Private to ${w.org?.name??'your organization'}`}</span></footer>
    </main>
  </div>;
}
