import assert from 'node:assert/strict';
import { test } from 'node:test';
import { displayLocalAmount } from '../src/checkout.ts';
test('local cents calculation remains unchanged', () => {
  assert.equal(displayLocalAmount(2500), '$25.00');
});
