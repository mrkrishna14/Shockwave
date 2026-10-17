import {JobView} from '../lib/api';
import {api} from '../lib/api';
import {Empty,Status} from './status';

export function JobCenter({jobs,org,onRefresh,onError}:{jobs:JobView[];org:string;onRefresh:()=>Promise<void>;onError:(value:string)=>void}){
  async function act(path:string,method:string){try{await api(path,{method,org});await onRefresh()}catch(e){onError(e instanceof Error?e.message:'Job action failed')}}
  return <section className="panel job-center" aria-label="Job activity"><div className="panel-header"><div><span className="eyebrow">OPERATIONS</span><h2>Job activity</h2></div><span className="small-muted">Execution success does not prove compatibility.</span></div>
    {jobs.length===0?<Empty title="No jobs yet">Actions will appear here with their progress and results.</Empty>:
      <div className="job-list">{[...jobs].reverse().slice(0,8).map(job=><div className="job-row" key={job.id}>
        <div><strong>{job.kind}</strong><div className="small-muted">{job.stage.replaceAll('_',' ')} · {new Date(job.updated_at).toLocaleString()} · attempt {job.attempts}</div>{job.error&&<p className="job-error">{job.error.message}</p>}</div>
        <div className="job-actions"><Status value={job.status}/>{job.status==='failed'&&job.error?.retryable&&<button className="button subtle" onClick={()=>void act(`/jobs/${job.id}/retry`,'POST')}>Retry</button>}{['queued','running'].includes(job.status)&&<button className="button subtle" onClick={()=>void act(`/jobs/${job.id}`,'DELETE')}>Cancel</button>}</div>
      </div>)}</div>}
  </section>;
}
