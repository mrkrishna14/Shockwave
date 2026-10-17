import './globals.css';
import type { Metadata } from 'next';
export const metadata: Metadata = {title:'Shockwave — Know your API\'s downstream impact', description:'See what changes in your customers\' code before you ship.'};
// Plex Sans/Mono and Source Serif load from Google Fonts; offline, the system stacks in globals.css take over.
const FONTS='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500&display=swap';
export default function RootLayout({children}:{children:React.ReactNode}) {return <html lang="en"><head><link rel="preconnect" href="https://fonts.googleapis.com"/><link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin=""/><link rel="stylesheet" href={FONTS}/></head><body>{children}</body></html>}
