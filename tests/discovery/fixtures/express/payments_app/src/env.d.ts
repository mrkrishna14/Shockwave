// Type declarations only: declaration files are not scanned (nothing here runs).
// app.get('/v2/declared-fake', handler);
declare namespace NodeJS {
  interface ProcessEnv {
    EXPORT_KIND?: string;
    REPORT_KIND?: string;
  }
}
