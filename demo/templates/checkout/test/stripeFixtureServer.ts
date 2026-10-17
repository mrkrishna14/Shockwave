// Local stand-in for Stripe's GET /v1/payment_intents/{intent}, used by the tests.
// It serves canned PaymentIntent responses from a fixture file; it is a simulation of
// Stripe responses for offline tests, not Stripe and not another payment provider.
import { readFileSync } from 'node:fs';
import { createServer, type IncomingMessage, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';

export interface RecordedRequest {
  method: string;
  path: string;
  stripeVersion: string | undefined;
  authorization: string | undefined;
}

export interface FixtureServer {
  baseUrl: string;
  requests: RecordedRequest[];
  close(): Promise<void>;
}

const RETRIEVE_PATH = /^\/v1\/payment_intents\/([A-Za-z0-9_]+)$/;

export async function startFixtureServer(fixturePath: string): Promise<FixtureServer> {
  const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as {
    responses: Record<string, unknown>;
  };
  const requests: RecordedRequest[] = [];
  const server: Server = createServer((req: IncomingMessage, res) => {
    const path = (req.url ?? '').split('?')[0];
    requests.push({
      method: req.method ?? '',
      path,
      stripeVersion: header(req, 'stripe-version'),
      authorization: header(req, 'authorization'),
    });
    const match = req.method === 'GET' ? RETRIEVE_PATH.exec(path) : null;
    const body = match ? fixture.responses[match[1]] : undefined;
    if (body === undefined) {
      res.writeHead(404, { 'Content-Type': 'application/json', 'Request-Id': 'req_fixture_404' });
      res.end(JSON.stringify({ error: { type: 'invalid_request_error', code: 'resource_missing', message: `No such fixture: ${path}` } }));
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json', 'Request-Id': 'req_fixture' });
    res.end(JSON.stringify(body));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    requests,
    close: () => new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve()))),
  };
}

function header(req: IncomingMessage, name: string): string | undefined {
  const value = req.headers[name];
  return Array.isArray(value) ? value[0] : value;
}
