import './globals.css';
import type { Metadata } from 'next';
export const metadata: Metadata = {title:'Shockwave — Know what breaks before you ship', description:'Permissioned API change impact and behavioral verification'};
export default function RootLayout({children}:{children:React.ReactNode}) {return <html lang="en"><body>{children}</body></html>}
