// Shockwave brand: three offset waves (logo) + lowercase wordmark.
export function Logo({size=32,title}:{size?:number;title?:string}){
  return <svg className="logo" width={size} height={size} viewBox="0 0 96 96" role={title?'img':undefined} aria-hidden={title?undefined:true} aria-label={title}>
    <g fill="none" stroke="currentColor" strokeWidth="11" strokeLinecap="butt">
      <path d="M4 40h11c19 0 23-24 44-24h15"/>
      <path d="M10 62h12c19 0 23-24 44-24h18"/>
      <path d="M26 84h8c19 0 23-24 44-24h12"/>
    </g>
  </svg>;
}

export function Brand({size=30,tagline=false}:{size?:number;tagline?:boolean}){
  return <div className="brand-lockup">
    <Logo size={size}/>
    <div><div className="wordmark" style={{fontSize:Math.round(size*0.86)}}>shockwave</div>{tagline&&<div className="brand-tagline">Know your API&apos;s downstream impact.</div>}</div>
  </div>;
}
