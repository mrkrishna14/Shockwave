import './globals.css';
import type { Metadata } from 'next';
export const metadata: Metadata = {title:'Shockwave — Know your API\'s downstream impact', description:'See what changes in your customers\' code before you ship.'};
export default function RootLayout({children}:{children:React.ReactNode}) {return <html lang="en"><body>{children}</body></html>}
