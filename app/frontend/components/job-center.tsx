import {JobView} from '../lib/api';
import {api} from '../lib/api';
import {Empty,Section,Status,Table,label,when} from './status';

const EVENTS:Record<string,string>={discovery:'Contract discovery',scan:'Repository scan',analysis:'Change assessment',verification:'Verification',migration:'Migration patch'};

export function JobCenter({jobs,org,onRefresh,onError}:{jobs:JobView[];org:string;onRefresh:()=>Promise<void>;onError:(value:string)=>void}){
  async function act(path:string,method:string){try{await api(path,{method,org});await onRefresh()}catch(e){onError(e instanceof Error?e.message:'Job action failed')}}
  return <Section title="Recent activity" sub="Execution success does not prove compatibility." label="Job activity">
    {jobs.length===0?<Empty title="No activity yet">Actions appear here with their progress and results.</Empty>:
      <Table headers={['Date','Event','Status','Details']}>{[...jobs].reverse().slice(0,8).map(job=><tr key={job.id}>
        <td className="date">{when(job.updated_at)}</td>
        <td>{EVENTS[job.kind]??label(job.kind)}<div className="small-muted">{label(job.stage)}</div></td>
        <td><Status value={job.status}/></td>
        <td><div className="button-row"><span className="small-muted">Attempt {job.attempts}</span>{job.status==='failed'&&job.error?.retryable&&<button className="link" onClick={()=>void act(`/jobs/${job.id}/retry`,'POST')}>Retry</button>}{['queued','running'].includes(job.status)&&<button className="link" onClick={()=>void act(`/jobs/${job.id}`,'DELETE')}>Cancel</button>}</div>{job.error&&<p className="job-error">{job.error.message}</p>}</td>
      </tr>)}</Table>}
  </Section>;
}
