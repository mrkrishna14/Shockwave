import type {APIProject, Endpoint, Invitation, Enrollment, RepositoryConnection, Risk, VerificationResult, MigrationResult, Readiness, Job, ProviderImpactSummary, APIChange} from '../contracts';
export type {APIProject, Endpoint, Invitation, Enrollment, RepositoryConnection, Risk, VerificationResult, MigrationResult, Readiness, Job, ProviderImpactSummary, APIChange} from '../contracts';

export type Organization={id:string;name:string;kind:'provider'|'consumer';role:'admin'|'member'};
export type Session={user:{email:string};organizations:Organization[];mode:string;consent_version:string};
export type Capabilities={mode:string;discovery:string;repository_access:string;scanner:string;verification:string;migration:string;email:string;publication:string;reasons:Record<string,string>};
export type JobView=Job&{attempts:number;cancel_requested:boolean};
export type DetailedRisk=Risk&{verifications:VerificationResult[];migrations:MigrationResult[];readiness:Readiness[];evidence_state:{current:boolean;verified:boolean;validated:boolean}};
export type RepoOption={github_repository_id:string;full_name:string;default_branch:string;mode:string};
export type SelectedRepo=RepositoryConnection&{enrollment_permissions:{enrollment_id:string;allow_model_processing:boolean;allow_pr_write:boolean;scan_consent_at:string}};
export type Mail={id:string;acceptance_url:string;recipient_email:string};
export type ConsumerRow={invitation_id:string;recipient_email:string;invitation_status:string;delivery_status:string;enrollment_status:string;share_summary:boolean;delivery_attention:boolean};
export type PlatformData={projects:APIProject[];endpoints:Endpoint[];invitations:Invitation[];consumers:ConsumerRow[];changes:APIChange[];impact:ProviderImpactSummary[];enrollments:Enrollment[];repos:RepositoryConnection[];selectedRepos:SelectedRepo[];repoOptions:RepoOption[];risks:DetailedRisk[];mail:Mail[];jobs:JobView[]};
type Page<T>={items:T[];next_cursor:string|null};
export const emptyData=():PlatformData=>({projects:[],endpoints:[],invitations:[],consumers:[],changes:[],impact:[],enrollments:[],repos:[],selectedRepos:[],repoOptions:[],risks:[],mail:[],jobs:[]});

const pendingKeys=new Map<string,string>();
export class ApiError extends Error{constructor(message:string,public status:number,public code:string){super(message)}}
export async function api<T>(path:string,{method='GET',body,org,signal}:{method?:string;body?:unknown;org?:string;signal?:AbortSignal}={}):Promise<T>{
  const headers:Record<string,string>={'Content-Type':'application/json'};
  if(org)headers['X-Organization-Id']=org;
  let key:string|undefined;
  if(method==='POST'){
    const identity=org+':'+path+':'+JSON.stringify(body??{});
    key=pendingKeys.get(identity)??crypto.randomUUID();pendingKeys.set(identity,key);
    headers['Idempotency-Key']=key;
  }
  const response=await fetch('/api/v1'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),signal,cache:'no-store'});
  const data=await response.json();
  if(!response.ok){
    if(method==='POST'&&response.status<500)pendingKeys.delete(org+':'+path+':'+JSON.stringify(body??{}));
    throw new ApiError(data.error?.message??`Request failed (${response.status})`,response.status,data.error?.code??'REQUEST_FAILED');
  }
  if(method==='POST')pendingKeys.delete(org+':'+path+':'+JSON.stringify(body??{}));
  return data as T;
}
export async function all<T>(path:string,org?:string,signal?:AbortSignal):Promise<T[]>{
  const found:T[]=[];let cursor:string|null=null;
  do{
    const separator=path.includes('?')?'&':'?';
    const result:Page<T>=await api<Page<T>>(path+separator+'limit=100'+(cursor?'&cursor='+encodeURIComponent(cursor):''),{org,signal});
    found.push(...result.items);cursor=result.next_cursor;
  }while(cursor&&found.length<2000);
  return found;
}
