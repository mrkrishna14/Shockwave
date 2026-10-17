'use client';
import {useCallback,useEffect,useRef,useState} from 'react';
import {all,api,ApiError,Capabilities,emptyData,PlatformData,Session,JobView,APIProject,Endpoint,Invitation,ConsumerRow,APIChange,ProviderImpactSummary,Enrollment,RepositoryConnection,SelectedRepo,RepoOption,DetailedRisk,Mail,IncomingInvitation,Health} from './api';

export type SignUp={email:string;password:string;account_type:'provider'|'consumer';organization_name:string};

type LocationState={tab:string;org:string;project:string;enrollment:string;risk:string};
const locationState=():LocationState=>{
  if(typeof window==='undefined')return {tab:'overview',org:'',project:'',enrollment:'',risk:''};
  const q=new URLSearchParams(window.location.search);
  return {tab:q.get('tab')||'overview',org:q.get('org')||'',project:q.get('project')||'',enrollment:q.get('enrollment')||'',risk:q.get('risk')||''};
};

export function useWorkspace(){
  const [session,setSession]=useState<Session|null>(null);
  const [ready,setReady]=useState(false);
  const [health,setHealth]=useState<Health|null>(null);
  const [capabilities,setCapabilities]=useState<Capabilities|null>(null);
  const [location,setLocation]=useState<LocationState>(locationState);
  const [data,setData]=useState<PlatformData>(emptyData);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState('');
  const serial=useRef(0);
  const abort=useRef<AbortController|null>(null);
  const org=session?.organizations.find(o=>o.id===location.org)??session?.organizations[0];
  const project=location.project||data.projects[0]?.id||'';
  const enrollment=location.enrollment||data.enrollments.find(e=>e.status==='active')?.id||'';

  const navigate=useCallback((changes:Partial<LocationState>,replace=false)=>{
    const next={...locationState(),...changes};
    const q=new URLSearchParams(window.location.search);
    for(const [key,value] of Object.entries(next))value?q.set(key,value):q.delete(key);
    window.history[replace?'replaceState':'pushState']({},'',window.location.pathname+(q.size?'?'+q:''));
    setLocation(next);
  },[]);

  useEffect(()=>{
    const onPop=()=>setLocation(locationState());
    window.addEventListener('popstate',onPop);
    api<Session>('/session').then(setSession).catch(()=>setSession(null)).finally(()=>setReady(true));
    api<Health>('/health').then(setHealth).catch(()=>setHealth(null));
    return()=>window.removeEventListener('popstate',onPop);
  },[]);

  const refresh=useCallback(async()=>{
    if(!session||!org)return;
    abort.current?.abort();const controller=new AbortController();abort.current=controller;
    const current=++serial.current;setLoading(true);
    try{
      const signal=controller.signal;
      const jobs=await all<JobView>('/jobs',org.id,signal);
      let next=emptyData();next.jobs=jobs;
      if(org.kind==='provider'){
        next.projects=await all<APIProject>('/api-projects',org.id,signal);
        const id=next.projects.some(p=>p.id===project)?project:next.projects[0]?.id;
        if(id){
          const [endpoints,invitations,consumers,changes]=await Promise.all([
            all<Endpoint>(`/api-projects/${id}/endpoints`,org.id,signal),
            all<Invitation>(`/api-projects/${id}/invitations`,org.id,signal),
            all<ConsumerRow>(`/api-projects/${id}/consumers`,org.id,signal),
            all<APIChange>(`/api-projects/${id}/changes`,org.id,signal)]);
          next={...next,endpoints,invitations,consumers,changes};
          if(changes.length)next.impact=await all<ProviderImpactSummary>(`/api-projects/${id}/impact?change_id=${changes.at(-1)!.id}`,org.id,signal);
        }
      }else{
        const [enrollments,repos,risks,mail,repoOptions,incoming]=await Promise.all([
          all<Enrollment>('/enrollments',org.id,signal),
          all<RepositoryConnection>('/repository-connections',org.id,signal),
          all<DetailedRisk>('/consumer/risks',org.id,signal),
          capabilities?.email==='development_inbox'?all<Mail>('/demo/inbox',org.id,signal):Promise.resolve([]),
          all<RepoOption>('/github/repositories',org.id,signal),
          all<IncomingInvitation>('/invitations/incoming',org.id,signal)]);
        next={...next,enrollments,repos,risks,mail,repoOptions,incoming};
        const id=enrollments.some(e=>e.id===enrollment)?enrollment:enrollments.find(e=>e.status==='active')?.id;
        if(id)next.selectedRepos=await all<SelectedRepo>(`/enrollments/${id}/repositories`,org.id,signal);
      }
      if(current===serial.current){setData(next);setError('')}
    }catch(e){if(current===serial.current&&!(e instanceof DOMException&&e.name==='AbortError'))setError(e instanceof Error?e.message:'Unable to load workspace')}
    finally{if(current===serial.current)setLoading(false)}
  },[session,org?.id,org?.kind,project,enrollment,capabilities?.email]);

  // One browser shares one session cookie: signing in elsewhere in another tab changes who this tab is.
  // Follow that change when the tab comes back instead of failing with membership errors.
  useEffect(()=>{
    if(!session)return;
    const check=()=>{
      if(document.visibilityState!=='visible')return;
      api<Session>('/session').then(current=>{
        if(current.user.id===session.user.id)return;
        abort.current?.abort();serial.current++;setData(emptyData());setSession(current);
        navigate({org:current.organizations[0]?.id||'',project:'',enrollment:'',risk:'',tab:'overview'},true);
      }).catch(e=>{if(e instanceof ApiError&&e.status===401){abort.current?.abort();serial.current++;setSession(null);setData(emptyData())}});
    };
    window.addEventListener('focus',check);document.addEventListener('visibilitychange',check);
    return()=>{window.removeEventListener('focus',check);document.removeEventListener('visibilitychange',check)};
  },[session?.user.id,navigate]);
  useEffect(()=>{if(session){api<Capabilities>('/capabilities').then(setCapabilities).catch(()=>setCapabilities(null))}},[session]);
  useEffect(()=>{void refresh();return()=>abort.current?.abort()},[refresh]);
  const active=data.jobs.some(j=>j.status==='queued'||j.status==='running');
  useEffect(()=>{if(!active)return;const timer=window.setInterval(()=>{if(!loading)void refresh()},2500);return()=>window.clearInterval(timer)},[active,loading,refresh]);

  async function enter(start:()=>Promise<unknown>){
    abort.current?.abort();serial.current++;setData(emptyData());setError('');
    await start();
    const signed=await api<Session>('/session');setSession(signed);navigate({org:signed.organizations[0]?.id||'',project:'',enrollment:'',risk:'',tab:'overview'},true);
  }
  const demoSignIn=(persona:'provider'|'consumer')=>enter(()=>api('/demo/session',{method:'POST',body:{persona}}));
  const logIn=(email:string,password:string)=>enter(()=>api('/auth/login',{method:'POST',body:{email,password}}));
  const signUp=(body:SignUp)=>enter(()=>api('/auth/signup',{method:'POST',body}));
  async function signOut(){
    abort.current?.abort();serial.current++;await api('/session',{method:'DELETE'});
    setSession(null);setData(emptyData());setCapabilities(null);navigate({org:'',project:'',enrollment:'',risk:'',tab:'overview'},true);
  }
  function chooseOrg(id:string){abort.current?.abort();serial.current++;setData(emptyData());navigate({org:id,project:'',enrollment:'',risk:'',tab:'overview'})}
  return {session,ready,health,capabilities,org,project,enrollment,location,data,loading,error,setError,navigate,refresh,demoSignIn,logIn,signUp,signOut,chooseOrg};
}
