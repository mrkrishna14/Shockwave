const test = require('node:test');
const assert = require('node:assert/strict');
const {display, unrelatedDiscount} = require('./consumer.cjs');
test('display_ten_dollars', () => assert.equal(display(JSON.parse(process.env.SW_RESPONSE)), '$10.00'));
test('unrelated_conversion', () => assert.equal(unrelatedDiscount(250), 2.5));
