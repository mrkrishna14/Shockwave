// Fictional Express payments server: a fixture for static route extraction tests.
// Shockwave tokenizes this file; it is never installed, executed or served.
'use strict';

const express = require('express');
const refundsRouter = require('./routes/refunds'); // defined in another file: not resolved

const app = express();
const charges = express.Router();

/* A commented-out route must be ignored:
app.get('/v1/commented-out', (req, res) => res.send('no'));
*/
// app.post('/v1/also-commented', createPayment);

const docs = "Call app.get('/fake') to register a route"; // a string literal, not a call
const statusUrl = 'https://status.example.test//v1'; // "//" inside a string is not a comment
const idPattern = /^ch_['"a-z0-9]+$/i; // a regex literal containing quote characters
const centsPerDollar = 1000 / 10 / 10; // division, not a regex

app.use(express.json());

app.get('/healthz', (req, res) => res.json({ status: 'ok', docs, statusUrl }));
app.post('/v1/payments', createPayment);

charges.get('/:id', getCharge);
charges.get('/:id/receipt', getReceipt);
charges
  .route('/:id/captures')
  .get(listCaptures)
  .post(createCapture);

app.use('/v1/charges', charges);
app.use('/v1/refunds', refundsRouter);

const reportPath = '/v1/reports/' + process.env.REPORT_KIND;
app.get(reportPath, getReport); // non-literal path
app.get(`/v1/exports/${process.env.EXPORT_KIND}`, getExport); // template with a substitution
app.get(`/v1/balance`, getBalance); // a template without substitutions is a literal
app.all('/v1/legacy', legacyHandler);
app.get('/v1/files/*', serveFile);
app.get('/v1/items/:id?', getItem);
app.get('/v1/disputes/:id(\\d+)', getDispute);

function createPayment(req, res) {
  res.status(201).json({ id: 'pay_demo', valid: idPattern.test('ch_demo') });
}

function getCharge(req, res) {
  res.json({ id: req.params.id, amount: 1000, currency: 'usd', centsPerDollar });
}

function getReceipt(req, res) {
  res.json({ charge: req.params.id });
}

function listCaptures(req, res) {
  res.json([]);
}

function createCapture(req, res) {
  res.status(201).json({});
}

function getReport(req, res) {
  res.json({});
}

function getExport(req, res) {
  res.json({});
}

function getBalance(req, res) {
  res.json({ available: 0 });
}

function legacyHandler(req, res) {
  res.sendStatus(410);
}

function serveFile(req, res) {
  res.sendStatus(404);
}

function getItem(req, res) {
  res.json({});
}

function getDispute(req, res) {
  res.json({});
}

module.exports = app;
