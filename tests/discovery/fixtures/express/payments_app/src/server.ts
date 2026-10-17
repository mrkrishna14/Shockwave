// TypeScript variant of the fictional payments server (tokenized only; never compiled or run).
import express, { Router, type Request, type Response } from 'express';
import type { Express } from 'express';
import { webhooksRouter } from './webhooks';

const app: Express = express();
const charges: Router = Router({ mergeParams: true });

interface ChargeParams {
  id: string;
}

charges.get('/:id', (req: Request<ChargeParams>, res: Response) => {
  res.json({ id: req.params.id, amount: 1000, currency: 'usd' });
});
charges.route('/:id/refunds').post(createRefund).all(methodNotAllowed);

app.use('/v2/charges', charges);
app.use('/v2/webhooks', webhooksRouter);
app.get<{}, { status: string }>('/v2/healthz', (_req, res) => {
  res.json({ status: 'ok' });
});

const note = `app.get('/v2/fake-in-template') is text, not a call`;
/* app.delete('/v2/commented-out', handler); */

export function registerAdminRoutes(adminRouter: Router): void {
  adminRouter.get('/v2/admin/audit', (_req: Request, res: Response) => {
    res.json({ note });
  });
}

function createRefund(req: Request<ChargeParams>, res: Response): void {
  res.status(201).json({ charge: req.params.id });
}

function methodNotAllowed(_req: Request, res: Response): void {
  res.sendStatus(405);
}

export default app;
