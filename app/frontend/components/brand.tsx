// Shockwave brand: three stacked waves (logo) + lowercase wordmark.
export function Logo({size=30,title}:{size?:number;title?:string}){
  return <svg className="logo" width={size} height={Math.round(size*.75)} viewBox="0 0 32 24" role={title?'img':undefined} aria-hidden={title?undefined:true} aria-label={title}>
    <g fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
      <path d="M2 5Q5.5 1.6 9 5T16 5T23 5T30 5"/>
      <path d="M2 12Q5.5 8.6 9 12T16 12T23 12T30 12"/>
      <path d="M2 19Q5.5 15.6 9 19T16 19T23 19T30 19"/>
    </g>
  </svg>;
}

export function Brand({size=30}:{size?:number}){
  return <div className="brand"><Logo size={size}/><span className="wordmark" style={{fontSize:Math.round(size*.72)}}>shockwave</span></div>;
}
