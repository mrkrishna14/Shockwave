'use client';
import {useState} from 'react';
import {useWorkspace} from '../lib/use-workspace';
import {Logo} from './brand';

type Workspace=ReturnType<typeof useWorkspace>;
const ACCOUNT_TYPES=[
  {kind:'provider' as const,eyebrow:'I publish an API',title:'Provider',body:'Load your API contract, invite the teams that depend on it, and assess a proposed change. You see impact counts only, never their code.'},
  {kind:'consumer' as const,eyebrow:'I depend on an API',title:'Consumer',body:'Accept invitations sent to your email, choose which repositories may be scanned, prove breakage with your tests, and review a validated fix.'},
];

export function AuthScreen({w,busy,run}:{w:Workspace;busy:boolean;run:(fn:()=>Promise<unknown>)=>void}){
  const [mode,setMode]=useState<'signin'|'signup'>('signin');
  const [kind,setKind]=useState<'provider'|'consumer'>('consumer');
  const [email,setEmail]=useState('');const [password,setPassword]=useState('');const [orgName,setOrgName]=useState('');
  function submit(e:React.FormEvent){
    e.preventDefault();
    run(()=>mode==='signin'?w.logIn(email,password):w.signUp({email,password,account_type:kind,organization_name:orgName}));
  }
  return <main className="login-screen">
    <div className="login-card">
      <div className="hero-brand"><Logo size={124} title="Shockwave logo"/><span className="wordmark">shockwave</span></div>
      <p className="hero-tagline">Know your API&apos;s downstream impact.</p>
      <p className="hero-sub">See what changes in your customers&apos; code before you ship.</p>
      <div className="auth-panel">
        <div className="auth-tabs" role="tablist" aria-label="Account">
          <button role="tab" aria-selected={mode==='signin'} className={mode==='signin'?'active':''} onClick={()=>{setMode('signin');w.setError('')}}>Sign in</button>
          <button role="tab" aria-selected={mode==='signup'} className={mode==='signup'?'active':''} onClick={()=>{setMode('signup');w.setError('')}}>Create account</button>
        </div>
        <form onSubmit={submit} className="auth-form">
          {mode==='signup'&&<>
            <div className="persona-grid compact" role="radiogroup" aria-label="Account type">
              {ACCOUNT_TYPES.map(t=><button type="button" key={t.kind} role="radio" aria-checked={kind===t.kind} className={'persona'+(kind===t.kind?' selected':'')} onClick={()=>setKind(t.kind)}>
                <span className="eyebrow">{t.eyebrow}</span><h2>{t.title}</h2><p>{t.body}</p>
              </button>)}
            </div>
            <label className="field">{kind==='provider'?'API team or company name':'Team or company name'}<input value={orgName} onChange={e=>setOrgName(e.target.value)} required maxLength={120} autoComplete="organization"/></label>
          </>}
          <label className="field">Email<input type="email" value={email} onChange={e=>setEmail(e.target.value)} required autoComplete="email"/></label>
          <label className="field">Password<input type="password" value={password} onChange={e=>setPassword(e.target.value)} required minLength={mode==='signup'?8:1} autoComplete={mode==='signup'?'new-password':'current-password'}/></label>
          {mode==='signup'&&kind==='consumer'&&<p className="small-muted">Use the email address your provider invited. Invitations appear in your workspace once you sign in.</p>}
          <button className="button primary" disabled={busy}>{mode==='signin'?'Sign in':`Create ${kind} account`}</button>
        </form>
        {w.error&&<div role="alert" className="notice danger">{w.error}</div>}
        {w.health?.demo_sessions&&<div className="demo-shortcuts"><span className="small-muted">Local demo accounts:</span>
          <button className="button subtle" disabled={busy} onClick={()=>run(()=>w.demoSignIn('provider'))}>Demo provider</button>
          <button className="button subtle" disabled={busy} onClick={()=>run(()=>w.demoSignIn('consumer'))}>Demo consumer</button>
        </div>}
      </div>
    </div>
    <footer className="login-footer"><span className="mode">{w.health?.demo_sessions?'Demo workspace':'Shockwave'}</span><span className="rfs">RFS: Self-Maintaining APIs</span></footer>
  </main>;
}
