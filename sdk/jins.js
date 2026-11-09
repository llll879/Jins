/**
 * Клиент Jins API v1 — один файл без зависимостей (Node 18+, браузеры, Deno, Bun).
 *
 *   const { Jins } = require('./jins');          // или: import { Jins } from './jins.js'
 *   const jins = new Jins('https://your-host', process.env.JINS_KEY);
 *   const { verdict } = await jins.check('@username');
 *
 * Не кладите ключ в браузерный код: вызывайте API со своего сервера.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.JinsSDK = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  class JinsError extends Error {
    constructor(status, code, message, requestId, details) {
      super(`[${status} ${code}] ${message}` + (requestId ? ` (request ${requestId})` : ''));
      this.name = 'JinsError';
      Object.assign(this, { status, code, requestId, details: details || {} });
    }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const enc = (v) => encodeURIComponent(String(v)).replace(/%40/g, '@');

  class Jins {
    constructor(baseUrl, apiKey, opts = {}) {
      const b = String(baseUrl).replace(/\/+$/, '');
      this.base = b.endsWith('/api/v1') ? b : b + '/api/v1';
      this.key = apiKey;
      this.timeout = opts.timeout || 20000;
      this.retries = opts.retries === undefined ? 3 : opts.retries;
      this.fetch = opts.fetch || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
      if (!this.fetch) throw new Error('fetch недоступен: используйте Node 18+ или передайте opts.fetch');
      this.lastMeta = null;       // meta последнего ответа (пагинация)
      this.lastRateLimit = null;  // { limit, remaining, reset }
    }

    async request(method, path, { params, body, idempotencyKey } = {}) {
      let url = this.base + path;
      if (params) {
        const q = Object.entries(params).filter(([, v]) => v !== undefined && v !== null)
          .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
        if (q) url += '?' + q;
      }
      const headers = { Authorization: `Bearer ${this.key}`, Accept: 'application/json' };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
      const safe = method === 'GET' || method === 'DELETE' || !!idempotencyKey;
      for (let attempt = 0; ; attempt++) {
        let res;
        try {
          const ctl = new AbortController();
          const t = setTimeout(() => ctl.abort(), this.timeout);
          try {
            res = await this.fetch(url, { method, headers, signal: ctl.signal,
              body: body !== undefined ? JSON.stringify(body) : undefined });
          } finally { clearTimeout(t); }
        } catch (e) {
          if (attempt >= this.retries || !safe) throw new JinsError(0, 'network_error', e.message || String(e));
          await sleep(Math.min(2 ** attempt * 1000, 10000));
          continue;
        }
        const retryable = res.status === 429 || (res.status >= 500 && safe);
        if (retryable && attempt < this.retries) {
          const ra = parseFloat(res.headers.get('Retry-After'));
          await sleep(Math.min((isNaN(ra) ? 2 ** attempt : ra) * 1000, 30000));
          continue;
        }
        return this._parse(res);
      }
    }

    async _parse(res) {
      const rl = res.headers.get('X-RateLimit-Limit');
      if (rl) this.lastRateLimit = { limit: +rl, remaining: +res.headers.get('X-RateLimit-Remaining'), reset: +res.headers.get('X-RateLimit-Reset') };
      const rid = res.headers.get('X-Request-Id');
      if (!(res.headers.get('Content-Type') || '').includes('json')) {
        if (!res.ok) throw new JinsError(res.status, 'http_error', (await res.text()).slice(0, 200), rid);
        return new Uint8Array(await res.arrayBuffer());  // например, PDF-чек
      }
      const j = await res.json();
      if (!j.ok) {
        const { code, message, status, ...extra } = j.error || {};
        throw new JinsError(res.status, code || 'error', message || '', rid, extra);
      }
      this.lastMeta = j.meta || null;
      return j.data;
    }

    get(path, params) { return this.request('GET', path, { params }); }
    _post(path, body, idempotencyKey) { return this.request('POST', path, { body: body || {}, idempotencyKey }); }

    // служебные
    ping() { return this.get('/ping'); }
    me() { return this.get('/me'); }
    stats() { return this.get('/stats'); }

    // репутация и пользователи
    check(ref) { return this.get(`/check/${enc(ref)}`); }
    async user(ref) { return (await this.get(`/users/${enc(ref)}`)).user; }
    reviews(ref, limit = 20, offset = 0) { return this.get(`/users/${enc(ref)}/reviews`, { limit, offset }); }
    baseSearch(q) { return this.get('/base/search', { q }); }
    baseList(kind = 'top') { return this.get('/base/list', { kind }); }
    async baseProfile(ref) { return (await this.get(`/base/${enc(ref)}`)).user; }

    // объявления и проекты
    async topics() { return (await this.get('/topics')).topics; }
    async posts({ topic, q, projectId, authorId, limit = 20, offset = 0 } = {}) {
      return (await this.get('/posts', { topic, q, project_id: projectId, author_id: authorId, limit, offset })).posts;
    }
    /** for await (const p of jins.iterPosts({ topic: 3 })) { ... } */
    async *iterPosts({ pageSize = 50, ...filters } = {}) {
      for (let offset = 0; ;) {
        const items = await this.posts({ ...filters, limit: pageSize, offset });
        yield* items;
        if (!items.length || !(this.lastMeta && this.lastMeta.has_more)) return;
        offset += items.length;
      }
    }
    async post(id) { return (await this.get(`/posts/${+id}`)).post; }
    submitPost(text, topicId, projectId) { return this._post('/posts', { text, topic_id: topicId, project_id: projectId }); }
    async projects(filters = {}) { return (await this.get('/projects', filters)).projects; }
    project(id) { return this.get(`/projects/${+id}`); }

    // сделки
    async deals({ kind = 'all', status, limit = 20, offset = 0 } = {}) { return (await this.get('/deals', { kind, status, limit, offset })).deals; }
    deal(kind, id) { return this.get(`/deals/${kind}/${+id}`); }
    createEscrowDeal({ sellerId, amount, terms, postId }, idempotencyKey) {
      return this._post('/deals/escrow', { seller_id: sellerId, amount, terms, post_id: postId }, idempotencyKey);
    }
    createDirectDeal({ sellerId, amount, terms, postId }, idempotencyKey) {
      return this._post('/deals/direct', { seller_id: sellerId, amount, terms, post_id: postId }, idempotencyKey);
    }
    dealMessage(kind, id, body) { return this._post(`/deals/${kind}/${+id}/messages`, { body }); }
    dealAction(kind, id, action, reason) { return this._post(`/deals/${kind}/${+id}/actions`, { action, reason }); }
    async receipts() { return (await this.get('/receipts')).receipts; }
    receiptPdf(id) { return this.get(`/deals/escrow/${+id}/receipt.pdf`); }

    // чаты
    chats() { return this.get('/chats'); }
    messages(peer, postId) { return this.get(`/chats/${+peer}/messages`, { post_id: postId }); }
    sendMessage(peer, text, { postId, replyTo } = {}) { return this._post(`/chats/${+peer}/messages`, { text, post_id: postId, reply_to: replyTo }); }

    // вебхуки
    async webhooks() { return (await this.get('/webhooks')).webhooks; }
    /** Возвращает вебхук вместе с secret — он показывается один раз. */
    async createWebhook(url, events = ['*'], description = '') { return (await this._post('/webhooks', { url, events, description })).webhook; }
    testWebhook(id) { return this._post(`/webhooks/${+id}/test`); }
    deleteWebhook(id) { return this.request('DELETE', `/webhooks/${+id}`); }
  }

  /**
   * Проверка подписи вебхука (только Node: нужен модуль crypto).
   * rawBody — СЫРОЕ тело запроса (Buffer/string), а не пересобранный JSON.
   */
  function verifyWebhook(secret, signatureHeader, rawBody, toleranceSec = 300) {
    const crypto = require('crypto');
    const parts = {};
    for (const p of String(signatureHeader || '').split(',')) { const i = p.indexOf('='); if (i > 0) parts[p.slice(0, i).trim()] = p.slice(i + 1).trim(); }
    const ts = parseInt(parts.t, 10);
    if (!ts || !parts.v1) return false;
    if (toleranceSec && Math.abs(Date.now() / 1000 - ts) > toleranceSec) return false;
    const mac = crypto.createHmac('sha256', secret).update(`${ts}.`).update(rawBody).digest('hex');
    const a = Buffer.from(mac), b = Buffer.from(parts.v1);
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  }

  return { Jins, JinsError, verifyWebhook };
}));
