export function Status({value}:{value:string}){
  const tone=/failed|revoked|regression|denied|expired|inconclusive/.test(value)?'danger':
    /validated|accepted|connected|succeeded|scanned/.test(value)?'success':
    /pending|partial|unknown|predicted|queued|running/.test(value)?'warning':'neutral';
  return <span className={'status status-'+tone}><span aria-hidden="true" className="status-dot"/>{value.replaceAll('_',' ')}</span>;
}
export function Empty({title,children}:{title:string;children?:React.ReactNode}){
  return <div className="empty"><strong>{title}</strong>{children&&<p>{children}</p>}</div>;
}
export function Table({headers,children}:{headers:string[];children:React.ReactNode}){
  return <div className="table-scroll"><table><thead><tr>{headers.map(h=><th scope="col" key={h}>{h}</th>)}</tr></thead><tbody>{children}</tbody></table></div>;
}
