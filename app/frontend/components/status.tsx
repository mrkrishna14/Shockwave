const LABELS:Record<string,string>={documented_and_found:'Documented and found',documented_not_found_in_scan:'Not found in scan',implementation_only:'Implementation only',not_compared:'Not compared',
  compatible_within_tested_scope:'Compatible within tested scope',partial:'Partial coverage',partially_reported:'Partially reported',all_reported:'All reported',not_accepted:'Not accepted',not_run:'Not run'};
// Tones follow the interface kit: predicted blue, verified regression red, validated green, unknown dark gray, unscanned light gray, caution amber.
function tone(value:string){
  if(/unscanned|not_accepted|cancelled|stale/.test(value))return 'unscanned';
  if(/regression|failed|revoked|denied|expired|error|conflict|blocked/.test(value))return 'regression';
  if(/predicted|patch_proposed/.test(value))return 'predicted';
  if(/validated|accepted|active|connected|succeeded|scanned|documented_and_found|all_reported|compatible|pr_opened|current|sent|delivered|assessed/.test(value))return 'validated';
  if(/pending|partial|queued|running|proposed|implementation_only/.test(value))return 'caution';
  return 'unknown';
}
export const label=(value:string)=>LABELS[value]??(value.charAt(0).toUpperCase()+value.slice(1)).replaceAll('_',' ');
export function Status({value,text}:{value:string;text?:string}){
  return <span className={'status tone-'+tone(value)}><span aria-hidden="true" className="status-dot"/>{text??label(value)}</span>;
}
export function Empty({title,children}:{title:string;children?:React.ReactNode}){
  return <div className="empty"><strong>{title}</strong>{children&&<p>{children}</p>}</div>;
}
export function Table({headers,children}:{headers:string[];children:React.ReactNode}){
  return <div className="table-scroll"><table><thead><tr>{headers.map(h=><th scope="col" key={h}>{h}</th>)}</tr></thead><tbody>{children}</tbody></table></div>;
}
export function PageHead({title,subtitle,meta,aside}:{title:string;subtitle?:React.ReactNode;meta?:string[];aside?:React.ReactNode}){
  return <header className="page-head">
    <div className="title-row"><h1 className="page-title" tabIndex={-1}>{title}</h1>{aside}</div>
    {subtitle&&<p className="subtitle">{subtitle}</p>}
    {meta&&meta.length>0&&<div className="meta">{meta.map(m=><span key={m}>{m}</span>)}</div>}
  </header>;
}
export function Section({title,sub,action,children,label:aria}:{title:string;sub?:React.ReactNode;action?:React.ReactNode;children?:React.ReactNode;label?:string}){
  return <section className="section" aria-label={aria}>
    <div className="section-head"><div><h2>{title}</h2>{sub&&<p className="section-sub">{sub}</p>}</div>{action}</div>
    {children}
  </section>;
}
export const plural=(n:number,one:string,many=one+'s')=>`${n} ${n===1?one:many}`;
export const when=(iso:string)=>{const d=new Date(iso);return `${d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}  ${d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',hour12:false})}`};
