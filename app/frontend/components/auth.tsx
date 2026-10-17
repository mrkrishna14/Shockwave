'use client';
import {useState} from 'react';
import {useWorkspace} from '../lib/use-workspace';
import {Brand} from './brand';

type Workspace=ReturnType<typeof useWorkspace>;
const ACCOUNT_TYPES=[
  {kind:'provider' as const,title:'Provider',body:'Load your API contract, invite the teams that depend on it, and assess a proposed change. You see impact counts only, never their code.'},
  {kind:'consumer' as const,title:'Consumer',body:'Accept invitations sent to your email, choose which repositories may be scanned, prove breakage with your tests, and review a validated fix.'},
];
const ROLES=[
  {persona:'provider' as const,title:'Provider workspace',body:'Manage an API and review shared consumer impact.'},
  {persona:'consumer' as const,title:'Consumer workspace',body:'Review affected code, test results, and migration patches.'},
];

export function AuthScreen({w,busy,run}:{w:Workspace;busy:boolean;run:(fn:()=>Promise<unknown>)=>void}){
  const [mode,setMode]=useState<'signin'|'signup'>('signin');
  const [kind,setKind]=useState<'provider'|'consumer'>('consumer');
  const [email,setEmail]=useState('');const [password,setPassword]=useState('');const [orgName,setOrgName]=useState('');
  const demo=Boolean(w.health?.demo_sessions);
  function submit(e:React.FormEvent){
    e.preventDefault();
    run(()=>mode==='signin'?w.logIn(email,password):w.signUp({email,password,account_type:kind,organization_name:orgName}));
  }
  return <div className="auth">
    <header className="topbar"><div className="topbar-inner"><Brand/></div></header>
    <main className="auth-main">
      <h1 className="page-title">Open a workspace</h1>
      <p className="subtitle">Review API changes from either side of the contract.</p>
      {w.error&&<div role="alert" className="notice danger" style={{textAlign:'left',marginTop:28}}>{w.error}</div>}
      {demo&&<>
        <ul className="roles" aria-label="Demo workspaces">{ROLES.map(r=><li key={r.persona}>
          <div><strong>{r.title}</strong><p>{r.body}</p></div>
          <button className="link" disabled={busy} aria-label={`Open the demo ${r.persona} workspace`} onClick={()=>run(()=>w.demoSignIn(r.persona))}>Open</button>
        </li>)}</ul>
        <p className="auth-note">Source code stays private to its owning organization.</p>
      </>}
      <div className="account">
        <div className="account-tabs" role="tablist" aria-label="Account">
          <button role="tab" aria-selected={mode==='signin'} className={mode==='signin'?'active':''} onClick={()=>{setMode('signin');w.setError('')}}>Sign in</button>
          <button role="tab" aria-selected={mode==='signup'} className={mode==='signup'?'active':''} onClick={()=>{setMode('signup');w.setError('')}}>Create account</button>
        </div>
        <form onSubmit={submit}>
          {mode==='signup'&&<>
            <div className="kinds" role="radiogroup" aria-label="Account type">
              {ACCOUNT_TYPES.map(t=><button type="button" key={t.kind} role="radio" aria-checked={kind===t.kind} className="kind" onClick={()=>setKind(t.kind)}>
                <span className="radio" aria-hidden="true"/><span><strong>{t.title}</strong><span>{t.body}</span></span>
              </button>)}
            </div>
            <label className="field">{kind==='provider'?'API team or company name':'Team or company name'}<input value={orgName} onChange={e=>setOrgName(e.target.value)} required maxLength={120} autoComplete="organization"/></label>
          </>}
          <label className="field">Email<input type="email" value={email} onChange={e=>setEmail(e.target.value)} required autoComplete="email" placeholder="name@company.com"/></label>
          <label className="field">Password<input type="password" value={password} onChange={e=>setPassword(e.target.value)} required minLength={mode==='signup'?8:1} autoComplete={mode==='signup'?'new-password':'current-password'}/></label>
          {mode==='signup'&&kind==='consumer'&&<p className="small-muted">Use the email address your provider invited. Invitations appear in your workspace once you sign in.</p>}
          <button className="button primary" disabled={busy}>{mode==='signin'?'Sign in':`Create ${kind} account`}</button>
        </form>
      </div>
    </main>
    <footer className="auth-foot">{demo&&<span>Demo sign-in · choose a role to explore</span>}<span>RFS: Self-Maintaining APIs</span></footer>
  </div>;
}
