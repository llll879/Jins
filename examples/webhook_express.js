// Приём вебхуков Jins на Express.  npm i express  ·  JINS_WEBHOOK_SECRET=whsec_... node webhook_express.js
const express = require('express');
const { verifyWebhook } = require('../sdk/jins');

const app = express();
const SECRET = process.env.JINS_WEBHOOK_SECRET;
const seen = new Set(); // в проде — Redis/БД: доставки могут повторяться

// ВАЖНО: express.raw, а не express.json — подпись считается по сырому телу
app.post('/jins-hook', express.raw({ type: '*/*' }), (req, res) => {
  if (!verifyWebhook(SECRET, req.get('X-Jins-Signature'), req.body)) return res.sendStatus(400);
  const event = JSON.parse(req.body.toString('utf8'));
  if (seen.has(event.id)) return res.sendStatus(204);
  seen.add(event.id);
  if (event.type === 'deal.closed') console.log('Сделка закрыта:', event.data);
  res.sendStatus(204);
});

app.listen(3000, () => console.log('listening on :3000'));
