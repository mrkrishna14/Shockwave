// Refund routes live in their own module; server.js mounts this router via require().
const express = require('express');

const router = express.Router();

router.get('/:id', (req, res) => {
  res.json({ id: req.params.id, amount: 250, currency: 'usd' });
});

module.exports = router;
